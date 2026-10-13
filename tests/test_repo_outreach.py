from datetime import UTC, datetime

import pytest

from repo import outreach


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple | None]] = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return FakeCursor(self._results.pop(0) if self._results else [])


TS = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


def test_lock_contact_locks_the_row():
    conn = FakeConn([[{"person_id": 1, "stage": "briefed"}]])
    assert outreach.lock_contact(conn, 1)["stage"] == "briefed"
    sql, params = conn.calls[0]
    assert sql.endswith("FOR UPDATE") and params == (1,)


def test_update_contact_only_touches_allowlisted_columns():
    conn = FakeConn([[{"person_id": 1}]])
    outreach.update_contact(conn, 1, {"stage": "briefed", "follow_up_on": None})
    sql, params = conn.calls[0]
    assert "SET stage = %s, follow_up_on = %s, updated_at = now()" in sql
    assert params == ("briefed", None, 1)
    with pytest.raises(ValueError):
        outreach.update_contact(conn, 1, {"person_id": 2})


def test_insert_event_returns_the_new_id():
    conn = FakeConn([[{"id": 42}]])
    eid = outreach.insert_event(
        conn,
        pipeline="referrer",
        person_id=1,
        stage="briefed",
        at=TS,
        source="cli",
        channel="email",
    )
    assert eid == 42
    sql, params = conn.calls[0]
    assert sql.startswith("INSERT INTO outreach_events") and "RETURNING id" in sql
    assert params == ("referrer", 1, None, "briefed", TS, "email", "cli", None, None, None)


def test_insert_event_passes_the_note():
    conn = FakeConn([[{"id": 7}]])
    outreach.insert_event(
        conn, pipeline="referrer", person_id=1, stage="skipped", at=TS, source="cli", note="why"
    )
    sql, params = conn.calls[0]
    assert "note" in sql and params[-1] == "why"


def test_referrer_event_with_message():
    conn = FakeConn([[{"found": True}]])
    assert outreach.referrer_event_with_message(conn, 1, "g1") is True
    sql, params = conn.calls[0]
    assert "pipeline = 'referrer'" in sql and params == (1, "g1")


def test_update_contact_accepts_the_working_queue_columns():
    conn = FakeConn([[{"person_id": 1}]])
    outreach.update_contact(conn, 1, {"nudge_count": 1, "last_nudged_at": TS, "rank": None})
    assert conn.calls[0][1] == (1, TS, None, 1)
