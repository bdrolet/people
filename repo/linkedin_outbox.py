"""Every SQL statement on linkedin_outbox and linkedin_sender_state. Takes an open
connection; decides nothing (services/linkedin_outbox.py does)."""

from datetime import datetime
from typing import Any

from models.linkedin_outbox import COUNTED_STATUSES

_COLUMNS = (
    "id, person_id, linkedin_username, profile_urn, thread_id, message, kind, mode, status, "
    "reason, approved_at, not_before, attempted_at, sent_at, sent_thread_id, thread_mismatch, "
    "outreach_event_id, session_hint, created_at, updated_at"
)
_INSERTABLE = frozenset(
    {
        "person_id",
        "linkedin_username",
        "profile_urn",
        "thread_id",
        "message",
        "kind",
        "mode",
        "status",
        "reason",
        "approved_at",
        "not_before",
        "attempted_at",
        "sent_at",
        "sent_thread_id",
        "thread_mismatch",
        "outreach_event_id",
        "session_hint",
    }
)
_UPDATABLE = _INSERTABLE - {"linkedin_username", "mode"}
_STATE_UPDATABLE = frozenset(
    {
        "paused_until",
        "paused_reason",
        "last_sent_at",
        "next_gap_seconds",
        "ramp_started_on",
        "consecutive_set_asides",
        "last_error",
    }
)
_STATE_COLUMNS = (
    "id, paused_until, paused_reason, last_sent_at, next_gap_seconds, ramp_started_on, "
    "consecutive_set_asides, last_error, updated_at"
)


def _check(changes: dict, allowed: frozenset[str]) -> None:
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ValueError(f"not writable: {sorted(unknown) or 'nothing to write'}")


def insert(conn: Any, **cols: Any) -> dict:
    _check(cols, _INSERTABLE)
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    return conn.execute(
        f"INSERT INTO linkedin_outbox ({names}) VALUES ({marks}) RETURNING {_COLUMNS}",
        tuple(cols.values()),
    ).fetchone()


def get(conn: Any, row_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM linkedin_outbox WHERE id = %s", (row_id,)
    ).fetchone()


def lock(conn: Any, row_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM linkedin_outbox WHERE id = %s FOR UPDATE", (row_id,)
    ).fetchone()


def update(conn: Any, row_id: int, changes: dict) -> dict:
    _check(changes, _UPDATABLE)
    sets = ", ".join(f"{c} = %s" for c in changes) + ", updated_at = now()"
    return conn.execute(
        f"UPDATE linkedin_outbox SET {sets} WHERE id = %s RETURNING {_COLUMNS}",
        (*changes.values(), row_id),
    ).fetchone()


def claim_next(conn: Any, now: datetime) -> dict | None:
    """Lock the oldest approved queued row that may go now. SKIP LOCKED: a second sender
    run never waits on, or takes, the row another one holds."""
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM linkedin_outbox
        WHERE status = 'queued' AND (not_before IS NULL OR not_before <= %s)
        ORDER BY approved_at, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        (now,),
    ).fetchone()


def list_rows(conn: Any, statuses: tuple[str, ...] | None) -> list[dict]:
    if statuses is None:
        return conn.execute(
            f"SELECT {_COLUMNS} FROM linkedin_outbox ORDER BY approved_at, id"
        ).fetchall()
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM linkedin_outbox WHERE status = ANY(%s)
        ORDER BY approved_at, id
        """,
        (list(statuses),),
    ).fetchall()


def count_counted_between(conn: Any, since: datetime, until: datetime) -> int:
    """Sends against the ceiling: sent, in flight, or possibly sent (check_linkedin)."""
    row = conn.execute(
        """
        SELECT count(*) AS n FROM linkedin_outbox
        WHERE status = ANY(%s)
          AND COALESCE(sent_at, attempted_at) >= %s AND COALESCE(sent_at, attempted_at) < %s
        """,
        (list(COUNTED_STATUSES), since, until),
    ).fetchone()
    return int(row["n"])


def stale_sending(conn: Any, older_than: datetime) -> list[dict]:
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM linkedin_outbox
        WHERE status = 'sending' AND attempted_at < %s
        ORDER BY id FOR UPDATE SKIP LOCKED
        """,
        (older_than,),
    ).fetchall()


def queue_stats(conn: Any, *, mismatch_since: datetime) -> dict:
    return conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE status = 'queued') AS queued,
            min(approved_at) FILTER (WHERE status = 'queued') AS oldest_approved_at,
            count(*) FILTER (WHERE status = 'check_linkedin') AS check_linkedin,
            count(*) FILTER (WHERE status = 'set_aside') AS set_aside,
            count(*) FILTER (WHERE status = 'sent' AND thread_mismatch
                             AND sent_at >= %s) AS thread_mismatch
        FROM linkedin_outbox
        """,
        (mismatch_since,),
    ).fetchone()


def status_counts(conn: Any) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM linkedin_outbox GROUP BY status"
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def daily_counts(conn: Any, *, since: datetime, tz: str) -> list[dict]:
    """Approved (by approved_at, any status) and sent (status sent, by sent_at) per day in
    `tz` and kind, from `since` on."""
    return conn.execute(
        """
        SELECT 'approved' AS what, (approved_at AT TIME ZONE %(tz)s)::date AS day, kind,
               count(*) AS n
        FROM linkedin_outbox WHERE approved_at >= %(since)s
        GROUP BY 2, 3
        UNION ALL
        SELECT 'sent', (sent_at AT TIME ZONE %(tz)s)::date, kind, count(*)
        FROM linkedin_outbox WHERE status = 'sent' AND sent_at >= %(since)s
        GROUP BY 2, 3
        """,
        {"tz": tz, "since": since},
    ).fetchall()


def get_state(conn: Any) -> dict:
    conn.execute("INSERT INTO linkedin_sender_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    return conn.execute(
        f"SELECT {_STATE_COLUMNS} FROM linkedin_sender_state WHERE id = 1"
    ).fetchone()


def lock_state(conn: Any) -> dict:
    """The state row, locked: serializes the ceiling check against concurrent writers."""
    conn.execute("INSERT INTO linkedin_sender_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    return conn.execute(
        f"SELECT {_STATE_COLUMNS} FROM linkedin_sender_state WHERE id = 1 FOR UPDATE"
    ).fetchone()


def update_state(conn: Any, changes: dict) -> dict:
    _check(changes, _STATE_UPDATABLE)
    sets = ", ".join(f"{c} = %s" for c in changes) + ", updated_at = now()"
    return conn.execute(
        f"UPDATE linkedin_sender_state SET {sets} WHERE id = 1 RETURNING {_STATE_COLUMNS}",
        tuple(changes.values()),
    ).fetchone()


def repoint_person(conn: Any, from_id: int, to_id: int) -> int:
    return conn.execute(
        "UPDATE linkedin_outbox SET person_id = %s, updated_at = now() WHERE person_id = %s",
        (to_id, from_id),
    ).rowcount
