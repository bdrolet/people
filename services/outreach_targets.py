"""`outreach today`'s brief targets (working-queue spec §4). Pure. The numbers are not
in this public repo: they come from a TOML file in the private consulting repo, passed
with --targets or OUTREACH_TARGETS."""

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from services.targets_common import TargetsError, positive, table, value  # noqa: F401

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Targets:
    per_day: int
    per_week: int
    start: date
    end: date
    total: int
    low_water_days: int
    workdays: frozenset[int]  # date.weekday() numbers, Monday = 0


def _date(t: dict, table: str, key: str, path: Path) -> date:
    v = value(t, table, key, path)
    if isinstance(v, datetime) or not isinstance(v, date):
        raise TargetsError(f"{path}: [{table}] {key} must be a date like 2026-10-13, got {v!r}")
    return v


def load_targets(path: Path) -> Targets:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise TargetsError(f"targets file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise TargetsError(f"{path}: {e}") from e
    briefs = table(data, "briefs", path)
    queue = table(data, "queue", path)
    work = table(data, "workdays", path)
    start = _date(briefs, "briefs", "start", path)
    end = _date(briefs, "briefs", "end", path)
    if start > end:
        raise TargetsError(f"{path}: [briefs] start {start} is after end {end}")
    days = work.get("days")
    if not isinstance(days, list) or not days:
        raise TargetsError(f'{path}: [workdays] days must be a non-empty list like ["Mon"]')
    unknown = [d for d in days if d not in WEEKDAYS]
    if unknown:
        raise TargetsError(
            f"{path}: [workdays] unknown day(s) {unknown}; use {', '.join(WEEKDAYS)}"
        )
    return Targets(
        per_day=positive(briefs, "briefs", "per_day", path),
        per_week=positive(briefs, "briefs", "per_week", path),
        start=start,
        end=end,
        total=positive(briefs, "briefs", "total", path),
        low_water_days=positive(queue, "queue", "low_water_days", path),
        workdays=frozenset(WEEKDAYS.index(d) for d in days),
    )
