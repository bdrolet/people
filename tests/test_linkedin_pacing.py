import random
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from models.linkedin_outbox import SendConfig
from services import linkedin_pacing as p

PT = ZoneInfo("America/Los_Angeles")
W = p.parse_window("Mon-Fri 08:00-19:00 America/Los_Angeles")


def at(y, m, d, hh, mm=0, tz=PT):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def test_parse_window():
    assert W.days == frozenset({0, 1, 2, 3, 4})
    assert (W.start, W.end) == (time(8), time(19))
    assert W.tz == PT
    assert p.parse_window("Mon,Wed,Sat 09:30-10:00 UTC").days == frozenset({0, 2, 5})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Mon-Fri 08:00 America/Los_Angeles",
        "Foo-Fri 08:00-19:00 UTC",
        "Mon-Fri 19:00-08:00 UTC",
        "Mon-Fri 08:00-19:00 Not/AZone",
    ],
)
def test_parse_window_rejects(text):
    with pytest.raises(ValueError):
        p.parse_window(text)


def test_window_weekday_hours():
    # 2026-11-03 is a Tuesday; 2026-11-07 a Saturday.
    assert not p.in_window(at(2026, 11, 3, 7, 59), W)
    assert p.in_window(at(2026, 11, 3, 8, 0), W)
    assert p.in_window(at(2026, 11, 3, 18, 59), W)
    assert not p.in_window(at(2026, 11, 3, 19, 0), W)
    assert not p.in_window(at(2026, 11, 7, 12, 0), W)


def test_window_uses_its_own_timezone():
    # Monday 2026-11-09: 15:30 UTC is 07:30 PST, 16:00 UTC is 08:00 PST.
    assert not p.in_window(datetime(2026, 11, 9, 15, 30, tzinfo=UTC), W)
    assert p.in_window(datetime(2026, 11, 9, 16, 0, tzinfo=UTC), W)


def test_next_window_start_skips_weekend():
    assert p.next_window_start(at(2026, 11, 6, 19, 30), W) == at(2026, 11, 9, 8, 0)
    assert p.next_window_start(at(2026, 11, 3, 6, 0), W) == at(2026, 11, 3, 8, 0)
    # Already inside: now.
    now = at(2026, 11, 3, 9, 0)
    assert p.next_window_start(now, W) == now


def test_gap_within_bounds():
    rng = random.Random(1)
    cfg = SendConfig()
    gaps = [p.draw_gap(cfg, rng) for _ in range(200)]
    assert all(180 <= g <= 480 for g in gaps)
    assert len(set(gaps)) > 1


def test_caps_ramp():
    cfg = SendConfig()
    start = date(2026, 11, 2)
    assert p.caps(cfg, today=date(2026, 11, 5), ramp_started_on=None) == (30, 150)
    assert p.caps(cfg, today=start, ramp_started_on=start) == (30, 150)
    assert p.caps(cfg, today=date(2026, 11, 9), ramp_started_on=start) == (40, 200)
    assert p.caps(cfg, today=date(2026, 11, 16), ramp_started_on=start) == (50, 250)
    assert p.caps(cfg, today=date(2026, 12, 2), ramp_started_on=start) == (50, 250)


def test_caps_without_ramp():
    cfg = SendConfig(ramp_days=0)
    assert p.caps(cfg, today=date(2026, 11, 5), ramp_started_on=None) == (50, 250)


def test_day_bounds_are_pacific():
    since, until = p.day_bounds(datetime(2026, 11, 4, 7, 0, tzinfo=UTC), PT)
    assert since == at(2026, 11, 3, 0, 0) and until == at(2026, 11, 4, 0, 0)


def test_week_bounds_are_pacific():
    # Sunday 23:30 PST is already Monday in UTC; the ceiling week is Pacific.
    since, until = p.week_bounds(datetime(2026, 11, 9, 7, 30, tzinfo=UTC), PT)
    assert since == at(2026, 11, 2, 0, 0) and until == at(2026, 11, 9, 0, 0)


def test_normalize_message():
    assert p.normalize_message("  Hi Ann,\r\n\r\nThanks.\rBen \n ") == "Hi Ann,\n\nThanks.\nBen"
    assert p.normalize_message("a\n   \nb") == "a\n\nb"
    assert p.normalize_message("  indented\nline  ") == "indented\nline"


@pytest.mark.parametrize("bad", ["", "  \n ", "a\tb", "a\x00b", "a\x7fb", "a\u0085b", "a b", "a b"])
def test_normalize_message_refuses(bad):
    with pytest.raises(p.InvalidMessage):
        p.normalize_message(bad)


def test_load_config_defaults_and_overrides():
    cfg = p.load_config({})
    assert cfg == SendConfig()
    assert (cfg.daily_cap, cfg.weekly_cap, cfg.ramp_start, cfg.ramp_days) == (50, 250, 30, 14)
    assert (cfg.gap_min, cfg.gap_max, cfg.mcp_version) == (180, 480, "4.29.0")
    assert cfg.window == W
    assert p.load_config({"LINKEDIN_DAILY_CAP": "60"}).daily_cap == 60
    with pytest.raises(ValueError):
        p.load_config({"LINKEDIN_GAP_MIN_SECONDS": "500"})
    with pytest.raises(ValueError):
        p.load_config({"LINKEDIN_DAILY_CAP": "lots"})


def test_window_seconds_inside_one_day():
    assert p.window_seconds(at(2026, 11, 3, 9), at(2026, 11, 3, 10, 30), W) == 5400


def test_window_seconds_skips_nights_and_weekend():
    # Approved Friday 19:30, after the window closed: by Monday 08:10 it has waited 10 min.
    assert p.window_seconds(at(2026, 11, 6, 19, 30), at(2026, 11, 9, 8, 10), W) == 600


def test_window_seconds_spans_days():
    # Tue 18:00 -> Wed 09:00: one hour Tuesday evening, one hour Wednesday morning.
    assert p.window_seconds(at(2026, 11, 3, 18), at(2026, 11, 4, 9), W) == 7200


def test_window_seconds_reversed_is_zero():
    assert p.window_seconds(at(2026, 11, 3, 10), at(2026, 11, 3, 9), W) == 0
