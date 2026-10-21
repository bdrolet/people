"""The funded-founder campaign status machine (campaign spec §7.1). The only writer of
status data on the campaign_* tables; handlers and the CLI call it, never
repo/campaign.py directly. Every write function takes an open connection and leaves
committing to the caller, so the event handlers keep it inside their durable transaction."""

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from models.campaign import CONTACT_STATUSES, OPT_OUT_PHRASES, Advance
from repo import campaign as campaign_repo
from repo import outreach as outreach_repo
from repo import people as people_repo
from services.linkedin_export import normalize_profile_url

TZ = ZoneInfo("America/New_York")
# The default when a caller does not name the date; send passes targets.follow_up_business_days.
FOLLOW_UP_BUSINESS_DAYS = 5
US_COUNTRIES = frozenset({"us", "usa", "united states"})
SOURCE = "cli"  # company and add-contact events have no other origin

Transition = Literal["advance", "noop", "invalid"]

# Spec §7.1, verbatim. `approved → drafted` happens only through the stale rule.
_ALLOWED: dict[str, set[str]] = {
    "candidate": {"unverified", "skipped_warm", "drafted", "skipped"},
    "unverified": {"drafted", "skipped"},  # after a re-verify
    "drafted": {"approved", "skipped"},
    "approved": {"sent", "drafted", "skipped"},
    "sent": {"followed_up", "replied", "opted_out", "bounced", "booked"},
    "followed_up": {"replied", "opted_out", "bounced", "booked"},
    "replied": {"opted_out", "booked"},
    "skipped_warm": set(),
    "skipped": set(),
    "opted_out": set(),
    "bounced": set(),
    "booked": set(),
}

_PRE_SEND = {"candidate", "unverified", "drafted", "approved"}
# Every status a row at the key status has already passed. Requesting one of these is a
# no-op returning the current row: that is what makes replaying an event safe. `approved`
# leaves out `drafted` so the stale rule can move it back.
_REACHED: dict[str, set[str]] = {
    "candidate": {"candidate"},
    "unverified": {"candidate", "unverified"},
    "skipped_warm": {"candidate", "skipped_warm"},
    "drafted": {"candidate", "unverified", "drafted"},
    "approved": {"candidate", "unverified", "approved"},
    "skipped": {"candidate", "skipped"},
    "sent": _PRE_SEND | {"sent"},
    "followed_up": _PRE_SEND | {"sent", "followed_up"},
    "replied": _PRE_SEND | {"sent", "followed_up", "replied"},
    "opted_out": _PRE_SEND | {"sent", "followed_up", "replied", "opted_out"},
    "bounced": _PRE_SEND | {"sent", "followed_up", "bounced"},
    "booked": _PRE_SEND | {"sent", "followed_up", "replied", "booked"},
}

TERMINAL = frozenset(s for s, targets in _ALLOWED.items() if not targets)


class InvalidTransition(ValueError):
    def __init__(self, current: str, target: str) -> None:
        self.current, self.target = current, target
        super().__init__(f"contact is at {current}; cannot move to {target}")


class NotFound(LookupError):
    pass


def transition(current: str, target: str) -> Transition:
    if current not in CONTACT_STATUSES or target not in CONTACT_STATUSES:
        raise ValueError(f"unknown contact status: {current!r} -> {target!r}")
    if target in _REACHED[current]:
        return "noop"
    if target in _ALLOWED[current]:
        return "advance"
    return "invalid"


def follow_up_date(sent_at: datetime, business_days: int) -> date:
    """`business_days` weekdays after the send's New York date (the send day not counted)."""
    day = sent_at.astimezone(TZ).date()
    left = business_days
    while left > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day


def _now() -> datetime:
    return datetime.now(UTC)


# --- runs and companies ---------------------------------------------------------------------


def start_run(conn: Any, *, now: datetime, targets: Any) -> dict:
    """Look back to the previous run, or `lookback_days_first_run` days on the first one."""
    last = campaign_repo.latest_run(conn)
    since = last["ran_at"] if last else now - timedelta(days=targets.lookback_days_first_run)
    return campaign_repo.insert_run(
        conn,
        ran_at=now,
        lookback_from=since.astimezone(TZ).date(),
        lookback_to=now.astimezone(TZ).date(),
    )


def _company_event(conn: Any, company_id: int, status: str, note: str | None = None) -> int:
    return campaign_repo.insert_event(
        conn,
        contact_id=None,
        company_id=company_id,
        status=status,
        at=_now(),
        source=SOURCE,
        note=note,
    )


def add_company(conn: Any, fields: dict, *, run_id: int) -> tuple[dict, bool]:
    """Insert unless the domain is known; a second add returns the first row, untouched."""
    fields = {**fields, "domain": fields["domain"].strip().lower(), "run_id": run_id}
    country = (fields.get("hq_country") or "").strip()
    non_us = bool(country) and country.lower() not in US_COUNTRIES
    if non_us:
        fields.update(status="below_bar", fit_notes="non-US")
    row = campaign_repo.insert_company_if_missing(conn, fields)
    if row is None:
        return campaign_repo.get_company_by_domain(conn, fields["domain"]), False
    if non_us:
        _company_event(conn, row["id"], "below_bar", "non-US")
    return row, True


def _company(conn: Any, company_id: int) -> dict:
    row = campaign_repo.get_company(conn, company_id)
    if row is None:
        raise NotFound(f"no campaign company {company_id}")
    return row


def score_company(conn: Any, company_id: int, *, score: int, notes: str, threshold: int) -> dict:
    _company(conn, company_id)
    status = "active" if score >= threshold else "below_bar"
    row = campaign_repo.update_company(
        conn, company_id, {"fit_score": score, "fit_notes": notes, "status": status}
    )
    _company_event(conn, company_id, status)
    return row


def promote_company(conn: Any, company_id: int) -> dict:
    """Ben's override of the score: `below_bar → active`."""
    row = _company(conn, company_id)
    if row["status"] == "active":
        return row
    if row["status"] != "below_bar":
        raise InvalidTransition(row["status"], "active")
    row = campaign_repo.update_company(conn, company_id, {"status": "active"})
    _company_event(conn, company_id, "active", "promoted")
    return row


# --- contacts -------------------------------------------------------------------------------


def _domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def _is_warm(conn: Any, email: str | None, linkedin_url: str | None) -> bool:
    """Spec global constraint: two-way history, or anyone in the referral pipeline."""
    if email:
        person = people_repo.get(conn, email)
        if person and person["message_count"] > 0 and person["my_response_count"] > 0:
            return True
    url = normalize_profile_url(linkedin_url)
    if url:
        person = people_repo.get_by_linkedin_url(conn, url)
        if person and outreach_repo.get_contact(conn, person["id"]) is not None:
            return True
    domain = _domain(email)
    return bool(domain and campaign_repo.people_in_outreach_by_domain(conn, domain))


def _existing_contact(conn: Any, company_id: int, email: str | None) -> dict | None:
    if not email:
        return None
    row = campaign_repo.get_contact_by_email(conn, email)
    return row if row and row["company_id"] == company_id else None


def add_contact(conn: Any, fields: dict, *, run_id: int) -> tuple[dict, str]:
    """Insert a buyer behind the warm guard. Returns the row and "candidate",
    "skipped_warm" or "existing" (already recorded for this company; left untouched)."""
    company_id, email = fields["company_id"], fields.get("email")
    existing = _existing_contact(conn, company_id, email)
    if existing:
        return existing, "existing"
    status = "skipped_warm" if _is_warm(conn, email, fields.get("linkedin_url")) else "candidate"
    row = campaign_repo.insert_contact_if_missing(
        conn, {**fields, "status": status, "run_id": run_id}
    )
    if row is None:  # lost a race with a concurrent add
        return _existing_contact(conn, company_id, email), "existing"
    campaign_repo.insert_event(
        conn, contact_id=row["id"], company_id=company_id, status=status, at=_now(), source=SOURCE
    )
    if status == "skipped_warm":
        company = _company(conn, company_id)
        if company["status"] != "warm":
            campaign_repo.update_company(conn, company_id, {"status": "warm"})
            _company_event(conn, company_id, "warm", f"warm contact {row['id']}")
    return row, status


def _write_status(
    conn: Any,
    row: dict,
    status: str,
    *,
    at: datetime,
    source: str,
    graph_message_id: str | None,
    note: str | None,
    changes: dict | None = None,
) -> Advance:
    """Write `status` and its columns, append the event. The table is the caller's check."""
    update: dict[str, Any] = {"status": status}
    if status == "sent":
        update["sent_at"] = at
        update["follow_up_on"] = follow_up_date(at, FOLLOW_UP_BUSINESS_DAYS)
        if graph_message_id:
            update["graph_message_id"] = graph_message_id
    else:
        update["follow_up_on"] = None  # only a `sent` row waits on a follow-up
    if status == "replied" and row.get("replied_at") is None:
        update["replied_at"] = at
    update.update(changes or {})
    updated = campaign_repo.update_contact(conn, row["id"], update)
    event_id = campaign_repo.insert_event(
        conn,
        contact_id=row["id"],
        company_id=row["company_id"],
        status=status,
        at=at,
        source=source,
        graph_message_id=graph_message_id,
        note=note,
    )
    return Advance(row=updated, changed=True, event_id=event_id)


def advance(
    conn: Any,
    contact_id: int,
    status: str,
    *,
    at: datetime,
    source: str,
    graph_message_id: str | None = None,
    note: str | None = None,
    changes: dict | None = None,
) -> Advance:
    """Move a contact to `status`: a no-op Advance when it is already at or past it,
    InvalidTransition when the table does not allow it. `changes` are extra contact
    columns written with the move (send's `draft_id`, its `follow_up_on`)."""
    row = campaign_repo.lock_contact(conn, contact_id)
    if row is None:
        raise NotFound(f"no campaign contact {contact_id}")
    kind = transition(row["status"], status)
    if kind == "invalid":
        raise InvalidTransition(row["status"], status)
    if kind == "noop":
        return Advance(row=row, changed=False, event_id=None)
    return _write_status(
        conn,
        row,
        status,
        at=at,
        source=source,
        graph_message_id=graph_message_id,
        note=note,
        changes=changes,
    )


def opt_out_company(
    conn: Any, company_id: int, *, at: datetime, source: str, note: str | None
) -> int:
    """One "no" from anyone at the company stops the company: every non-terminal contact,
    including ones never emailed, goes to `opted_out`. Returns how many changed."""
    company = _company(conn, company_id)
    changed = 0
    for contact in campaign_repo.contacts_by_domain(conn, company["domain"]):
        if contact["company_id"] != company_id:
            continue
        row = campaign_repo.lock_contact(conn, contact["id"])
        if row is None or row["status"] in TERMINAL:
            continue
        _write_status(
            conn, row, "opted_out", at=at, source=source, graph_message_id=None, note=note
        )
        changed += 1
    if company["status"] != "opted_out":
        campaign_repo.update_company(conn, company_id, {"status": "opted_out"})
        campaign_repo.insert_event(
            conn,
            contact_id=None,
            company_id=company_id,
            status="opted_out",
            at=at,
            source=source,
            note=note,
        )
    return changed


# --- opt-out phrases ------------------------------------------------------------------------

_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")
# "no" opens ordinary replies ("No problem, send me the intake"), so it counts only as the
# whole line or followed by punctuation ("No, thanks"); the other phrases may also be
# followed by a space ("Not interested in this", "Unsubscribe me").
_PUNCTUATION_ONLY = frozenset({"no"})


def is_opt_out(body: str | None) -> bool:
    """True when the first non-empty line is an opt-out phrase or starts with one."""
    first = next((ln for ln in (body or "").splitlines() if ln.strip()), "")
    line = _EDGE_PUNCT.sub("", first.strip()).lower()
    if not line:
        return False
    for phrase in OPT_OUT_PHRASES:
        if line == phrase:
            return True
        if line.startswith(phrase) and len(line) > len(phrase):
            nxt = line[len(phrase)]
            if (nxt == " " and phrase not in _PUNCTUATION_ONLY) or (
                not nxt.isalnum() and not nxt.isspace() and nxt != "_"
            ):
                return True
    return False
