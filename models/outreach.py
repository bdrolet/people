"""Referral outreach pipeline types (outreach spec §4). Pure: no imports from other layers."""

from dataclasses import dataclass, field
from datetime import date

REFERRER_STAGES = (
    "candidate",
    "to_contact",
    "briefed",
    "replied",
    "introduced",
    "declined",
    "skipped",
)
BUYER_STAGES = ("intro", "conversation", "proposal", "signed", "lost")
# Event kinds that are not stages (working-queue spec §3.2): appended to outreach_events,
# never stored in outreach_contacts.stage, never a CLI stage choice, never an Asana task.
ACTIVITY_KINDS = ("nudge",)
CHANNELS = ("email", "imessage", "whatsapp", "linkedin", "call", "in_person")
SOURCES = ("event", "cli", "hubspot", "seed")
LOST_REASONS = ("no_budget", "no_fit", "price", "scope", "timing", "no_response", "other")
# The stages `outreach report` measures against the floors (spec §9), in funnel order.
REPORT_STAGES = ("replied", "intro", "conversation", "proposal", "signed")


@dataclass
class Advance:
    row: dict  # the outreach_contacts or outreach_opportunities row after the call
    changed: bool  # False: the row was already at or past the requested stage
    event_id: int | None  # the appended outreach_events id, when changed


@dataclass
class CohortCounts:
    cohort_week: date | None
    briefed: int = 0
    replied: int = 0
    intro: int = 0
    conversation: int = 0
    proposal: int = 0
    signed: int = 0


@dataclass
class FunnelCounts:
    cohorts: list[CohortCounts]
    lost_reasons: dict[str, int] = field(default_factory=dict)
    follow_ups_due: int = 0
