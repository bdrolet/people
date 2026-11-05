"""Pure pacing rules for the LinkedIn send queue (send-queue design, "The sender"): the
send window, the randomized gap, the ceiling with its ramp, the Pacific day and week the
ceiling is counted in, and the message-text rules the MCP enforces. No I/O."""

import random
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models.linkedin_outbox import SendConfig, Window

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WINDOW = re.compile(r"^\s*(\S+)\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})\s+(\S+)\s*$")


class InvalidMessage(ValueError):
    pass


def _day(name: str) -> int:
    try:
        return DAY_NAMES.index(name.strip().lower()[:3])
    except ValueError:
        raise ValueError(f"unknown day {name!r}") from None


def parse_window(text: str) -> Window:
    """`Mon-Fri 08:00-19:00 America/Los_Angeles`; days are a range or a comma list."""
    m = _WINDOW.match(text or "")
    if not m:
        raise ValueError(f"send window {text!r}: expected 'Mon-Fri 08:00-19:00 <tz>'")
    days_text, h1, m1, h2, m2, tz_name = m.groups()
    days: set[int] = set()
    for part in days_text.split(","):
        if "-" in part:
            a, b = (_day(x) for x in part.split("-", 1))
            if b < a:
                raise ValueError(f"send window {text!r}: day range runs backwards")
            days.update(range(a, b + 1))
        else:
            days.add(_day(part))
    start, end = time(int(h1), int(m1)), time(int(h2), int(m2))
    if end <= start:
        raise ValueError(f"send window {text!r}: end must be after start")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"send window {text!r}: unknown time zone") from None
    return Window(frozenset(days), start, end, tz)


def in_window(now: datetime, w: Window) -> bool:
    local = now.astimezone(w.tz)
    return local.weekday() in w.days and w.start <= local.time() < w.end


def next_window_start(now: datetime, w: Window) -> datetime:
    """`now` when inside the window, else the next opening."""
    if in_window(now, w):
        return now
    local = now.astimezone(w.tz)
    for offset in range(8):
        d = local.date() + timedelta(days=offset)
        opening = datetime.combine(d, w.start, tzinfo=w.tz)
        if d.weekday() in w.days and opening > local:
            return opening
    raise ValueError("send window has no days")


def draw_gap(cfg: SendConfig, rng: random.Random) -> int:
    return rng.randint(cfg.gap_min, cfg.gap_max)


def caps(cfg: SendConfig, *, today: date, ramp_started_on: date | None) -> tuple[int, int]:
    """Today's (daily, weekly) ceiling. The daily cap climbs linearly from ramp_start to
    daily_cap over ramp_days from the first queued send; the weekly cap scales with it."""
    if cfg.ramp_days <= 0:
        return cfg.daily_cap, cfg.weekly_cap
    d = 0 if ramp_started_on is None else max(0, (today - ramp_started_on).days)
    step = min(d, cfg.ramp_days)
    daily = cfg.ramp_start + (cfg.daily_cap - cfg.ramp_start) * step // cfg.ramp_days
    return daily, cfg.weekly_cap * daily // cfg.daily_cap


def day_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    d = now.astimezone(tz).date()
    start = datetime.combine(d, time(0), tzinfo=tz)
    return start, datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)


def week_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    d = now.astimezone(tz).date()
    monday = d - timedelta(days=d.weekday())
    start = datetime.combine(monday, time(0), tzinfo=tz)
    return start, datetime.combine(monday + timedelta(days=7), time(0), tzinfo=tz)


# The MCP's own rules (mcp-server-linkedin 4.29.0, linkedin/contracts.py): refuse every C0
# control but CR/LF, DEL, NEL and the Unicode line/paragraph separators; then end lines on
# CR/CRLF/LF, blank whitespace-only lines, and trim the whole text. JS whitespace, since
# LinkedIn trims in the browser.
_REFUSED = {"\x7f", "\u0085", " ", " "}
_JS_WHITESPACE = " \t\n\v\f\r               　﻿"


def normalize_message(text: str) -> str:
    """The exact text LinkedIn would send, or InvalidMessage. The queue stores this, so
    the stored text is what goes out, byte for byte."""
    if any((ord(c) < 32 and c not in "\r\n") or c in _REFUSED for c in text):
        raise InvalidMessage("message must not contain control characters other than line breaks")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = ["" if all(c in _JS_WHITESPACE for c in line) else line for line in lines]
    out = "\n".join(lines).strip(_JS_WHITESPACE)
    if not out:
        raise InvalidMessage("message must contain non-whitespace characters")
    return out


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def load_config(env: Mapping[str, str]) -> SendConfig:
    base = SendConfig()
    cfg = SendConfig(
        daily_cap=_int(env, "LINKEDIN_DAILY_CAP", base.daily_cap),
        weekly_cap=_int(env, "LINKEDIN_WEEKLY_CAP", base.weekly_cap),
        ramp_start=_int(env, "LINKEDIN_RAMP_START", base.ramp_start),
        ramp_days=_int(env, "LINKEDIN_RAMP_DAYS", base.ramp_days),
        gap_min=_int(env, "LINKEDIN_GAP_MIN_SECONDS", base.gap_min),
        gap_max=_int(env, "LINKEDIN_GAP_MAX_SECONDS", base.gap_max),
        window=parse_window(env["LINKEDIN_SEND_WINDOW"])
        if env.get("LINKEDIN_SEND_WINDOW")
        else base.window,
        mcp_version=env.get("LINKEDIN_MCP_VERSION") or base.mcp_version,
    )
    if cfg.gap_min > cfg.gap_max:
        raise ValueError("LINKEDIN_GAP_MIN_SECONDS must not exceed LINKEDIN_GAP_MAX_SECONDS")
    if cfg.daily_cap <= 0 or cfg.ramp_start > cfg.daily_cap:
        raise ValueError("LINKEDIN_DAILY_CAP must be positive and at least LINKEDIN_RAMP_START")
    return cfg
