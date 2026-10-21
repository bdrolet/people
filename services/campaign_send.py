"""Sending the funded-founder campaign (campaign spec §7): the preflight that gates a
send, the stale-approval rule, and the daily batch. Unlike services/campaign.py this
module commits: each send is its own transaction, so a crash mid-batch never loses the
record of an email that already went out."""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
    # Approved contacts whose draft may or may not have gone out: Ben checks Outlook Drafts.
    needs_check: list[int] = field(default_factory=list)


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
    `drafted` (note `stale`) for another look. Commits each move; returns how many. A
    contact with a draft_id is left alone: its send's outcome is unknown, so it stays
    `approved` in send's `needs_check` until Ben sends or discards the draft."""
    cutoff = now - timedelta(days=stale_after_days)
    moved = 0
    for contact in campaign_repo.contacts(conn, status="approved"):
        approved_at = campaign_repo.last_event_at(conn, contact["id"], "approved")
        if approved_at is None or approved_at >= cutoff:
            continue
        if contact.get("draft_id"):
            log.warning(
                "contact %s: stale but draft %s may have gone out; left for a check",
                contact["id"],
                contact["draft_id"],
            )
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


def _reconcile(conn: Any, contact: dict, inbox: Any, *, now: datetime, follow_up_on) -> bool:
    """An approved contact with a draft_id had a send whose outcome is unknown. Never
    send it again: if Graph shows the message went out, record the send; otherwise leave
    it for Ben. True when it was recorded as sent."""
    try:
        hits = inbox.search_sent(to=contact["email"], subject=contact["subject"])
    except Exception:
        log.exception("contact %s: could not search sent mail", contact["id"])
        return False
    # The search spans every folder and KQL matches the subject loosely. Only an item
    # inbox-api confirms is not a draft (`is_draft is False`; None means an inbox-api too
    # old to say), with the exact subject and not the draft's own id, proves a send.
    hits = [
        h
        for h in hits
        if h.get("is_draft") is False
        and h.get("subject") == contact["subject"]
        and h.get("message_id")
        and h["message_id"] != contact["draft_id"]
    ]
    if not hits:
        log.warning(
            "contact %s: draft %s was never seen sent; check Outlook Drafts",
            contact["id"],
            contact["draft_id"],
        )
        return False
    campaign.advance(
        conn,
        contact["id"],
        "sent",
        at=now,
        source=campaign.SOURCE,
        graph_message_id=hits[0].get("message_id"),
        note="reconciled",
        changes={"follow_up_on": follow_up_on},
    )
    conn.commit()
    return True


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
    force: bool = False,
) -> SendResult:
    """Send approved contacts, oldest first, up to today's cap. `postal_address` is
    already in each rendered body; preflight checks it is set. `force` ignores the
    bounce halt (`campaign resume`)."""
    run = campaign_repo.latest_run(conn)
    run_id = run["id"] if run else None
    approved = campaign_repo.contacts(conn, status="approved")
    budget = targets.per_day - campaign_repo.sent_today(conn, now.astimezone(campaign.TZ).date())
    follow_up_on = campaign.follow_up_date(now, targets.follow_up_business_days)
    sent, lost, attempted, needs_check = 0, 0, 0, []
    # Pre-pass, whatever the halt or the budget: a contact with a draft_id had a send
    # whose outcome is unknown. Reconciling only reads the mailbox; the rest go to Ben.
    fresh = []
    for contact in approved:
        if not contact.get("draft_id"):
            fresh.append(contact)
        elif _reconcile(conn, contact, inbox, now=now, follow_up_on=follow_up_on):
            sent += 1
        else:
            needs_check.append(contact["id"])
    halted = not force and _halted(conn, run_id)
    for contact in [] if halted else fresh:
        if not force and _halted(conn, run_id):
            halted = True
            break
        if sent + lost >= budget:  # a lost send may have reached Graph: it uses a slot
            break
        if not _ensure_person(conn, contact):
            continue
        if attempted:
            sleep(pause_s)
        attempted += 1
        try:
            draft = inbox.create_draft(
                [contact["email"]], contact["subject"], contact["body"], from_address=from_address
            )
        except Exception:
            log.exception("contact %s: draft failed; left approved", contact["id"])
            conn.rollback()
            continue
        # Recorded before sending: if the send's outcome is lost, the next batch
        # reconciles this draft instead of writing and sending a second email.
        campaign_repo.update_contact(conn, contact["id"], {"draft_id": draft["id"]})
        conn.commit()
        try:
            receipt = inbox.send_draft(draft["id"], from_address=from_address)
        except Exception:
            log.exception(
                "contact %s: send of draft %s failed or was lost", contact["id"], draft["id"]
            )
            conn.rollback()
            needs_check.append(contact["id"])
            lost += 1
            continue
        # The email is out: from here an error must surface, never be retried as unsent.
        campaign.advance(
            conn,
            contact["id"],
            "sent",
            at=now,
            source=campaign.SOURCE,
            graph_message_id=receipt.get("id"),
            changes={"follow_up_on": follow_up_on},
        )
        conn.commit()
        sent += 1
    if run_id is not None:
        campaign_repo.update_run_counts(
            conn, run_id, campaign_repo.status_counts_for_run(conn, run_id)
        )
        conn.commit()
    return SendResult(
        sent=sent, remaining=len(approved) - sent, halted=halted, needs_check=needs_check
    )
