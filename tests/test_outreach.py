from datetime import UTC, date, datetime

import pytest

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
