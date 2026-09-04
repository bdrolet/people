"""email_sent → my_response_count / last_contacted for each To+Cc recipient;
writing to someone makes them eligible. Spec §6.2."""

import logging

import clients.otel as otel
from clients import db
from services import google_contacts_sync, hubspot_mirror, ingest

logger = logging.getLogger(__name__)


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_sent"})
    recipients = list(event.get("to") or []) + list(event.get("cc") or [])
    with db.get_conn() as conn:
        results = ingest.record_outbound(
            conn,
            recipients=recipients,
            display_by_email=None,
            sent_at=ingest.parse_ts(event["sent_at"]),
        )
        conn.commit()
        for res in results:
            if res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
        conn.commit()
    logger.info("email_sent %s → %d recipients", event.get("graph_message_id"), len(results))
