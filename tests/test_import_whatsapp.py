import pytest

from scripts import import_whatsapp
from tests.fixtures.whatsapp import build_store


class FakeConn:
    """Records SQL and answers the two reads run() makes: the watermark and the
    people rows to match against."""

    def __init__(self):
        self.calls: list[str] = []
        self.committed = self.rolled_back = False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append(flat)
        rows: list[dict] = []
        if "MAX(source_pk)" in flat:
            rows = [{"watermark": 0}]
        elif "SELECT id, display_name, phone_numbers FROM people" in flat:
            rows = [{"id": 1, "display_name": "Alice Example", "phone_numbers": ["+15550100001"]}]

        class C:
            rowcount = len(rows)

            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return C()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_dry_run_writes_nothing_and_rolls_back(tmp_path):
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    assert not any("INSERT INTO whatsapp_" in c for c in conn.calls)
    assert not any("DELETE FROM whatsapp_" in c for c in conn.calls)
    assert conn.rolled_back and not conn.committed


def test_dry_run_still_matches_for_real(tmp_path):
    """--dry-run reads and matches for real, so its counts are the ones a real run
    would produce (§6)."""
    conn = FakeConn()
    b, _ = import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    assert b.matched_by_phone == 1


def test_a_real_run_writes_in_the_documented_order(tmp_path):
    """§6.7: chats first (messages and members reference them), then handles,
    members, messages, the recomputes, and the audit row."""
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=False)
    order = [
        next(i for i, c in enumerate(conn.calls) if marker in c)
        for marker in (
            "INSERT INTO whatsapp_chats",
            "INSERT INTO whatsapp_handles",
            "INSERT INTO whatsapp_chat_members",
            "INSERT INTO whatsapp_messages",
            "UPDATE whatsapp_chats",
            "UPDATE whatsapp_handles",
            "INSERT INTO whatsapp_imports",
        )
    ]
    assert order == sorted(order)
    assert conn.committed


def test_an_incremental_run_never_deletes(tmp_path):
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=False, dry_run=False)
    joined = " ".join(conn.calls)
    assert "DELETE FROM whatsapp_messages" not in joined
    assert "CREATE TEMP TABLE" not in joined


def test_a_full_run_reconciles_deletions(tmp_path):
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=False)
    joined = " ".join(conn.calls)
    assert "DELETE FROM whatsapp_messages" in joined
    assert "DELETE FROM whatsapp_chats" in joined


def test_the_summary_prints_counts_not_content(tmp_path):
    """§6.8: counts only — the repo is public and this lands in terminals."""
    conn = FakeConn()
    b, deleted = import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    out = import_whatsapp.summary(b, dry_run=True, deleted=deleted)
    assert "DRY RUN" in out
    assert "chats" in out and "handles" in out and "members" in out
    for secret in ("Alice", "Hello", "15550100001", "@s.whatsapp.net", "Soccer Carpool", "lid:"):
        assert secret not in out, secret


def test_the_summary_reports_the_watermark_and_the_dropped_rows(tmp_path):
    conn = FakeConn()
    b, _ = import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    out = import_whatsapp.summary(b, dry_run=False, deleted=0)
    # The synthetic fixture holds 14 messages (Task 4 added a group-only sender),
    # so max_pk — and the watermark — is 14, not the plan's original 13.
    assert "watermark 14" in out
    assert "senderless" in out and "duplicate" in out


def test_a_missing_store_exits_2_with_the_path_hint(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        import_whatsapp.main_with(["--db", str(tmp_path / "nope.sqlite")], lambda: FakeConn())
    assert e.value.code == 2
    assert "ChatStorage.sqlite" in capsys.readouterr().err


def test_a_schema_error_exits_2_naming_the_column(tmp_path, capsys):
    import sqlite3

    path = build_store(tmp_path)
    db = sqlite3.connect(path)
    db.executescript("ALTER TABLE ZWAMESSAGE DROP COLUMN ZSTANZAID;")
    db.commit()
    db.close()
    with pytest.raises(SystemExit) as e:
        import_whatsapp.main_with(["--db", str(path)], lambda: FakeConn())
    assert e.value.code == 2
    assert "ZSTANZAID" in capsys.readouterr().err


def test_main_with_prints_the_summary(tmp_path, capsys):
    import_whatsapp.main_with(["--db", str(build_store(tmp_path)), "--full"], lambda: FakeConn())
    out = capsys.readouterr().out
    assert "messages" in out and "watermark" in out


def test_otel_is_flushed_even_when_the_import_fails(tmp_path, monkeypatch):
    """The script is local and short-lived, so nothing is exported unless it
    flushes explicitly (§10)."""
    flushed: list[bool] = []
    monkeypatch.setattr(import_whatsapp.otel, "flush", lambda: flushed.append(True))
    monkeypatch.setattr(import_whatsapp.otel, "setup_telemetry", lambda name: None)
    with pytest.raises(SystemExit):
        import_whatsapp.main_with(["--db", str(tmp_path / "nope.sqlite")], lambda: FakeConn())
    assert flushed
