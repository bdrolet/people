"""email_classified → counters + eligibility (durable), then Google/HubSpot
side effects (best-effort). Spec §6.1."""

import logging

import clients.otel as otel
from clients import db
from services import google_contacts_sync, hubspot_mirror, ingest

logger = logging.getLogger(__name__)


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_classified"})
    with db.get_conn() as conn:
        res = ingest.record_inbound(
            conn,
            sender=event["sender"],
            display=event.get("sender_display"),
            received_at=ingest.parse_ts(event["received_at"]),
            category=event.get("category", ""),
        )
        conn.commit()
        row = res.row
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
