"""email_sent → my_response_count / last_contacted for each To+Cc recipient;
writing to someone makes them eligible. Spec §6.2. A note from the consulting
identity also briefs each To recipient at `to_contact` and nudges each one at
`briefed` (outreach spec §6.2, working-queue spec §3.3), inside the same durable
transaction. Only briefs get an Asana task, after the commit. Mail from the campaign
sender also confirms the funded-founder campaign send, in the same transaction."""

import logging

import clients.otel as otel
from clients import db
from services import (
    campaign,
    eligibility,
    google_contacts_sync,
    hubspot_mirror,
    ingest,
    outreach,
    outreach_sync,
)

logger = logging.getLogger(__name__)


def _record_outreach(conn, event: dict, results: list, sent_at) -> tuple[list[int], int]:
    """Brief or nudge each To recipient in the pipeline. Returns the brief event ids
    (they go on to the Asana sync) and the number of nudges."""
    if not outreach.is_outreach_sender(event.get("from") or ""):
        return [], 0
    to = {eligibility.normalize(a) for a in event.get("to") or []}
    briefed: list[int] = []
    nudged = 0
    for res in results:
        if res.row.get("id") is None or res.row.get("email") not in to:
            continue  # Cc recipients are copied, not briefed or nudged
        done = outreach.advance_from_sent(
            conn, res.row["id"], at=sent_at, graph_message_id=event.get("graph_message_id")
        )
        if done is None:
            continue
        kind, event_id = done
        if kind == "briefed":
            briefed.append(event_id)
        else:
            nudged += 1
    return briefed, nudged


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
        briefed, nudged = _record_outreach(conn, event, results, sent_at)
        confirmed = campaign.confirm_sent_from_event(
            conn,
            to=list(event.get("to") or []),
            from_address=event.get("from") or "",
            graph_message_id=event.get("graph_message_id"),
            at=sent_at,
        )
        conn.commit()
        if briefed:
            outreach_sync.sync_events(conn, briefed)
        for res in results:
            if res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
        conn.commit()
    logger.info(
        "email_sent %s → %d recipients, %d briefed, %d nudged, %d campaign confirmed",
        event.get("graph_message_id"),
        len(results),
        len(briefed),
        nudged,
        confirmed,
    )
