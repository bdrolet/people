"""`outreach today`'s brief targets (working-queue spec §4). Pure. The numbers are not
in this public repo: they come from a TOML file in the private consulting repo, passed
with --targets or OUTREACH_TARGETS."""

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class TargetsError(ValueError):
    pass


@dataclass(frozen=True)
class Targets:
    per_day: int
    per_week: int
    start: date
    end: date
    total: int
    low_water_days: int
    workdays: frozenset[int]  # date.weekday() numbers, Monday = 0


def _table(data: dict, name: str, path: Path) -> dict:
    t = data.get(name)
    if not isinstance(t, dict):
        raise TargetsError(f"{path}: missing [{name}]")
    return t


def _value(t: dict, table: str, key: str, path: Path) -> object:
    if key not in t:
        raise TargetsError(f"{path}: [{table}] lacks {key}")
    return t[key]


def _positive(t: dict, table: str, key: str, path: Path) -> int:
    v = _value(t, table, key, path)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise TargetsError(f"{path}: [{table}] {key} must be a positive whole number, got {v!r}")
    return v


def _date(t: dict, table: str, key: str, path: Path) -> date:
    v = _value(t, table, key, path)
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
    briefs = _table(data, "briefs", path)
    queue = _table(data, "queue", path)
    work = _table(data, "workdays", path)
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
        per_day=_positive(briefs, "briefs", "per_day", path),
        per_week=_positive(briefs, "briefs", "per_week", path),
        start=start,
        end=end,
        total=_positive(briefs, "briefs", "total", path),
        low_water_days=_positive(queue, "queue", "low_water_days", path),
        workdays=frozenset(WEEKDAYS.index(d) for d in days),
    )
