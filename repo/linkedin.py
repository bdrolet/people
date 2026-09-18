"""Reads and writes on the linkedin_* snapshot tables
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4). Takes an open
connection; never opens or commits one."""

from datetime import date
from typing import Any

from models.linkedin import LinkedInSnapshot

_CONNECTION_INSERT = (
    "profile_url", "first_name", "last_name", "full_name", "email", "company", "position",
    "connected_on", "person_email", "match_method", "message_count", "my_message_count",
    "last_message_at", "last_my_message_at", "snapshot_at",
)  # fmt: skip
_MESSAGE_INSERT = (
    "conversation_id", "conversation_title", "sender_name", "sender_profile_url",
    "recipient_names", "recipient_profile_urls", "sent_at", "subject", "content", "folder",
    "from_me", "snapshot_at",
)  # fmt: skip
_RECOMMENDATION_INSERT = (
    "direction", "first_name", "last_name", "full_name", "company", "job_title", "text",
    "status", "created_on", "profile_url", "snapshot_at",
)  # fmt: skip
# An empty Python list must bind as text[] on pg8000 too.
_CASTS = {"recipient_profile_urls": "::text[]"}


def _insert_many(
    conn: Any, table: str, columns: tuple[str, ...], rows: list[tuple], chunk: int
) -> None:
    placeholder = "(" + ", ".join(f"%s{_CASTS.get(c, '')}" for c in columns) + ")"
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join([placeholder] * len(batch))}",
            tuple(v for row in batch for v in row),
        )


def replace_snapshot(conn: Any, s: LinkedInSnapshot, chunk: int = 500) -> None:
    """Wholesale replace (§5.5). The caller's transaction makes it atomic: an
    error before commit leaves the previous snapshot intact."""
    conn.execute("DELETE FROM linkedin_messages")
    conn.execute("DELETE FROM linkedin_recommendations")
    conn.execute("DELETE FROM linkedin_connections")
    _insert_many(
        conn,
        "linkedin_connections",
        _CONNECTION_INSERT,
        [
            (
                c.profile_url,
                c.first_name,
                c.last_name,
                c.full_name,
                c.email,
                c.company,
                c.position,
                c.connected_on,
                c.person_email,
                c.match_method,
                c.message_count,
                c.my_message_count,
                c.last_message_at,
                c.last_my_message_at,
                s.snapshot_at,
            )  # fmt: skip
            for c in s.connections
        ],
        chunk,
    )
    _insert_many(
        conn,
        "linkedin_messages",
        _MESSAGE_INSERT,
        [
            (
                m.conversation_id,
                m.conversation_title,
                m.sender_name,
                m.sender_profile_url,
                m.recipient_names,
                m.recipient_profile_urls,
                m.sent_at,
                m.subject,
                m.content,
                m.folder,
                m.from_me,
                s.snapshot_at,
            )  # fmt: skip
            for m in s.messages
        ],
        chunk,
    )
    _insert_many(
        conn,
        "linkedin_recommendations",
        _RECOMMENDATION_INSERT,
        [
            (
                r.direction,
                r.first_name,
                r.last_name,
                r.full_name,
                r.company,
                r.job_title,
                r.text,
                r.status,
                r.created_on,
                r.profile_url,
                s.snapshot_at,
            )  # fmt: skip
            for r in s.recommendations
        ],
        chunk,
    )
    given = sum(r.direction == "given" for r in s.recommendations)
    conn.execute(
        """
        INSERT INTO linkedin_imports (snapshot_at, source, connections, messages,
            recommendations_given, recommendations_received, matched_by_email, matched_by_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            s.snapshot_at,
            s.source,
            len(s.connections),
            len(s.messages),
            given,
            len(s.recommendations) - given,
            s.matched_by_email,
            s.matched_by_name,
        ),
    )


_CONNECTION_COLUMNS = """
    profile_url, full_name, email, company, position, connected_on, person_email,
    match_method, message_count, my_message_count, last_message_at, last_my_message_at,
    snapshot_at
"""
_ORDER = "ORDER BY last_message_at DESC NULLS LAST, connected_on DESC NULLS LAST"
# Must match linkedin_messages_participants_idx exactly for the planner to use it.
_PARTICIPANTS = "(recipient_profile_urls || ARRAY[sender_profile_url])"


def connection_for_person(conn: Any, email: str) -> dict | None:
    return conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections WHERE person_email = %s {_ORDER} LIMIT 1",
        (email,),
    ).fetchone()


def search_connections(conn: Any, q: str, limit: int) -> list[dict]:
    term = q.strip()
    like = f"%{term.lower()}%"
    return conn.execute(
        f"""
        SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections
        WHERE full_name ILIKE %s OR company ILIKE %s OR position ILIKE %s
           OR similarity(full_name, %s) > 0.3 OR similarity(company, %s) > 0.3
           OR similarity(position, %s) > 0.3
        {_ORDER} LIMIT %s
        """,
        (like, like, like, term, term, term, limit),
    ).fetchall()


def list_connections(
    conn: Any,
    *,
    q: str | None = None,
    company: str | None = None,
    position: str | None = None,
    min_messages: int | None = None,
    replied: bool | None = None,
    quiet_since: date | None = None,
    unmatched: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (("full_name", q), ("company", company), ("position", position)):
        if value:
            where.append(f"{column} ILIKE %s")
            params.append(f"%{value.strip()}%")
    if min_messages is not None:
        where.append("message_count >= %s")
        params.append(min_messages)
    if replied is not None:
        where.append("my_message_count > 0" if replied else "my_message_count = 0")
    if quiet_since is not None:
        where.append("last_message_at < %s")
        params.append(quiet_since)
    if unmatched is not None:
        where.append("person_email IS NULL" if unmatched else "person_email IS NOT NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections {clause} {_ORDER} LIMIT %s",
        (*params, limit),
    ).fetchall()


def get_connection(conn: Any, profile_url: str) -> dict | None:
    return conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections WHERE profile_url = %s",
        (profile_url,),
    ).fetchone()


def messages_for(conn: Any, profile_url: str, limit: int) -> list[dict]:
    return conn.execute(
        f"""
        SELECT conversation_id, conversation_title, sender_name, sender_profile_url,
               recipient_names, sent_at, subject, content, folder, from_me
        FROM linkedin_messages
        WHERE {_PARTICIPANTS} @> ARRAY[%s]::text[]
        ORDER BY sent_at DESC LIMIT %s
        """,
        (profile_url, limit),
    ).fetchall()


def recommendations_for(conn: Any, profile_url: str) -> list[dict]:
    return conn.execute(
        """
        SELECT direction, full_name, company, job_title, text, status, created_on
        FROM linkedin_recommendations WHERE profile_url = %s
        ORDER BY created_on DESC NULLS LAST
        """,
        (profile_url,),
    ).fetchall()


def latest_import(conn: Any) -> dict | None:
    return conn.execute(
        """
        SELECT snapshot_at, source, connections, messages, recommendations_given,
               recommendations_received, matched_by_email, matched_by_name
        FROM linkedin_imports ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
