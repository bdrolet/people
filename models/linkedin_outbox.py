"""LinkedIn send queue types (docs/superpowers/specs/2026-11-05-linkedin-send-queue-design.md).
Pure: no imports from other layers."""

from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

STATUSES = ("queued", "sending", "sent", "set_aside", "check_linkedin", "cancelled")
KINDS = ("brief", "nudge", "reply", "other")
MODES = ("queued", "send_now", "manual")
# Rows that count against the ceiling: an unknown outcome may have gone out, so it counts.
COUNTED_STATUSES = ("sent", "sending", "check_linkedin")


@dataclass(frozen=True)
class Window:
    days: frozenset[int]  # Monday = 0
    start: time
    end: time
    tz: ZoneInfo


def _default_window() -> Window:
    return Window(frozenset(range(5)), time(8), time(19), ZoneInfo("America/Los_Angeles"))


@dataclass(frozen=True)
class SendConfig:
    daily_cap: int = 50
    weekly_cap: int = 250
    ramp_start: int = 30
    ramp_days: int = 14
    gap_min: int = 180
    gap_max: int = 480
    window: Window = field(default_factory=_default_window)
    mcp_version: str = "4.29.0"
