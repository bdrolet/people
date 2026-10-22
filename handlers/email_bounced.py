"""email_bounced → the funded-founder campaign's bounce handling (campaign spec): each
failed recipient's campaign contact moves to `bounced` and its people row is stamped
`email_bounced_at`. One durable transaction; no Google/HubSpot side effects."""

import logging

import clients.otel as otel
from clients import db
from services import campaign, ingest

logger = logging.getLogger(__name__)


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_bounced"})
    failed = list(event.get("failed_recipients") or [])
    with db.get_conn() as conn:
        moved = campaign.record_bounce_from_event(
            conn,
            failed_recipients=failed,
            graph_message_id=event.get("graph_message_id") or event.get("message_id"),
            at=ingest.parse_ts(event["received_at"]),
            status=event.get("status"),
            diagnostic=event.get("diagnostic"),
        )
        conn.commit()
    logger.info(
        "email_bounced %s → %d failed recipients, %d campaign contacts bounced",
        event.get("graph_message_id"),
        len(failed),
        moved,
    )
