from datetime import UTC, datetime

import pytest

import clients.db as db
import services.google_contacts_sync as gsync
import services.hubspot_mirror as mirror
import services.ingest as ingest
import services.outreach as outreach
import services.outreach_sync as outreach_sync
from handlers import email_classified, email_sent, sync
from models.types import IngestResult
from services.outreach_sync import SyncResult


class FakeConn:
    def __init__(self, log=None):
        self.commits = 0
        self.log = log if log is not None else []

    def commit(self):
        self.commits += 1
        self.log.append("commit")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def calls(monkeypatch):
    log = []
    conn = FakeConn(log)
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(
        gsync, "ensure_contact", lambda c, r: (log.append(("google", r["email"])), r)[1]
    )
    monkeypatch.setattr(
        mirror, "ensure_contact", lambda c, r: (log.append(("hubspot", r["email"])), r)[1]
    )
    monkeypatch.setattr(mirror, "log_email", lambda r, e: log.append(("log", r["email"])))
    monkeypatch.setattr(outreach, "advance_from_event", lambda c, pid, stage, **kw: None)
    monkeypatch.setattr(outreach, "advance_from_sent", lambda c, pid, **kw: None)
    monkeypatch.setattr(
        outreach_sync, "sync_events", lambda c, ids, **kw: log.append(("sync", list(ids)))
    )
    return log, conn


def _event(category="review"):
    return {
        "event": "email_classified",
        "message_id": "m1",
        "category": category,
        "importance": "P2",
        "confidence": 0.9,
        "subject": "Hi",
        "sender": "alice@x.com",
        "sender_display": "Alice",
        "to": ["me@x.com"],
        "cc": [],
        "received_at": "2026-09-03T12:00:00Z",
        "tags": [],
        "reasoning": "",
        "body": "b",
        "body_html": None,
        "web_link": None,
    }


def test_classified_newly_eligible_runs_google_and_hubspot(monkeypatch, calls):
    log, conn = calls
    row = {"email": "alice@x.com", "eligible": True, "hubspot_contact_id": None}
    monkeypatch.setattr(
        ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=True)
    )
    email_classified.handle(_event())
    assert conn.commits == 2
    assert log[0] == "commit"  # durable commit happened before any google/hubspot call
    assert ("google", "alice@x.com") in log and ("hubspot", "alice@x.com") in log
    assert ("log", "alice@x.com") in log  # log_email is called; mirror decides based on hubspot id


def test_classified_not_eligible_skips_external(monkeypatch, calls):
    log, _ = calls
    row = {"email": "bot@x.com", "eligible": False, "hubspot_contact_id": None}
    monkeypatch.setattr(
        ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=False)
    )
    email_classified.handle(_event("ignore"))
    assert ("google", "bot@x.com") not in log and ("hubspot", "bot@x.com") not in log


def test_classified_already_eligible_still_ensures_hubspot_and_logs(monkeypatch, calls):
    log, _ = calls
    row = {
        "email": "alice@x.com",
        "eligible": True,
        "google_resource_name": "people/c1",
        "hubspot_contact_id": "hs1",
    }
    monkeypatch.setattr(
        ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=False)
    )
    email_classified.handle(_event())
    assert ("hubspot", "alice@x.com") in log and ("log", "alice@x.com") in log


def test_classified_passes_parsed_fields(monkeypatch, calls):
    seen = {}

    def rec(conn, **kw):
        seen.update(kw)
        return IngestResult(row={"email": "alice@x.com", "eligible": False}, newly_eligible=False)

    monkeypatch.setattr(ingest, "record_inbound", rec)
    email_classified.handle(_event())
    assert seen["sender"] == "alice@x.com" and seen["category"] == "review"
    assert seen["received_at"] == datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_sent_records_to_and_cc(monkeypatch, calls):
    log, conn = calls
    seen = {}

    def rec(conn, **kw):
        seen.update(kw)
        return [IngestResult(row={"email": "bob@x.com", "eligible": True}, newly_eligible=True)]

    monkeypatch.setattr(ingest, "record_outbound", rec)
    email_sent.handle(
        {
            "event": "email_sent",
            "graph_message_id": "g1",
            "conversation_id": None,
            "sent_at": "2026-09-03T12:00:00Z",
            "from": "me@x.com",
            "to": ["bob@x.com"],
            "cc": ["carol@x.com"],
            "subject": "Re",
        }
    )
    assert seen["recipients"] == ["bob@x.com", "carol@x.com"]
    assert conn.commits == 2
    assert log[0] == "commit"
    assert ("google", "bob@x.com") in log and ("hubspot", "bob@x.com") in log


def _sent(frm="Ben@Consulting.Example.com "):
    return {
        "event": "email_sent",
        "graph_message_id": "g1",
        "conversation_id": None,
        "sent_at": "2026-10-13T15:00:00Z",
        "from": frm,
        "to": ["bob@x.com", "me@x.com"],
        "cc": ["carol@x.com"],
        "subject": "Hello",
    }


def _outbound_rows(monkeypatch):
    # record_outbound already drops Ben's own addresses (OWN_ADDRESSES); To and Cc both come back.
    rows = [
        IngestResult(row={"id": 1, "email": "bob@x.com", "eligible": True}, newly_eligible=False),
        IngestResult(row={"id": 2, "email": "carol@x.com", "eligible": True}, newly_eligible=False),
    ]
    monkeypatch.setattr(ingest, "record_outbound", lambda conn, **kw: rows)


def test_sent_records_outreach_only_for_to_recipients(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)
    seen = []

    def adv(c, pid, *, at, graph_message_id):
        seen.append((pid, at, graph_message_id))
        log.append("advance")
        return ("briefed", 11)

    monkeypatch.setattr(outreach, "advance_from_sent", adv)
    email_sent.handle(_sent())
    assert seen == [(1, datetime(2026, 10, 13, 15, 0, tzinfo=UTC), "g1")]  # carol is Cc
    # Durable first: the write is inside the first commit; the Asana sync comes after it.
    assert log.index("advance") < log.index("commit") < log.index(("sync", [11]))


def test_one_email_briefs_one_recipient_and_nudges_another(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    rows = [
        IngestResult(row={"id": 1, "email": "bob@x.com", "eligible": False}, newly_eligible=False),
        IngestResult(row={"id": 3, "email": "dan@x.com", "eligible": False}, newly_eligible=False),
    ]
    monkeypatch.setattr(ingest, "record_outbound", lambda conn, **kw: rows)
    outcome = {1: ("briefed", 11), 3: ("nudge", 12)}
    monkeypatch.setattr(outreach, "advance_from_sent", lambda c, pid, **kw: outcome[pid])
    email_sent.handle(_sent() | {"to": ["bob@x.com", "dan@x.com"]})
    # Only briefs produce Asana tasks; the nudge is recorded but never synced.
    assert [e for e in log if isinstance(e, tuple) and e[0] == "sync"] == [("sync", [11])]


def test_sent_from_another_identity_records_no_outreach(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)
    seen = []
    monkeypatch.setattr(outreach, "advance_from_sent", lambda c, pid, **kw: seen.append(pid))
    email_sent.handle(_sent(frm="ben@alias.example.com"))
    assert seen == [] and not any(isinstance(e, tuple) and e[0] == "sync" for e in log)


def test_sent_no_event_means_no_sync(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)  # advance_from_event returns None: a replay, or no pipeline row
    email_sent.handle(_sent())
    assert not any(isinstance(e, tuple) and e[0] == "sync" for e in log)


@pytest.mark.parametrize(
    "category,automated,expected",
    [("review", False, [(5, "replied")]), ("ignore", False, []), ("respond", True, [])],
)
def test_classified_advances_a_reply(monkeypatch, calls, category, automated, expected):
    log, _ = calls
    row = {"id": 5, "email": "alice@x.com", "eligible": True, "automated": automated}
    monkeypatch.setattr(
        ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=False)
    )
    advanced = []

    def adv(c, pid, stage, *, at, graph_message_id):
        advanced.append((pid, stage))
        log.append("advance")

    monkeypatch.setattr(outreach, "advance_from_event", adv)
    email_classified.handle(_event(category))
    assert advanced == expected
    if expected:
        assert log.index("advance") < log.index("commit")


def test_sync_handler_runs_google_hubspot_then_the_asana_backstop(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(gsync, "run_sync", lambda c: {"updated": 1})
    monkeypatch.setattr(mirror, "reconcile", lambda c: {"adopted": 1})
    monkeypatch.setattr(outreach_sync, "sync_pending", lambda c: SyncResult(synced=2))
    monkeypatch.setattr(outreach, "record_population", lambda c, today: None)
    result = sync.run()
    assert conn.commits == 2
    assert result == {
        "google": {"updated": 1},
        "hubspot": {"adopted": 1},
        "asana": {"synced": 2, "failed": 0, "skipped": False, "late": {}},
    }


def test_sync_handler_records_the_population_and_survives_its_failure(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(gsync, "run_sync", lambda c: {})
    monkeypatch.setattr(mirror, "reconcile", lambda c: {})
    monkeypatch.setattr(outreach_sync, "sync_pending", lambda c: SyncResult())

    def boom(c, today):
        raise RuntimeError("gauge query failed")

    monkeypatch.setattr(outreach, "record_population", boom)
    assert sync.run()["asana"]["synced"] == 0  # a gauge failure never fails the nightly sync
