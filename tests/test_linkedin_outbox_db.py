"""linkedin_outbox SQL against a real Postgres. Skipped unless TEST_DATABASE_URL is set
(a throwaway container: docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=x postgres:16)."""

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from repo import linkedin_outbox as repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = Path(__file__).resolve().parent.parent / "repo" / "schema.sql"
TS = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True, row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        yield c


def _person(conn, email="alice@example.com") -> int:
    return conn.execute(
        "INSERT INTO people (email, first_seen) VALUES (%s, %s) RETURNING id", (email, TS)
    ).fetchone()["id"]


def _row(conn, **kw) -> dict:
    cols = {
        "linkedin_username": "alice",
        "message": "Hi",
        "kind": "brief",
        "mode": "queued",
        "status": "queued",
        "approved_at": TS,
        **kw,
    }
    return repo.insert(conn, **cols)


def test_schema_is_still_idempotent(conn):
    conn.execute(SCHEMA.read_text())


def test_outbox_rejects_unknown_status(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _row(conn, status="maybe")


def test_outbox_rejects_unknown_kind_and_mode(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _row(conn, kind="spam")
    with pytest.raises(psycopg.errors.CheckViolation):
        _row(conn, mode="robot")


def test_events_accept_linkedin_outbox_source(conn):
    pid = _person(conn)
    conn.execute(
        "INSERT INTO outreach_events (pipeline, person_id, stage, at, channel, source) "
        "VALUES ('referrer', %s, 'briefed', %s, 'linkedin', 'linkedin_outbox')",
        (pid, TS),
    )


def test_insert_get_update(conn):
    r = _row(conn, thread_id="t-1")
    assert r["id"] and r["status"] == "queued" and r["thread_mismatch"] is False
    assert repo.get(conn, r["id"])["thread_id"] == "t-1"
    u = repo.update(conn, r["id"], {"status": "sent", "sent_at": TS})
    assert u["status"] == "sent" and u["updated_at"] >= r["updated_at"]
    assert repo.get(conn, 999) is None


def test_claim_next_skips_future_not_before_and_takes_oldest(conn):
    _row(conn, approved_at=TS - timedelta(hours=3), not_before=TS + timedelta(hours=1))
    older = _row(conn, approved_at=TS - timedelta(hours=2))
    _row(conn, approved_at=TS - timedelta(hours=1))
    _row(conn, approved_at=TS - timedelta(hours=5), status="set_aside")
    assert repo.claim_next(conn, TS)["id"] == older["id"]


def test_claim_next_empty(conn):
    assert repo.claim_next(conn, TS) is None


def test_counted_between_includes_sending_and_check(conn):
    _row(conn, status="sent", sent_at=TS)
    _row(conn, status="sending", attempted_at=TS)
    _row(conn, status="check_linkedin", attempted_at=TS)
    _row(conn, status="set_aside", attempted_at=TS)
    _row(conn, status="sent", sent_at=TS - timedelta(days=2))
    assert repo.count_counted_between(conn, TS - timedelta(hours=1), TS + timedelta(hours=1)) == 3


def test_stale_sending(conn):
    old = _row(conn, status="sending", attempted_at=TS - timedelta(minutes=16))
    _row(conn, status="sending", attempted_at=TS - timedelta(minutes=10))
    got = repo.stale_sending(conn, TS - timedelta(minutes=15))
    assert [r["id"] for r in got] == [old["id"]]


def test_list_rows_and_stats(conn):
    _row(conn, approved_at=TS - timedelta(hours=2))
    _row(conn, approved_at=TS - timedelta(hours=1))
    _row(conn, status="check_linkedin")
    _row(conn, status="sent", sent_at=TS, thread_mismatch=True)
    assert len(repo.list_rows(conn, ("queued",))) == 2
    assert len(repo.list_rows(conn, None)) == 4
    stats = repo.queue_stats(conn, mismatch_since=TS - timedelta(days=1))
    assert stats["queued"] == 2
    assert stats["oldest_approved_at"] == TS - timedelta(hours=2)
    assert stats["check_linkedin"] == 1 and stats["set_aside"] == 0
    assert stats["thread_mismatch"] == 1


def test_state_singleton(conn):
    s = repo.get_state(conn)
    assert s["id"] == 1 and s["paused_reason"] is None and s["consecutive_set_asides"] == 0
    repo.update_state(conn, {"paused_reason": "login"})
    assert repo.lock_state(conn)["paused_reason"] == "login"
    assert repo.get_state(conn)["paused_reason"] == "login"


def test_person_delete_sets_null(conn):
    pid = _person(conn)
    r = _row(conn, person_id=pid)
    conn.execute("DELETE FROM people WHERE id = %s", (pid,))
    assert repo.get(conn, r["id"])["person_id"] is None


def test_repoint_person(conn):
    a, b = _person(conn), _person(conn, "bob@example.com")
    _row(conn, person_id=a)
    _row(conn, person_id=a)
    assert repo.repoint_person(conn, a, b) == 2
    assert {r["person_id"] for r in repo.list_rows(conn, None)} == {b}


def test_status_counts(conn):
    _row(conn)
    _row(conn)
    _row(conn, status="check_linkedin")
    assert repo.status_counts(conn) == {"queued": 2, "check_linkedin": 1}


def test_daily_counts_use_pacific_days(conn):
    evening = datetime.fromisoformat("2026-11-04T17:30:00-08:00")  # 01:30 UTC on the 5th
    _row(conn, approved_at=evening, kind="nudge")
    _row(conn, approved_at=evening, status="sent", sent_at=evening)
    _row(conn, approved_at=evening, status="set_aside", attempted_at=evening)
    _row(conn, approved_at=evening - timedelta(days=30))  # before `since`
    rows = repo.daily_counts(
        conn, since=datetime.fromisoformat("2026-10-23T00:00:00-07:00"), tz="America/Los_Angeles"
    )
    got = sorted((r["what"], r["day"], r["kind"], r["n"]) for r in rows)
    assert got == [
        ("approved", date(2026, 11, 4), "brief", 2),
        ("approved", date(2026, 11, 4), "nudge", 1),
        ("sent", date(2026, 11, 4), "brief", 1),
    ]
