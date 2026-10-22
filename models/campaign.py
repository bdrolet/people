"""Funded-founder campaign types (campaign spec §7.1). Pure: no imports from other layers.
The tuples mirror the CHECK constraints in repo/schema.sql."""

from dataclasses import dataclass

ROUNDS = ("seed", "a", "b", "other")
COMPANY_STATUSES = ("new", "below_bar", "warm", "active", "opted_out", "closed")
CONTACT_STATUSES = (
    "candidate",
    "unverified",
    "skipped_warm",
    "drafted",
    "approved",
    "skipped",
    "sent",
    "followed_up",
    "replied",
    "opted_out",
    "bounced",
    "booked",
)
VERIFICATIONS = ("valid", "accept_all", "unknown", "invalid")
EMAIL_SOURCES = ("apollo", "hunter", "manual")
EVENT_SOURCES = ("cli", "event", "page")
# A reply line that is, or starts with, one of these (case-folded, trimmed) opts the contact out.
OPT_OUT_PHRASES = (
    "no",
    "unsubscribe",
    "remove",
    "not interested",
    "no thanks",
    "please remove me",
    "stop",
    "opt out",
    "opt-out",
)


@dataclass
class Advance:
    row: dict  # the campaign_contacts or campaign_companies row after the call
    changed: bool  # False: the row was already at the requested status
    event_id: int | None  # the appended campaign_events id, when changed


@dataclass
class RunCounts:
    companies_found: int = 0
    above_bar: int = 0
    candidate: int = 0
    unverified: int = 0
    skipped_warm: int = 0
    drafted: int = 0
    approved: int = 0
    skipped: int = 0
    sent: int = 0
    followed_up: int = 0
    replied: int = 0
    opted_out: int = 0
    bounced: int = 0
    booked: int = 0
