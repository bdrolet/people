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
