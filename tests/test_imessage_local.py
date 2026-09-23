from datetime import UTC, datetime

import pytest

from clients import imessage_local
from tests.fixtures.imessage import build_chat_db


def test_read_returns_messages_handles_and_chats(tmp_path):
    raw = imessage_local.read(build_chat_db(tmp_path))
    assert len(raw.messages) == 10
    assert raw.handles[1] == "+15550100001"
    assert raw.chat_handles[2] == [1, 2, 3]
    assert raw.max_rowid == 10


def test_incremental_selection_uses_watermark(tmp_path):
    raw = imessage_local.read(build_chat_db(tmp_path), since_rowid=8)
    assert {m["guid"] for m in raw.messages} == {"g9", "g10"}
    assert raw.max_rowid == 10  # watermark is the DB max, not the selection max


def test_window_start_pulls_older_rows_back_in(tmp_path):
    """Rows 1-9 are below the watermark but inside the window, so they come back
    for edit/unsend refresh (spec §5.1). Row 10 (ROWID 10) is above the watermark,
    so it is selected on its own merits regardless of the window — all ten guids
    come back."""
    raw = imessage_local.read(
        build_chat_db(tmp_path), since_rowid=9, window_start=datetime(2025, 6, 1, tzinfo=UTC)
    )
    assert {m["guid"] for m in raw.messages} == {f"g{i}" for i in range(1, 11)}


def test_full_collects_all_guids(tmp_path):
    raw = imessage_local.read(build_chat_db(tmp_path), full=True)
    assert raw.all_guids == {f"g{i}" for i in range(1, 11)}
    assert raw.all_chat_guids == {"iMessage;-;+15550100001", "iMessage;+;chat9", "SMS;-;262966"}


def test_missing_optional_column_is_none(tmp_path):
    import sqlite3

    path = build_chat_db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript(
        "ALTER TABLE message RENAME TO m_old;"
        "CREATE TABLE message AS SELECT ROWID, guid, text, attributedBody, handle_id,"
        " is_from_me, date, service, cache_has_attachments, associated_message_type FROM m_old;"
    )
    db.commit()
    db.close()
    raw = imessage_local.read(path)
    assert raw.messages[0]["date_edited"] is None


def test_missing_required_column_fails_naming_it(tmp_path):
    import sqlite3

    path = build_chat_db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript("ALTER TABLE message DROP COLUMN guid;")
    db.commit()
    db.close()
    with pytest.raises(imessage_local.SchemaError, match="guid"):
        imessage_local.read(path)


def test_missing_file_raises_full_disk_access_error(tmp_path):
    with pytest.raises(imessage_local.FullDiskAccessError):
        imessage_local.read(tmp_path / "nope.db")
