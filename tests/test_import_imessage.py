import pytest

from scripts import import_imessage
from tests.fixtures.imessage import build_chat_db


class FakeConn:
    def __init__(self):
        self.calls, self.committed, self.rolled_back = [], False, False

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))

        class C:
            def fetchone(self_inner):
                return {"max_rowid": 0}

            def fetchall(self_inner):
                return []

        return C()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def no_google(monkeypatch):
    monkeypatch.setattr(import_imessage.google_contacts, "list_phone_index", lambda: {})


def test_dry_run_writes_nothing(tmp_path, no_google):
    conn = FakeConn()
    import_imessage.run(lambda: conn, build_chat_db(tmp_path), full=True, dry_run=True)
    assert not any("INSERT INTO imessage_" in c for c in conn.calls)
    assert conn.rolled_back and not conn.committed


def test_real_run_upserts_and_records_import(tmp_path, no_google):
    conn = FakeConn()
    batch = import_imessage.run(lambda: conn, build_chat_db(tmp_path), full=True, dry_run=False)
    joined = " ".join(conn.calls)
    assert "INSERT INTO imessage_messages" in joined
    assert "INSERT INTO imessage_imports" in joined
    assert batch.max_rowid == 10


def test_missing_full_disk_access_exits_2(tmp_path, capsys, no_google):
    with pytest.raises(SystemExit) as e:
        import_imessage.main_with(["--db", str(tmp_path / "nope.db")], lambda: FakeConn())
    assert e.value.code == 2
    assert "Full Disk Access" in capsys.readouterr().err


def test_summary_prints_counts_not_content(tmp_path, no_google):
    batch = import_imessage.run(
        lambda: FakeConn(), build_chat_db(tmp_path), full=True, dry_run=True
    )
    out = import_imessage.summary(batch, dry_run=True, deleted=0, watermark_before=5)
    assert "messages" in out and "handles" in out
    assert "Hi from Alice" not in out and "+15550100001" not in out


def test_summary_shows_watermark_transition():
    batch = import_imessage.IMessageBatch(mode="incremental", max_rowid=1_296_518)
    out = import_imessage.summary(batch, dry_run=False, deleted=0, watermark_before=1_284_110)
    assert "1,284,110 → 1,296,518" in out


def test_incremental_run_never_deletes(tmp_path, no_google):
    conn = FakeConn()
    import_imessage.run(lambda: conn, build_chat_db(tmp_path), full=False, dry_run=False)
    joined = " ".join(conn.calls)
    assert "DELETE FROM imessage_messages" not in joined
    assert "DELETE FROM imessage_chats" not in joined
    assert "CREATE TEMP TABLE" not in joined


class RaisingConn:
    """Simulates a transient DB failure on a reporting-only re-read, after the
    real write transaction (via a separate FakeConn) already committed."""

    def execute(self, sql, params=None):
        raise RuntimeError("connection reset")

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_reporting_failure_after_commit_does_not_crash(tmp_path, capsys, no_google):
    write_conn = FakeConn()
    calls = {"n": 0}

    def get_conn():
        calls["n"] += 1
        # First two connections (summary's watermark-before read, then run()'s
        # single write-transaction connection for a --full run) succeed; the
        # third — the post-commit latest_import() re-read — fails.
        return write_conn if calls["n"] <= 2 else RaisingConn()

    import_imessage.main_with(["--db", str(build_chat_db(tmp_path)), "--full"], get_conn)
    out, err = capsys.readouterr()
    assert "messages" in out
    assert "could not be read back" in err
    assert "deleted 0" in out
