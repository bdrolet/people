from datetime import UTC, date, datetime

import pytest

from models.outreach import Advance
from services import outreach as svc


@pytest.mark.parametrize(
    "current,target",
    [
        ("candidate", "to_contact"),
        ("candidate", "skipped"),
        ("to_contact", "briefed"),
        ("to_contact", "skipped"),
        ("briefed", "replied"),
        ("briefed", "declined"),
        ("replied", "introduced"),
        ("replied", "declined"),
    ],
)
def test_referrer_transitions_in_the_table_advance(current, target):
    assert svc.transition("referrer", current, target) == "advance"


@pytest.mark.parametrize(
    "current,target",
    [
        ("briefed", "briefed"),
        ("replied", "briefed"),
        ("introduced", "replied"),
        ("introduced", "introduced"),
        ("declined", "briefed"),
        ("skipped", "candidate"),
    ],
)
def test_reaching_a_stage_already_reached_is_a_noop(current, target):
    assert svc.transition("referrer", current, target) == "noop"


@pytest.mark.parametrize(
    "current,target",
    [
        ("candidate", "briefed"),  # deciding is Ben's act; the email only proves it
        ("to_contact", "replied"),
        ("declined", "replied"),
        ("skipped", "briefed"),
        ("skipped", "to_contact"),
        ("declined", "to_contact"),
    ],
)
def test_moves_outside_the_table_are_invalid(current, target):
    assert svc.transition("referrer", current, target) == "invalid"


@pytest.mark.parametrize("current", ["declined", "skipped"])
def test_force_reopens_a_terminal_referrer(current):
    assert svc.transition("referrer", current, "to_contact", force=True) == "advance"


def test_force_does_not_bypass_the_table_for_an_open_row():
    assert svc.transition("referrer", "to_contact", "replied", force=True) == "invalid"


@pytest.mark.parametrize(
    "current,target,expected",
    [
        ("intro", "conversation", "advance"),
        ("intro", "lost", "advance"),
        ("conversation", "proposal", "advance"),
        ("proposal", "signed", "advance"),
        ("proposal", "conversation", "noop"),
        ("intro", "proposal", "invalid"),
        ("signed", "lost", "invalid"),
        ("lost", "conversation", "invalid"),
        ("lost", "intro", "noop"),
    ],
)
def test_buyer_transitions(current, target, expected):
    assert svc.transition("buyer", current, target) == expected


def test_unknown_stage_is_a_programming_error():
    with pytest.raises(ValueError):
        svc.transition("referrer", "briefed", "ghosted")


def test_cohort_week_is_the_monday_of_the_brief():
    assert svc.cohort_week(datetime(2026, 10, 15, 16, 0, tzinfo=UTC)) == date(2026, 10, 12)


def test_cohort_week_uses_new_york_time():
    # Sunday 2026-10-18 23:30 in New York is Monday 03:30 UTC.
    assert svc.cohort_week(datetime(2026, 10, 19, 3, 30, tzinfo=UTC)) == date(2026, 10, 12)


def test_follow_up_is_seven_local_days_later():
    assert svc.follow_up_date(datetime(2026, 10, 14, 2, 0, tzinfo=UTC)) == date(2026, 10, 20)


def test_iso_week_labels():
    assert svc.iso_week(date(2026, 10, 12)) == "2026-W42"
    assert svc.iso_week(None) == "none"


def test_invalid_transition_message_names_the_current_stage_and_the_reopen_hint():
    e = svc.InvalidTransition("referrer", "declined", "briefed")
    assert "declined" in str(e) and "--force" in str(e)
    assert "--force" not in str(svc.InvalidTransition("referrer", "to_contact", "replied"))


TS = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


class FakeRepo:
    """In-memory stand-in for repo/outreach.py: same function names and shapes."""

    def __init__(self):
        self.contacts: dict[int, dict] = {}
        self.opps: dict[int, dict] = {}
        self.events: list[dict] = []

    def insert_contact_if_missing(self, conn, person_id):
        if person_id in self.contacts:
            return None
        self.contacts[person_id] = {
            "person_id": person_id,
            "stage": "candidate",
            "cohort_week": None,
            "briefed_at": None,
            "replied_at": None,
            "channel": None,
            "has_lead": False,
            "follow_up_on": None,
            "notes": None,
        }
        return dict(self.contacts[person_id])

    def get_contact(self, conn, person_id):
        row = self.contacts.get(person_id)
        return dict(row) if row else None

    lock_contact = get_contact

    def update_contact(self, conn, person_id, changes):
        self.contacts[person_id].update(changes)
        return dict(self.contacts[person_id])

    def insert_opportunity(self, conn, *, referrer_id, buyer_id, buyer_label, at):
        oid = len(self.opps) + 1
        self.opps[oid] = {
            "id": oid,
            "referrer_id": referrer_id,
            "buyer_id": buyer_id,
            "buyer_label": buyer_label,
            "stage": "intro",
            "lost_reason": None,
            "intro_at": at,
            "conversation_at": None,
            "proposal_at": None,
            "signed_at": None,
            "lost_at": None,
        }
        return dict(self.opps[oid])

    def lock_opportunity(self, conn, opportunity_id):
        row = self.opps.get(opportunity_id)
        return dict(row) if row else None

    def update_opportunity(self, conn, opportunity_id, changes):
        self.opps[opportunity_id].update(changes)
        return dict(self.opps[opportunity_id])

    def repoint_events(self, conn, from_id, to_id):
        moved = [e for e in self.events if e.get("person_id") == from_id]
        for e in moved:
            e["person_id"] = to_id
        return len(moved)

    def repoint_opportunities(self, conn, from_id, to_id):
        n = 0
        for o in self.opps.values():
            for col in ("referrer_id", "buyer_id"):
                if o[col] == from_id:
                    o[col] = to_id
                    n += 1
        return n

    def move_contact(self, conn, from_id, to_id):
        row = self.contacts.pop(from_id)
        row["person_id"] = to_id
        self.contacts[to_id] = row

    def delete_contact(self, conn, person_id):
        self.contacts.pop(person_id, None)

    def insert_event(self, conn, **kw):
        self.events.append(kw)
        return len(self.events)


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, n, attrs=None):
        self.calls.append((n, attrs))


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(svc, "outreach_repo", fake)
    for name in ("outreach_stage_events", "outreach_lost"):
        monkeypatch.setattr(svc.otel, name, Recorder())
    return fake


def _at(repo, person_id, stage):
    repo.insert_contact_if_missing(None, person_id)
    repo.contacts[person_id]["stage"] = stage


def test_advance_without_a_pipeline_row_returns_none(repo):
    assert svc.advance(None, 1, "briefed", at=TS, source="event", channel="email") is None
    assert repo.events == []


def test_briefed_sets_cohort_first_brief_and_follow_up(repo):
    _at(repo, 1, "to_contact")
    res = svc.advance(
        None, 1, "briefed", at=TS, source="event", channel="email", graph_message_id="g1"
    )
    assert isinstance(res, Advance) and res.changed and res.event_id == 1
    row = repo.contacts[1]
    assert row["stage"] == "briefed"
    assert row["cohort_week"] == date(2026, 10, 12)
    assert row["briefed_at"] == TS and row["channel"] == "email"
    assert row["follow_up_on"] == date(2026, 10, 20)
    assert repo.events[0] == {
        "pipeline": "referrer",
        "person_id": 1,
        "stage": "briefed",
        "at": TS,
        "channel": "email",
        "source": "event",
        "graph_message_id": "g1",
        "from_address": None,
    }
    assert svc.otel.outreach_stage_events.calls == [
        (1, {"pipeline": "referrer", "stage": "briefed", "channel": "email", "source": "event"})
    ]


def test_advance_twice_appends_one_event(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "briefed", at=TS, source="event", channel="email")
    again = svc.advance(None, 1, "briefed", at=TS, source="event", channel="email")
    assert again is not None and not again.changed and again.event_id is None
    assert len(repo.events) == 1


def test_replied_clears_follow_up_and_records_first_reply(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "briefed", at=TS, source="event", channel="email")
    svc.advance(None, 1, "replied", at=TS, source="event", channel="email")
    assert repo.contacts[1]["follow_up_on"] is None
    assert repo.contacts[1]["replied_at"] == TS


def test_a_rebrief_after_reopen_keeps_the_first_cohort(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "briefed", at=TS, source="cli", channel="imessage")
    svc.advance(None, 1, "declined", at=TS, source="cli")
    later = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    svc.advance(None, 1, "to_contact", at=later, source="cli", force=True)
    svc.advance(None, 1, "briefed", at=later, source="cli", channel="email")
    row = repo.contacts[1]
    assert row["cohort_week"] == date(2026, 10, 12) and row["channel"] == "imessage"
    assert row["follow_up_on"] == date(2026, 11, 9)


def test_invalid_move_raises_and_writes_nothing(repo):
    _at(repo, 1, "candidate")
    with pytest.raises(svc.InvalidTransition):
        svc.advance(None, 1, "briefed", at=TS, source="cli", channel="email")
    assert repo.events == [] and repo.contacts[1]["stage"] == "candidate"


@pytest.mark.parametrize("stage", ["candidate", "to_contact", "declined", "skipped"])
def test_advance_from_event_ignores_disallowed_moves(repo, stage):
    _at(repo, 1, stage)
    assert svc.advance_from_event(None, 1, "replied", at=TS, graph_message_id="m") is None
    assert repo.events == []


def test_advance_from_event_returns_the_event_id(repo):
    _at(repo, 1, "briefed")
    assert svc.advance_from_event(None, 1, "replied", at=TS, graph_message_id="m") == 1
    assert repo.events[0]["source"] == "event" and repo.events[0]["channel"] == "email"


def test_ensure_contact_inserts_once_with_a_candidate_event(repo):
    assert svc.ensure_contact(None, 5, at=TS, source="seed") == 1
    assert svc.ensure_contact(None, 5, at=TS, source="seed") is None
    assert [e["stage"] for e in repo.events] == ["candidate"]


def test_open_opportunity_from_replied(repo):
    _at(repo, 1, "replied")
    res = svc.open_opportunity(None, 1, "  Buyer Example, Co  ", at=TS, buyer_id=9)
    assert res.row["buyer_label"] == "Buyer Example, Co" and res.row["buyer_id"] == 9
    assert repo.contacts[1]["stage"] == "introduced"
    assert [(e["pipeline"], e["stage"]) for e in repo.events] == [
        ("referrer", "introduced"),
        ("buyer", "intro"),
    ]
    assert res.event_id == 2


def test_open_opportunity_from_briefed_records_the_implied_reply(repo):
    _at(repo, 1, "briefed")
    svc.open_opportunity(None, 1, "Buyer, Co", at=TS)
    assert [e["stage"] for e in repo.events] == ["replied", "introduced", "intro"]


def test_a_second_intro_adds_an_opportunity_and_keeps_the_stage(repo):
    _at(repo, 1, "introduced")
    svc.open_opportunity(None, 1, "Second, Co", at=TS)
    assert len(repo.opps) == 1 and [e["stage"] for e in repo.events] == ["intro"]


@pytest.mark.parametrize("stage", ["candidate", "to_contact", "declined", "skipped"])
def test_open_opportunity_rejects_a_referrer_who_has_not_replied(repo, stage):
    _at(repo, 1, stage)
    with pytest.raises(svc.InvalidTransition):
        svc.open_opportunity(None, 1, "Buyer, Co", at=TS)
    assert repo.opps == {}


def test_open_opportunity_needs_a_pipeline_row_and_a_label(repo):
    with pytest.raises(svc.NotInPipeline):
        svc.open_opportunity(None, 1, "Buyer, Co", at=TS)
    _at(repo, 1, "replied")
    with pytest.raises(ValueError):
        svc.open_opportunity(None, 1, "   ", at=TS)


def test_advance_opportunity_sets_the_stage_timestamp(repo):
    _at(repo, 1, "replied")
    opp = svc.open_opportunity(None, 1, "Buyer, Co", at=TS).row
    res = svc.advance_opportunity(None, opp["id"], "conversation", at=TS)
    assert res.changed and res.row["conversation_at"] == TS
    assert (
        repo.events[-1]["opportunity_id"] == opp["id"] and repo.events[-1].get("person_id") is None
    )


def test_lost_needs_a_known_reason_and_is_counted(repo):
    _at(repo, 1, "replied")
    oid = svc.open_opportunity(None, 1, "Buyer, Co", at=TS).row["id"]
    with pytest.raises(ValueError):
        svc.advance_opportunity(None, oid, "lost", at=TS)
    with pytest.raises(ValueError):
        svc.advance_opportunity(None, oid, "lost", at=TS, lost_reason="vibes")
    with pytest.raises(ValueError):
        svc.advance_opportunity(None, oid, "conversation", at=TS, lost_reason="price")
    res = svc.advance_opportunity(None, oid, "lost", at=TS, lost_reason="timing")
    assert res.row["lost_reason"] == "timing" and res.row["lost_at"] == TS
    assert svc.otel.outreach_lost.calls == [(1, {"lost_reason": "timing"})]


def test_advance_opportunity_unknown_id(repo):
    with pytest.raises(svc.OpportunityNotFound):
        svc.advance_opportunity(None, 99, "conversation", at=TS)


def test_set_lead_appends_to_notes(repo):
    _at(repo, 1, "replied")
    svc.set_lead(None, 1, "Pat Example, Co", today=date(2026, 10, 14))
    svc.set_lead(None, 1, "Sam Example, Co", today=date(2026, 10, 15))
    row = repo.contacts[1]
    assert row["has_lead"] is True
    assert row["notes"] == "2026-10-14 lead: Pat Example, Co\n2026-10-15 lead: Sam Example, Co"
    with pytest.raises(svc.NotInPipeline):
        svc.set_lead(None, 2, "x", today=date(2026, 10, 14))


def test_merge_moves_a_loser_row_the_keeper_lacks(repo):
    _at(repo, 2, "briefed")
    repo.insert_event(None, pipeline="referrer", person_id=2, stage="briefed")
    assert svc.merge_person(None, 2, 1) == 2
    assert 2 not in repo.contacts and repo.contacts[1]["stage"] == "briefed"
    assert repo.events[0]["person_id"] == 1


def test_merge_keeps_the_further_row(repo):
    _at(repo, 1, "candidate")
    _at(repo, 2, "replied")
    svc.merge_person(None, 2, 1)
    assert repo.contacts[1]["stage"] == "replied" and 2 not in repo.contacts


def test_merge_keeps_the_keeper_on_a_tie_or_when_it_is_further(repo):
    _at(repo, 1, "introduced")
    _at(repo, 2, "briefed")
    svc.merge_person(None, 2, 1)
    assert repo.contacts[1]["stage"] == "introduced" and 2 not in repo.contacts


def test_merge_moves_opportunities_on_both_sides(repo):
    _at(repo, 2, "replied")
    svc.open_opportunity(None, 2, "B, Co", at=TS, buyer_id=2)
    svc.merge_person(None, 2, 1)
    assert repo.opps[1]["referrer_id"] == 1 and repo.opps[1]["buyer_id"] == 1


def test_merge_with_no_pipeline_rows_is_a_noop(repo):
    assert svc.merge_person(None, 2, 1) == 0


def test_outreach_sender_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", " Ben@Consulting.Example.com , other@example.com")
    assert svc.is_outreach_sender("ben@consulting.example.com ")
    assert svc.is_outreach_sender("BEN@consulting.example.com")
    assert not svc.is_outreach_sender("alias@consulting.example.com")
    assert not svc.is_outreach_sender("")
    monkeypatch.delenv("OUTREACH_FROM_ADDRESSES")
    assert not svc.is_outreach_sender("ben@consulting.example.com")


def test_queue_item_matches_the_spec_shape():
    row = {
        "id": 42,
        "email": "alice@example.com",
        "display_name": "Alice Example",
        "company": "Co",
        "job_title": "CTO",
        "notes": "met at a conference",
        "stage": "to_contact",
        "follow_up_on": None,
        "has_lead": False,
        "email_last": datetime(2025, 3, 2, 12, tzinfo=UTC),
        "imessage_last": datetime(2026, 7, 19, 12, tzinfo=UTC),
        "has_imessage": True,
        "whatsapp_last": None,
        "has_whatsapp": False,
        "linkedin_url": "https://www.linkedin.com/in/alice-example",
        "linkedin_last": datetime(2024, 11, 30, 12, tzinfo=UTC),
        "last_interaction": datetime(2026, 7, 19, 12, tzinfo=UTC),
        "labels": ["former-colleague"],
        "outreach_notes": None,
    }
    assert svc.queue_item(row) == {
        "person": {
            "id": 42,
            "email": "alice@example.com",
            "display_name": "Alice Example",
            "company": "Co",
            "job_title": "CTO",
        },
        "stage": "to_contact",
        "last_interaction": "2026-07-19",
        "channels": {
            "email": True,
            "imessage": True,
            "linkedin": "https://www.linkedin.com/in/alice-example",
            "whatsapp": False,
        },
        "last_messages": {
            "email": "2025-03-02",
            "imessage": "2026-07-19",
            "linkedin": "2024-11-30",
        },
        "labels": ["former-colleague"],
        "notes": "met at a conference",
        "outreach_notes": None,
        "has_lead": False,
        "follow_up_on": None,
    }


def test_queue_rejects_an_unknown_stage():
    with pytest.raises(ValueError):
        svc.queue(None, stage="pinged")


def test_record_population_labels_cohorts_by_iso_week(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        svc.outreach_repo,
        "population",
        lambda c: [
            {"pipeline": "referrer", "stage": "briefed", "cohort_week": date(2026, 10, 12), "n": 3},
            {"pipeline": "referrer", "stage": "candidate", "cohort_week": None, "n": 40},
        ],
    )
    monkeypatch.setattr(svc.outreach_repo, "follow_ups_due_count", lambda c, today: 2)
    monkeypatch.setattr(
        svc.otel, "set_outreach_population", lambda rows, due: seen.update(rows=rows, due=due)
    )
    svc.record_population(None, today=date(2026, 10, 20))
    assert seen == {
        "rows": [
            (3, {"pipeline": "referrer", "stage": "briefed", "cohort_week": "2026-W42"}),
            (40, {"pipeline": "referrer", "stage": "candidate", "cohort_week": "none"}),
        ],
        "due": 2,
    }


def test_counts_merges_referrer_and_buyer_rows_by_cohort(monkeypatch):
    w1, w2 = date(2026, 10, 12), date(2026, 10, 19)
    monkeypatch.setattr(
        svc.outreach_repo,
        "referrer_cohort_counts",
        lambda c, since, until: [
            {"cohort_week": w2, "briefed": 1, "replied": 0},
            {"cohort_week": w1, "briefed": 2, "replied": 1},
        ],
    )
    monkeypatch.setattr(
        svc.outreach_repo,
        "buyer_cohort_counts",
        lambda c, since, until: [
            {"cohort_week": w1, "intro": 2, "conversation": 1, "proposal": 0, "signed": 0}
        ],
    )
    monkeypatch.setattr(
        svc.outreach_repo,
        "lost_reasons",
        lambda c, since, until: [{"lost_reason": "timing", "n": 1}],
    )
    monkeypatch.setattr(svc.outreach_repo, "follow_ups_due_count", lambda c, today: 4)
    out = svc.counts(None, today=date(2026, 10, 21))
    assert [c.cohort_week for c in out.cohorts] == [w1, w2]
    assert (out.cohorts[0].briefed, out.cohorts[0].replied, out.cohorts[0].intro) == (2, 1, 2)
    assert out.lost_reasons == {"timing": 1} and out.follow_ups_due == 4
