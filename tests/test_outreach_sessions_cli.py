import io
import json
from datetime import date

import pytest

from scripts import outreach_sessions as cli

JONAH = {"id": 388, "email": "jonah@example.com", "display_name": "Jonah Example"}
ANA = {"id": 41, "email": "ana@example.com", "display_name": "Ana Example"}
TODAY = date(2026, 10, 13)
SID = "ab12cd34-0000-4000-8000-000000000001"
DOC = "https://docs.google.com/document/d/doc388/edit"
SECTIONS = """## Who they are

Director at a health-tech startup.

## History

Worked together in 2019.

## Log

- 2026-10-14 · prepped brief (iMessage)

## Talking points (next step: brief)

- ask about the new role
"""


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def run(monkeypatch, tmp_path):
    state_path = tmp_path / "sessions.json"
    projects = tmp_path / "projects"
    projects.mkdir()
    people = {"jonah@example.com": JONAH, "388": JONAH, "ana@example.com": ANA, "41": ANA}
    spawned, resumed, named = [], [], []

    def resolve(conn, ident):
        if ident not in people:
            raise cli.person_lookup.NotFound(ident)
        return people[ident]

    def snapshot(conn, pid, limit):
        if pid == 41:
            return None
        return {"pid": pid, "limit": limit, "item": {"doc_url": _run.doc_urls.get(pid)}}

    def spawn(name, context, *, cwd, prompt=None):
        spawned.append({"name": name, "context": context, "prompt": prompt, "cwd": cwd})
        short = f"id{len(spawned):06d}"
        _run.live[f"{short}-0000-4000-8000-000000000000"] = str(cwd)
        return short

    def resume(session_id, name, *, cwd, prompt=None):
        resumed.append({"session_id": session_id, "name": name, "cwd": cwd, "prompt": prompt})
        if _run.resume_as:  # a stopped session comes back under a new id
            _run.live[_run.resume_as] = str(cwd)
            return _run.resume_as[:8]
        _run.live[session_id] = str(cwd)
        return session_id[:8]

    def people_named(conn, text):
        named.append(text)
        return _run.named_result

    monkeypatch.setattr(cli.person_lookup, "resolve", resolve)
    monkeypatch.setattr(cli.outreach_session, "snapshot", snapshot)
    monkeypatch.setattr(
        cli.outreach_session,
        "context_block",
        lambda snap, today, doc=None: f"CONTEXT {snap['pid']}" + (f" + {doc}" if doc else ""),
    )
    monkeypatch.setattr(cli.claude_sessions, "spawn", spawn)
    monkeypatch.setattr(cli.claude_sessions, "resume", resume)
    monkeypatch.setattr(cli.claude_sessions, "live_sessions", lambda cwd: dict(_run.live))
    monkeypatch.setattr(cli.claude_sessions, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cli.outreach, "people_named", people_named)
    monkeypatch.setattr(cli.docs_api, "get_doc", lambda doc_id: {"content": f"DOC {doc_id}"})

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(
            list(argv), lambda: Conn(), out=out, err=err, today=TODAY, state_path=state_path
        )
        return code, out.getvalue(), err.getvalue()

    _run.spawned, _run.resumed, _run.named = spawned, resumed, named
    _run.live, _run.doc_urls, _run.named_result = {}, {}, []
    _run.resume_as = None
    _run.state_path, _run.projects = state_path, projects
    return _run


def _record(run, record):
    run.state_path.write_text(json.dumps({"388": record}))


def _transcript(run, session_id, cwd):
    project = run.projects / "-Users-me-src-repo--claude-worktrees-w"
    project.mkdir(exist_ok=True)
    (project / f"{session_id}.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")


def test_dry_run_spawns_nothing(run):
    code, out, err = run("jonah@example.com")
    assert code == 0 and "388" in out and "→ spawn" in out and run.spawned == []
    assert "dry run" in err and not run.state_path.exists()


def test_spawn_preloads_context_starts_the_prep_and_records_the_session(run):
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "spawned id000001" in out
    [s] = run.spawned
    assert s["name"] == "outreach: Jonah Example" and s["context"] == "CONTEXT 388"
    assert s["prompt"] == cli.outreach_session.PREP_PROMPT and s["cwd"] == cli.REPO
    assert json.loads(run.state_path.read_text())["388"] == {
        "session_id": "id000001-0000-4000-8000-000000000000",
        "cwd": str(cli.REPO),
        "name": "outreach: Jonah Example",
    }


def test_the_same_person_named_twice_spawns_once(run):
    code, out, _ = run("388", "jonah@example.com", "--spawn")
    assert code == 0 and len(run.spawned) == 1
    assert len([line for line in out.splitlines() if line.startswith("388\t")]) == 1


def test_idle_spawns_without_a_prompt(run):
    run("388", "--spawn", "--idle")
    assert run.spawned[0]["prompt"] is None


def test_a_live_session_is_not_duplicated(run):
    run("388", "--spawn")
    code, out, _ = run("388", "--spawn")
    assert "already open id000001" in out and len(run.spawned) == 1 and run.resumed == []


def test_a_closed_session_is_resumed_from_its_transcript_cwd(run, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _record(run, {"session_id": SID, "cwd": str(cli.REPO), "name": "outreach: Jonah Example"})
    _transcript(run, SID, worktree)
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "resumed ab12cd34" in out and run.spawned == []
    assert run.resumed == [
        {
            "session_id": SID,
            "name": "outreach: Jonah Example",
            "cwd": worktree,
            "prompt": cli.outreach_session.RELOAD_PROMPT,
        }
    ]
    assert json.loads(run.state_path.read_text())["388"]["cwd"] == str(worktree)


def test_dry_run_says_resume(run, tmp_path):
    _record(run, {"session_id": SID})
    _transcript(run, SID, tmp_path)
    code, out, _ = run("388")
    assert "→ resume" in out and run.resumed == []


def test_idle_resume_has_no_prompt(run, tmp_path):
    _record(run, {"session_id": SID})
    _transcript(run, SID, tmp_path)
    run("388", "--spawn", "--idle")
    assert run.resumed[0]["prompt"] is None


def test_a_legacy_short_id_is_resumed_and_upgraded(run, tmp_path):
    run.state_path.write_text(json.dumps({"388": "ab12cd34"}))
    _transcript(run, SID, tmp_path)
    code, out, _ = run("388", "--spawn")
    assert "resumed ab12cd34" in out
    assert json.loads(run.state_path.read_text())["388"] == {
        "session_id": SID,
        "cwd": str(tmp_path),
        "name": "outreach: Jonah Example",
    }


def test_a_live_legacy_entry_is_already_open(run):
    run.state_path.write_text(json.dumps({"388": "ab12cd34"}))
    run.live = {SID: "/somewhere"}
    code, out, _ = run("388", "--spawn")
    assert "already open ab12cd34" in out and run.spawned == [] and run.resumed == []


def test_a_closed_session_without_a_transcript_gets_a_fresh_spawn(run):
    _record(run, {"session_id": SID})
    code, out, _ = run("388", "--spawn")
    assert "spawned id000001" in out and run.resumed == []


def test_a_transcript_whose_directory_is_gone_gets_a_fresh_spawn(run, tmp_path):
    _record(run, {"session_id": SID})
    _transcript(run, SID, tmp_path / "removed-worktree")
    code, out, _ = run("388", "--spawn")
    assert "spawned id000001" in out and run.resumed == []


def test_a_fresh_spawn_with_a_doc_carries_it_and_reloads(run):
    run.doc_urls[388] = DOC
    run("388", "--spawn")
    [s] = run.spawned
    assert s["context"] == "CONTEXT 388 + DOC doc388"
    assert s["prompt"] == cli.outreach_session.RELOAD_PROMPT


def test_an_unavailable_doc_does_not_block_the_spawn(run, monkeypatch):
    run.doc_urls[388] = DOC

    def unavailable(doc_id):
        raise cli.docs_api.DocsApiUnavailable("DOCS_API_URL unset")

    monkeypatch.setattr(cli.docs_api, "get_doc", unavailable)
    code, out, _ = run("388", "--spawn")
    assert "spawned id000001 (doc unavailable: DocsApiUnavailable)" in out
    assert run.spawned[0]["context"] == "CONTEXT 388"
    assert run.spawned[0]["prompt"] == cli.outreach_session.RELOAD_PROMPT


def test_a_name_matches_a_recorded_session_first(run):
    _record(run, {"session_id": SID, "name": "outreach: Jonah Example"})
    run.live = {SID: "/x"}
    code, out, _ = run("JONAH")
    assert code == 0 and out.startswith("388\t") and "already open ab12cd34" in out
    assert run.named == []


def test_a_name_falls_back_to_pipeline_people(run):
    run.named_result = [{"id": 388, "display_name": "Jonah Example"}]
    code, out, _ = run("jonah ex")
    assert code == 0 and out.startswith("388\t") and run.named == ["jonah ex"]


def test_an_ambiguous_name_stops_with_the_ids(run):
    run.named_result = [{"id": 388, "display_name": "Jo A"}, {"id": 41, "display_name": "Jo B"}]
    code, _, err = run("jo", "--spawn")
    assert code == 1 and "[388, 41]" in err and run.spawned == []


def test_an_unknown_name_is_not_found(run):
    code, _, err = run("zed")
    assert code == 1 and "no person matches zed" in err


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


def test_print_context_includes_the_doc(run):
    run.doc_urls[388] = DOC
    code, out, _ = run("--print-context", "388")
    assert code == 0 and out.strip() == "CONTEXT 388 + DOC doc388"


def test_spawn_failure_is_reported(run, monkeypatch):
    monkeypatch.setattr(cli.claude_sessions, "spawn", lambda *a, **k: None)
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "spawn failed" in out


def test_write_doc_parses_the_sections_and_writes(run, monkeypatch, tmp_path):
    path = tmp_path / "sections.md"
    path.write_text(SECTIONS)
    seen = {}

    def write(conn, pid, sections, *, today):
        seen.update(pid=pid, step=sections.next_step, today=today)
        return "created https://docs.google.com/document/d/new1/edit"

    monkeypatch.setattr(cli.outreach_doc_write, "write", write)
    code, out, _ = run("--write-doc", "388", "--sections", str(path))
    assert code == 0 and seen == {"pid": 388, "step": "brief", "today": TODAY}
    assert "388\toutreach: Jonah Example\t→ created" in out


def test_write_doc_refuses_bad_sections(run, tmp_path):
    path = tmp_path / "sections.md"
    path.write_text("## History\n\nonly this\n")
    code, _, err = run("--write-doc", "388", "--sections", str(path))
    assert code == 1 and "missing or empty" in err


def test_write_doc_needs_a_sections_file(run, tmp_path):
    code, _, err = run("--write-doc", "388")
    assert code == 1 and "--sections" in err
    code, _, err = run("--write-doc", "388", "--sections", str(tmp_path / "missing.md"))
    assert code == 1 and "cannot read" in err


def test_write_doc_reports_a_conflict(run, monkeypatch, tmp_path):
    path = tmp_path / "sections.md"
    path.write_text(SECTIONS)

    def write(conn, pid, sections, *, today):
        raise cli.outreach_doc_write.DocConflict("the doc changed while it was being written")

    monkeypatch.setattr(cli.outreach_doc_write, "write", write)
    code, _, err = run("--write-doc", "388", "--sections", str(path))
    assert code == 1 and "changed while it was being written" in err


def test_an_unlistable_session_list_stops_a_spawn(run, monkeypatch):
    monkeypatch.setattr(cli.claude_sessions, "live_sessions", lambda cwd: None)
    code, out, err = run("jonah@example.com", "--spawn")
    assert code == 1
    assert "can't list running Claude sessions" in err
    assert run.spawned == [] and run.resumed == [] and not run.state_path.exists()


def test_an_unlistable_session_list_warns_on_a_dry_run(run, monkeypatch):
    monkeypatch.setattr(cli.claude_sessions, "live_sessions", lambda cwd: None)
    code, out, err = run("jonah@example.com")
    assert code == 0
    assert "→ spawn" in out
    assert "warning:" in err and "claude agents --json" in err
    assert run.spawned == []


def test_state_is_saved_when_a_later_person_raises(run, monkeypatch):
    real = cli.outreach_session.snapshot

    def snapshot(conn, pid, limit):
        if pid == 41:
            raise RuntimeError("db went away")
        return real(conn, pid, limit)

    monkeypatch.setattr(cli.outreach_session, "snapshot", snapshot)
    with pytest.raises(RuntimeError):
        run("jonah@example.com", "ana@example.com", "--spawn")
    saved = json.loads(run.state_path.read_text())
    assert list(saved) == ["388"] and saved["388"]["session_id"].startswith("id000001")


def test_write_doc_reads_the_sections_from_stdin(run, monkeypatch):
    """`--sections -`: a session pipes its sections in, so it changes no file (a file it
    writes shows up in Claude Code's diff panel)."""
    seen = {}

    def write(conn, pid, sections, *, today):
        seen.update(pid=pid, step=sections.next_step)
        return "updated https://docs.google.com/document/d/doc388/edit"

    monkeypatch.setattr(cli.outreach_doc_write, "write", write)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECTIONS))
    code, out, _ = run("--write-doc", "388", "--sections", "-")
    assert code == 0 and seen == {"pid": 388, "step": "brief"} and "→ updated" in out


def test_a_resume_under_a_new_id_records_the_new_id(run, tmp_path):
    """`claude --bg --resume` of a stopped session forks a new id; recording the old one
    made the next run resume (and duplicate) a session that was already open."""
    new = "cd34ef56-0000-4000-8000-000000000002"
    _record(run, {"session_id": SID, "name": "outreach: Jonah Example"})
    _transcript(run, SID, tmp_path)
    run.resume_as = new
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "resumed cd34ef56" in out
    assert json.loads(run.state_path.read_text())["388"]["session_id"] == new
    code, out, _ = run("388", "--spawn")
    assert "already open cd34ef56" in out and len(run.resumed) == 1
