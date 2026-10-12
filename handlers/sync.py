"""Nightly: Google Contacts incremental sync, HubSpot reconcile, then the outreach
Asana backstop (outreach spec §8): every countable stage event still missing its
completed task is retried."""

import logging

from clients import db
from services import google_contacts_sync, hubspot_mirror, outreach_sync

logger = logging.getLogger(__name__)


def run() -> dict:
    with db.get_conn() as conn:
        google = google_contacts_sync.run_sync(conn)
        conn.commit()
        hubspot = hubspot_mirror.reconcile(conn)
        conn.commit()
        asana = outreach_sync.sync_pending(conn).as_dict()  # commits its own progress
    logger.info("sync complete google=%s hubspot=%s asana=%s", google, hubspot, asana)
    return {"google": google, "hubspot": hubspot, "asana": asana}
