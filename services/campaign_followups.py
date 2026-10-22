"""The funded-founder campaign's one follow-up (campaign spec §5.7): a threaded reply to
each `sent` contact whose follow-up date has come. Like campaign_send this module
commits, one transaction per contact, so a crash never loses the record of a nudge
that already went out."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clients import inbox_api
from repo import campaign as campaign_repo
from services import campaign
from services.campaign_draft import render_follow_up
from services.campaign_send import confirmed_sent_hit

log = logging.getLogger(__name__)

FALLBACK_NOTE = "fallback: fresh Re:"
PAUSE_S = 1.0


@dataclass
class FollowUpResult:
    sent: int
    fallback: int
    skipped: int
    failed: int = 0


def _first_name(full_name: str | None) -> str:
    tokens = (full_name or "").split()
    return tokens[0] if tokens else ""


def _original_id(conn: Any, contact: dict, inbox: Any) -> str | None:
    """The first email's Graph id: the stored one, else a confirmed sent item (stored on
    the contact, committed with the follow-up), else None."""
    if contact.get("graph_message_id"):
        return contact["graph_message_id"]
    hits = inbox.search_sent(to=contact["email"], subject=contact["subject"])
    hit = next((h for h in hits if confirmed_sent_hit(h, contact)), None)
    if hit is None:
        return None
    campaign_repo.update_contact(conn, contact["id"], {"graph_message_id": hit["message_id"]})
    return hit["message_id"]


def _send(conn: Any, contact: dict, inbox: Any, *, from_address: str) -> str | None:
    """Send the follow-up; return the event note (the fallback's, or None)."""
    body = render_follow_up(first_name=_first_name(contact.get("full_name")))
    to = [contact["email"]]
    message_id = _original_id(conn, contact, inbox)
    if message_id:
        inbox.reply(message_id, body=body, to=to, from_address=from_address)
        return None
    draft = inbox.create_draft(to, "Re: " + contact["subject"], body, from_address=from_address)
    inbox.send_draft(draft["id"], from_address=from_address)
    return FALLBACK_NOTE


def run(
    conn: Any,
    *,
    now: datetime,
    from_address: str,
    inbox: Any = inbox_api,
    sleep: Callable[[float], None] = time.sleep,
) -> FollowUpResult:
    """Follow up every `sent` contact due on or before today (New York)."""
    today = now.astimezone(campaign.TZ).date()
    result = FollowUpResult(sent=0, fallback=0, skipped=0)
    attempted = 0
    for due in campaign_repo.contacts(conn, status="sent", follow_up_due_on=today):
        contact = campaign_repo.lock_contact(conn, due["id"])
        if contact is None or contact["status"] != "sent":
            log.info("contact %s: no longer sent; follow-up skipped", due["id"])
            conn.rollback()
            result.skipped += 1
            continue
        if attempted:
            sleep(PAUSE_S)
        attempted += 1
        try:
            note = _send(conn, contact, inbox, from_address=from_address)
        except Exception:
            log.exception("contact %s: follow-up failed; left sent", contact["id"])
            conn.rollback()
            result.failed += 1
            continue
        # The follow-up is out: from here an error must surface, never be retried.
        campaign.advance(
            conn, contact["id"], "followed_up", at=now, source=campaign.SOURCE, note=note
        )
        conn.commit()
        result.sent += 1
        result.fallback += note == FALLBACK_NOTE
    return result
