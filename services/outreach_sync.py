"""Strategy sync: one completed Asana task per countable stage event (outreach spec §8).
Best-effort and never on the durable path: the event row is already committed, a failure
leaves it pending, and `outreach sync-asana` and the nightly people-sync retry it. Like
hubspot_mirror.reconcile, this commits its own progress after each external write, so a
crash between the create and the complete never makes a second task."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

import clients.otel as otel
from clients import tasks_api
from repo import outreach as outreach_repo
from services.outreach import TZ

logger = logging.getLogger(__name__)

GOAL_TAG = "serves:consulting"
# (pipeline, stage) -> (lead/tripwire tag, task-name prefix). Spec §8's table, verbatim.
TASKS: dict[tuple[str, str], tuple[str, str]] = {
    ("referrer", "briefed"): ("outreach", "Brief"),
    ("buyer", "intro"): ("intro", "Intro"),
    ("buyer", "conversation"): ("conversation", "Conversation"),
    ("buyer", "proposal"): ("proposal", "Proposal"),
    ("buyer", "signed"): ("signed-client", "Signed"),
}
_KEYS = [f"{pipeline}:{stage}" for pipeline, stage in TASKS]


@dataclass
class SyncResult:
    synced: int = 0
    failed: int = 0
    skipped: bool = False  # TASKS_API_URL unset: nothing attempted
    late: dict[str, int] = field(default_factory=dict)  # synced events by lateness bucket

    def as_dict(self) -> dict[str, Any]:
        return {
            "synced": self.synced,
            "failed": self.failed,
            "skipped": self.skipped,
            "late": self.late,
        }


def produces_task(pipeline: str, stage: str) -> bool:
    return (pipeline, stage) in TASKS


def task_payload(ev: dict) -> dict:
    tag, prefix = TASKS[(ev["pipeline"], ev["stage"])]
    if ev["pipeline"] == "referrer":
        subject = ev["person_name"]
    elif ev["stage"] == "intro":
        subject = f"{ev['buyer_label']} via {ev['person_name']}"
    else:
        subject = ev["buyer_label"]
    when = ev["at"].astimezone(TZ).date().isoformat()
    via = f" via {ev['channel']}" if ev.get("channel") else ""
    return {
        "name": f"{prefix}: {subject}",
        "tags": [tag, GOAL_TAG],
        "context": f"Referral outreach: {ev['pipeline']} {ev['stage']} on {when}{via} "
        f"(outreach event {ev['id']}).",
    }


def late_bucket(at: datetime, now: datetime) -> str:
    """How long after the stage event its task reached Asana. The strategy layer counts
    by completed_at, so a late sync can move a count into the wrong week (spec §8)."""
    age = now - at
    if age.days < 1:
        return "0d"
    if age.days < 2:
        return "1d"
    if age.days <= 7:
        return "2-7d"
    return "8d+"


def _outcome(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        if 400 <= exc.response.status_code < 500:
            return "failed"  # tasks-api rejected it; retrying may not help
    return "retry"


def _sync_one(conn: Any, ev: dict, now: datetime, result: SyncResult) -> None:
    bucket = late_bucket(ev["at"], now)
    try:
        gid = ev["asana_task_gid"]
        if gid is None:
            gid = tasks_api.create_task(**task_payload(ev))
            outreach_repo.set_asana_gid(conn, ev["id"], gid)
            conn.commit()
        tasks_api.complete_task(gid)
        outreach_repo.set_asana_completed(conn, ev["id"])
        conn.commit()
    except Exception as exc:
        conn.rollback()  # a DB failure must not leave the shared connection aborted
        result.failed += 1
        otel.external_errors.add(1, {"system": "tasks"})
        otel.outreach_sync.add(1, {"target": "asana", "outcome": _outcome(exc), "late": bucket})
        logger.warning("Asana sync failed for outreach event %s", ev["id"], exc_info=True)
        return
    result.synced += 1
    result.late[bucket] = result.late.get(bucket, 0) + 1
    otel.outreach_sync.add(1, {"target": "asana", "outcome": "ok", "late": bucket})


def sync_events(conn: Any, event_ids: list[int], *, now: datetime | None = None) -> SyncResult:
    """Right after a commit, for the events that commit created (handlers and the CLI)."""
    result = SyncResult()
    if not event_ids:
        return result
    if not tasks_api.configured():
        result.skipped = True
        return result
    now = now or datetime.now(UTC)
    for event_id in event_ids:
        for ev in outreach_repo.sync_rows(conn, _KEYS, event_id=event_id):
            _sync_one(conn, ev, now, result)
    return result


def sync_pending(conn: Any, *, now: datetime | None = None, limit: int = 200) -> SyncResult:
    """The backstop: every countable event still missing its completed task."""
    result = SyncResult()
    if not tasks_api.configured():
        result.skipped = True
        return result
    now = now or datetime.now(UTC)
    for ev in outreach_repo.sync_rows(conn, _KEYS, limit=limit):
        _sync_one(conn, ev, now, result)
    return result
