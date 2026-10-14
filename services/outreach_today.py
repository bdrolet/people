"""`outreach today` (working-queue spec §5): what to do today and whether Ben is on pace.
Pure: the counts come from services/outreach.py::today_counts and the targets from
services/outreach_targets.py.

Each brief line has two marks: what was owed by the end of yesterday and what is owed
by tonight. Below the first is behind; between them is on pace (the day still has
time); at or above the second is met. Marks count only workdays inside the plan window,
so the days before a mid-week start are never owed."""

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models.outreach import TodayCounts
from services.outreach_targets import Targets

TZ = ZoneInfo("America/New_York")
MET, ON_PACE, BEHIND, NA = "met", "on pace", "behind", "n/a"
FRESH_HOURS = 48  # how far back the Fresh section looks


@dataclass
class Today:
    day: date
    verdict: str  # ON TRACK | ON PACE | BEHIND
    statuses: dict[str, str]  # today, week, plan, nudges → met | on pace | behind | n/a
    briefs_today: int
    today_target: int | None  # None: no target today
    no_target_reason: str | None  # "weekend" | "outside plan"
    briefs_week: int
    week_by_yesterday: int
    week_by_tonight: int
    per_week: int
    need_today: int | None  # set when the week line is not met
    plan_phase: str  # "before start" | "active" | "ended"
    briefs_plan: int
    plan_by_yesterday: int
    plan_by_tonight: int
    total: int
    start: date
    end: date
    nudges_due: int
    nudges_overdue: int
    nudges_week: int
    queue_depth: int
    queue_workdays: float
    low_queue: bool
    next_up: list[dict]
    nudges: list[dict]
    closes: list[dict]
    fresh: list[dict]  # who on the list messaged Ben in the last FRESH_HOURS, newest first


def workdays(start: date, end: date, days: frozenset[int]) -> int:
    """Workdays from start to end inclusive; 0 when end is before start."""
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() in days)


def _status(actual: int, by_yesterday: int, by_tonight: int) -> str:
    if actual >= by_tonight:
        return MET
    if actual < by_yesterday:
        return BEHIND
    return ON_PACE


def build(counts: TodayCounts, targets: Targets, *, now: datetime) -> Today:
    day = now.astimezone(TZ).date()
    yesterday = day - timedelta(days=1)
    in_plan = targets.start <= day <= targets.end
    is_workday = day.weekday() in targets.workdays

    # Week marks: workdays this week inside the plan window.
    lo = max(day - timedelta(days=day.weekday()), targets.start)
    week_by_yesterday = min(
        targets.per_day * workdays(lo, min(yesterday, targets.end), targets.workdays),
        targets.per_week,
    )
    week_by_tonight = min(
        targets.per_day * workdays(lo, min(day, targets.end), targets.workdays),
        targets.per_week,
    )
    plan_by_yesterday = min(
        targets.per_day * workdays(targets.start, min(yesterday, targets.end), targets.workdays),
        targets.total,
    )
    plan_by_tonight = min(
        targets.per_day * workdays(targets.start, min(day, targets.end), targets.workdays),
        targets.total,
    )
    plan_phase = (
        "before start" if day < targets.start else "ended" if day > targets.end else "active"
    )

    today_target = targets.per_day if in_plan and is_workday else None
    no_target_reason = None if today_target else ("weekend" if not is_workday else "outside plan")

    nudges = [f for f in counts.follow_ups if f["kind"] != "close"]
    closes = [f for f in counts.follow_ups if f["kind"] == "close"]
    overdue = sum(1 for f in nudges if f["overdue"])

    week_status = _status(counts.briefs_week, week_by_yesterday, week_by_tonight)
    statuses = {
        "today": NA
        if today_target is None
        else MET
        if counts.briefs_today >= today_target
        else ON_PACE,
        "week": week_status,
        "plan": _status(counts.briefs_plan, plan_by_yesterday, plan_by_tonight)
        if plan_phase == "active"
        else NA,
        "nudges": MET if not nudges else BEHIND if overdue else ON_PACE,
    }
    live = [s for s in statuses.values() if s != NA]
    verdict = (
        "BEHIND" if BEHIND in live else "ON TRACK" if all(s == MET for s in live) else "ON PACE"
    )

    return Today(
        day=day,
        verdict=verdict,
        statuses=statuses,
        briefs_today=counts.briefs_today,
        today_target=today_target,
        no_target_reason=no_target_reason,
        briefs_week=counts.briefs_week,
        week_by_yesterday=week_by_yesterday,
        week_by_tonight=week_by_tonight,
        per_week=targets.per_week,
        need_today=None if week_status == MET else week_by_tonight - counts.briefs_week,
        plan_phase=plan_phase,
        briefs_plan=counts.briefs_plan,
        plan_by_yesterday=plan_by_yesterday,
        plan_by_tonight=plan_by_tonight,
        total=targets.total,
        start=targets.start,
        end=targets.end,
        nudges_due=len(nudges),
        nudges_overdue=overdue,
        nudges_week=counts.nudges_week,
        queue_depth=counts.queue_depth,
        queue_workdays=counts.queue_depth / targets.per_day,
        low_queue=counts.queue_depth < targets.low_water_days * targets.per_day,
        next_up=counts.next_up,
        nudges=nudges,
        closes=closes,
        fresh=counts.fresh,
    )


def _name(person: dict) -> str:
    return person.get("display_name") or person.get("email") or f"person {person['id']}"


def _md(iso: str | None) -> str:
    return iso[5:] if iso else "?"


# Wide enough for every channel at once, so `last` lines up whatever mix a person has
# (linkedin-identity design §6). The order is services/outreach.py::_channels's.
_CHANNELS_WIDTH = len(" · ".join(("email", "imessage", "linkedin", "whatsapp")))


def _brief_line(n: int, item: dict) -> str:
    channels = " · ".join(k for k, v in item["channels"].items() if v)
    rank = f"   #rank {item['rank']}" if item.get("rank") else ""
    last = item["last_interaction"] or "never"
    return f"  {n}. {_name(item['person']):<18}{channels:<{_CHANNELS_WIDTH}} last {last}{rank}"


def _nudge_line(item: dict) -> str:
    brief = item["brief"]["channel"]
    via = item["suggested_channel"] or brief or "any channel"
    how = f"switch to {via}" if item["kind"] == "nudge 2" and via != brief else via
    due = f"due {_md(item['follow_up_on'])}" + (" (overdue)" if item["overdue"] else "")
    return (
        f"  {_name(item['person']):<18}{item['kind']} · briefed {_md(item['brief']['at'])}"
        f" · {due} · {how}"
    )


def _close_line(item: dict) -> str:
    return (
        f"  {_name(item['person']):<18}briefed {_md(item['brief']['at'])} · "
        f"{item['nudge_count']} nudges, no reply → outreach mark {item['person']['id']} declined"
    )


def _fresh_line(item: dict) -> str:
    """`  Jamie Example     imessage · 10-14 09:00 · briefed · unanswered · "are you around?"`"""
    when = datetime.fromisoformat(item["at"]).strftime("%m-%d %H:%M")
    parts = [
        item["channel"],
        when,
        item["stage"],
        "answered" if item["answered"] else "unanswered",
    ]
    if item["text"]:
        parts.append(f'"{item["text"]}"')
    return f"  {_name(item['person']):<18}{' · '.join(parts)}"


def render(t: Today) -> str:
    head = f"Outreach: {t.day:%a %Y-%m-%d}"
    lines = [f"{head:<60}{t.verdict}", ""]
    if t.fresh:
        # First thing on the screen: someone on the list just wrote, and a reply while
        # it's fresh beats any brief still owed today.
        lines += [f"Fresh: messaged you in the last {FRESH_HOURS}h (Pacific time)"]
        lines += [_fresh_line(i) for i in t.fresh] + [""]
    if t.today_target is not None:
        parts = [f"today {t.briefs_today}/{t.today_target}"]
    else:
        parts = [f"today {t.briefs_today}, no target ({t.no_target_reason})"]
    parts.append(f"week {t.briefs_week}/{t.week_by_tonight} expected ({t.per_week})")
    if t.need_today:
        parts.append(f"need {t.need_today} today")
    if t.plan_phase == "active":
        parts.append(
            f"plan {t.briefs_plan}/{t.plan_by_tonight} by today "
            f"({t.total} by {t.end:%b} {t.end.day})"
        )
    elif t.plan_phase == "before start":
        parts.append(f"plan starts {t.start.isoformat()}")
    else:
        parts.append(f"plan ended {t.end.isoformat()} at {t.briefs_plan}/{t.total}")
    lines.append("Briefs   " + " · ".join(parts))
    overdue = f" ({t.nudges_overdue} overdue)" if t.nudges_overdue else ""
    lines.append(f"Nudges   due {t.nudges_due}{overdue} · sent this week {t.nudges_week}")
    lines.append(f"Queue    {t.queue_depth} at to_contact ({t.queue_workdays:.1f} workdays)")
    if t.low_queue:
        lines.append(
            f"Queue low: {t.queue_depth} at to_contact ({t.queue_workdays:.1f} workdays). "
            "Reassess or refill."
        )
    if t.next_up:
        lines += ["", "Brief next"] + [_brief_line(n, i) for n, i in enumerate(t.next_up, 1)]
    if t.nudges:
        lines += ["Nudge"] + [_nudge_line(i) for i in t.nudges]
    if t.closes:
        lines += ["Propose close"] + [_close_line(i) for i in t.closes]
    return "\n".join(lines)


def to_json(t: Today) -> str:
    return json.dumps(asdict(t), indent=2, default=lambda v: v.isoformat())
