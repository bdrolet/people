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
    out = import_imessage.summary(batch, dry_run=True, deleted=0)
    assert "messages" in out and "handles" in out
    assert "Hi from Alice" not in out and "+15550100001" not in out
