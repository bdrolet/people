"""The referral outreach stage machine (outreach spec §4, §6.1). The only writer of
stage data on the outreach_* tables; handlers and scripts call it, never repo/outreach.py
directly. Every write function takes an open connection and leaves committing to the
caller, so the event handlers keep it inside their durable transaction (spec §6)."""

import os
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from models.outreach import BUYER_STAGES, REFERRER_STAGES

TZ = ZoneInfo("America/New_York")
FOLLOW_UP_DAYS = 7

Transition = Literal["advance", "noop", "invalid"]

# Spec §4.3, verbatim. Anything not listed is rejected (CLI) or ignored (handlers).
_ALLOWED: dict[str, dict[str, set[str]]] = {
    "referrer": {
        "candidate": {"to_contact", "skipped"},
        "to_contact": {"briefed", "skipped"},
        "briefed": {"replied", "declined"},
        "replied": {"introduced", "declined"},
        "introduced": set(),
        "declined": set(),
        "skipped": set(),
    },
    "buyer": {
        "intro": {"conversation", "lost"},
        "conversation": {"proposal", "lost"},
        "proposal": {"signed", "lost"},
        "signed": set(),
        "lost": set(),
    },
}

# Every stage a row at the key stage has necessarily passed through. Requesting one of
# these is a no-op returning the current row: that is what makes replaying an event safe.
_REACHED: dict[str, dict[str, set[str]]] = {
    "referrer": {
        "candidate": {"candidate"},
        "to_contact": {"candidate", "to_contact"},
        "briefed": {"candidate", "to_contact", "briefed"},
        "replied": {"candidate", "to_contact", "briefed", "replied"},
        "introduced": {"candidate", "to_contact", "briefed", "replied", "introduced"},
        # Reopened only with --force, so asking for to_contact is rejected (the CLI shows
        # the --force hint) rather than silently no-op.
        "declined": {"candidate", "briefed", "declined"},
        "skipped": {"candidate", "skipped"},
    },
    "buyer": {
        "intro": {"intro"},
        "conversation": {"intro", "conversation"},
        "proposal": {"intro", "conversation", "proposal"},
        "signed": {"intro", "conversation", "proposal", "signed"},
        "lost": {"intro", "lost"},
    },
}

_REOPENABLE = {"declined", "skipped"}

# How far along a referrer row is; merge_person keeps the further row.
REFERRER_RANK = {
    "candidate": 0,
    "to_contact": 1,
    "skipped": 1,
    "briefed": 2,
    "declined": 3,
    "replied": 3,
    "introduced": 4,
}


class InvalidTransition(Exception):
    def __init__(self, pipeline: str, current: str, target: str) -> None:
        self.pipeline, self.current, self.target = pipeline, current, target
        hint = "; `outreach mark ... --force` reopens it" if current in _REOPENABLE else ""
        super().__init__(f"{pipeline} is at {current}; cannot move to {target}{hint}")


class NotInPipeline(LookupError):
    """The person has no outreach_contacts row (run `outreach decide` first)."""


class OpportunityNotFound(LookupError):
    pass


def transition(pipeline: str, current: str, target: str, *, force: bool = False) -> Transition:
    stages = REFERRER_STAGES if pipeline == "referrer" else BUYER_STAGES
    if target not in stages or current not in stages:
        raise ValueError(f"unknown {pipeline} stage: {current!r} -> {target!r}")
    if (
        force
        and pipeline == "referrer"
        and current in _REOPENABLE
        and target not in (current, "candidate")
    ):
        return "advance"
    if target in _REACHED[pipeline][current]:
        return "noop"
    if target in _ALLOWED[pipeline][current]:
        return "advance"
    return "invalid"


def cohort_week(at: datetime) -> date:
    local = at.astimezone(TZ).date()
    return local - timedelta(days=local.weekday())


def follow_up_date(at: datetime) -> date:
    return at.astimezone(TZ).date() + timedelta(days=FOLLOW_UP_DAYS)


def iso_week(d: date | None) -> str:
    if d is None:
        return "none"
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def is_outreach_sender(address: str) -> bool:
    """Spec §6.2: only these identities' sent mail counts as a brief. The list is env
    (OUTREACH_FROM_ADDRESSES, comma-separated) so the public repo holds no address."""
    wanted = {
        a.strip().lower()
        for a in os.environ.get("OUTREACH_FROM_ADDRESSES", "").split(",")
        if a.strip()
    }
    return (address or "").strip().lower() in wanted
