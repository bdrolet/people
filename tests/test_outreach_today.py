import json
from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from models.outreach import TodayCounts
from services.outreach_targets import Targets
from services.outreach_today import build, render, to_json, workdays

TZ = ZoneInfo("America/New_York")
WEEKDAYS = frozenset(range(5))
TARGETS = Targets(
    per_day=5,
    per_week=25,
    start=date(2026, 10, 13),  # a Tuesday
    end=date(2026, 11, 21),
    total=150,
    low_water_days=5,
    workdays=WEEKDAYS,
)


def _counts(**kw) -> TodayCounts:
    base = dict(
        briefs_today=0,
        briefs_week=0,
        briefs_plan=0,
        nudges_week=0,
        queue_depth=100,
        follow_ups=[],
        next_up=[],
    )
    return TodayCounts(**(base | kw))


def _at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=TZ)


def _item(kind, overdue, *, pid=41, suggested=None, n=0):
    return {
        "person": {"id": pid, "email": f"p{pid}@example.com", "display_name": f"Person {pid}"},
        "kind": kind,
        "nudge_count": n,
        "follow_up_on": "2026-10-13",
        "overdue": overdue,
        "brief": {"channel": "email", "at": "2026-10-06", "graph_message_id": "g1"},
        "last_nudge_channel": None,
        "last_nudged_at": None,
        "channels": {"email": True, "imessage": True, "linkedin": False, "whatsapp": False},
        "suggested_channel": suggested,
    }


def _queued(pid=7, rank=1):
    return {
        "person": {"id": pid, "email": "ana@example.com", "display_name": "Ana Example"},
        "rank": rank,
        "last_interaction": "2026-09-30",
        "channels": {"email": True, "imessage": True, "linkedin": False, "whatsapp": False},
    }


def test_workdays_counts_inclusive_and_skips_weekends():
    assert workdays(date(2026, 10, 12), date(2026, 10, 18), WEEKDAYS) == 5
    assert workdays(date(2026, 10, 13), date(2026, 10, 13), WEEKDAYS) == 1
    assert workdays(date(2026, 10, 14), date(2026, 10, 13), WEEKDAYS) == 0


def test_first_morning_of_a_plan_that_starts_mid_week_is_on_pace():
    # Tue 2026-10-13: Monday is before the plan, so nothing is owed by yesterday.
    t = build(_counts(), TARGETS, now=_at(date(2026, 10, 13)))
    assert (t.week_by_yesterday, t.week_by_tonight) == (0, 5)
    assert (t.plan_by_yesterday, t.plan_by_tonight) == (0, 5)
    assert t.statuses == {"today": "on pace", "week": "on pace", "plan": "on pace", "nudges": "met"}
    assert t.verdict == "ON PACE" and t.need_today == 5


def test_monday_morning_after_a_full_first_week():
    t = build(_counts(briefs_plan=20), TARGETS, now=_at(date(2026, 10, 19)))
    assert (t.plan_by_yesterday, t.plan_by_tonight) == (20, 25)
    assert (t.week_by_yesterday, t.week_by_tonight) == (0, 5)
    assert t.verdict == "ON PACE"


def test_mid_week_behind():
    # Thu 10-22: Mon–Wed owed 15; 9 done.
    t = build(_counts(briefs_week=9, briefs_plan=29), TARGETS, now=_at(date(2026, 10, 22), 15))
    assert (t.week_by_yesterday, t.week_by_tonight) == (15, 20)
    assert t.statuses["week"] == "behind" and t.statuses["plan"] == "behind"
    assert t.verdict == "BEHIND" and t.need_today == 11


def test_every_line_met_is_on_track():
    t = build(
        _counts(briefs_today=5, briefs_week=10, briefs_plan=10),
        TARGETS,
        now=_at(date(2026, 10, 14), 18),
    )
    assert t.verdict == "ON TRACK" and t.need_today is None


def test_weekend_sets_no_target_and_keeps_fridays_marks():
    t = build(_counts(briefs_week=20, briefs_plan=20), TARGETS, now=_at(date(2026, 10, 17)))
    assert t.today_target is None and t.no_target_reason == "weekend"
    assert (t.week_by_yesterday, t.week_by_tonight) == (20, 20)
    assert t.statuses["today"] == "n/a" and t.verdict == "ON TRACK"
    assert "no target (weekend)" in render(t)


def test_before_start_the_plan_takes_no_part():
    t = build(_counts(), TARGETS, now=_at(date(2026, 10, 12)))
    assert t.plan_phase == "before start" and t.statuses["plan"] == "n/a"
    assert t.today_target is None and t.no_target_reason == "outside plan"
    assert t.verdict == "ON TRACK"
    assert "plan starts 2026-10-13" in render(t)


def test_after_end_the_plan_takes_no_part():
    t = build(_counts(briefs_plan=150), TARGETS, now=_at(date(2026, 11, 23)))
    assert t.plan_phase == "ended" and t.statuses["plan"] == "n/a"
    assert "plan ended 2026-11-21" in render(t)


def test_marks_are_capped_at_per_week_and_total():
    t = build(
        _counts(briefs_today=6, briefs_week=25, briefs_plan=54),
        replace(TARGETS, per_day=6),
        now=_at(date(2026, 10, 23), 18),
    )
    assert t.week_by_tonight == 25
    t = build(_counts(), replace(TARGETS, total=12), now=_at(date(2026, 10, 16)))
    assert t.plan_by_tonight == 12


def test_nudge_statuses():
    wed = _at(date(2026, 10, 14))
    assert (
        build(_counts(follow_ups=[_item("nudge 1", False)]), TARGETS, now=wed).statuses["nudges"]
        == "on pace"
    )
    behind = build(
        _counts(follow_ups=[_item("nudge 1", False), _item("nudge 2", True)]), TARGETS, now=wed
    )
    assert behind.statuses["nudges"] == "behind" and behind.verdict == "BEHIND"
    assert (behind.nudges_due, behind.nudges_overdue) == (2, 1)
    closes = build(_counts(follow_ups=[_item("close", True, n=2)]), TARGETS, now=wed)
    assert closes.statuses["nudges"] == "met" and closes.nudges_due == 0
    assert len(closes.closes) == 1


def test_low_queue_warning():
    t = build(_counts(queue_depth=24), TARGETS, now=_at(date(2026, 10, 14)))
    assert t.low_queue
    assert "Queue low: 24 at to_contact (4.8 workdays). Reassess or refill." in render(t)
    assert not build(_counts(queue_depth=25), TARGETS, now=_at(date(2026, 10, 14))).low_queue


def test_render_layout():
    t = build(
        _counts(
            briefs_today=2,
            briefs_week=7,
            briefs_plan=7,
            nudges_week=4,
            queue_depth=38,
            next_up=[_queued()],
            follow_ups=[
                _item("nudge 1", True, pid=11),
                _item("nudge 2", False, pid=12, suggested="imessage", n=1),
                _item("close", False, pid=41, n=2),
            ],
        ),
        TARGETS,
        now=_at(date(2026, 10, 14), 10),
    )
    lines = render(t).splitlines()
    assert lines[0].startswith("Outreach: Wed 2026-10-14") and lines[0].endswith("BEHIND")
    assert lines[2] == (
        "Briefs   today 2/5 · week 7/10 expected (25) · need 3 today · "
        "plan 7/10 by today (150 by Nov 21)"
    )
    assert lines[3] == "Nudges   due 2 (1 overdue) · sent this week 4"
    assert lines[4] == "Queue    38 at to_contact (7.6 workdays)"
    text = "\n".join(lines)
    assert "Brief next" in text and "Ana Example" in text and "#rank 1" in text
    assert "nudge 1 · briefed 10-06 · due 10-13 (overdue) · email" in text
    assert "nudge 2 · briefed 10-06 · due 10-13 · switch to imessage" in text
    assert "2 nudges, no reply → outreach mark 41 declined" in text


def test_to_json_is_plain_json():
    t = build(_counts(next_up=[_queued()]), TARGETS, now=_at(date(2026, 10, 13)))
    data = json.loads(to_json(t))
    assert data["day"] == "2026-10-13" and data["verdict"] == "ON PACE"
    assert data["next_up"][0]["person"]["id"] == 7
