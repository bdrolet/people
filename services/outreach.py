"""The referral outreach stage machine (outreach spec §4, §6.1). The only writer of
stage data on the outreach_* tables; handlers and scripts call it, never repo/outreach.py
directly. Every write function takes an open connection and leaves committing to the
caller, so the event handlers keep it inside their durable transaction (spec §6)."""

import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import clients.otel as otel
from models.outreach import (
    BUYER_STAGES,
    CHANNELS,
    LOST_REASONS,
    REFERRER_STAGES,
    Advance,
    CohortCounts,
    FunnelCounts,
    TodayCounts,
)
from repo import outreach as outreach_repo

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


class NotQueued(ValueError):
    """Only a `to_contact` referrer has a queue position (working-queue spec §6)."""


class InvalidDocUrl(ValueError):
    """Only a Google Docs document link can be a contact doc (contact-docs spec D8)."""


DOC_URL = re.compile(r"^https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)(?:[/?#]\S*)?$")


class OpportunityNotFound(LookupError):
    pass


def transition(pipeline: str, current: str, target: str, *, force: bool = False) -> Transition:
    """Classify a move. `force` reopens a declined or skipped referrer: it may target any
    stage but candidate, e.g. `mark replied --force` records a late reply after a
    no-response decline."""
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


def _outreach_addresses() -> list[str]:
    return [
        a.strip().lower()
        for a in os.environ.get("OUTREACH_FROM_ADDRESSES", "").split(",")
        if a.strip()
    ]


def is_outreach_sender(address: str) -> bool:
    """Spec §6.2: only these identities' sent mail counts as a brief. The list is env
    (OUTREACH_FROM_ADDRESSES, comma-separated) so the public repo holds no address."""
    return (address or "").strip().lower() in set(_outreach_addresses())


def brief_from_address() -> str | None:
    """The identity `outreach draft` sends from: the first OUTREACH_FROM_ADDRESSES entry.
    inbox-api's default sender is not a consulting address, so drafts name it explicitly."""
    addresses = _outreach_addresses()
    return addresses[0] if addresses else None


def _count_event(pipeline: str, stage: str, channel: str | None, source: str) -> None:
    otel.outreach_stage_events.add(
        1, {"pipeline": pipeline, "stage": stage, "channel": channel or "none", "source": source}
    )


def get_contact(conn: Any, person_id: int) -> dict | None:
    return outreach_repo.get_contact(conn, person_id)


def ensure_contact(conn: Any, person_id: int, *, at: datetime, source: str) -> int | None:
    """Put a person in the pipeline at `candidate` (the seed, or `decide` for someone the
    seed never proposed). Returns the candidate event's id, or None if they were already in."""
    if outreach_repo.insert_contact_if_missing(conn, person_id) is None:
        return None
    _count_event("referrer", "candidate", None, source)
    return outreach_repo.insert_event(
        conn, pipeline="referrer", person_id=person_id, stage="candidate", at=at, source=source
    )


def advance(
    conn: Any,
    person_id: int,
    stage: str,
    *,
    at: datetime,
    source: str,
    channel: str | None = None,
    graph_message_id: str | None = None,
    from_address: str | None = None,
    force: bool = False,
    note: str | None = None,
) -> Advance | None:
    """Move a referrer to `stage` (spec §6.1). None when the person has no pipeline row;
    a no-op Advance when they are already at or past it; InvalidTransition otherwise."""
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        return None
    kind = transition("referrer", row["stage"], stage, force=force)
    if kind == "invalid":
        raise InvalidTransition("referrer", row["stage"], stage)
    if kind == "noop":
        return Advance(row=row, changed=False, event_id=None)

    changes: dict[str, Any] = {"stage": stage}
    if row["stage"] == "to_contact":
        changes["rank"] = None  # a queue position means nothing once they leave the queue
    if stage == "briefed":
        changes["follow_up_on"] = follow_up_date(at)
        if row["briefed_at"] is None:
            changes.update(briefed_at=at, cohort_week=cohort_week(at), channel=channel)
    else:
        changes["follow_up_on"] = None
    if stage == "replied" and row["replied_at"] is None:
        changes["replied_at"] = at

    updated = outreach_repo.update_contact(conn, person_id, changes)
    event_id = outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=person_id,
        stage=stage,
        at=at,
        channel=channel,
        source=source,
        graph_message_id=graph_message_id,
        from_address=from_address,
        note=note,
    )
    _count_event("referrer", stage, channel, source)
    return Advance(row=updated, changed=True, event_id=event_id)


def advance_from_event(
    conn: Any, person_id: int, stage: str, *, at: datetime, graph_message_id: str | None
) -> int | None:
    """The event handlers' entry (spec §6.2, §6.3): a move outside the table and a person
    with no pipeline row are both ignored silently. Returns the new event's id, if any."""
    if stage == "replied":
        # Spec §6.3: a reply counts only if it came after the brief. Inbox's sweep
        # republishes older mail, so a pre-brief email can arrive after the brief.
        row = outreach_repo.lock_contact(conn, person_id)
        if row and row["briefed_at"] is not None and at < row["briefed_at"]:
            return None
    try:
        res = advance(
            conn,
            person_id,
            stage,
            at=at,
            channel="email",
            source="event",
            graph_message_id=graph_message_id,
        )
    except InvalidTransition:
        return None
    return res.event_id if res else None


def open_opportunity(
    conn: Any,
    referrer_id: int,
    buyer_label: str,
    *,
    at: datetime,
    buyer_id: int | None = None,
    channel: str | None = None,
    source: str = "cli",
) -> Advance:
    """An introduction with the buyer on the thread (spec §4.2). The referrer moves to
    `introduced`; from `briefed`, the implied `replied` is appended first. Advance.row is
    the new opportunity and Advance.event_id its `intro` event."""
    label = (buyer_label or "").strip()
    if not label:
        raise ValueError('a buyer label ("Name, Company") is required')
    contact = outreach_repo.lock_contact(conn, referrer_id)
    if contact is None:
        raise NotInPipeline(str(referrer_id))
    if contact["stage"] == "briefed":
        advance(conn, referrer_id, "replied", at=at, channel=channel, source=source)
    advance(conn, referrer_id, "introduced", at=at, channel=channel, source=source)
    opp = outreach_repo.insert_opportunity(
        conn, referrer_id=referrer_id, buyer_id=buyer_id, buyer_label=label, at=at
    )
    event_id = outreach_repo.insert_event(
        conn,
        pipeline="buyer",
        opportunity_id=opp["id"],
        stage="intro",
        at=at,
        channel=channel,
        source=source,
    )
    _count_event("buyer", "intro", channel, source)
    return Advance(row=opp, changed=True, event_id=event_id)


def advance_opportunity(
    conn: Any,
    opportunity_id: int,
    stage: str,
    *,
    at: datetime,
    lost_reason: str | None = None,
    source: str = "cli",
) -> Advance:
    if stage == "lost":
        if lost_reason not in LOST_REASONS:
            raise ValueError(f"lost needs a reason, one of: {', '.join(LOST_REASONS)}")
    elif lost_reason is not None:
        raise ValueError("a lost reason applies only to the lost stage")
    opp = outreach_repo.lock_opportunity(conn, opportunity_id)
    if opp is None:
        raise OpportunityNotFound(str(opportunity_id))
    kind = transition("buyer", opp["stage"], stage)
    if kind == "invalid":
        raise InvalidTransition("buyer", opp["stage"], stage)
    if kind == "noop":
        return Advance(row=opp, changed=False, event_id=None)
    changes: dict[str, Any] = {"stage": stage, f"{stage}_at": at}
    if stage == "lost":
        changes["lost_reason"] = lost_reason
    updated = outreach_repo.update_opportunity(conn, opportunity_id, changes)
    event_id = outreach_repo.insert_event(
        conn, pipeline="buyer", opportunity_id=opportunity_id, stage=stage, at=at, source=source
    )
    _count_event("buyer", stage, None, source)
    if stage == "lost":
        otel.outreach_lost.add(1, {"lost_reason": str(lost_reason)})
    return Advance(row=updated, changed=True, event_id=event_id)


def set_lead(conn: Any, person_id: int, lead: str, *, today: date) -> dict:
    """A name Ben must chase (spec §4.2): a flag and a note, never an opportunity."""
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        raise NotInPipeline(str(person_id))
    line = f"{today.isoformat()} lead: {lead.strip()}"
    notes = f"{row['notes']}\n{line}" if row.get("notes") else line
    return outreach_repo.update_contact(conn, person_id, {"has_lead": True, "notes": notes})


def doc_id(url: str) -> str:
    """The Drive file id inside a Google Docs link."""
    match = DOC_URL.match(url.strip())
    if match is None:
        raise InvalidDocUrl(f"not a Google Docs link: {url}")
    return match.group(1)


def set_doc(conn: Any, person_id: int, url: str | None) -> dict:
    """Record the person's contact doc, or clear it with None (contact-docs spec D8). The
    link is the only record of which doc belongs to whom."""
    if url is not None:
        doc_id(url)
        url = url.strip()
    if outreach_repo.lock_contact(conn, person_id) is None:
        raise NotInPipeline(str(person_id))
    return outreach_repo.update_contact(conn, person_id, {"doc_url": url})


def people_named(conn: Any, text: str) -> list[dict]:
    """Pipeline people whose display name contains `text` (contact-docs spec §4)."""
    text = text.strip()
    return outreach_repo.pipeline_people_named(conn, text) if text else []


def merge_person(conn: Any, from_id: int, to_id: int) -> int:
    """Fold one person's pipeline into another's before the `from_id` people row is
    deleted (the outreach tables cascade). Events and opportunities all move. When both
    have a contact row, the further-along one survives; a tie keeps the keeper's. When
    the keeper's row wins, the loser's notes and has_lead are dropped (events and
    opportunities are kept)."""
    moved = outreach_repo.repoint_events(conn, from_id, to_id)
    moved += outreach_repo.repoint_opportunities(conn, from_id, to_id)
    loser = outreach_repo.lock_contact(conn, from_id)
    if loser is None:
        return moved
    keeper = outreach_repo.lock_contact(conn, to_id)
    if keeper is None:
        outreach_repo.move_contact(conn, from_id, to_id)
    elif REFERRER_RANK[loser["stage"]] > REFERRER_RANK[keeper["stage"]]:
        outreach_repo.delete_contact(conn, to_id)
        outreach_repo.move_contact(conn, from_id, to_id)
    else:
        outreach_repo.delete_contact(conn, from_id)
    return moved + 1


def _day(ts: datetime | None) -> str | None:
    return ts.astimezone(TZ).date().isoformat() if ts else None


# The order nudge 2 tries channels in when switching away from the brief's.
SWITCH_ORDER = ("email", "imessage", "whatsapp", "linkedin")


def _channels(r: dict) -> dict:
    return {
        "email": r["email"] is not None,
        "imessage": bool(r["has_imessage"]),
        "linkedin": r["linkedin_url"] or False,
        "whatsapp": bool(r["has_whatsapp"]),
    }


def queue_item(r: dict) -> dict:
    last = {
        "email": _day(r["email_last"]),
        "imessage": _day(r["imessage_last"]),
        "linkedin": _day(r["linkedin_last"]),
        "whatsapp": _day(r["whatsapp_last"]),
    }
    return {
        "person": {
            "id": r["id"],
            "email": r["email"],
            "display_name": r["display_name"],
            "company": r["company"],
            "job_title": r["job_title"],
        },
        "stage": r["stage"],
        "rank": r["rank"],
        "last_interaction": _day(r["last_interaction"]),
        "channels": _channels(r),
        "last_messages": {k: v for k, v in last.items() if v},
        "labels": list(r["labels"] or []),
        "notes": r["notes"],
        "outreach_notes": r["outreach_notes"],
        "has_lead": bool(r["has_lead"]),
        "follow_up_on": r["follow_up_on"].isoformat() if r["follow_up_on"] else None,
        "doc_url": r["doc_url"],
    }


def queue(conn: Any, *, stage: str = "to_contact", limit: int = 50) -> list[dict]:
    if stage not in REFERRER_STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(REFERRER_STAGES)}")
    return [queue_item(r) for r in outreach_repo.queue(conn, stage=stage, limit=limit)]


def set_rank(conn: Any, person_id: int, rank: int) -> dict:
    if rank < 1:
        raise ValueError("rank must be 1 or more")
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        raise NotInPipeline(str(person_id))
    if row["stage"] != "to_contact":
        raise NotQueued(f"person {person_id} is at {row['stage']}, not to_contact")
    return outreach_repo.update_contact(conn, person_id, {"rank": rank})


def set_ranks(conn: Any, person_ids: list[int]) -> None:
    """Rank 1..N in the given order and clear every other rank (working-queue spec §6.2).
    The caller commits, so a failure part-way leaves nothing behind."""
    if len(set(person_ids)) != len(person_ids):
        raise ValueError("a person appears more than once")
    outreach_repo.clear_ranks(conn)
    for n, person_id in enumerate(person_ids, start=1):
        set_rank(conn, person_id, n)


def clear_ranks(conn: Any) -> int:
    return outreach_repo.clear_ranks(conn)


def follow_up_item(r: dict, *, today: date) -> dict:
    """Working-queue spec §3.4: nudge 1 stays on the brief's channel (and email thread);
    nudge 2 switches to a channel not used yet when one exists; after two, propose a close."""
    count = int(r["nudge_count"] or 0)
    channels = _channels(r)
    brief_channel = r["brief_channel"]
    if count == 0:
        kind, suggested = "nudge 1", brief_channel
    elif count == 1:
        used = {brief_channel, r["last_nudge_channel"]}
        kind = "nudge 2"
        suggested = next((c for c in SWITCH_ORDER if channels[c] and c not in used), None)
    else:
        kind, suggested = "close", None
    return {
        "person": {"id": r["id"], "email": r["email"], "display_name": r["display_name"]},
        "kind": kind,
        "nudge_count": count,
        "follow_up_on": r["follow_up_on"].isoformat(),
        "overdue": r["follow_up_on"] < today,
        "brief": {
            "channel": brief_channel,
            "at": _day(r["brief_at"] or r["briefed_at"]),
            "graph_message_id": r["brief_message_id"],
        },
        "last_nudge_channel": r["last_nudge_channel"],
        "last_nudged_at": _day(r["last_nudged_at"]),
        "channels": channels,
        "suggested_channel": suggested,
    }


def follow_ups(conn: Any, *, today: date) -> list[dict]:
    return [follow_up_item(r, today=today) for r in outreach_repo.follow_ups(conn, today=today)]


FRESH_TEXT_MAX = 120


def fresh_item(r: dict) -> dict:
    """One `repo/outreach.py::fresh_inbound` row as JSON-shaped data: when, in New York
    time, and the message flattened to one line. Email has no text (people keeps only
    timestamps)."""
    text = " ".join((r["text"] or "").split()) or None
    if text and len(text) > FRESH_TEXT_MAX:
        text = text[:FRESH_TEXT_MAX] + "…"
    return {
        "person": {"id": r["person_id"], "display_name": r["display_name"]},
        "stage": r["stage"],
        "channel": r["channel"],
        "at": r["at"].astimezone(TZ).isoformat(),
        "text": text,
        "answered": bool(r["answered"]),
    }


def today_counts(
    conn: Any,
    *,
    today: date,
    per_day: int,
    plan_start: date,
    plan_end: date,
    fresh_since: datetime | None = None,
) -> TodayCounts:
    """The numbers `outreach today` needs (working-queue spec §5), in New York dates. The
    week runs Monday to today; the plan from its start to today, capped at its end. With
    `fresh_since`, also everyone on the list who messaged Ben since then, so he can
    answer while it's fresh."""
    week_start = today - timedelta(days=today.weekday())
    fresh = (
        [fresh_item(r) for r in outreach_repo.fresh_inbound(conn, since=fresh_since)]
        if fresh_since is not None
        else []
    )
    return TodayCounts(
        fresh=fresh,
        briefs_today=outreach_repo.first_briefs_between(conn, since=today, until=today),
        briefs_week=outreach_repo.first_briefs_between(conn, since=week_start, until=today),
        briefs_plan=(
            outreach_repo.first_briefs_between(conn, since=plan_start, until=min(today, plan_end))
            if today >= plan_start
            else 0
        ),
        nudges_week=outreach_repo.nudges_between(conn, since=week_start, until=today),
        queue_depth=outreach_repo.stage_count(conn, "to_contact"),
        follow_ups=follow_ups(conn, today=today),
        next_up=queue(conn, limit=per_day),
    )


def shared_briefs(conn: Any) -> list[dict]:
    return outreach_repo.shared_briefs(conn)


def record_population(conn: Any, *, today: date) -> None:
    """Refresh the funnel-shape gauges (spec §12.2). The cohort label is an ISO week,
    the only per-cohort attribute, so cardinality stays one series per stage per week."""
    rows = [
        (
            int(r["n"]),
            {
                "pipeline": r["pipeline"],
                "stage": r["stage"],
                "cohort_week": iso_week(r["cohort_week"]),
            },
        )
        for r in outreach_repo.population(conn)
    ]
    otel.set_outreach_population(rows, outreach_repo.follow_ups_due_count(conn, today))


def counts(
    conn: Any, *, today: date, since: date | None = None, until: date | None = None
) -> FunnelCounts:
    """Spec §9's numbers, per cohort week, all derived from the DB (never from counters)."""
    by_week: dict[date | None, CohortCounts] = {}

    def cohort(week: date | None) -> CohortCounts:
        return by_week.setdefault(week, CohortCounts(cohort_week=week))

    for r in outreach_repo.referrer_cohort_counts(conn, since=since, until=until):
        c = cohort(r["cohort_week"])
        c.briefed, c.replied = int(r["briefed"]), int(r["replied"])
    for r in outreach_repo.buyer_cohort_counts(conn, since=since, until=until):
        c = cohort(r["cohort_week"])
        c.intro, c.conversation = int(r["intro"]), int(r["conversation"])
        c.proposal, c.signed = int(r["proposal"]), int(r["signed"])
    cohorts = sorted(
        by_week.values(), key=lambda c: (c.cohort_week is None, c.cohort_week or date.min)
    )
    lost = {
        r["lost_reason"]: int(r["n"])
        for r in outreach_repo.lost_reasons(conn, since=since, until=until)
    }
    return FunnelCounts(cohorts, lost, outreach_repo.follow_ups_due_count(conn, today))


NUDGE_LABELS = ("1", "2", "3+")


def nudge_due_date(briefed_at: datetime, nudge_count: int) -> date:
    """Working-queue spec §3.1: due dates count from the brief, never from the last nudge,
    so a late nudge does not push the schedule back. +14 after nudge 1, +21 after 2 or more."""
    return briefed_at.astimezone(TZ).date() + timedelta(
        days=FOLLOW_UP_DAYS * (min(nudge_count, 2) + 1)
    )


def nudge(
    conn: Any,
    person_id: int,
    *,
    at: datetime,
    channel: str,
    source: str,
    graph_message_id: str | None = None,
    from_address: str | None = None,
    note: str | None = None,
) -> int | None:
    """Record a follow-up on a briefed referrer (working-queue spec §3.3). A nudge is an
    event, not a stage: the contact stays at `briefed`. Returns the event id, or None when
    the person is not at `briefed`, `at` is less than a day after the brief (a same-day
    correction is not a follow-up), or this email was already recorded for them."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; one of {', '.join(CHANNELS)}")
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None or row["stage"] != "briefed" or row["briefed_at"] is None:
        return None
    if at < row["briefed_at"] + timedelta(days=1):
        return None
    if graph_message_id and outreach_repo.referrer_event_with_message(
        conn, person_id, graph_message_id
    ):
        return None
    n = int(row["nudge_count"] or 0) + 1
    last = row["last_nudged_at"]
    outreach_repo.update_contact(
        conn,
        person_id,
        {
            "nudge_count": n,
            "last_nudged_at": max(at, last) if last else at,
            "follow_up_on": nudge_due_date(row["briefed_at"], n),
        },
    )
    event_id = outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=person_id,
        stage="nudge",
        at=at,
        channel=channel,
        source=source,
        graph_message_id=graph_message_id,
        from_address=from_address,
        note=note,
    )
    otel.outreach_nudges.add(
        1, {"channel": channel, "source": source, "n": NUDGE_LABELS[min(n, 3) - 1]}
    )
    return event_id


def advance_from_sent(
    conn: Any, person_id: int, *, at: datetime, graph_message_id: str | None
) -> tuple[str, int] | None:
    """Mail from an outreach address to a pipeline contact (working-queue spec §3.3): it
    briefs a `to_contact` row and nudges a `briefed` one. Any other stage, or no row, is
    ignored. Returns the kind and the new event id, if one was written."""
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        return None
    if row["stage"] == "to_contact":
        event_id = advance_from_event(
            conn, person_id, "briefed", at=at, graph_message_id=graph_message_id
        )
        return ("briefed", event_id) if event_id is not None else None
    if row["stage"] == "briefed":
        event_id = nudge(
            conn,
            person_id,
            at=at,
            channel="email",
            source="event",
            graph_message_id=graph_message_id,
        )
        return ("nudge", event_id) if event_id is not None else None
    return None
