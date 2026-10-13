import io
from datetime import UTC, date, datetime

import httpx
import pytest

from models.outreach import Advance, CohortCounts, FunnelCounts
from scripts import outreach as cli
from services import outreach as svc
from services.outreach_sync import SyncResult

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
ALICE = {"id": 7, "email": "alice@example.com", "display_name": "Alice Example"}


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


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, n, attrs=None):
        self.calls.append(attrs)


@pytest.fixture
def run(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(cli.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(cli.otel, "flush", lambda: None)
    monkeypatch.setattr(cli.outreach, "record_population", lambda c, today: None)
    monkeypatch.setattr(cli.person_lookup, "resolve", lambda c, ident: dict(ALICE))

    def sync_events(c, ids):
        conn.log.append(("sync", list(ids)))
        return SyncResult(synced=len(ids))

    monkeypatch.setattr(cli.outreach_sync, "sync_events", sync_events)

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(list(argv), lambda: conn, out=out, err=err, now=NOW)
        return code, out.getvalue(), err.getvalue()

    _run.conn = conn
    return _run


def _advance_returns(monkeypatch, result, seen=None):
    def advance(c, pid, stage, **kw):
        if seen is not None:
            seen.update(pid=pid, stage=stage, **kw)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli.outreach, "advance", advance)


def test_mark_briefed_needs_a_channel(run):
    code, _, err = run("mark", "alice@example.com", "briefed")
    assert code == 1 and "--channel is required" in err


def test_from_needs_the_email_channel(run):
    code, _, err = run(
        "mark", "alice@example.com", "briefed", "--channel", "imessage", "--from", "a@example.com"
    )
    assert code == 1 and "--from" in err


def test_mark_briefed_commits_then_syncs(monkeypatch, run):
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "briefed"}, True, 11), seen)
    code, out, _ = run("mark", "alice@example.com", "briefed", "--channel", "imessage")
    assert code == 0 and "briefed" in out and "asana: task created" in out
    assert run.conn.log[:2] == ["commit", ("sync", [11])]
    assert seen == {
        "pid": 7,
        "stage": "briefed",
        "at": NOW,
        "channel": "imessage",
        "source": "cli",
        "from_address": None,
        "force": False,
    }


def test_mark_replied_creates_no_task(monkeypatch, run):
    _advance_returns(monkeypatch, Advance({"stage": "replied"}, True, 12))
    run("mark", "alice@example.com", "replied", "--channel", "whatsapp")
    assert not any(isinstance(e, tuple) for e in run.conn.log)


def test_an_invalid_move_names_the_current_stage(monkeypatch, run):
    _advance_returns(monkeypatch, svc.InvalidTransition("referrer", "declined", "briefed"))
    code, _, err = run("mark", "alice@example.com", "briefed", "--channel", "email")
    assert code == 1 and "declined" in err and "--force" in err
    assert "rollback" in run.conn.log


def test_mark_someone_not_in_the_pipeline(monkeypatch, run):
    _advance_returns(monkeypatch, None)
    code, _, err = run("mark", "alice@example.com", "replied", "--channel", "email")
    assert code == 1 and "outreach decide" in err


def test_mark_passes_at_from_and_force(monkeypatch, run):
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "briefed"}, True, None), seen)
    run(
        "mark",
        "7",
        "briefed",
        "--channel",
        "email",
        "--from",
        "alias@example.com",
        "--at",
        "2026-10-10",
        "--force",
    )
    assert seen["from_address"] == "alias@example.com" and seen["force"] is True
    assert seen["at"] == datetime(2026, 10, 10, 12, 0, tzinfo=svc.TZ)


def test_decide_records_the_stage_before_the_label_and_survives_a_label_failure(monkeypatch, run):
    monkeypatch.setattr(
        cli.outreach, "ensure_contact", lambda c, pid, **kw: run.conn.log.append("ensure")
    )
    _advance_returns(monkeypatch, Advance({"stage": "to_contact"}, True, 3))

    def update(c, pid, *, labels):
        run.conn.log.append(("label", labels))
        raise RuntimeError("google down")

    monkeypatch.setattr(cli.person_edit, "update", update)
    code, out, _ = run("decide", "alice@example.com")
    assert code == 0 and "to_contact" in out and "warning" in out
    assert run.conn.log[:3] == ["ensure", "commit", ("label", {"add": ["consulting-outreach"]})]


def test_decide_skip_adds_no_label(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "ensure_contact", lambda c, pid, **kw: None)
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "skipped"}, True, 4), seen)
    monkeypatch.setattr(cli.person_edit, "update", lambda *a, **k: pytest.fail("labelled a skip"))
    assert run("decide", "alice@example.com", "--skip")[0] == 0 and seen["stage"] == "skipped"


def test_intro_opens_an_opportunity_and_syncs(monkeypatch, run):
    seen = {}

    def open_opportunity(c, referrer_id, label, **kw):
        seen.update(referrer_id=referrer_id, label=label, **kw)
        return Advance({"id": 4, "buyer_label": label}, True, 20)

    monkeypatch.setattr(cli.outreach, "open_opportunity", open_opportunity)
    code, out, _ = run("intro", "alice@example.com", "Bo Buyer, Co", "--buyer", "bo@example.com")
    assert code == 0 and "opportunity 4" in out
    assert seen["label"] == "Bo Buyer, Co" and seen["buyer_id"] == 7 and seen["at"] == NOW
    assert ("sync", [20]) in run.conn.log


def test_lost_needs_a_known_reason(run):
    with pytest.raises(SystemExit):
        run("lost", "4", "--reason", "vibes")


def test_buyer_advance(monkeypatch, run):
    seen = {}

    def advance_opportunity(c, oid, stage, **kw):
        seen.update(oid=oid, stage=stage, **kw)
        return Advance({"id": oid, "stage": stage, "buyer_label": "Bo, Co"}, True, 30)

    monkeypatch.setattr(cli.outreach, "advance_opportunity", advance_opportunity)
    assert run("lost", "4", "--reason", "timing")[0] == 0
    assert seen == {"oid": 4, "stage": "lost", "at": NOW, "lost_reason": "timing", "source": "cli"}
    assert not any(isinstance(e, tuple) for e in run.conn.log)  # lost makes no task
    run("signed", "4")
    assert ("sync", [30]) in run.conn.log


def test_report_without_floors(monkeypatch, run):
    monkeypatch.setattr(
        cli.outreach,
        "counts",
        lambda c, today, since=None, until=None: FunnelCounts(
            [CohortCounts(date(2026, 10, 12), briefed=3, replied=1)]
        ),
    )
    monkeypatch.setattr(cli.otel, "outreach_report_runs", Recorder())
    code, out, _ = run("report")
    assert code == 0 and out.startswith("Cohort") and "Floor@n" not in out
    assert cli.otel.outreach_report_runs.calls == [{"verdict_below": "0"}]


def test_report_with_a_bad_floors_path(run):
    code, _, err = run("report", "--floors", "/nonexistent/floors.toml")
    assert code == 1 and "not found" in err


def test_draft_refuses_a_person_without_email(monkeypatch, run):
    monkeypatch.setattr(
        cli.person_lookup, "resolve", lambda c, ident: {"id": 8, "email": None, "display_name": "P"}
    )
    code, _, err = run("draft", "8")
    assert code == 1 and "no email" in err


def test_draft_goes_from_the_consulting_address(monkeypatch, run):
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com,other@example.com")
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})
    seen = {}
    monkeypatch.setattr(
        cli.inbox_api,
        "create_draft",
        lambda to, subject, body="", from_address=None: (
            seen.update(to=to, subject=subject, body=body, from_address=from_address)
            or {"id": "d", "web_link": "https://o/d"}
        ),
    )
    code, out, _ = run("draft", "alice@example.com")
    assert code == 0 and "https://o/d" in out and "ben@consulting.example.com" in out
    assert seen == {
        "to": ["alice@example.com"],
        "subject": "",
        "body": "",
        "from_address": "ben@consulting.example.com",
    }


def test_draft_without_outreach_addresses_is_a_user_error(monkeypatch, run):
    monkeypatch.delenv("OUTREACH_FROM_ADDRESSES", raising=False)
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})
    code, _, err = run("draft", "alice@example.com")
    assert code == 1 and "OUTREACH_FROM_ADDRESSES" in err


def test_parse_at():
    assert cli.parse_at(None, NOW) == NOW
    assert cli.parse_at("2026-10-10", NOW) == datetime(2026, 10, 10, 12, 0, tzinfo=svc.TZ)
    assert cli.parse_at("2026-10-10T09:30:00Z", NOW) == datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "exc", [cli.inbox_api.InboxApiUnavailable("inbox-api down"), httpx.ConnectError("down")]
)
def test_draft_inbox_failures_are_user_errors_not_tracebacks(monkeypatch, run, exc):
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli.inbox_api, "create_draft", boom)
    code, _, err = run("draft", "alice@example.com")
    assert code == 1 and err.startswith("error:")


def test_decide_not_linked_warning_says_so(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "ensure_contact", lambda c, pid, **kw: None)
    _advance_returns(monkeypatch, Advance({"stage": "to_contact"}, True, 3))

    def update(c, pid, *, labels):
        raise cli.person_edit.NotLinked(str(pid))

    monkeypatch.setattr(cli.person_edit, "update", update)
    code, out, _ = run("decide", "alice@example.com")
    assert code == 0 and "no Google contact is linked yet" in out
