"""Nightly: Google Contacts incremental sync, HubSpot reconcile, then the outreach
Asana backstop (outreach spec §8): every countable stage event still missing its
completed task is retried."""

import logging
from datetime import datetime

from clients import db
from services import google_contacts_sync, hubspot_mirror, outreach, outreach_sync

logger = logging.getLogger(__name__)


def run() -> dict:
    with db.get_conn() as conn:
        google = google_contacts_sync.run_sync(conn)
        conn.commit()
        hubspot = hubspot_mirror.reconcile(conn)
        conn.commit()
        asana = outreach_sync.sync_pending(conn).as_dict()  # commits its own progress
        try:
            outreach.record_population(conn, today=datetime.now(outreach.TZ).date())
        except Exception:
            # A gauge is telemetry: its failure must not fail the nightly sync.
            logger.warning("outreach population gauge failed", exc_info=True)
    logger.info("sync complete google=%s hubspot=%s asana=%s", google, hubspot, asana)
    return {"google": google, "hubspot": hubspot, "asana": asana}
