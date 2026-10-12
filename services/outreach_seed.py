"""Propose referral candidates from the warm-pool bars measured 2026-10-12 (outreach
spec §7.1). Pure: the script fetches the rows, this decides. A row with no people
link is listed for Ben (creating the person is the creating-person path), never
inserted, and nobody already in the pipeline is proposed again."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SOURCES = ("email", "imessage", "linkedin", "whatsapp")
RECRUITER_TERMS = re.compile(r"recruit|talent|sourcer|sourcing|headhunt", re.IGNORECASE)


@dataclass
class Unlinked:
    source: str
    name: str | None
    detail: str  # LinkedIn profile URL, or the phone handle


@dataclass
class SeedPlan:
    candidates: dict[int, set[str]] = field(default_factory=dict)  # person id -> sources
    already_in: set[int] = field(default_factory=set)
    unlinked: list[Unlinked] = field(default_factory=list)

    def by_source(self) -> dict[str, int]:
        return {s: sum(1 for srcs in self.candidates.values() if s in srcs) for s in SOURCES}


def _recent(ts: datetime | None, cutoff: datetime) -> bool:
    return ts is not None and ts >= cutoff


def _two_way(row: dict) -> bool:
    """Both sides wrote: Ben sent some, and the total holds some of theirs."""
    return row["my_message_count"] > 0 and row["message_count"] - row["my_message_count"] > 0


def plan(
    *,
    email_rows: list[dict],
    linkedin_rows: list[dict],
    imessage_rows: list[dict],
    whatsapp_rows: list[dict],
    existing: set[int],
    now: datetime,
    window_days: int = 730,
    min_messages: int = 10,
) -> SeedPlan:
    cutoff = now - timedelta(days=window_days)
    result = SeedPlan()

    def add(person_id: int | None, source: str, name: str | None, detail: str) -> None:
        if person_id is None:
            result.unlinked.append(Unlinked(source, name, detail))
        elif person_id in existing:
            result.already_in.add(person_id)
        else:
            result.candidates.setdefault(person_id, set()).add(source)

    for r in email_rows:
        if (
            r["eligible"]
            and not r["automated"]
            and r["message_count"] > 0
            and r["my_response_count"] > 0
            and _recent(r["last_interaction"], cutoff)
        ):
            add(r["id"], "email", r["display_name"], r["email"])
    for r in linkedin_rows:
        if (
            _two_way(r)
            and not RECRUITER_TERMS.search(r["position"] or "")
            and _recent(r["last_message_at"], cutoff)
        ):
            add(r["person_id"], "linkedin", r["full_name"], r["profile_url"])
    for source, rows in (("imessage", imessage_rows), ("whatsapp", whatsapp_rows)):
        for r in rows:
            if (
                _two_way(r)
                and r["message_count"] >= min_messages
                and _recent(r["last_message_at"], cutoff)
            ):
                add(r["person_id"], source, r["display_name"], r["handle"])
    return result
