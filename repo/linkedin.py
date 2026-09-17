"""Reads and writes on the linkedin_* snapshot tables
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4). Takes an open
connection; never opens or commits one."""

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
