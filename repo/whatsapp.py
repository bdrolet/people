"""Reads and writes on the whatsapp_* snapshot tables
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §4, §6.7). Takes an
open connection; never opens or commits one."""

from datetime import UTC, datetime
from typing import Any

from models.whatsapp import (
    WhatsAppBatch,
    WhatsAppChat,
    WhatsAppChatMember,
    WhatsAppHandle,
    WhatsAppMessage,
)

# Identity columns only. member_count/message_count/my_message_count/last_message_at
# are owned by recompute_chat_stats: an incremental batch holds no messages for most
# chats, so writing its partial counters here would clobber the stored totals — the
# same shape as the last_message_at bug the iMessage import shipped (§6.7).
_CHAT_COLUMNS = ("chat_jid", "kind", "subject", "handle", "created_at", "last_message_at")
_CHAT_UPDATE = ("kind", "subject", "handle", "created_at", "last_message_at")
_CHAT_SET_OVERRIDES = {
    "last_message_at": (
        "last_message_at = GREATEST(EXCLUDED.last_message_at, whatsapp_chats.last_message_at)"
    )
}

_HANDLE_COLUMNS = ("handle", "jid", "display_name", "person_id", "match_method")
_HANDLE_UPDATE = ("jid", "display_name", "person_id", "match_method")
# person_id/match_method are rewritten verbatim, including to NULL — the match is
# redone from scratch every run (§6.5). display_name is coalesced: a handle seen this
# run only as a group sender may carry no name, which must not erase a known one.
_HANDLE_SET_OVERRIDES = {
    "display_name": (
        "display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)"
    )
}

_MESSAGE_COLUMNS = (
    "chat_jid", "stanza_id", "sender_handle", "from_me", "sent_at", "text",
    "message_type", "has_media", "media_kind", "source_pk",
)  # fmt: skip
_MESSAGE_UPDATE = (
    "sender_handle", "from_me", "sent_at", "text", "message_type", "has_media",
    "media_kind", "source_pk",
)  # fmt: skip

_MEMBER_COLUMNS = ("chat_jid", "handle", "person_id", "is_admin", "is_active")


def _upsert_many(
    conn: Any,
    table: str,
    columns: tuple[str, ...],
    conflict: str,
    update_cols: tuple[str, ...],
    rows: list[tuple],
    chunk: int,
    set_overrides: dict[str, str] | None = None,
) -> int:
    overrides = set_overrides or {}
    placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
    set_clause = (
        ", ".join(overrides.get(c, f"{c} = EXCLUDED.{c}") for c in update_cols)
        + ", updated_at = now()"
    )
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        conn.execute(
            f"""
            INSERT INTO {table} ({", ".join(columns)}) VALUES {", ".join([placeholder] * len(part))}
            ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}
            """,
            tuple(v for row in part for v in row),
        )
    return len(rows)


def latest_watermark(conn: Any) -> int:
    """§6.2: MAX(source_pk) from the messages themselves, not the audit table — a
    run that wrote rows and then failed before recording its import must not cause
    the next run to re-read everything it already has."""
    row = conn.execute("SELECT MAX(source_pk) AS watermark FROM whatsapp_messages").fetchone()
    return (row["watermark"] or 0) if row else 0


def upsert_chats(conn: Any, chats: list[WhatsAppChat], chunk: int = 500) -> int:
    return _upsert_many(
        conn,
        "whatsapp_chats",
        _CHAT_COLUMNS,
        "chat_jid",
        _CHAT_UPDATE,
        [(c.chat_jid, c.kind, c.subject, c.handle, c.created_at, c.last_message_at) for c in chats],
        chunk,
        set_overrides=_CHAT_SET_OVERRIDES,
    )


def upsert_handles(conn: Any, handles: list[WhatsAppHandle], chunk: int = 500) -> int:
    """Link fields only (§6.7 step 2) — the stats columns belong exclusively to
    recompute_handle_stats and must never be touched here, or an ON CONFLICT would
    reset a counter to its insert default."""
    return _upsert_many(
        conn,
        "whatsapp_handles",
        _HANDLE_COLUMNS,
        "handle",
        _HANDLE_UPDATE,
        [(h.handle, h.jid, h.display_name, h.person_id, h.match_method) for h in handles],
        chunk,
        set_overrides=_HANDLE_SET_OVERRIDES,
    )


def replace_members(conn: Any, members: list[WhatsAppChatMember], chunk: int = 500) -> int:
    """Delete-and-replace per chat (§6.7 step 3): membership is a full snapshot with
    no per-row id, so an upsert alone would never notice a departure. Only the chats
    present in this batch are cleared — sessions and members are re-read in full on
    every run (§6.2), so that is all of them.

    No ON CONFLICT on the insert: build_batch dedupes members on (chat_jid, handle)
    before they reach here, and the delete already made room, so a plain multi-row
    INSERT is correct and simpler."""
    if not members:
        return 0
    chat_jids = sorted({m.chat_jid for m in members})
    for i in range(0, len(chat_jids), chunk):
        part = chat_jids[i : i + chunk]
        conn.execute(
            "DELETE FROM whatsapp_chat_members WHERE chat_jid IN"
            f" ({', '.join(['%s'] * len(part))})",
            tuple(part),
        )
    placeholder = "(" + ", ".join(["%s"] * len(_MEMBER_COLUMNS)) + ")"
    for i in range(0, len(members), chunk):
        part_members = members[i : i + chunk]
        conn.execute(
            f"INSERT INTO whatsapp_chat_members ({', '.join(_MEMBER_COLUMNS)})"
            f" VALUES {', '.join([placeholder] * len(part_members))}",
            tuple(
                v
                for m in part_members
                for v in (m.chat_jid, m.handle, m.person_id, m.is_admin, m.is_active)
            ),
        )
    return len(members)


def upsert_messages(conn: Any, messages: list[WhatsAppMessage], chunk: int = 500) -> int:
    return _upsert_many(
        conn,
        "whatsapp_messages",
        _MESSAGE_COLUMNS,
        "chat_jid, stanza_id",
        _MESSAGE_UPDATE,
        [
            (
                m.chat_jid,
                m.stanza_id,
                m.sender_handle,
                m.from_me,
                m.sent_at,
                m.text,
                m.message_type,
                m.has_media,
                m.media_kind,
                m.source_pk,
            )
            for m in messages
        ],
        chunk,
    )


def delete_missing(
    conn: Any, keys: set[tuple[str, str]], chat_jids: set[str], chunk: int = 500
) -> int:
    """Reconciling delete for a --full run: anything in the DB the store no longer
    has is removed, via temp tables rather than a giant NOT IN (...) list.

    The sets come from the batch, not from raw SQLite: the batch is by definition
    exactly what should be stored, so a row dropped by this version's filters (a
    senderless group inbound, a status session) is swept too."""
    conn.execute(
        "CREATE TEMP TABLE tmp_wa_keys (chat_jid TEXT, stanza_id TEXT,"
        " PRIMARY KEY (chat_jid, stanza_id)) ON COMMIT DROP"
    )
    conn.execute("CREATE TEMP TABLE tmp_wa_chats (chat_jid TEXT PRIMARY KEY) ON COMMIT DROP")
    ordered_keys = sorted(keys)
    for i in range(0, len(ordered_keys), chunk):
        part = ordered_keys[i : i + chunk]
        conn.execute(
            "INSERT INTO tmp_wa_keys (chat_jid, stanza_id) VALUES "
            + ", ".join(["(%s, %s)"] * len(part)),
            tuple(v for key in part for v in key),
        )
    ordered_chats = sorted(chat_jids)
    for i in range(0, len(ordered_chats), chunk):
        part_chats = ordered_chats[i : i + chunk]
        conn.execute(
            "INSERT INTO tmp_wa_chats (chat_jid) VALUES " + ", ".join(["(%s)"] * len(part_chats)),
            tuple(part_chats),
        )
    deleted = conn.execute(
        """
        DELETE FROM whatsapp_messages m
        WHERE NOT EXISTS (
            SELECT 1 FROM tmp_wa_keys t
            WHERE t.chat_jid = m.chat_jid AND t.stanza_id = m.stanza_id
        )
        RETURNING m.chat_jid, m.stanza_id
        """
    ).fetchall()
    # Cascades to whatsapp_chat_members and whatsapp_messages for that chat (§4.3).
    conn.execute(
        """
        DELETE FROM whatsapp_chats c
        WHERE NOT EXISTS (SELECT 1 FROM tmp_wa_chats t WHERE t.chat_jid = c.chat_jid)
        """
    )
    return len(deleted)


def recompute_chat_stats(conn: Any) -> None:
    """Derive every chat counter from the stored rows, in one statement.

    Not written from the batch: an incremental batch holds only its window, so a
    partial count would overwrite the total (§6.7). member_count counts every
    member row, active or not, because it exists to tell a five-person family group
    from a 1,212-member community group (§5.7)."""
    conn.execute(
        """
        UPDATE whatsapp_chats c SET
            message_count    = COALESCE(s.n, 0),
            my_message_count = COALESCE(s.mine, 0),
            last_message_at  = s.last_at,
            member_count     = COALESCE(mc.n, 0),
            updated_at       = now()
        FROM (SELECT chat_jid FROM whatsapp_chats) k
        LEFT JOIN (
            SELECT chat_jid, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE from_me) AS mine,
                   MAX(sent_at) AS last_at
            FROM whatsapp_messages GROUP BY chat_jid
        ) s ON s.chat_jid = k.chat_jid
        LEFT JOIN (
            SELECT chat_jid, COUNT(*) AS n FROM whatsapp_chat_members GROUP BY chat_jid
        ) mc ON mc.chat_jid = k.chat_jid
        WHERE c.chat_jid = k.chat_jid
        """
    )


def recompute_handle_stats(conn: Any) -> None:
    """One statement (§6.7 step 5), three aggregates:

    - 1:1 counts come from the chat, not the sender, because a 1:1 chat has exactly
      one counterpart and Ben's own rows carry sender_handle NULL — counting by
      sender would lose half the conversation.
    - group counts are messages the handle SENT. WhatsApp can attribute group
      senders where chat.db could not, so this deliberately differs from
      imessage_handles.group_message_count (§4.1).
    - group_count is active memberships only.

    LEFT JOINed from the handles table so a handle with nothing left zeroes rather
    than keeping a stale count after a --full run that deleted rows.
    """
    conn.execute(
        """
        WITH direct AS (
            SELECT c.handle,
                   COUNT(*) AS one_to_one,
                   COUNT(*) FILTER (WHERE m.from_me) AS mine,
                   MAX(m.sent_at) AS last_at,
                   MAX(m.sent_at) FILTER (WHERE m.from_me) AS last_mine_at
            FROM whatsapp_chats c
            JOIN whatsapp_messages m ON m.chat_jid = c.chat_jid
            WHERE c.kind = 'direct' AND c.handle IS NOT NULL
            GROUP BY c.handle
        ), grp AS (
            SELECT m.sender_handle AS handle,
                   COUNT(*) AS sent,
                   MAX(m.sent_at) AS last_sent
            FROM whatsapp_messages m
            JOIN whatsapp_chats c ON c.chat_jid = m.chat_jid
            WHERE c.kind = 'group' AND m.sender_handle IS NOT NULL
            GROUP BY m.sender_handle
        ), mem AS (
            SELECT handle, COUNT(*) AS groups
            FROM whatsapp_chat_members WHERE is_active GROUP BY handle
        )
        UPDATE whatsapp_handles h SET
            message_count         = COALESCE(d.one_to_one, 0),
            my_message_count      = COALESCE(d.mine, 0),
            last_message_at       = d.last_at,
            last_my_message_at    = d.last_mine_at,
            group_message_count   = COALESCE(g.sent, 0),
            last_group_message_at = g.last_sent,
            group_count           = COALESCE(mm.groups, 0),
            updated_at            = now()
        FROM (SELECT handle FROM whatsapp_handles) k
        LEFT JOIN direct d ON d.handle = k.handle
        LEFT JOIN grp g ON g.handle = k.handle
        LEFT JOIN mem mm ON mm.handle = k.handle
        WHERE h.handle = k.handle
        """
    )


def record_import(
    conn: Any, b: WhatsAppBatch, *, started_at: datetime, messages_deleted: int
) -> None:
    """Append-only audit row (§4.5). The upserted counts are derived from the
    batch's lists here rather than stored as counters on WhatsAppBatch.

    finished_at is stamped in Python (datetime.now(UTC)), not via SQL now(): the
    whole row is written in a single INSERT with no later UPDATE, so there is no
    reason to defer to the database's clock for a value nothing else reads back."""
    finished_at = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO whatsapp_imports (started_at, finished_at, mode, watermark,
            chats_upserted, members_upserted, handles_upserted, messages_upserted,
            messages_deleted, senderless_dropped, duplicate_stanza_ids, sessions_skipped,
            handles_unnormalized, matched_by_phone, matched_by_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            started_at,
            finished_at,
            b.mode,
            b.watermark,
            len(b.chats),
            len(b.members),
            len(b.handles),
            len(b.messages),
            messages_deleted,
            b.senderless_dropped,
            b.duplicate_stanza_ids,
            b.sessions_skipped,
            b.handles_unnormalized,
            b.matched_by_phone,
            b.matched_by_name,
        ),
    )


def repoint_person(conn: Any, from_id: int, to_id: int) -> int:
    """Move every WhatsApp link from one person row onto another, and report how
    many moved. scripts/merge_duplicate_contacts.py must call this before deleting
    a loser row: both FKs are ON DELETE SET NULL, so a delete first would silently
    unlink the handles and memberships (§4, §11 step 5)."""
    moved = conn.execute(
        "UPDATE whatsapp_handles SET person_id = %s WHERE person_id = %s", (to_id, from_id)
    ).rowcount
    moved += conn.execute(
        "UPDATE whatsapp_chat_members SET person_id = %s WHERE person_id = %s",
        (to_id, from_id),
    ).rowcount
    return moved
