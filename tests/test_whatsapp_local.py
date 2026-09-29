from datetime import UTC, datetime
from pathlib import Path

import pytest

from clients import whatsapp_local
from tests.fixtures.whatsapp import build_store


def test_read_returns_sessions_messages_and_members(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert len(raw.sessions) == 8
    assert len(raw.messages) == 13
    assert len(raw.members) == 4
    assert raw.max_pk == 13


def test_group_creation_date_comes_through_the_join(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    group = next(s for s in raw.sessions if s["ZCONTACTJID"].endswith("@g.us"))
    assert group["ZGROUPCREATIONDATE"] is not None


def test_media_columns_come_through_the_join(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    by_stanza = {m["ZSTANZAID"]: m for m in raw.messages}
    assert by_stanza["s9"]["ZMEDIALOCALPATH"] is not None
    # The §5.3 trap: a ZMEDIAITEM row exists but carries no real content.
    assert by_stanza["s10"]["ZMEDIAITEM"] is not None
    assert by_stanza["s10"]["ZMEDIALOCALPATH"] is None
    assert by_stanza["s10"]["ZMEDIAURL"] is None
    assert by_stanza["s10"]["ZVCARDSTRING"] is None


def test_incremental_selection_uses_the_watermark(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path), since_pk=11)
    assert {m["ZSTANZAID"] for m in raw.messages} == {"s12", "s13"}
    assert raw.max_pk == 13  # the store's max, not the selection's


def test_window_start_pulls_older_rows_back_in(tmp_path):
    """Spec §6.2: a 14-day re-scan window catches late-arriving and edited
    messages below the watermark, exactly as the iMessage import does."""
    raw = whatsapp_local.read(
        build_store(tmp_path), since_pk=13, window_start=datetime(2025, 6, 1, tzinfo=UTC)
    )
    assert len(raw.messages) == 13


def test_full_ignores_the_watermark(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path), since_pk=13, full=True)
    assert len(raw.messages) == 13


def test_the_store_is_copied_not_opened_in_place(tmp_path, monkeypatch):
    """Spec §5.1: the app is live, so the original is never opened; the copy
    includes -wal and -shm so the WAL's contents are visible."""
    path = build_store(tmp_path)
    opened: list[str] = []
    real_connect = whatsapp_local.sqlite3.connect

    def spy(dsn, *a, **kw):
        opened.append(dsn)
        return real_connect(dsn, *a, **kw)

    monkeypatch.setattr(whatsapp_local.sqlite3, "connect", spy)
    whatsapp_local.read(path)
    assert len(opened) == 1
    assert str(path) not in opened[0]
    assert "mode=ro" in opened[0] and "immutable" not in opened[0]


def test_sidecars_are_copied(tmp_path, monkeypatch):
    path = build_store(tmp_path)
    (tmp_path / "ChatStorage.sqlite-wal").write_bytes(b"")
    (tmp_path / "ChatStorage.sqlite-shm").write_bytes(b"")
    copied: list[str] = []
    real_copy = whatsapp_local.shutil.copy2
    monkeypatch.setattr(
        whatsapp_local.shutil,
        "copy2",
        lambda src, dst: (copied.append(Path(src).name), real_copy(src, dst))[1],
    )
    whatsapp_local.read(path)
    assert copied == [
        "ChatStorage.sqlite",
        "ChatStorage.sqlite-wal",
        "ChatStorage.sqlite-shm",
    ]


def test_temp_copy_is_deleted_even_when_the_read_raises(tmp_path, monkeypatch):
    """Review Focus 4: the copy holds the full plaintext history, so a leak on
    the error path is a privacy failure, not untidiness (spec §7)."""
    path = build_store(tmp_path)
    seen: list[Path] = []
    real_mkdtemp = whatsapp_local.tempfile.mkdtemp

    def spy(*a, **kw):
        made = real_mkdtemp(*a, **kw)
        seen.append(Path(made))
        return made

    monkeypatch.setattr(whatsapp_local.tempfile, "mkdtemp", spy)
    monkeypatch.setattr(
        whatsapp_local, "_check_schema", lambda conn: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        whatsapp_local.read(path)
    assert seen and not seen[0].exists()


def test_temp_copy_is_deleted_on_success(tmp_path, monkeypatch):
    path = build_store(tmp_path)
    seen: list[Path] = []
    real_mkdtemp = whatsapp_local.tempfile.mkdtemp
    monkeypatch.setattr(
        whatsapp_local.tempfile,
        "mkdtemp",
        lambda *a, **kw: (seen.append(Path(m := real_mkdtemp(*a, **kw))), m)[1],
    )
    whatsapp_local.read(path)
    assert seen and not seen[0].exists()


def test_missing_store_raises_store_unreadable(tmp_path):
    with pytest.raises(whatsapp_local.StoreUnreadableError):
        whatsapp_local.read(tmp_path / "nope.sqlite")


def test_missing_column_fails_naming_it(tmp_path):
    import sqlite3

    path = build_store(tmp_path)
    db = sqlite3.connect(path)
    db.executescript("ALTER TABLE ZWAMESSAGE DROP COLUMN ZSTANZAID;")
    db.commit()
    db.close()
    with pytest.raises(whatsapp_local.SchemaError, match="ZSTANZAID"):
        whatsapp_local.read(path)


def test_default_store_path_is_the_group_container():
    assert "group.net.whatsapp.WhatsApp.shared" in str(whatsapp_local.DEFAULT_STORE)
    assert whatsapp_local.DEFAULT_STORE.name == "ChatStorage.sqlite"


def test_copy_failure_raises_store_unreadable(tmp_path, monkeypatch):
    """StoreUnreadableError's own docstring says 'could not be found, copied, or
    opened' — a shutil.copy2 failure (a vanishing sidecar, a permissions error)
    must surface as that, not a raw OSError."""
    path = build_store(tmp_path)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(whatsapp_local.shutil, "copy2", boom)
    with pytest.raises(whatsapp_local.StoreUnreadableError):
        whatsapp_local.read(path)
