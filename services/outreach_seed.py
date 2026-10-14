"""Propose referral candidates from the warm-pool bars measured 2026-10-12 (outreach
spec §7.1). Pure: the script fetches the rows, this decides. Nobody already in the
pipeline is proposed again.

A warm LinkedIn connection with no people row gets one outcome (linkedin-identity
design §5): `link` to the row already holding its URL, `in_google` when only a Google
contact does, `possible_duplicate` of a person with the same last name and first
initial, or `create`. iMessage and WhatsApp rows with no people row are listed for
Ben (the creating-person path), never inserted."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from services.linkedin_export import normalize_name

SOURCES = ("email", "imessage", "linkedin", "whatsapp")
RECRUITER_TERMS = re.compile(r"recruit|talent|sourcer|sourcing|headhunt", re.IGNORECASE)
LINKEDIN_KINDS = ("link", "in_google", "possible_duplicate", "create")


@dataclass
class Unlinked:
    source: str
    name: str | None
    detail: str  # the phone handle


@dataclass
class LinkedInOutcome:
    kind: str  # one of LINKEDIN_KINDS
    row: dict  # a repo/outreach.py::linkedin_seed_rows row
    person_id: int | None = None  # link: the people row holding this URL
    candidates: list[int] = field(default_factory=list)  # possible_duplicate: people ids
    google_names: list[str] = field(default_factory=list)  # possible_duplicate: no row yet


@dataclass
class SeedPlan:
    candidates: dict[int, set[str]] = field(default_factory=dict)  # person id -> sources
    already_in: set[int] = field(default_factory=set)
    unlinked: list[Unlinked] = field(default_factory=list)
    linkedin: list[LinkedInOutcome] = field(default_factory=list)

    def by_source(self) -> dict[str, int]:
        return {s: sum(1 for srcs in self.candidates.values() if s in srcs) for s in SOURCES}

    def linkedin_of(self, kind: str) -> list[LinkedInOutcome]:
        return [o for o in self.linkedin if o.kind == kind]


def name_key(name: str | None) -> tuple[str, str] | None:
    """(last name, first initial), normalized: the possible-duplicate rule. "Jon Smith"
    and "Jonathan Smith" share one. None for a single-word or empty name."""
    parts = normalize_name(name).split()
    if len(parts) < 2:
        return None
    return (parts[-1], parts[0][0])


def _full_name(row: dict) -> str:
    joined = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p)
    return joined or row["full_name"]


def contact_for(row: dict) -> dict:
    """The Google contact a LinkedIn-only person is created with (design §5)."""
    if row.get("first_name"):
        name = {"givenName": row["first_name"]}
        if row.get("last_name"):
            name["familyName"] = row["last_name"]
    else:
        name = {"givenName": row["full_name"]}
    contact: dict = {
        "names": [name],
        "urls": [{"value": f"https://www.{row['profile_url']}", "type": "profile"}],
    }
    org = {k: v for k, v in (("name", row.get("company")), ("title", row.get("position"))) if v}
    if org:
        contact["organizations"] = [org]
    return contact


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
    people_rows: list[dict] | None = None,
    google_urls: dict[str, list[dict]] | None = None,
    google_contacts: list[dict] | None = None,
) -> SeedPlan:
    cutoff = now - timedelta(days=window_days)
    result = SeedPlan()
    people_rows = people_rows or []
    google_urls = google_urls or {}

    people_by_url = {r["linkedin_url"]: r["id"] for r in people_rows if r.get("linkedin_url")}
    people_by_name: dict[tuple[str, str], list[int]] = {}
    for r in people_rows:
        if key := name_key(r.get("display_name")):
            people_by_name.setdefault(key, []).append(r["id"])
    linked_rns = {r["google_resource_name"] for r in people_rows if r.get("google_resource_name")}
    google_by_name: dict[tuple[str, str], list[str]] = {}
    for c in google_contacts or []:
        if c["resource_name"] in linked_rns:
            continue  # the people side already checked this one
        if key := name_key(c.get("display_name")):
            google_by_name.setdefault(key, []).append(c["display_name"])

    def classify(row: dict) -> LinkedInOutcome:
        url = row["profile_url"]
        if url in people_by_url:
            return LinkedInOutcome("link", row, person_id=people_by_url[url])
        if google_urls.get(url):
            return LinkedInOutcome("in_google", row)
        key = name_key(_full_name(row))
        ids = people_by_name.get(key, []) if key else []
        names = google_by_name.get(key, []) if key else []
        if ids or names:
            return LinkedInOutcome(
                "possible_duplicate", row, candidates=sorted(ids), google_names=sorted(names)
            )
        return LinkedInOutcome("create", row)

    def add(person_id: int, source: str) -> None:
        if person_id in existing:
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
            add(r["id"], "email")
    for r in linkedin_rows:
        if (
            _two_way(r)
            and not RECRUITER_TERMS.search(r["position"] or "")
            and _recent(r["last_message_at"], cutoff)
        ):
            if r["person_id"] is None:
                result.linkedin.append(classify(r))
            else:
                add(r["person_id"], "linkedin")
    for source, rows in (("imessage", imessage_rows), ("whatsapp", whatsapp_rows)):
        for r in rows:
            if (
                _two_way(r)
                and r["message_count"] >= min_messages
                and _recent(r["last_message_at"], cutoff)
            ):
                if r["person_id"] is None:
                    result.unlinked.append(Unlinked(source, r["display_name"], r["handle"]))
                else:
                    add(r["person_id"], source)
    return result
