import importlib.util
from pathlib import Path

import pytest

import repo.linkedin as linkedin_repo
import repo.people as people_repo
from services.linkedin_export import ExportError

spec = importlib.util.spec_from_file_location("import_linkedin", Path("scripts/import_linkedin.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin"


class FakeConn:
    """Mimics the commit-on-success / rollback-on-error context manager of both
    clients/db.py connection flavours."""

    def __init__(self):
        self.committed = self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def wired(monkeypatch):
    state = {"conns": [], "replaced": []}

    def get_conn():
        c = FakeConn()
        state["conns"].append(c)
        return c

    state["get_conn"] = get_conn
    monkeypatch.setattr(
        people_repo,
        "names_for_matching",
        lambda conn: [{"email": "alice@example.com", "display_name": "Alice Example"}],
    )
    monkeypatch.setattr(
        linkedin_repo, "replace_snapshot", lambda conn, s: state["replaced"].append(s)
    )
    return state


def test_run_matches_and_replaces(wired):
    s = mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=False)
    assert wired["replaced"] == [s]
    assert s.matched_by_email == 1
    assert wired["conns"][0].committed and not wired["conns"][0].rolled_back


def test_dry_run_writes_nothing(wired):
    s = mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=True)
    assert wired["replaced"] == []
    assert s.matched_by_email == 1  # people were still read for real match counts
    assert wired["conns"][0].rolled_back


def test_error_during_replace_rolls_back(wired, monkeypatch):
    def boom(conn, s):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(linkedin_repo, "replace_snapshot", boom)
    with pytest.raises(RuntimeError):
        mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=False)
    assert wired["conns"][0].rolled_back


def test_bad_export_never_opens_db(wired, tmp_path):
    (tmp_path / "Connections.csv").write_text("First Name,Last Name\nA,B\n")
    with pytest.raises(ExportError):
        mod.run(wired["get_conn"], tmp_path, me=None, dry_run=False)
    assert wired["conns"] == []


def test_summary_prints_counts_not_content(wired):
    s = mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=True)
    out = mod.summary(s, dry_run=True)
    assert out.splitlines() == [
        "DRY RUN — nothing written",
        "connections 6 (email-matched 1, name-matched 0, unmatched 5)",
        "messages 7 in 4 conversations (me=linkedin.com/in/me-example; by-url 5, by-name 1, unmatched 1, skipped 1)",
        "recommendations given 2 (linked 1), received 1 (linked 1)",
        f"snapshot_at {s.snapshot_at:%Y-%m-%dT%H:%M:%SZ}",
    ]
    assert "Coffee" not in out and "Hi there" not in out


def test_summary_reports_missing_files(wired, tmp_path):
    (tmp_path / "Connections.csv").write_bytes((FIXTURE / "Connections.csv").read_bytes())
    s = mod.run(wired["get_conn"], tmp_path, me=None, dry_run=True)
    assert mod.summary(s, dry_run=True).splitlines()[-1] == (
        "missing (treated as empty): messages.csv, Recommendations_Given.csv, Recommendations_Received.csv"
    )
