"""email_sent → my_response_count / last_contacted for each To+Cc recipient;
writing to someone makes them eligible. Spec §6.2. A note from the consulting
identity also proves `briefed` for each To recipient at `to_contact` (outreach
spec §6.2), inside the same durable transaction; its Asana task follows the commit."""

import logging

import clients.otel as otel
from clients import db
from services import (
    eligibility,
    google_contacts_sync,
    hubspot_mirror,
    ingest,
    outreach,
    outreach_sync,
)

logger = logging.getLogger(__name__)


def _advance_briefed(conn, event: dict, results: list, sent_at) -> list[int]:
    if not outreach.is_outreach_sender(event.get("from") or ""):
        return []
    to = {eligibility.normalize(a) for a in event.get("to") or []}
    event_ids = []
    for res in results:
        if res.row.get("id") is None or res.row.get("email") not in to:
            continue  # Cc recipients are copied, not briefed
        event_id = outreach.advance_from_event(
            conn,
            res.row["id"],
            "briefed",
            at=sent_at,
            graph_message_id=event.get("graph_message_id"),
        )
        if event_id is not None:
            event_ids.append(event_id)
    return event_ids


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_sent"})
    recipients = list(event.get("to") or []) + list(event.get("cc") or [])
    sent_at = ingest.parse_ts(event["sent_at"])
    with db.get_conn() as conn:
        results = ingest.record_outbound(
            conn,
            recipients=recipients,
            display_by_email=None,
            sent_at=sent_at,
        )
        briefed = _advance_briefed(conn, event, results, sent_at)
        conn.commit()
        if briefed:
            outreach_sync.sync_events(conn, briefed)
        for res in results:
            if res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
        conn.commit()
    logger.info(
        "email_sent %s → %d recipients, %d briefed",
        event.get("graph_message_id"),
        len(results),
        len(briefed),
    )
