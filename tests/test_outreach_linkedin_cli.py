"""`outreach linkedin …`: argument handling and output, with the outbox service faked."""

import io
import json
from datetime import UTC, datetime

import pytest

from scripts import outreach as cli
from scripts import outreach_linkedin as ocli
from services import linkedin_outbox as svc
from services.linkedin_result import Outcome

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
ALICE = {
    "id": 7,
    "email": "alice@example.com",
    "display_name": "Alice Example",
    "linkedin_url": "linkedin.com/in/alice-ex",
}


class Conn:
    def __init__(self):
        self.log: list = []

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _row(**kw):
    return {
        "id": 12,
        "person_id": 7,
        "linkedin_username": "alice-ex",
        "profile_urn": None,
        "thread_id": None,
        "message": "Hi Alice",
        "kind": "brief",
        "mode": "queued",
        "status": "queued",
        "reason": None,
        "approved_at": NOW,
        "not_before": None,
        "sent_at": None,
        "thread_mismatch": False,
        **kw,
    }


@pytest.fixture
def run(monkeypatch, tmp_path):
    for var in ("OUTREACH_TARGETS", "OUTREACH_FLOORS", "LINKEDIN_DAILY_CAP"):
        monkeypatch.delenv(var, raising=False)
    conn = Conn()
    monkeypatch.setattr(cli.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(cli.otel, "flush", lambda: None)
    monkeypatch.setattr(cli.outreach, "record_population", lambda c, today: None)
    people = {"alice@example.com": dict(ALICE), "7": dict(ALICE)}

    def resolve(c, ident):
        if ident not in people:
            raise ocli.person_lookup.NotFound(ident)
        return people[ident]

    monkeypatch.setattr(ocli.person_lookup, "resolve", resolve)
    msg = tmp_path / "msg.txt"
    msg.write_text("Hi Alice\n")

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(list(argv), lambda: conn, out=out, err=err, now=NOW)
        return code, out.getvalue(), err.getvalue()

    _run.conn = conn
    _run.msg = str(msg)
    _run.people = people
    return _run


def test_queue_prints_id_and_expected_time(monkeypatch, run):
    seen = {}

    def queue(c, **kw):
        seen.update(kw)
        return _row()

    monkeypatch.setattr(ocli.linkedin_outbox, "queue", queue)
    monkeypatch.setattr(ocli.linkedin_outbox, "estimate_send_times", lambda c, now, cfg: {12: NOW})
    code, out, err = run(
        "linkedin",
        "queue",
        "--person",
        "alice@example.com",
        "--kind",
        "brief",
        "--message-file",
        run.msg,
    )
    assert code == 0, err
    assert seen["person_id"] == 7 and seen["username"] == "alice-ex"
    assert seen["message"] == "Hi Alice\n" and seen["send_now"] is False
    assert "#12" in out and "Tue 10:00" in out and "Hi Alice" not in out
    assert run.conn.log == ["commit"]


def test_queue_person_without_linkedin_url_needs_username(monkeypatch, run):
    run.people["alice@example.com"]["linkedin_url"] = None
    code, _, err = run(
        "linkedin",
        "queue",
        "--person",
        "alice@example.com",
        "--kind",
        "brief",
        "--message-file",
        run.msg,
    )
    assert code == 1 and "--username" in err


def test_queue_needs_person_or_username(run):
    code, _, err = run("linkedin", "queue", "--kind", "other", "--message-file", run.msg)
    assert code == 1 and "--person or --username" in err


def test_queue_username_accepts_a_profile_url(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(
        ocli.linkedin_outbox, "queue", lambda c, **kw: seen.update(kw) or _row(person_id=None)
    )
    monkeypatch.setattr(ocli.linkedin_outbox, "estimate_send_times", lambda c, now, cfg: {})
    code, _, err = run(
        "linkedin",
        "queue",
        "--username",
        "https://www.linkedin.com/in/bob-b/",
        "--kind",
        "other",
        "--message-file",
        run.msg,
    )
    assert code == 0, err
    assert seen["username"] == "bob-b" and seen["person_id"] is None


def test_queue_send_now_prints_exact_send_arguments(monkeypatch, run):
    monkeypatch.setattr(
        ocli.linkedin_outbox,
        "queue",
        lambda c, **kw: _row(mode="send_now", status="sending", message="Hi Alice"),
    )
    code, out, err = run(
        "linkedin",
        "queue",
        "--person",
        "7",
        "--kind",
        "reply",
        "--send-now",
        "--message-file",
        run.msg,
    )
    assert code == 0, err
    args = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert args == {"linkedin_username": "alice-ex", "message": "Hi Alice", "confirm_send": True}
    assert "outreach linkedin result 12 --mcp-result" in out


def test_queue_send_now_at_ceiling_errors_and_exits_1(monkeypatch, run):
    def queue(c, **kw):
        raise svc.CeilingReached(
            {"sent_today": 30, "cap_today": 30, "sent_week": 30, "cap_week": 150}
        )

    monkeypatch.setattr(ocli.linkedin_outbox, "queue", queue)
    code, _, err = run(
        "linkedin",
        "queue",
        "--person",
        "7",
        "--kind",
        "reply",
        "--send-now",
        "--message-file",
        run.msg,
    )
    assert code == 1 and "30/30 today" in err and "rollback" in run.conn.log


def test_queue_invalid_message_errors(monkeypatch, run, tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("tab\there")
    monkeypatch.setattr(
        ocli.linkedin_outbox,
        "queue",
        lambda c, **kw: (_ for _ in ()).throw(ocli.InvalidMessage("control characters")),
    )
    code, _, err = run(
        "linkedin", "queue", "--person", "7", "--kind", "other", "--message-file", str(bad)
    )
    assert code == 1 and "control characters" in err


def test_list_hides_text_unless_show_text(monkeypatch, run):
    monkeypatch.setattr(ocli.linkedin_outbox, "list_rows", lambda c, statuses: [_row()])
    monkeypatch.setattr(ocli.linkedin_outbox, "estimate_send_times", lambda c, now, cfg: {12: NOW})
    code, out, _ = run("linkedin", "list")
    assert code == 0 and "12" in out and "alice-ex" in out and "Hi Alice" not in out
    code, out, _ = run("linkedin", "list", "--show-text")
    assert "Hi Alice" in out
    code, out, _ = run("linkedin", "list", "--json")
    data = json.loads(out)
    assert data[0]["id"] == 12 and "message" not in data[0]


def test_result_mcp_result_json(monkeypatch, run):
    seen = {}

    def apply_result(c, row_id, result, **kw):
        seen.update(row_id=row_id, result=result)
        return Outcome("sent", None, "sent", thread_id="t")

    monkeypatch.setattr(ocli.linkedin_outbox, "apply_result", apply_result)
    code, out, err = run(
        "linkedin", "result", "12", "--mcp-result", '{"status": "sent", "sent": true}'
    )
    assert code == 0, err
    assert seen == {"row_id": 12, "result": {"status": "sent", "sent": True}}
    assert "sent" in out and run.conn.log == ["commit"]


def test_result_needs_exactly_one_source(run):
    code, _, err = run("linkedin", "result", "12")
    assert code == 1 and "--status or --mcp-result" in err


def test_result_status_by_hand(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(
        ocli.linkedin_outbox,
        "resolve",
        lambda c, row_id, status, **kw: seen.update(status=status, **kw) or _row(status=status),
    )
    code, _, err = run("linkedin", "result", "12", "--status", "sent", "--thread-id", "t9")
    assert code == 0, err
    assert seen["status"] == "sent" and seen["thread_id"] == "t9"


def test_record_manual(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(
        ocli.linkedin_outbox,
        "record_manual",
        lambda c, **kw: seen.update(kw) or _row(mode="manual", status="sent", outreach_event_id=5),
    )
    code, out, err = run(
        "linkedin",
        "record",
        "--person",
        "7",
        "--kind",
        "nudge",
        "--message-file",
        run.msg,
        "--at",
        "2026-11-02",
    )
    assert code == 0, err
    assert seen["person_id"] == 7 and seen["kind"] == "nudge"
    assert seen["at"].date().isoformat() == "2026-11-02"
    assert "#12" in out


def test_status_prints_counts_against_cap(monkeypatch, run):
    monkeypatch.setattr(
        ocli.linkedin_outbox,
        "status",
        lambda c, now, cfg: {
            "sent_today": 3,
            "cap_today": 30,
            "sent_week": 9,
            "cap_week": 150,
            "reached": False,
            "queued": 2,
            "oldest_approved_at": NOW,
            "check_linkedin": 1,
            "set_aside": 0,
            "thread_mismatch": 0,
            "paused_reason": "login",
            "paused_until": None,
            "last_sent_at": None,
            "last_error": "login",
            "next_send_at": None,
            "in_window": True,
        },
    )
    code, out, _ = run("linkedin", "status")
    assert code == 0
    assert "today 3/30" in out and "week 9/150" in out and "login" in out
    assert "outreach linkedin resume" in out


@pytest.mark.parametrize(
    "argv,fn",
    [
        (["cancel", "12"], "cancel"),
        (["requeue", "12"], "requeue"),
        (["resume"], "resume"),
        (["pause", "--reason", "travel"], "pause"),
    ],
)
def test_simple_commands_commit(monkeypatch, run, argv, fn):
    monkeypatch.setattr(ocli.linkedin_outbox, fn, lambda c, *a, **kw: _row(status="cancelled"))
    code, _, err = run("linkedin", *argv)
    assert code == 0, err and run.conn.log == ["commit"]


def test_edit_reads_new_text(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(
        ocli.linkedin_outbox,
        "edit",
        lambda c, row_id, message, now: seen.update(row_id=row_id, message=message) or _row(),
    )
    code, _, err = run("linkedin", "edit", "12", "--message-file", run.msg)
    assert code == 0, err and seen == {"row_id": 12, "message": "Hi Alice\n"}


def test_not_found_is_a_user_error(monkeypatch, run):
    def cancel(c, row_id):
        raise svc.OutboxNotFound(row_id)

    monkeypatch.setattr(ocli.linkedin_outbox, "cancel", cancel)
    code, _, err = run("linkedin", "cancel", "99")
    assert code == 1 and "99" in err
