"""Rows for the dashboard's DB-derived gauges (outreach dashboard design, "New metrics").
Pure: each function turns numbers the CLIs already compute into (value, labels) rows for
clients/otel.py::set_rows. Zero rows are kept so a stage or reason never drops out of a
sum. No person, username or message text ever becomes a label."""

from datetime import date, timedelta

from models.linkedin_outbox import KINDS, STATUSES
from models.outreach import LOST_REASONS, FunnelCounts
from services.outreach_targets import Targets

Row = tuple[float, dict[str, str]]

# The stages `outreach report` prints, in its column order.
COHORT_STAGES = ("briefed", "replied", "intro", "conversation", "qualified", "proposal", "signed")
DAILY_WHAT = ("approved", "sent")


def iso_week(d: date | None) -> str:
    """The cohort label `people.outreach_stage_population` already uses."""
    if d is None:
        return "none"
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def cohort_rows(counts: FunnelCounts) -> list[Row]:
    """`outreach report`'s cohort table, one row per cohort and stage."""
    return [
        (getattr(c, stage), {"cohort_week": iso_week(c.cohort_week), "stage": stage})
        for c in counts.cohorts
        for stage in COHORT_STAGES
    ]


def activity_rows(
    *, briefs_today: int, briefs_week: int, nudges_today: int, nudges_week: int
) -> list[Row]:
    return [
        (briefs_today, {"kind": "first_briefs", "window": "today"}),
        (briefs_week, {"kind": "first_briefs", "window": "week"}),
        (nudges_today, {"kind": "nudges", "window": "today"}),
        (nudges_week, {"kind": "nudges", "window": "week"}),
    ]


def lost_reason_rows(lost: dict[str, int]) -> list[Row]:
    reasons = list(LOST_REASONS) + sorted(set(lost) - set(LOST_REASONS))
    return [(lost.get(r, 0), {"lost_reason": r}) for r in reasons]


def target_rows(t: Targets) -> list[Row]:
    """Ben's private pace numbers: published to his own Grafana stack, never committed."""
    return [
        (t.per_day, {"target": "briefs_per_day"}),
        (t.per_week, {"target": "briefs_per_week"}),
        (t.total, {"target": "briefs_total"}),
        (t.low_water_days, {"target": "queue_low_water_days"}),
    ]


def outbox_status_rows(counts: dict[str, int]) -> list[Row]:
    return [(counts.get(s, 0), {"status": s}) for s in STATUSES]


def linkedin_daily_rows(rows: list[dict], *, today: date, days: int = 14) -> list[Row]:
    """Approved and sent per day and kind for the last `days` days (today included),
    zero-filled. `rows` carry what, day, kind and n (repo/linkedin_outbox.py::daily_counts)."""
    n = {(r["what"], r["day"], r["kind"]): int(r["n"]) for r in rows}
    out: list[Row] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        for what in DAILY_WHAT:
            for kind in KINDS:
                labels = {"what": what, "kind": kind, "day": day.isoformat()}
                out.append((n.get((what, day, kind), 0), labels))
    return out
