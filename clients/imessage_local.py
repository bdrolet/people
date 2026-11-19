"""Read-only access to the local Messages database
(docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §5.1). Local script use
only — people's Cloud Functions never read iMessage, exactly as clients/graph_local.py
is import-only."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

EPOCH_2001 = 978307200
NS = 1_000_000_000

REQUIRED = (
    "ROWID",
    "guid",
    "text",
    "attributedBody",
    "handle_id",
    "is_from_me",
    "date",
    "service",
    "cache_has_attachments",
    "associated_message_type",
)
OPTIONAL = ("date_edited", "date_retracted")


class FullDiskAccessError(Exception):
    """chat.db could not be opened — usually missing Full Disk Access."""


class SchemaError(Exception):
    """chat.db is missing a column the import requires."""


@dataclass
class RawChatDb:
    messages: list[dict[str, Any]]
    handles: dict[int, str]
    chats: list[dict[str, Any]]
    chat_handles: dict[int, list[int]]
    max_rowid: int
    all_guids: set[str] | None = None
    all_chat_guids: set[str] | None = None


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.Error as e:
        raise FullDiskAccessError(str(e)) from e


def read(
    path: Path,
    *,
    since_rowid: int = 0,
    window_start: datetime | None = None,
    full: bool = False,
) -> RawChatDb:
    conn = _connect(path)
    try:
        present = {r["name"] for r in conn.execute("PRAGMA table_info(message)")}
        present.add("ROWID")
        missing = [c for c in REQUIRED if c not in present]
        if missing:
            raise SchemaError(f"chat.db message table is missing: {', '.join(missing)}")

        select_cols = ", ".join(
            [f"m.{c}" for c in REQUIRED]
            + [(f"m.{c}" if c in present else f"NULL AS {c}") for c in OPTIONAL]
        )

        where = "1=1"
        params: list[Any] = []
        if not full:
            where = "m.ROWID > ?"
            params = [since_rowid]
            if window_start is not None:
                window_ns = int((window_start.timestamp() - EPOCH_2001) * NS)
                where = f"({where} OR m.date >= ?)"
                params.append(window_ns)

        rows = conn.execute(
            f"SELECT {select_cols}, "
            "(SELECT MIN(chat_id) FROM chat_message_join j WHERE j.message_id = m.ROWID)"
            " AS chat_id"
            f" FROM message m WHERE {where} ORDER BY m.ROWID",
            params,
        ).fetchall()
        messages = [dict(row) for row in rows]

        max_rowid = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0

        handles = {row["ROWID"]: row["id"] for row in conn.execute("SELECT ROWID, id FROM handle")}

        chats = [
            {"ROWID": row["ROWID"], "guid": row["guid"], "display_name": row["display_name"]}
            for row in conn.execute("SELECT ROWID, guid, display_name FROM chat")
        ]

        chat_handles: dict[int, list[int]] = {}
        for row in conn.execute(
            "SELECT chat_id, handle_id FROM chat_handle_join ORDER BY chat_id, handle_id"
        ):
            chat_handles.setdefault(row["chat_id"], []).append(row["handle_id"])

        all_guids: set[str] | None = None
        all_chat_guids: set[str] | None = None
        if full:
            all_guids = {row["guid"] for row in conn.execute("SELECT guid FROM message")}
            all_chat_guids = {row["guid"] for row in conn.execute("SELECT guid FROM chat")}

        return RawChatDb(
            messages=messages,
            handles=handles,
            chats=chats,
            chat_handles=chat_handles,
            max_rowid=max_rowid,
            all_guids=all_guids,
            all_chat_guids=all_chat_guids,
        )
    finally:
        conn.close()


# --- Live reads for the iMessage MCP server (mcp_servers/imessage.py) --------------------
# Read-only, like everything above: the MCP server never writes chat.db, so reading a thread
# doesn't mark it read in Messages.

DEFAULT_PATH = Path.home() / "Library" / "Messages" / "chat.db"

LIVE_MESSAGE_COLUMNS = (
    "ROWID",
    "guid",
    "text",
    "attributedBody",
    "is_from_me",
    "date",
    "service",
    "cache_has_attachments",
    "associated_message_type",
    "error",
    "is_delivered",
    "is_read",
)
LIVE_CHAT_COLUMNS = ("ROWID", "guid", "display_name", "style", "service_name")


def _columns(conn: sqlite3.Connection, table: str, wanted: tuple[str, ...], alias: str) -> str:
    """`alias.col` for each wanted column the table has, `NULL AS col` for the rest, so the
    live reads survive a chat.db (or a test fixture) without the newer columns."""
    present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    present.add("ROWID")
    return ", ".join(f"{alias}.{c}" if c in present else f"NULL AS {c}" for c in wanted)


@dataclass
class ChatIndex:
    chats: list[dict[str, Any]]  # LIVE_CHAT_COLUMNS + last_date, newest first
    handles: dict[int, str]  # handle ROWID -> raw handle id
    chat_handles: dict[int, list[int]]  # chat ROWID -> handle ROWIDs


def chat_index(path: Path) -> ChatIndex:
    conn = _connect(path)
    try:
        cols = _columns(conn, "chat", LIVE_CHAT_COLUMNS, "c")
        chats = [
            dict(r)
            for r in conn.execute(
                f"SELECT {cols}, MAX(m.date) AS last_date FROM chat c"
                " LEFT JOIN chat_message_join j ON j.chat_id = c.ROWID"
                " LEFT JOIN message m ON m.ROWID = j.message_id"
                " GROUP BY c.ROWID ORDER BY last_date IS NULL, last_date DESC"
            )
        ]
        handles = {row["ROWID"]: row["id"] for row in conn.execute("SELECT ROWID, id FROM handle")}
        chat_handles: dict[int, list[int]] = {}
        for row in conn.execute(
            "SELECT chat_id, handle_id FROM chat_handle_join ORDER BY chat_id, handle_id"
        ):
            chat_handles.setdefault(row["chat_id"], []).append(row["handle_id"])
        return ChatIndex(chats=chats, handles=handles, chat_handles=chat_handles)
    finally:
        conn.close()


def _live_select(conn: sqlite3.Connection) -> str:
    return (
        f"SELECT {_columns(conn, 'message', LIVE_MESSAGE_COLUMNS, 'm')},"
        " h.id AS handle, j.chat_id AS chat_id"
        " FROM message m"
        " JOIN chat_message_join j ON j.message_id = m.ROWID"
        " LEFT JOIN handle h ON h.ROWID = m.handle_id"
    )


def chat_messages(path: Path, chat_ids: list[int], *, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` messages across `chat_ids`, oldest first."""
    if not chat_ids:
        return []
    conn = _connect(path)
    try:
        marks = ", ".join("?" for _ in chat_ids)
        rows = conn.execute(
            f"{_live_select(conn)} WHERE j.chat_id IN ({marks}) ORDER BY m.date DESC LIMIT ?",
            [*chat_ids, limit],
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def recent_messages(path: Path, *, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` messages in any chat, newest first (search scans these: most
    bodies live only in attributedBody, so SQL LIKE can't find them)."""
    conn = _connect(path)
    try:
        rows = conn.execute(f"{_live_select(conn)} ORDER BY m.date DESC LIMIT ?", [limit])
        return [dict(r) for r in rows]
    finally:
        conn.close()


def max_message_rowid(path: Path) -> int:
    conn = _connect(path)
    try:
        return conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
    finally:
        conn.close()


def messages_after(path: Path, rowid: int) -> list[dict[str, Any]]:
    """Messages written after `rowid`, oldest first (send confirmation)."""
    conn = _connect(path)
    try:
        rows = conn.execute(f"{_live_select(conn)} WHERE m.ROWID > ? ORDER BY m.ROWID", [rowid])
        return [dict(r) for r in rows]
    finally:
        conn.close()
