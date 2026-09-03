"""Nightly: Google Contacts incremental sync, then HubSpot reconcile."""

import logging

from clients import db
from services import google_contacts_sync, hubspot_mirror

logger = logging.getLogger(__name__)


def run() -> dict:
    with db.get_conn() as conn:
        google = google_contacts_sync.run_sync(conn)
        conn.commit()
        hubspot = hubspot_mirror.reconcile(conn)
        conn.commit()
    logger.info("sync complete google=%s hubspot=%s", google, hubspot)
    return {"google": google, "hubspot": hubspot}
