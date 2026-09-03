"""Turn one email into people rows: counters, timestamps, eligibility. DB
only — callers commit, then do external side effects for newly eligible rows.
Shared by the event handlers and scripts/import_contacts.py so both compute
the same thing."""

import logging
from datetime import datetime, timezone
from typing import Any

import clients.otel as otel
from models.types import IngestResult
from repo import people
from services import eligibility

logger = logging.getLogger(__name__)


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _apply_flags(conn: Any, row: dict, *, automated: bool, eligible_now: bool) -> IngestResult:
    was_eligible = bool(row.get("eligible"))
    people.set_flags(conn, row["email"], automated=automated, eligible=eligible_now)
    updated = people.get(conn, row["email"])
    if updated is None:
        logger.warning("people row %s vanished after write", row["email"])
        updated = row
    newly = (not was_eligible) and bool(updated.get("eligible"))
    if newly:
        otel.eligibility_changes.add(1)
    return IngestResult(row=updated, newly_eligible=newly)


def record_inbound(
    conn: Any, *, sender: str, display: str | None, received_at: datetime, category: str
) -> IngestResult:
    email = eligibility.normalize(sender)
    row = people.upsert_inbound(conn, email, display, received_at)
    otel.people_upserts.add(1, {"direction": "inbound"})
    return _apply_flags(
        conn,
        row,
        automated=eligibility.is_automated(email),
        eligible_now=eligibility.inbound_eligible(email, category),
    )


def record_outbound(
    conn: Any,
    *,
    recipients: list[str],
    display_by_email: dict[str, str] | None,
    sent_at: datetime,
) -> list[IngestResult]:
    results: list[IngestResult] = []
    displays = {eligibility.normalize(k): v for k, v in (display_by_email or {}).items()}
    for raw in recipients:
        email = eligibility.normalize(raw)
        if not email or eligibility.is_own(email):
            continue
        display = displays.get(email)
        row = people.upsert_outbound(conn, email, display, sent_at)
        otel.people_upserts.add(1, {"direction": "outbound"})
        automated = eligibility.is_automated(email)
        results.append(_apply_flags(conn, row, automated=automated, eligible_now=not automated))
    return results
