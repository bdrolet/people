import pytest

from services.linkedin_result import Outcome, map_result


def sent(thread_id="t-1", **kw):
    return {"status": "sent", "sent": True, "retry_safe": False, "thread_id": thread_id, **kw}


def refused(status, retry_safe=True):
    return {"status": status, "sent": False, "retry_safe": retry_safe}


def err(code, retry_after=None):
    return {"error": {"code": code, "retry_after": retry_after}}


def test_sent_with_matching_thread():
    o = map_result(sent("t-1"), row_thread_id="t-1")
    assert o == Outcome("sent", None, "sent", thread_id="t-1", thread_mismatch=False)


def test_sent_with_other_thread_flags_mismatch():
    o = map_result(sent("t-2"), row_thread_id="t-1")
    assert o.row_status == "sent" and o.thread_mismatch and o.thread_id == "t-2"


def test_sent_without_row_thread_is_not_mismatch():
    assert not map_result(sent("t-2"), row_thread_id=None).thread_mismatch
    # A sent result whose page stayed on the compose route has thread_id null.
    assert not map_result(sent(None), row_thread_id="t-1").thread_mismatch


def test_send_unconfirmed_is_check_linkedin():
    o = map_result(refused("send_unconfirmed", retry_safe=False), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause, o.metric) == (
        "check_linkedin",
        "send_unconfirmed",
        None,
        "check_linkedin",
    )


def test_missing_sent_is_check_linkedin():
    o = map_result({"status": "outcome_unknown"}, row_thread_id=None)
    assert o.row_status == "check_linkedin" and o.reason == "outcome_unknown"


def test_any_retry_unsafe_unsent_is_check_linkedin():
    o = map_result(refused("message_unavailable", retry_safe=False), row_thread_id=None)
    assert o.row_status == "check_linkedin"


@pytest.mark.parametrize(
    "status",
    ["message_unavailable", "composer_occupied", "recipient_resolution_failed", "invalid_message"],
)
def test_one_message_problems_are_set_aside(status):
    o = map_result(refused(status), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause, o.metric) == ("set_aside", status, None, "set_aside")


@pytest.mark.parametrize(
    "status",
    [
        "enter_to_send_enabled",
        "confirmation_required",
        "composer_unavailable",
        "compose_interact_failed",
        "send_unavailable",
        "brand_new",
    ],
)
def test_other_statuses_pause_and_keep_the_row(status):
    o = map_result(refused(status), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause, o.metric) == ("queued", status, "hold", "paused")


def test_browser_busy_requeues_without_pause():
    o = map_result(err("browser_busy"), row_thread_id=None)
    assert (o.row_status, o.pause, o.metric) == ("queued", None, "browser_busy")


def test_auth_required_requeues_and_holds_for_login():
    o = map_result(err("auth_required"), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause) == ("queued", "login", "hold")


def test_browser_unavailable_requeues_and_holds():
    o = map_result(err("browser_unavailable"), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause) == ("queued", "browser_unavailable", "hold")


@pytest.mark.parametrize("code", ["invalid_argument", "not_found"])
def test_recipient_errors_are_set_aside(code):
    o = map_result(err(code), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause) == ("set_aside", code, None)


def test_rate_limited_error_pauses_until():
    o = map_result(err("rate_limited", 120), row_thread_id=None)
    assert (o.row_status, o.pause, o.pause_seconds, o.metric) == (
        "check_linkedin",
        "until",
        420,
        "rate_limited",
    )
    assert map_result(err("rate_limited"), row_thread_id=None).pause_seconds == 3900


def test_outcome_unknown_error_is_check_without_pause():
    o = map_result(err("outcome_unknown"), row_thread_id=None)
    assert (o.row_status, o.pause) == ("check_linkedin", None)


@pytest.mark.parametrize(
    "code", ["account_restricted", "network", "page_unreadable", "internal", "something_new"]
)
def test_other_errors_check_and_hold(code):
    o = map_result(err(code), row_thread_id=None)
    assert (o.row_status, o.reason, o.pause, o.metric) == ("check_linkedin", code, "hold", "paused")
