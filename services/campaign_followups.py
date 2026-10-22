"""The funded-founder campaign's one follow-up (campaign spec §5.7): a threaded reply to
each `sent` contact whose follow-up date has come. At most once: the contact moves to
`followed_up` and commits before any send, so a lost response or a crash can never
lead to a second follow-up ("There is no third message"). The send's outcome is an
annotation on that move; a failure is left for Ben, never retried."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from clients import inbox_api
from repo import campaign as campaign_repo
from services import campaign
from services.campaign_draft import render_follow_up
from services.campaign_send import confirmed_sent_hit

log = logging.getLogger(__name__)

SENDING_NOTE = "follow-up: sending"
SENT_NOTE = "follow-up: sent (reply)"
FALLBACK_NOTE = "follow-up: sent (fallback: fresh Re:)"
PAUSE_S = 1.0
# The original is gone (deleted or moved): a fresh `Re:` is the only way to follow up.
# A timeout or 5xx is not: Graph may already have sent the reply.
GONE_STATUSES = frozenset({404, 410})


@dataclass
class FollowUpResult:
    sent: int
    fallback: int
    skipped: int
    failed: int = 0
    remaining: int = 0  # due contacts left for another day: the daily budget ran out


def _first_name(full_name: str | None) -> str:
    tokens = (full_name or "").split()
    return tokens[0] if tokens else ""


def _found_id(contact: dict, inbox: Any) -> str | None:
    """The first email's Graph id from a confirmed sent item, or None."""
    hits = inbox.search_sent(to=contact["email"], subject=contact["subject"])
    hit = next((h for h in hits if confirmed_sent_hit(h, contact)), None)
    return hit["message_id"] if hit else None


def _gone(error: Exception) -> bool:
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in GONE_STATUSES


def _send(contact: dict, message_id: str | None, inbox: Any, *, body: str, from_address: str):
    """Send the follow-up; return the outcome note. Raises on a failed send."""
    to = [contact["email"]]
    if message_id:
        try:
            inbox.reply(message_id, body=body, to=to, from_address=from_address)
            return SENT_NOTE
        except Exception as e:
            if not _gone(e):
                raise
            log.warning("contact %s: original %s is gone; fresh Re:", contact["id"], message_id)
    draft = inbox.create_draft(to, "Re: " + contact["subject"], body, from_address=from_address)
    inbox.send_draft(draft["id"], from_address=from_address)
    return FALLBACK_NOTE


def run(
    conn: Any,
    *,
    now: datetime,
    from_address: str,
    postal_address: str,
    inbox: Any = inbox_api,
    sleep: Callable[[float], None] = time.sleep,
    budget: int | None = None,
) -> FollowUpResult:
    """Follow up every `sent` contact due on or before today (New York). `budget` caps
    the sends (reply or fallback, failed ones included: each may have gone out); the due
    contacts past it are counted in `remaining` and left for the next run."""
    today = now.astimezone(campaign.TZ).date()
    result = FollowUpResult(sent=0, fallback=0, skipped=0)
    attempted = 0
    due_rows = campaign_repo.contacts(conn, status="sent", follow_up_due_on=today)
    for n, due in enumerate(due_rows):
        if budget is not None and attempted >= budget:
            result.remaining = len(due_rows) - n
            break
        contact = campaign_repo.lock_contact(conn, due["id"])
        if contact is None or contact["status"] != "sent":
            log.info("contact %s: no longer sent; follow-up skipped", due["id"])
            conn.rollback()
            result.skipped += 1
            continue
        company = campaign_repo.get_company(conn, contact["company_id"])
        reason = (
            "company gone"
            if company is None
            else campaign.sendable(conn, contact, company, allowed=("sent",), history=False)
        )
        if reason is not None:
            log.warning("contact %s: follow-up skipped: %s", contact["id"], reason)
            conn.rollback()
            result.skipped += 1
            continue
        message_id = contact.get("graph_message_id")
        changes = {}
        if not message_id:
            try:  # a read: nothing is recorded or sent yet, so the next run retries
                message_id = _found_id(contact, inbox)
            except Exception:
                log.exception("contact %s: could not search sent mail; left sent", contact["id"])
                conn.rollback()
                result.failed += 1
                continue
            if message_id:
                changes["graph_message_id"] = message_id
        # Recorded before sending: whatever happens to the send, there is no second one.
        campaign.advance(
            conn,
            contact["id"],
            "followed_up",
            at=now,
            source=campaign.SOURCE,
            note=SENDING_NOTE,
            changes=changes or None,
        )
        conn.commit()
        if attempted:
            sleep(PAUSE_S)
        attempted += 1
        body = render_follow_up(
            first_name=_first_name(contact.get("full_name")), postal_address=postal_address
        )
        try:
            note = _send(contact, message_id, inbox, body=body, from_address=from_address)
        except Exception as e:
            log.exception("contact %s: follow-up failed; check Sent Items", contact["id"])
            conn.rollback()
            note = f"follow-up: FAILED {type(e).__name__}: {e}; check Sent Items"
            result.failed += 1
        else:
            result.sent += 1
            result.fallback += note == FALLBACK_NOTE
        campaign.annotate(conn, contact["id"], at=now, source=campaign.SOURCE, note=note)
        conn.commit()
    return result
