"""The outreach pipeline retired to pipelines: with OUTREACH_WRITES_DISABLED set, every stage
writer raises OutreachRetired, the event handlers skip their outreach calls, and the CLI refuses
its write commands; readers, the `linkedin` send-queue commands and contact merges keep working."""

import io
from datetime import UTC, date, datetime

import pytest

import clients.db as db
import services.campaign as campaign
import services.ingest as ingest
import services.outreach as outreach
import services.outreach_sync as outreach_sync
from handlers import email_classified, email_sent, sync
from models.types import IngestResult
from scripts import outreach as cli
from scripts import outreach_linkedin as ocli
from scripts import outreach_sessions as sessions_cli

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
TODAY = date(2026, 11, 10)
DANA = {"id": 7, "email": "dana@example.com", "display_name": "Dana Okafor"}
MOVED = "the outreach pipeline has moved to pipelines; use `pipelines`"


@pytest.fixture
def retired(monkeypatch):
    monkeypatch.setenv("OUTREACH_WRITES_DISABLED", "1")


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


# --- the guard ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "disabled"), [(None, False), ("", False), ("0", False), ("1", True), ("yes", True)]
)
def test_writes_disabled_reads_the_environment(monkeypatch, value, disabled):
    if value is None:
        monkeypatch.delenv("OUTREACH_WRITES_DISABLED", raising=False)
    else:
        monkeypatch.setenv("OUTREACH_WRITES_DISABLED", value)
    assert outreach.writes_disabled() is disabled


WRITERS = [
    ("ensure_contact", (None, 7), {"at": NOW, "source": "cli"}),
    ("advance", (None, 7, "briefed"), {"at": NOW, "source": "cli", "channel": "email"}),
    ("advance_from_event", (None, 7, "replied"), {"at": NOW, "graph_message_id": "g1"}),
    ("open_opportunity", (None, 7, "Acme"), {"at": NOW}),
    ("advance_opportunity", (None, 1, "signed"), {"at": NOW}),
    ("qualify", (None, 1, True), {"at": NOW}),
    ("set_lead", (None, 7, "Eli Park, Acme"), {"today": TODAY}),
    ("set_doc", (None, 7, None), {}),
    ("set_rank", (None, 7, 1), {}),
    ("set_ranks", (None, [7]), {}),
    ("clear_ranks", (None,), {}),
    ("nudge", (None, 7), {"at": NOW, "channel": "email", "source": "cli"}),
]


@pytest.mark.parametrize(("name", "args", "kwargs"), WRITERS, ids=[w[0] for w in WRITERS])
def test_every_stage_writer_raises_when_retired(retired, name, args, kwargs):
    with pytest.raises(outreach.OutreachRetired, match="pipelines"):
        getattr(outreach, name)(*args, **kwargs)


def test_a_send_records_no_stage_when_retired(retired):
    """The LinkedIn send queue's `result` keeps working: its stage hook records nothing."""
    assert outreach.record_send(None, 7, at=NOW, channel="linkedin", source="x") is None
    assert outreach.advance_from_sent(None, 7, at=NOW, graph_message_id="g1") is None


def test_readers_and_merges_still_work_when_retired(retired, monkeypatch):
    repo = outreach.outreach_repo
    monkeypatch.setattr(repo, "get_contact", lambda c, pid: {"person_id": pid})
    monkeypatch.setattr(repo, "queue", lambda c, stage, limit: [])
    monkeypatch.setattr(repo, "follow_ups", lambda c, today: [])
    monkeypatch.setattr(repo, "repoint_events", lambda c, a, b: 2)
    monkeypatch.setattr(repo, "repoint_opportunities", lambda c, a, b: 0)
    monkeypatch.setattr(repo, "lock_contact", lambda c, pid: None)
    assert outreach.get_contact(None, 7) == {"person_id": 7}
    assert outreach.queue(None) == []
    assert outreach.follow_ups(None, today=TODAY) == []
    assert outreach.merge_person(None, 8, 7) == 2


# --- the event handlers -------------------------------------------------------------------------


def test_email_sent_skips_outreach_when_retired(retired, monkeypatch):
    conn = Conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "owner@example.com")
    rows = [IngestResult(row={"id": 1, "email": "dana@example.com"}, newly_eligible=False)]
    monkeypatch.setattr(ingest, "record_outbound", lambda c, **kw: rows)
    monkeypatch.setattr(campaign, "confirm_sent_from_event", lambda c, **kw: 0)
    called = []
    monkeypatch.setattr(outreach, "advance_from_sent", lambda *a, **kw: called.append(a))
    monkeypatch.setattr(outreach_sync, "sync_events", lambda *a, **kw: called.append(a))
    email_sent.handle(
        {
            "event": "email_sent",
            "graph_message_id": "g1",
            "sent_at": "2026-11-10T15:00:00Z",
            "from": "owner@example.com",
            "to": ["dana@example.com"],
        }
    )
    assert called == [] and "commit" in conn.log


def test_email_classified_skips_outreach_when_retired(retired, monkeypatch):
    conn = Conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    res = IngestResult(row={"id": 1, "email": "dana@example.com"}, newly_eligible=False)
    monkeypatch.setattr(ingest, "record_inbound", lambda c, **kw: res)
    monkeypatch.setattr(campaign, "record_inbound_from_event", lambda c, **kw: None)
    monkeypatch.setattr(email_classified.hubspot_mirror, "log_email", lambda r, e: None)
    called = []
    monkeypatch.setattr(outreach, "advance_from_event", lambda *a, **kw: called.append(a))
    email_classified.handle(
        {
            "event": "email_classified",
            "message_id": "m1",
            "category": "review",
            "sender": "dana@example.com",
            "received_at": "2026-11-10T15:00:00Z",
        }
    )
    assert called == [] and "commit" in conn.log


def test_nightly_sync_skips_the_asana_outreach_sync_when_retired(retired, monkeypatch):
    conn = Conn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(sync.google_contacts_sync, "run_sync", lambda c: {"ok": 1})
    monkeypatch.setattr(sync.hubspot_mirror, "reconcile", lambda c: {"ok": 1})
    monkeypatch.setattr(sync.outreach, "record_population", lambda c, today: None)
    called = []
    monkeypatch.setattr(outreach_sync, "sync_pending", lambda c: called.append(c))
    result = sync.run()
    assert called == [] and result["asana"] == {"skipped": "outreach retired"}


# --- the command lines --------------------------------------------------------------------------


@pytest.fixture
def run(monkeypatch):
    for var in ("OUTREACH_TARGETS", "OUTREACH_FLOORS", "LINKEDIN_DAILY_CAP"):
        monkeypatch.delenv(var, raising=False)
    conn = Conn()
    monkeypatch.setattr(cli.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(cli.otel, "flush", lambda: None)
    monkeypatch.setattr(cli.outreach, "record_population", lambda c, today: None)
    monkeypatch.setattr(cli.person_lookup, "resolve", lambda c, ident: dict(DANA))
    monkeypatch.setattr(ocli.person_lookup, "resolve", lambda c, ident: dict(DANA))

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(list(argv), lambda: conn, out=out, err=err, now=NOW)
        return code, out.getvalue(), err.getvalue()

    _run.conn = conn
    return _run


WRITE_COMMANDS = [
    ("decide", "7"),
    ("draft", "7"),
    ("mark", "7", "briefed", "--channel", "email"),
    ("nudge", "7", "--channel", "email"),
    ("rank", "7", "1"),
    ("lead", "7", "Eli Park, Acme"),
    ("doc", "7", "--clear"),
    ("intro", "7", "Eli Park, Acme"),
    ("conversation", "1"),
    ("proposal", "1"),
    ("signed", "1"),
    ("lost", "1", "--reason", "timing"),
    ("qualify", "1", "yes"),
    ("sync-asana",),
]


@pytest.mark.parametrize("argv", WRITE_COMMANDS, ids=[a[0] for a in WRITE_COMMANDS])
def test_cli_write_commands_refuse_when_retired(retired, run, monkeypatch, argv):
    for name in ("advance", "nudge", "set_rank", "set_lead", "set_doc", "open_opportunity"):
        monkeypatch.setattr(cli.outreach, name, lambda *a, **kw: pytest.fail("wrote"))
    code, out, err = run(*argv)
    assert code == 1
    assert MOVED in err
    assert run.conn.log == []


def test_cli_write_commands_still_run_without_the_guard(run, monkeypatch):
    monkeypatch.delenv("OUTREACH_WRITES_DISABLED", raising=False)
    monkeypatch.setattr(cli.outreach, "set_lead", lambda c, pid, lead, today: {})
    code, _, err = run("lead", "7", "Eli Park, Acme")
    assert code == 0 and MOVED not in err


def test_cli_read_commands_work_when_retired(retired, run, monkeypatch):
    monkeypatch.setattr(cli.outreach, "queue", lambda c, stage, limit: [])
    monkeypatch.setattr(cli.outreach, "follow_ups", lambda c, today: [])
    monkeypatch.setattr(cli.outreach, "shared_briefs", lambda c: [])
    for argv in (("queue",), ("queue", "--json"), ("followups",)):
        code, _, err = run(*argv)
        assert code == 0, (argv, err)


def test_cli_linkedin_commands_still_dispatch_when_retired(retired, run, monkeypatch):
    monkeypatch.setattr(ocli.linkedin_outbox, "list_rows", lambda c, statuses: [])
    monkeypatch.setattr(ocli.linkedin_outbox, "estimate_send_times", lambda c, now, cfg: {})
    code, out, err = run("linkedin", "list", "--json")
    assert code == 0, err
    assert out.strip() == "[]"


def test_outreach_sessions_refuses_when_retired(retired):
    out, err = io.StringIO(), io.StringIO()
    code = sessions_cli.main_with(
        ["7", "--spawn"], lambda: pytest.fail("connected"), out=out, err=err, today=TODAY
    )
    assert code == 1
    assert MOVED in err.getvalue()
