"""Reads and writes on the imessage_* snapshot tables
(docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §4). Takes an open
connection; never opens or commits one."""

from typing import Any

from models.imessage import IMessageBatch, IMessageChat, IMessageHandle, IMessageMessage

_CHAT_COLUMNS = ("chat_guid", "display_name", "is_group", "participant_handles", "last_message_at")
_CHAT_UPDATE = ("display_name", "is_group", "participant_handles", "last_message_at")
# build_batch (services/imessage_export.py) always emits every chat (spec §5.1), but on an
# incremental run only sets last_message_at from messages in the current batch's window — most
# chats carry None. A plain `last_message_at = EXCLUDED.last_message_at` would clobber the stored
# value with that NULL. GREATEST ignores NULL arguments and returns NULL only when both are NULL,
# so this keeps the newer of the two and never regresses a real value to NULL.
_CHAT_SET_OVERRIDES = {
    "last_message_at": "last_message_at = GREATEST(EXCLUDED.last_message_at, imessage_chats.last_message_at)"
}

_MESSAGE_COLUMNS = (
    "guid", "chat_guid", "sender_handle", "from_me", "sent_at", "text", "service",
    "has_attachments", "edited_at", "retracted",
)  # fmt: skip
_MESSAGE_UPDATE = (
    "chat_guid", "sender_handle", "from_me", "sent_at", "text", "service",
    "has_attachments", "edited_at", "retracted",
)  # fmt: skip

_HANDLE_INSERT_COLUMNS = (
    "handle",
    "display_name",
    "google_resource_name",
    "person_id",
    "match_method",
)
_HANDLE_UPDATE = ("display_name", "google_resource_name", "person_id", "match_method")

# An empty Python list must bind as text[] on pg8000 too.
_CASTS = {"participant_handles": "::text[]"}

# person_id is the FK to people(id); person_email is exposed for the API by joining
# people rather than being stored on imessage_handles (spec 2026-09-24-person-identity §5.3).
_HANDLE_COLUMNS = """
    h.handle, h.display_name, h.google_resource_name, h.person_id, p.email AS person_email,
    h.match_method, h.message_count, h.my_message_count, h.last_message_at,
    h.last_my_message_at, h.group_message_count, h.last_group_message_at
"""
_HANDLES_JOIN = "imessage_handles h LEFT JOIN people p ON p.id = h.person_id"


def _upsert_many(
    conn: Any,
    table: str,
    columns: tuple[str, ...],
    conflict_col: str,
    update_cols: tuple[str, ...],
    rows: list[tuple],
    chunk: int,
    set_overrides: dict[str, str] | None = None,
) -> None:
    overrides = set_overrides or {}
    placeholder = "(" + ", ".join(f"%s{_CASTS.get(c, '')}" for c in columns) + ")"
    set_clause = (
        ", ".join(overrides.get(c, f"{c} = EXCLUDED.{c}") for c in update_cols)
        + ", updated_at = now()"
    )
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        conn.execute(
            f"""
            INSERT INTO {table} ({", ".join(columns)}) VALUES {", ".join([placeholder] * len(batch))}
            ON CONFLICT ({conflict_col}) DO UPDATE SET {set_clause}
            """,
            tuple(v for row in batch for v in row),
        )


def _insert_scalar_values(conn: Any, table: str, column: str, values: set[str], chunk: int) -> None:
    ordered = sorted(values)
    for i in range(0, len(ordered), chunk):
        batch = ordered[i : i + chunk]
        placeholders = ", ".join(["(%s)"] * len(batch))
        conn.execute(f"INSERT INTO {table} ({column}) VALUES {placeholders}", tuple(batch))


def latest_watermark(conn: Any) -> int:
    row = conn.execute("SELECT max_rowid FROM imessage_imports ORDER BY id DESC LIMIT 1").fetchone()
    return row["max_rowid"] if row else 0


def upsert_chats(conn: Any, chats: list[IMessageChat], chunk: int = 500) -> None:
    _upsert_many(
        conn,
        "imessage_chats",
        _CHAT_COLUMNS,
        "chat_guid",
        _CHAT_UPDATE,
        [
            (c.chat_guid, c.display_name, c.is_group, c.participant_handles, c.last_message_at)
            for c in chats
        ],
        chunk,
        set_overrides=_CHAT_SET_OVERRIDES,
    )


def upsert_messages(conn: Any, messages: list[IMessageMessage], chunk: int = 500) -> int:
    _upsert_many(
        conn,
        "imessage_messages",
        _MESSAGE_COLUMNS,
        "guid",
        _MESSAGE_UPDATE,
        [
            (
                m.guid,
                m.chat_guid,
                m.sender_handle,
                m.from_me,
                m.sent_at,
                m.text,
                m.service,
                m.has_attachments,
                m.edited_at,
                m.retracted,
            )
            for m in messages
        ],
        chunk,
    )
    return len(messages)


def upsert_handles(conn: Any, handles: list[IMessageHandle], chunk: int = 500) -> None:
    """Link fields only (§5.5) — stats columns belong exclusively to
    recompute_handle_stats and must never be touched here."""
    _upsert_many(
        conn,
        "imessage_handles",
        _HANDLE_INSERT_COLUMNS,
        "handle",
        _HANDLE_UPDATE,
        [
            (h.handle, h.display_name, h.google_resource_name, h.person_id, h.match_method)
            for h in handles
        ],
        chunk,
    )


def delete_missing(conn: Any, guids: set[str], chat_guids: set[str], chunk: int = 500) -> int:
    """§5.5 reconciling delete for a --full run: anything in the DB that chat.db
    no longer has is removed, via temp tables rather than a giant NOT IN (...) list."""
    conn.execute("CREATE TEMP TABLE tmp_guids (guid TEXT PRIMARY KEY) ON COMMIT DROP")
    conn.execute("CREATE TEMP TABLE tmp_chat_guids (chat_guid TEXT PRIMARY KEY) ON COMMIT DROP")
    _insert_scalar_values(conn, "tmp_guids", "guid", guids, chunk)
    _insert_scalar_values(conn, "tmp_chat_guids", "chat_guid", chat_guids, chunk)
    deleted = conn.execute(
        """
        DELETE FROM imessage_messages m
        WHERE NOT EXISTS (SELECT 1 FROM tmp_guids t WHERE t.guid = m.guid)
        RETURNING m.guid
        """
    ).fetchall()
    conn.execute(
        """
        DELETE FROM imessage_chats c
        WHERE NOT EXISTS (SELECT 1 FROM tmp_chat_guids t WHERE t.chat_guid = c.chat_guid)
        """
    )
    return len(deleted)


def recompute_handle_stats(conn: Any) -> None:
    """One statement (§5.5): a 1:1 chat has exactly one participant, so this
    counts both sides' messages for that participant — which is what
    message_count means (spec §4.1). Handles with no messages keep their
    column defaults."""
    conn.execute(
        """
        UPDATE imessage_handles h SET
            message_count = COALESCE(s.one_to_one, 0),
            my_message_count = COALESCE(s.mine, 0),
            last_message_at = s.last_at,
            last_my_message_at = s.last_mine_at,
            group_message_count = COALESCE(s.grp, 0),
            last_group_message_at = s.last_group_at,
            updated_at = now()
        FROM (
            SELECT p.handle,
                   COUNT(*) FILTER (WHERE c.is_group = false)                     AS one_to_one,
                   COUNT(*) FILTER (WHERE c.is_group = false AND m.from_me)       AS mine,
                   MAX(m.sent_at) FILTER (WHERE c.is_group = false)               AS last_at,
                   MAX(m.sent_at) FILTER (WHERE c.is_group = false AND m.from_me) AS last_mine_at,
                   COUNT(*) FILTER (WHERE c.is_group = true)                      AS grp,
                   MAX(m.sent_at) FILTER (WHERE c.is_group = true)                AS last_group_at
            FROM imessage_chats c
            CROSS JOIN LATERAL unnest(c.participant_handles) AS p(handle)
            JOIN imessage_messages m ON m.chat_guid = c.chat_guid
            GROUP BY p.handle
        ) s
        WHERE h.handle = s.handle
        """
    )


def record_import(conn: Any, b: IMessageBatch, *, messages_deleted: int) -> None:
    """Append-only audit row (§4.4). messages_upserted/chats/handles are derived
    from the batch's lists here, not stored as counters on IMessageBatch."""
    conn.execute(
        """
        INSERT INTO imessage_imports (ran_at, mode, max_rowid, messages_upserted,
            messages_deleted, undecoded, handles, matched_by_email, matched_by_google,
            linked_to_people, chats)
        VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            b.mode,
            b.max_rowid,
            len(b.messages),
            messages_deleted,
            b.undecoded,
            len(b.handles),
            b.matched_by_email,
            b.matched_by_google,
            b.linked_to_people,
            len(b.chats),
        ),
    )


def handles(
    conn: Any,
    *,
    q: str | None = None,
    min_messages: int | None = None,
    replied: bool | None = None,
    quiet_since: Any = None,
    unmatched: bool | None = None,
    include_groups: bool = False,
    limit: int = 50,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if q:
        where.append("(h.display_name ILIKE %s OR h.handle ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like, like]
    if min_messages is not None:
        where.append("h.message_count >= %s")
        params.append(min_messages)
    if replied is not None:
        where.append("h.my_message_count > 0" if replied else "h.my_message_count = 0")
    if quiet_since is not None:
        where.append("h.last_message_at < %s")
        params.append(quiet_since)
    if unmatched is not None:
        where.append(
            "h.person_id IS NULL AND h.google_resource_name IS NULL"
            if unmatched
            else "(h.person_id IS NOT NULL OR h.google_resource_name IS NOT NULL)"
        )
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = (
        "GREATEST(COALESCE(h.last_message_at, 'epoch'::timestamptz),"
        " COALESCE(h.last_group_message_at, 'epoch'::timestamptz)) DESC"
        if include_groups
        else "h.last_message_at DESC NULLS LAST"
    )
    return conn.execute(
        f"SELECT {_HANDLE_COLUMNS} FROM {_HANDLES_JOIN} {clause} ORDER BY {order} LIMIT %s",
        (*params, limit),
    ).fetchall()


def handle(conn: Any, handle: str) -> dict | None:
    return conn.execute(
        f"SELECT {_HANDLE_COLUMNS} FROM {_HANDLES_JOIN} WHERE h.handle = %s", (handle,)
    ).fetchone()


def handle_groups(conn: Any, handle: str) -> list[dict]:
    """The group chats `handle` is a participant in (§7.3): chat_guid, display_name,
    participant count, message count, last_message_at. No message text."""
    return conn.execute(
        """
        SELECT c.chat_guid, c.display_name,
               array_length(c.participant_handles, 1) AS participant_count,
               COUNT(m.guid) AS message_count, MAX(m.sent_at) AS last_message_at
        FROM imessage_chats c
        LEFT JOIN imessage_messages m ON m.chat_guid = c.chat_guid
        WHERE c.is_group AND %s = ANY(c.participant_handles)
        GROUP BY c.chat_guid, c.display_name, c.participant_handles
        ORDER BY last_message_at DESC NULLS LAST
        """,
        (handle,),
    ).fetchall()


def repoint_person(conn: Any, from_id: int, to_id: int) -> int:
    """Move every handle linked to from_id onto to_id, and report how many moved."""
    return conn.execute(
        "UPDATE imessage_handles SET person_id = %s WHERE person_id = %s",
        (to_id, from_id),
    ).rowcount


def repoint_google_resource(conn: Any, from_rn: str, to_rn: str) -> int:
    """Move handles matched to one Google contact onto another, and report how
    many moved. Used when duplicate contacts are merged: the handle's
    google_resource_name is its own match record, independent of person_id, so
    it would otherwise be left naming a contact that no longer exists."""
    return conn.execute(
        "UPDATE imessage_handles SET google_resource_name = %s WHERE google_resource_name = %s",
        (to_rn, from_rn),
    ).rowcount


def summary_for_person(conn: Any, person_id: int) -> dict | None:
    """Aggregated over every handle linked to this person (§7.1 IMessageSummary)."""
    return conn.execute(
        """
        SELECT array_agg(handle ORDER BY handle) AS handles,
               COALESCE(SUM(message_count), 0) AS message_count,
               COALESCE(SUM(my_message_count), 0) AS my_message_count,
               MAX(last_message_at) AS last_message_at,
               MAX(last_my_message_at) AS last_my_message_at,
               COALESCE(SUM(group_message_count), 0) AS group_message_count,
               MAX(last_group_message_at) AS last_group_message_at,
               MAX(updated_at) AS imported_at
        FROM imessage_handles WHERE person_id = %s
        HAVING COUNT(*) > 0
        """,
        (person_id,),
    ).fetchone()


def search_handles(conn: Any, q: str, limit: int) -> list[dict]:
    term = q.strip()
    like = f"%{term.lower()}%"
    return conn.execute(
        f"""
        SELECT {_HANDLE_COLUMNS} FROM {_HANDLES_JOIN}
        WHERE h.display_name ILIKE %s OR h.handle ILIKE %s
           OR similarity(h.display_name, %s) > 0.3 OR similarity(h.handle, %s) > 0.3
        ORDER BY h.last_message_at DESC NULLS LAST LIMIT %s
        """,
        (like, like, term, term, limit),
    ).fetchall()


def latest_import(conn: Any) -> dict | None:
    return conn.execute(
        """
        SELECT ran_at, mode, max_rowid, messages_upserted, messages_deleted, undecoded,
               handles, matched_by_email, matched_by_google, linked_to_people, chats
        FROM imessage_imports ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
