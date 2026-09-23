"""Synthetic chat.db (spec §10). Invented numbers (+1555010…) and example.com only."""

import sqlite3
from pathlib import Path

NS = 1_000_000_000
EPOCH_2001 = 978307200

# typedstream fragment: an NSString payload "Attr only body" inside an NSAttributedString.
# Verified against the real typedstream layout — decodes to "Attr only body".
ATTR_BODY = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08"
    b"NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x0eAttr only body\x86"
)

DDL = """
CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
  handle_id INTEGER, is_from_me INTEGER, date INTEGER, date_edited INTEGER,
  date_retracted INTEGER, service TEXT, cache_has_attachments INTEGER,
  associated_message_type INTEGER);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT, style INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
"""


def apple_ns(unix_seconds: int) -> int:
    return (unix_seconds - EPOCH_2001) * NS


def build_chat_db(tmp_path: Path) -> Path:
    """1:1 chat with Alice, a group chat, a short-code chat. Covers every decode path."""
    path = tmp_path / "chat.db"
    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.executemany(
        "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
        [
            (1, "+15550100001", "iMessage"),
            (2, "bob@example.com", "iMessage"),
            (3, "+15550100002", "SMS"),
            (4, "262966", "SMS"),
        ],
    )
    db.executemany(
        "INSERT INTO chat (ROWID, guid, display_name, style) VALUES (?, ?, ?, ?)",
        [
            (1, "iMessage;-;+15550100001", None, 45),
            (2, "iMessage;+;chat9", "Fixture Group", 43),
            (3, "SMS;-;262966", None, 45),
        ],
    )
    db.executemany(
        "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
        [(1, 1), (2, 1), (2, 2), (2, 3), (3, 4)],
    )
    rows = [
        # (rowid, guid, text, attr, handle_id, from_me, date, edited, retracted,
        #  service, attach, assoc, chat)
        (1, "g1", "Hi from Alice", None, 1, 0, apple_ns(1_750_000_000), 0, 0, "iMessage", 0, 0, 1),
        (2, "g2", "Reply from me", None, 0, 1, apple_ns(1_750_000_060), 0, 0, "iMessage", 0, 0, 1),
        (3, "g3", None, ATTR_BODY, 1, 0, apple_ns(1_750_000_120), 0, 0, "iMessage", 0, 0, 1),
        (
            4,
            "g4",
            None,
            b"\x04\x0bstreamtyped\xff\xff",
            1,
            0,
            apple_ns(1_750_000_180),
            0,
            0,
            "iMessage",
            0,
            0,
            1,
        ),  # undecodable
        (
            5,
            "g5",
            "Liked a message",
            None,
            1,
            0,
            apple_ns(1_750_000_240),
            0,
            0,
            "iMessage",
            0,
            2000,
            1,
        ),  # reaction, skipped
        (
            6,
            "g6",
            "Edited text",
            None,
            1,
            0,
            apple_ns(1_750_000_300),
            apple_ns(1_750_000_400),
            0,
            "iMessage",
            0,
            0,
            1,
        ),  # edited
        (
            7,
            "g7",
            "Unsent",
            None,
            1,
            0,
            apple_ns(1_750_000_360),
            0,
            apple_ns(1_750_000_500),
            "iMessage",
            0,
            0,
            1,
        ),  # retracted
        (8, "g8", "Group hello", None, 2, 0, apple_ns(1_750_000_420), 0, 0, "iMessage", 0, 0, 2),
        (
            9,
            "g9",
            "Your code is 123456",
            None,
            4,
            0,
            apple_ns(1_750_000_480),
            0,
            0,
            "SMS",
            0,
            0,
            3,
        ),  # short code, dropped
        (10, "g10", "Old one", None, 1, 0, 550_000_000, 0, 0, "iMessage", 1, 0, 1),  # seconds-era
    ]
    db.executemany(
        "INSERT INTO message (ROWID, guid, text, attributedBody, handle_id, is_from_me, date,"
        " date_edited, date_retracted, service, cache_has_attachments, associated_message_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [r[:-1] for r in rows],
    )
    db.executemany(
        "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
        [(r[-1], r[0]) for r in rows],
    )
    db.commit()
    db.close()
    return path
