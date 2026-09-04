from datetime import UTC, datetime

import pytest

import clients.db as db
import services.google_contacts_sync as gsync
import services.hubspot_mirror as mirror
import services.ingest as ingest
from handlers import email_classified, email_sent, sync
from models.types import IngestResult


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


def test_sync_handler_commits_twice_on_success(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(gsync, "run_sync", lambda c: {"updated": 1})
    monkeypatch.setattr(mirror, "reconcile", lambda c: {"adopted": 1})
    result = sync.run()
    assert conn.commits == 2
    assert result == {"google": {"updated": 1}, "hubspot": {"adopted": 1}}
