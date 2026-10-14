import io
from datetime import date

import pytest

from scripts import outreach_sessions as cli

JONAH = {"id": 388, "email": "jonah@example.com", "display_name": "Jonah Example"}
ANA = {"id": 41, "email": "ana@example.com", "display_name": "Ana Example"}
TODAY = date(2026, 10, 13)


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def run(monkeypatch, tmp_path):
    state_path = tmp_path / "sessions.json"
    spawned = []
    people = {"jonah@example.com": JONAH, "388": JONAH, "ana@example.com": ANA}

    def resolve(conn, ident):
        if ident not in people:
            raise cli.person_lookup.NotFound(ident)
        return people[ident]

    monkeypatch.setattr(cli.person_lookup, "resolve", resolve)
    monkeypatch.setattr(
        cli.outreach_session,
        "snapshot",
        lambda conn, pid, limit: None if pid == 41 else {"pid": pid, "limit": limit},
    )
    monkeypatch.setattr(
        cli.outreach_session, "context_block", lambda snap, today: f"CONTEXT {snap['pid']}"
    )

    def spawn(name, context, *, cwd, prompt=None):
        spawned.append({"name": name, "context": context, "prompt": prompt, "cwd": cwd})
        return f"id{len(spawned):06d}"

    monkeypatch.setattr(cli.claude_sessions, "spawn", spawn)

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(
            list(argv), lambda: Conn(), out=out, err=err, today=TODAY, state_path=state_path
        )
        return code, out.getvalue(), err.getvalue()

    _run.spawned, _run.live, _run.state_path = spawned, set(), state_path
    monkeypatch.setattr(cli.claude_sessions, "live_session_ids", lambda cwd: _run.live)
    return _run


def test_dry_run_spawns_nothing(run):
    code, out, err = run("jonah@example.com")
    assert code == 0 and "388" in out and "spawn" in out and run.spawned == []
    assert "dry run" in err


def test_spawn_preloads_context_and_starts_the_prep(run):
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "spawned id000001" in out
    [s] = run.spawned
    assert s["name"] == "outreach: Jonah Example" and s["context"] == "CONTEXT 388"
    assert s["prompt"] == cli.outreach_session.PREP_PROMPT and s["cwd"] == cli.REPO
    assert '"388": "id000001"' in run.state_path.read_text()


def test_idle_spawns_without_a_prompt(run):
    run("388", "--spawn", "--idle")
    assert run.spawned[0]["prompt"] is None


def test_a_live_session_is_not_duplicated(run):
    run("388", "--spawn")
    run.live = {"id000001"}
    code, out, _ = run("388", "--spawn")
    assert "already open id000001" in out and len(run.spawned) == 1


def test_a_gone_session_frees_the_person(run):
    run("388", "--spawn")
    run.live = set()
    run("388", "--spawn")
    assert len(run.spawned) == 2


def test_someone_outside_the_pipeline_is_skipped(run):
    code, out, _ = run("ana@example.com", "--spawn")
    assert code == 0 and "not in the pipeline" in out and run.spawned == []


def test_unknown_person_is_a_user_error(run):
    code, _, err = run("nobody@example.com", "--spawn")
    assert code == 1 and "no person matches nobody@example.com" in err and run.spawned == []


def test_next_takes_the_queue_in_rank_order(run, monkeypatch):
    seen = {}

    def queue(conn, *, stage="to_contact", limit=50):
        seen.update(stage=stage, limit=limit)
        return [{"person": JONAH}]

    monkeypatch.setattr(cli.outreach, "queue", queue)
    code, out, _ = run("--next", "3", "--spawn")
    assert code == 0 and seen == {"stage": "to_contact", "limit": 3} and len(run.spawned) == 1


def test_people_and_next_are_exclusive(run):
    code, _, err = run("388", "--next", "2")
    assert code == 1 and "--next" in err


def test_the_cap_is_enforced(run):
    code, _, err = run("--next", "11")
    assert code == 1 and str(cli.MAX_PEOPLE) in err


def test_print_context(run):
    code, out, _ = run("--print-context", "388")
    assert code == 0 and out.strip() == "CONTEXT 388" and run.spawned == []


def test_spawn_failure_is_reported(run, monkeypatch):
    monkeypatch.setattr(cli.claude_sessions, "spawn", lambda *a, **k: None)
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "spawn failed" in out
