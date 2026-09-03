import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

import clients.graph_local as glocal
import services.google_contacts_sync as gsync
import services.hubspot_mirror as mirror
import services.ingest as ingest
from models.types import IngestResult

spec = importlib.util.spec_from_file_location("import_contacts", Path("scripts/import_contacts.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeConn:
    """Minimal conn stand-in: run() only ever calls .commit() on it directly
    (record_inbound/record_outbound/ensure_contact are monkeypatched below and
    ignore conn)."""

    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def wired(monkeypatch):
    log = []
    monkeypatch.setattr(
        ingest,
        "record_inbound",
        lambda conn, **kw: (
            log.append(("in", kw["sender"])),
            IngestResult({"email": kw["sender"], "eligible": True}, True),
        )[1],
    )
    monkeypatch.setattr(
        ingest,
        "record_outbound",
        lambda conn, **kw: [
            IngestResult({"email": r, "eligible": True}, True)
            for r in kw["recipients"]
            if not log.append(("out", r))
        ],
    )
    monkeypatch.setattr(
        gsync, "ensure_contact", lambda c, r: (log.append(("google", r["email"])), r)[1]
    )
    monkeypatch.setattr(
        mirror, "ensure_contact", lambda c, r: (log.append(("hubspot", r["email"])), r)[1]
    )
    return log


def msg(folder, frm, to, ts="2026-09-03T12:00:00Z"):
    return {
        "folder": folder,
        "from": frm,
        "to": to,
        "cc": [],
        "from_name": None,
        "received_at": ts,
        "sent_at": ts,
        "category": "review",
    }


def test_run_routes_inbox_and_sent(wired):
    counts = mod.run(
        FakeConn(),
        [msg("inbox", "alice@x.com", ["me@x.com"]), msg("sentitems", "me@x.com", ["bob@x.com"])],
        own={"me@x.com"},
        dry_run=False,
    )
    assert ("in", "alice@x.com") in wired and ("out", "bob@x.com") in wired
    assert counts["inbound"] == 1 and counts["outbound"] == 1
    assert ("google", "alice@x.com") in wired and ("google", "bob@x.com") in wired


def test_dry_run_skips_side_effects(wired):
    mod.run(None, [msg("inbox", "alice@x.com", ["me@x.com"])], own={"me@x.com"}, dry_run=True)
    assert not any(k in ("google", "hubspot") for k, _ in wired)


def test_run_commits_per_message_when_not_dry_run(wired):
    conn = FakeConn()
    mod.run(
        conn,
        [msg("inbox", "alice@x.com", ["me@x.com"]), msg("sentitems", "me@x.com", ["bob@x.com"])],
        own={"me@x.com"},
        dry_run=False,
    )
    assert conn.commits == 2


def test_run_does_not_commit_in_dry_run(wired):
    conn = FakeConn()
    mod.run(
        conn,
        [msg("inbox", "alice@x.com", ["me@x.com"]), msg("sentitems", "me@x.com", ["bob@x.com"])],
        own={"me@x.com"},
        dry_run=True,
    )
    assert conn.commits == 0


def test_run_skips_messages_without_timestamp(wired):
    conn = FakeConn()
    counts = mod.run(
        conn,
        [
            msg("inbox", "alice@x.com", ["me@x.com"], ts=None),
            msg("sentitems", "me@x.com", ["bob@x.com"], ts=None),
        ],
        own={"me@x.com"},
        dry_run=False,
    )
    assert counts["skipped"] == 2
    assert counts["inbound"] == 0
    assert counts["outbound"] == 0
    assert not wired


class FakeResp:
    def __init__(self, status_code, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def test_iter_messages_retries_on_429(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return FakeResp(429, headers={"Retry-After": "0"})
        return FakeResp(200, payload={"value": []})

    monkeypatch.setattr(glocal.requests, "get", fake_get)
    monkeypatch.setattr(glocal.time, "sleep", lambda s: None)

    g = glocal.GraphLocal()
    g._token = "t"
    result = list(g.iter_messages("inbox", datetime(2026, 1, 1, tzinfo=UTC)))
    assert result == []
    assert len(calls) == 2


def test_retry_after_http_date_falls_back(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            # Retry-After as an HTTP-date (RFC 7231), not a delay in seconds.
            return FakeResp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return FakeResp(200, payload={"value": []})

    monkeypatch.setattr(glocal.requests, "get", fake_get)
    monkeypatch.setattr(glocal.time, "sleep", lambda s: sleeps.append(s))

    g = glocal.GraphLocal()
    g._token = "t"
    result = list(g.iter_messages("inbox", datetime(2026, 1, 1, tzinfo=UTC)))
    assert result == []
    assert len(calls) == 2
    # falls back to exponential backoff (2**0 == 1) instead of raising
    assert sleeps == [1]
