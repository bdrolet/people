"""email_classified → counters + eligibility (durable), then Google/HubSpot
side effects (best-effort). Spec §6.1. Non-ignore mail from a briefed outreach
contact also proves `replied`, inside the durable transaction."""

import logging

import clients.otel as otel
from clients import db
from services import google_contacts_sync, hubspot_mirror, ingest, outreach

logger = logging.getLogger(__name__)


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_classified"})
    with db.get_conn() as conn:
        received_at = ingest.parse_ts(event["received_at"])
        res = ingest.record_inbound(
            conn,
            sender=event["sender"],
            display=event.get("sender_display"),
            received_at=received_at,
            category=event.get("category", ""),
        )
        row = res.row
        # Outreach spec §6.3: any non-ignore mail from a briefed contact proves `replied`.
        # An out-of-office is filed `ignore` by inbox and does not count.
        if (
            row.get("id") is not None
            and event.get("category") != "ignore"
            and not row.get("automated")
        ):
            outreach.advance_from_event(
                conn,
                row["id"],
                "replied",
                at=received_at,
                graph_message_id=event.get("graph_message_id") or event.get("message_id"),
            )
        conn.commit()
        if row.get("eligible"):
            row = google_contacts_sync.ensure_contact(conn, row)
            row = hubspot_mirror.ensure_contact(conn, row)
        hubspot_mirror.log_email(row, event)
        conn.commit()
    logger.info(
        "email_classified %s from %s — eligible=%s newly=%s",
        event.get("message_id"),
        row.get("email"),
        row.get("eligible"),
        res.newly_eligible,
    )
