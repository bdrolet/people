"""Sending the funded-founder campaign (campaign spec §7): the preflight that gates a
send, the stale-approval rule, and the daily batch. Unlike services/campaign.py this
module commits: each send is its own transaction, so a crash mid-batch never loses the
record of an email that already went out."""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import dns.exception
import dns.resolver

from clients import inbox_api
from repo import campaign as campaign_repo
from repo import people as people_repo
from services import campaign, person_create
from services.campaign_targets import CampaignTargets

log = logging.getLogger(__name__)

LABEL = "consulting-campaign"
# Microsoft 365's DKIM selectors; CAMPAIGN_DKIM_SELECTORS (comma-separated) overrides.
DEFAULT_DKIM_SELECTORS = ("selector1", "selector2")
MAX_BOUNCES = 2  # a third bounce in the run halts sending


class PreflightError(RuntimeError):
    pass


@dataclass
class SendResult:
    sent: int
    remaining: int
    halted: bool


# --- preflight ------------------------------------------------------------------------------


def _txt(resolver: Any, name: str) -> list[str]:
    try:
        answer = resolver.resolve(name, "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as e:
        raise PreflightError(f"dns: TXT lookup for {name} failed: {e}") from e
    return [b"".join(r.strings).decode(errors="replace") for r in answer]


def _dkim_selectors() -> tuple[str, ...]:
    raw = os.environ.get("CAMPAIGN_DKIM_SELECTORS", "")
    selectors = tuple(s.strip() for s in raw.split(",") if s.strip())
    return selectors or DEFAULT_DKIM_SELECTORS


def preflight(
    domain: str,
    *,
    postal_address: str | None,
    inbox_reachable: Callable[[], bool],
    resolver: Any = dns.resolver,
) -> None:
    """Raise PreflightError naming the first thing that would make a send unsafe."""
    if not any(t.lower().startswith("v=spf1") for t in _txt(resolver, domain)):
        raise PreflightError(f"spf: no v=spf1 TXT record at {domain}")
    if not any("v=dmarc1" in t.lower() for t in _txt(resolver, f"_dmarc.{domain}")):
        raise PreflightError(f"dmarc: no v=DMARC1 TXT record at _dmarc.{domain}")
    selectors = _dkim_selectors()
    if not any(_txt(resolver, f"{s}._domainkey.{domain}") for s in selectors):
        names = ", ".join(f"{s}._domainkey.{domain}" for s in selectors)
        raise PreflightError(f"dkim: no TXT record at {names}")
    if not (postal_address or "").strip():
        raise PreflightError("postal address: CAMPAIGN_POSTAL_ADDRESS is empty")
    if not inbox_reachable():
        raise PreflightError("inbox: inbox-api is not reachable")


# --- stale approvals ------------------------------------------------------------------------


def restage_stale(conn: Any, *, now: datetime, stale_after_days: int) -> int:
    """Approved contacts whose latest approval is older than `stale_after_days` go back to
    `drafted` (note `stale`) for another look. Commits each move; returns how many."""
    cutoff = now - timedelta(days=stale_after_days)
    moved = 0
    for contact in campaign_repo.contacts(conn, status="approved"):
        approved_at = campaign_repo.last_event_at(conn, contact["id"], "approved")
        if approved_at is None or approved_at >= cutoff:
            continue
        result = campaign.advance(
            conn, contact["id"], "drafted", at=now, source=campaign.SOURCE, note="stale"
        )
        conn.commit()
        moved += result.changed
    return moved


# --- the people row -------------------------------------------------------------------------


def _google_contact(conn: Any, contact: dict) -> dict:
    out: dict = {}
    given, _, family = (contact.get("full_name") or "").strip().partition(" ")
    name = {k: v for k, v in (("givenName", given), ("familyName", family.strip())) if v}
    if name:
        out["names"] = [name]
    if contact.get("email"):
        out["emailAddresses"] = [{"value": contact["email"]}]
    if contact.get("linkedin_url"):
        out["urls"] = [{"value": contact["linkedin_url"], "type": "profile"}]
    company = campaign_repo.get_company(conn, contact["company_id"]) or {}
    org = {k: v for k, v in (("name", company.get("name")), ("title", contact.get("title"))) if v}
    if org:
        out["organizations"] = [org]
    return out


def _person_id(conn: Any, contact: dict) -> int | None:
    """The contact's `people` row id, creating the person (Google contact, row, label)
    when no row exists. None when no row can be had this batch."""
    if contact.get("person_id"):
        return contact["person_id"]
    existing = people_repo.get(conn, contact["email"]) if contact.get("email") else None
    if existing:
        return existing["id"]
    try:
        return person_create.create(conn, contact=_google_contact(conn, contact), labels=[LABEL])[
            "id"
        ]
    except person_create.PartiallyCreated as e:
        log.warning(
            "contact %s: person %s created without its label: %s",
            contact["id"],
            e.row["id"],
            e.reason,
        )
        return e.row["id"]
    except person_create.Duplicate as e:
        if e.candidates:
            return e.candidates[0]
        log.warning("contact %s: Google contact exists but has no row yet; skipped", contact["id"])
        return None


def _ensure_person(conn: Any, contact: dict) -> bool:
    """Link the people row before sending, committed on its own: a send that fails later
    must not roll back a row whose Google contact already exists."""
    try:
        person_id = _person_id(conn, contact)
    except Exception:
        log.exception("contact %s: could not create the people row; skipped", contact["id"])
        conn.rollback()
        return False
    if person_id is None:
        conn.rollback()
        return False
    if contact.get("person_id") != person_id:
        campaign_repo.update_contact(conn, contact["id"], {"person_id": person_id})
    conn.commit()
    return True


# --- the batch ------------------------------------------------------------------------------


def _halted(conn: Any, run_id: int | None) -> bool:
    return run_id is not None and campaign_repo.bounced_count_for_run(conn, run_id) > MAX_BOUNCES


def send_batch(
    conn: Any,
    *,
    now: datetime,
    targets: CampaignTargets,
    from_address: str,
    postal_address: str,
    inbox: Any = inbox_api,
    pause_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> SendResult:
    """Send approved contacts, oldest first, up to today's cap. `postal_address` is
    already in each rendered body; preflight checks it is set."""
    run = campaign_repo.latest_run(conn)
    run_id = run["id"] if run else None
    approved = campaign_repo.contacts(conn, status="approved")
    budget = targets.per_day - campaign_repo.sent_today(conn, now.astimezone(campaign.TZ).date())
    sent, halted = 0, False
    follow_up_on = campaign.follow_up_date(now, targets.follow_up_business_days)
    for contact in approved:
        if sent >= budget:
            break
        if _halted(conn, run_id):
            halted = True
            break
        if not _ensure_person(conn, contact):
            continue
        if sent:
            sleep(pause_s)
        try:
            draft = inbox.create_draft(
                [contact["email"]], contact["subject"], contact["body"], from_address=from_address
            )
            receipt = inbox.send_draft(draft["id"], from_address=from_address)
        except Exception:
            log.exception("contact %s: send failed; left approved", contact["id"])
            conn.rollback()
            continue
        # The email is out: from here an error must surface, never be retried as unsent.
        campaign.advance(
            conn,
            contact["id"],
            "sent",
            at=now,
            source=campaign.SOURCE,
            graph_message_id=receipt.get("id"),
            changes={"draft_id": draft["id"], "follow_up_on": follow_up_on},
        )
        conn.commit()
        sent += 1
    if run_id is not None:
        campaign_repo.update_run_counts(
            conn, run_id, campaign_repo.status_counts_for_run(conn, run_id)
        )
        conn.commit()
    return SendResult(sent=sent, remaining=len(approved) - sent, halted=halted)
