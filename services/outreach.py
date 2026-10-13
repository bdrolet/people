"""The referral outreach stage machine (outreach spec §4, §6.1). The only writer of
stage data on the outreach_* tables; handlers and scripts call it, never repo/outreach.py
directly. Every write function takes an open connection and leaves committing to the
caller, so the event handlers keep it inside their durable transaction (spec §6)."""

import os
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
        "last_interaction": _day(r["last_interaction"]),
        "channels": {
            "email": r["email"] is not None,
            "imessage": bool(r["has_imessage"]),
            "linkedin": r["linkedin_url"] or False,
            "whatsapp": bool(r["has_whatsapp"]),
        },
        "last_messages": {k: v for k, v in last.items() if v},
        "labels": list(r["labels"] or []),
        "notes": r["notes"],
        "outreach_notes": r["outreach_notes"],
        "has_lead": bool(r["has_lead"]),
        "follow_up_on": r["follow_up_on"].isoformat() if r["follow_up_on"] else None,
    }


def queue(conn: Any, *, stage: str = "to_contact", limit: int = 50) -> list[dict]:
    if stage not in REFERRER_STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(REFERRER_STAGES)}")
    return [queue_item(r) for r in outreach_repo.queue(conn, stage=stage, limit=limit)]


def follow_ups(conn: Any, *, today: date) -> list[dict]:
    return outreach_repo.follow_ups(conn, today=today)


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
