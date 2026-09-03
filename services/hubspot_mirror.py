"""HubSpot as a bounded mirror of eligible people (spec §8). At most cap()
contacts, most recently interacted-with first. Managed = has a
hubspot_contact_id in people; unmanaged contacts count toward the cap but are
never archived. Every HubSpot failure is swallowed and counted."""

import logging
import os
import time
from typing import Any

import clients.hubspot as hubspot
import clients.otel as otel
from repo import people
from services.ingest import parse_ts

logger = logging.getLogger(__name__)

_COUNT_TTL_S = 60
_count_cache: tuple[float, int] | None = None


def _reset_count_cache() -> None:
    global _count_cache
    _count_cache = None


def enabled() -> bool:
    return os.environ.get("HUBSPOT_WRITES_ENABLED", "false").lower() in ("1", "true", "yes")


def cap() -> int:
    return int(os.environ.get("HUBSPOT_MAX_CONTACTS", "1000"))


def _total() -> int:
    global _count_cache
    now = time.monotonic()
    if _count_cache and now - _count_cache[0] < _COUNT_TTL_S:
        return _count_cache[1]
    total = hubspot.count_contacts()
    _count_cache = (now, total)
    return total


def _bump_total(delta: int) -> None:
    global _count_cache
    if _count_cache:
        _count_cache = (_count_cache[0], _count_cache[1] + delta)


def _evict_oldest(conn: Any) -> bool:
    victim = people.oldest_managed(conn)
    if victim is None:
        logger.warning("HubSpot cap reached by unmanaged contacts — not creating")
        return False
    hubspot.archive_contact(victim["hubspot_contact_id"])
    people.clear_hubspot(conn, victim["email"])
    _bump_total(-1)
    otel.hubspot_evictions.add(1)
    logger.info(
        "Evicted %s from HubSpot (last interaction %s)", victim["email"], victim["last_interaction"]
    )
    return True


def _create(conn: Any, row: dict) -> str:
    # Unlike google_contacts_sync.ensure_contact, a set_hubspot DB failure
    # here is swallowed by the caller (system="hubspot") and healed by the
    # nightly adopt phase (by email).
    existing = hubspot.find_by_email(row["email"])
    if existing:
        cid = existing["id"]
    else:
        cid = hubspot.create_contact(
            row["email"], row.get("display_name"), row.get("last_interaction")
        )
        _bump_total(1)
        otel.hubspot_contacts_created.add(1)
    people.set_hubspot(conn, row["email"], cid)
    return cid


def ensure_contact(conn: Any, row: dict) -> dict:
    if not enabled() or row.get("hubspot_contact_id") or not row.get("eligible"):
        return row
    try:
        if _total() >= cap() and not _evict_oldest(conn):
            return row
        _create(conn, row)
        return people.get(conn, row["email"]) or row
    except Exception:
        otel.external_errors.add(1, {"system": "hubspot"})
        logger.warning("HubSpot ensure_contact failed for %s", row["email"], exc_info=True)
        return row


def log_email(row: dict, event: dict) -> None:
    cid = row.get("hubspot_contact_id")
    if not enabled() or not cid:
        return
    try:
        received = parse_ts(event["received_at"])
        hubspot.update_last_email_date(cid, received)
        hubspot.log_email(
            cid,
            event.get("subject", ""),
            event.get("sender", ""),
            event.get("body") or "",
            received,
            body_html=event.get("body_html"),
        )
        otel.hubspot_engagements_logged.add(1)
    except Exception:
        otel.external_errors.add(1, {"system": "hubspot"})
        logger.warning("HubSpot log_email failed for %s", row.get("email"), exc_info=True)


def reconcile(conn: Any) -> dict[str, int]:
    """Nightly: adopt, heal, enforce, fill (spec §8.3). Raises on a HubSpot
    listing failure or any DB failure so the sync run reports it; per-contact
    HubSpot write failures are swallowed and counted.

    Controller ruling: unlike other services, this one commits its own
    progress along the way instead of leaving it all to the handler's final
    commit. Each phase here interleaves an external HubSpot write with a DB
    write, and a function timeout partway through must not discard DB rows
    that already match HubSpot reality (or, worse, replay a HubSpot create
    against a row we already wrote). The handler's final commit still covers
    the Google half of the sync run.
    """
    counts = {"adopted": 0, "healed": 0, "evicted": 0, "filled": 0}
    if not enabled():
        return counts
    _reset_count_cache()
    live = {c["id"]: c for c in hubspot.list_contacts()}
    total = len(live)

    # adopt
    managed_ids = {r["hubspot_contact_id"] for r in people.managed(conn)}
    for cid, c in live.items():
        if cid in managed_ids or not c["email"]:
            continue
        row = people.get(conn, c["email"])
        if row and row.get("eligible") and not row.get("hubspot_contact_id"):
            people.set_hubspot(conn, row["email"], cid)
            counts["adopted"] += 1

    # heal
    for r in people.managed(conn):
        if r["hubspot_contact_id"] not in live:
            people.clear_hubspot(conn, r["email"])
            counts["healed"] += 1

    conn.commit()

    # enforce
    while total > cap():
        try:
            if not _evict_oldest(conn):
                break  # only unmanaged contacts left — _evict_oldest already warned
        except Exception:
            otel.external_errors.add(1, {"system": "hubspot"})
            logger.warning("eviction failed during reconcile", exc_info=True)
            break
        total -= 1
        counts["evicted"] += 1
        conn.commit()

    # fill
    room = cap() - total
    if room > 0:
        for row in people.eligible_not_in_hubspot(conn, room):
            try:
                _create(conn, row)
                counts["filled"] += 1
                conn.commit()
            except Exception:
                otel.external_errors.add(1, {"system": "hubspot"})
                logger.warning("fill create failed for %s", row["email"], exc_info=True)
    return counts
