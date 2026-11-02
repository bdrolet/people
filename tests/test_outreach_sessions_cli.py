import io
import json
import subprocess
from datetime import date

import pytest

from scripts import outreach_sessions as cli

JONAH = {"id": 388, "email": "jonah@example.com", "display_name": "Jonah Example"}
ANA = {"id": 41, "email": "ana@example.com", "display_name": "Ana Example"}
TODAY = date(2026, 10, 13)
SID = "ab12cd34-0000-4000-8000-000000000001"
DECK_ID = "f14db301-1793648803"
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


def _done(argv, stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeDeckSessions:
    """Records each deck-sessions call and answers with canned results.

    `results` maps a people id (str) to the result `ensure` returns; anyone not listed
    comes back `launched` (`would launch` without --apply)."""

    def __init__(self):
        self.results: dict[str, dict] = {}
        self.listed: list[dict] = []
        self.exit_code = 0
        self.stderr = ""
        self.calls: list[tuple[list[str], str | None]] = []

    def specs(self):
        stdin = next(s for a, s in self.calls if a[0] == "ensure")
        return [json.loads(line) for line in stdin.splitlines()]

    def verbs(self):
        return [a[0] for a, _ in self.calls]

    def __call__(self, argv, stdin=None):
        self.calls.append((argv, stdin))
        if self.exit_code:
            return _done(argv, code=self.exit_code, stderr=self.stderr)
        if argv[0] == "ensure":
            apply = "--apply" in argv
            out = []
            for line in stdin.splitlines():
                key = json.loads(line)["key"]
                default = (
                    {"outcome": "launched", "deck_id": DECK_ID, "status": "starting"}
                    if apply
                    else {"outcome": "would launch"}
                )
                base = {
                    "key": key,
                    "detail": None,
                    "deck_id": None,
                    "status": None,
                    "group": None,
                    "adopted": False,
                }
                out.append(json.dumps(base | self.results.get(key, default)))
            return _done(argv, "\n".join(out) + "\n", stderr=self.stderr)
        if argv[0] == "sessions":
            rows = self.listed
            if "--group" in argv:
                group = argv[argv.index("--group") + 1]
                rows = [r for r in rows if r["group"] == group]
            return _done(argv, json.dumps(rows))
        if argv[0] == "archive":
            apply = "--apply" in argv
            out = []
            for sid in (a for a in argv[1:] if a != "--apply"):
                status = next(r["status"] for r in self.listed if r["deck_id"] == sid)
                outcome = (
                    "kept (busy)"
                    if status == "running"
                    else "archived"
                    if apply
                    else "would archive"
                )
                out.append(json.dumps({"deck_id": sid, "outcome": outcome, "detail": None}))
            return _done(argv, "\n".join(out) + "\n")
        raise AssertionError(argv)


def _session(key, title=None, deck_id=DECK_ID, status="waiting", group="outreach", archived=False):
    return {
        "deck_id": deck_id,
        "key": str(key),
        "title": title or f"outreach: x #{key}",
        "group": group,
        "status": status,
        "archived": archived,
    }


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.delenv("OUTREACH_SESSION_MODEL", raising=False)
    people = {"jonah@example.com": JONAH, "388": JONAH, "ana@example.com": ANA, "41": ANA}
    named = []
    deck = FakeDeckSessions()
    fetched = []

    def resolve(conn, ident):
        if ident not in people:
            raise cli.person_lookup.NotFound(ident)
        return people[ident]

    def snapshot(conn, pid, limit):
        if pid == 41:
            return None
        return {"pid": pid, "limit": limit, "item": {"doc_url": _run.doc_urls.get(pid)}}

    def people_named(conn, text):
        named.append(text)
        return _run.named_result

    def get_doc(doc_id):
        fetched.append(doc_id)
        return {"content": f"DOC {doc_id}"}

    monkeypatch.setattr(cli.person_lookup, "resolve", resolve)
    monkeypatch.setattr(cli.outreach_session, "snapshot", snapshot)
    monkeypatch.setattr(
        cli.outreach_session,
        "context_block",
        lambda snap, today, doc=None: f"CONTEXT {snap['pid']}" + (f" + {doc}" if doc else ""),
    )
    monkeypatch.setattr(cli.outreach, "people_named", people_named)
    monkeypatch.setattr(cli.docs_api, "get_doc", get_doc)
    monkeypatch.setattr(cli.outreach_repo, "pipeline_row", lambda conn, pid: _run.stages.get(pid))

    def _run(*argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(
            list(argv),
            lambda: Conn(),
            out=out,
            err=err,
            today=TODAY,
            deck=deck,
            runner=_run.runner,
            env=env or {},
            state_path=tmp_path / "sessions.json",
            projects_dir=tmp_path / "projects",
        )
        return code, out.getvalue(), err.getvalue()

    _run.deck, _run.named, _run.fetched = deck, named, fetched
    _run.doc_urls, _run.named_result, _run.stages = {}, [], {}
    _run.runner = lambda argv, stdin=None: pytest.fail(f"unexpected {argv}")
    return _run


# --- the spec each person becomes ----------------------------------------


def test_dry_run_launches_nothing(run):
    code, out, err = run("jonah@example.com")
    assert code == 0 and "388\toutreach: Jonah Example #388\t→ would launch" in out
    assert run.deck.calls[0][0] == ["ensure"] and "dry run" in err
    assert run.fetched == []  # no doc fetch for a dry run


def test_one_spec_per_person(run):
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "→ launched f14db301" in out
    assert run.deck.calls[0][0] == ["ensure", "--apply"]
    assert run.deck.specs() == [
        {
            "key_hint": "person_id",
            "key": "388",
            "title": "outreach: Jonah Example #388",
            "adopt_prefix": None,
            "context": "CONTEXT 388",
            "cwd": str(cli.REPO),
            "group": "outreach",
            "parent": "conductor-outreach",
            "parent_is_default": True,
            "hints": {},
            "model": "opus",
            "idle_timeout": None,
            "prompt": cli.outreach_session.PREP_PROMPT,
            "reopen_prompt": cli.outreach_session.RELOAD_PROMPT,
            "refresh": False,
        }
    ]


def test_a_person_with_a_doc_launches_on_reload_with_the_doc(run):
    run.doc_urls[388] = DOC
    run("388", "--spawn")
    [spec] = run.deck.specs()
    assert spec["context"] == "CONTEXT 388 + DOC doc388"
    assert spec["prompt"] == cli.outreach_session.RELOAD_PROMPT


def test_an_unavailable_doc_does_not_block_the_launch(run, monkeypatch):
    run.doc_urls[388] = DOC

    def unavailable(doc_id):
        raise cli.docs_api.DocsApiUnavailable("DOCS_API_URL unset")

    monkeypatch.setattr(cli.docs_api, "get_doc", unavailable)
    code, out, _ = run("388", "--spawn")
    assert "launched f14db301 (doc unavailable: DocsApiUnavailable)" in out
    [spec] = run.deck.specs()
    assert spec["context"] == "CONTEXT 388"
    assert spec["prompt"] == cli.outreach_session.RELOAD_PROMPT


def test_idle_sends_no_prompts(run):
    run("388", "--spawn", "--idle")
    [spec] = run.deck.specs()
    assert spec["prompt"] is None and spec["reopen_prompt"] is None


def test_sessions_run_on_opus_unless_configured(run):
    run("388")
    run("388", env={"OUTREACH_SESSION_MODEL": "sonnet"})
    models = [json.loads(s)["model"] for a, s in run.deck.calls if a[0] == "ensure"]
    assert models == ["opus", "sonnet"]


def test_group_and_parent_precedence(run):
    env = {"OUTREACH_SESSIONS_GROUP": "g-env", "OUTREACH_SESSIONS_PARENT": "p-env"}
    run("388", env=env)
    run("388", "--group", "g-flag", "--parent", "none", env=env)
    first, second = (json.loads(s) for a, s in run.deck.calls if a[0] == "ensure")
    assert (first["group"], first["parent"], first["parent_is_default"]) == (
        "g-env",
        "p-env",
        False,
    )
    assert (second["group"], second["parent"]) == ("g-flag", "none")


def test_parent_self(run):
    run("388", "--parent", "self", env={"AGENTDECK_INSTANCE_ID": "781a5faa-1"})
    assert run.deck.specs()[0]["parent"] == "781a5faa-1"
    code, _, err = run("388", "--parent", "self")
    assert code == 1 and "inside an agent-deck session" in err


def test_a_missing_explicit_parent_is_an_error(run):
    run.deck.results["388"] = {"outcome": "failed", "detail": "parent bob not found in agent-deck"}
    code, out, err = run("388", "--spawn", "--parent", "bob")
    assert code == 1 and "parent bob not found" in err and out == ""


def test_deck_sessions_warnings_are_forwarded(run):
    run.deck.stderr = "warning: parent conductor-outreach not found; launching without a parent\n"
    _, _, err = run("388", "--spawn")
    assert "conductor-outreach not found" in err


def test_the_same_person_named_twice_launches_once(run):
    code, out, _ = run("388", "jonah@example.com", "--spawn")
    assert code == 0 and len(run.deck.specs()) == 1
    assert len([line for line in out.splitlines() if line.startswith("388\t")]) == 1


def test_someone_outside_the_pipeline_is_never_sent(run):
    code, out, _ = run("ana@example.com", "388", "--spawn")
    assert code == 0 and "41\toutreach: Ana Example #41\t→ not in the pipeline" in out
    assert [s["key"] for s in run.deck.specs()] == ["388"]


def test_nobody_to_send_skips_deck_sessions(run):
    code, out, _ = run("ana@example.com", "--spawn")
    assert code == 0 and "not in the pipeline" in out and run.deck.calls == []


# --- outcome mapping -----------------------------------------------------

OUTCOMES = [
    (
        {"outcome": "already open", "deck_id": DECK_ID, "status": "waiting"},
        "already open f14db301 (waiting)",
    ),
    ({"outcome": "already open", "detail": "exists"}, "already open (exists)"),
    ({"outcome": "reopened", "deck_id": DECK_ID, "status": "stopped"}, "reopened f14db301"),
    ({"outcome": "duplicate", "detail": "11223344-9,55667788-9"}, "duplicate 11223344,55667788"),
    ({"outcome": "failed", "detail": "tmux exploded"}, "failed: tmux exploded"),
    ({"outcome": "would reopen", "deck_id": DECK_ID}, "would reopen f14db301"),
]


@pytest.mark.parametrize(("result", "text"), OUTCOMES)
def test_outcomes_map_to_the_familiar_words(run, result, text):
    run.deck.results["388"] = result
    _, out, _ = run("388", "--spawn")
    assert out.rstrip().endswith(f"→ {text}")


def test_spawn_closes_with_agent_deck_hints(run):
    _, _, err = run("388", "--spawn")
    assert "agent-deck session attach <id>" in err and "agent-deck -g outreach" in err


# --- names ---------------------------------------------------------------


def test_a_name_matches_a_session_title_first(run):
    run.deck.listed = [_session(388, "outreach: Jonah Example #388")]
    code, out, _ = run("JONAH")
    assert code == 0 and out.startswith("388\t") and run.named == []


def test_the_id_suffix_is_not_part_of_the_name(run):
    run.deck.listed = [_session(388, "outreach: Jonah Example #388")]
    run.named_result = []
    code, _, err = run("388x")
    assert code == 1 and "no person matches 388x" in err


def test_a_name_falls_back_to_pipeline_people(run):
    run.named_result = [{"id": 388, "display_name": "Jonah Example"}]
    code, out, _ = run("jonah ex")
    assert code == 0 and out.startswith("388\t") and run.named == ["jonah ex"]


def test_an_ambiguous_session_name_stops_with_the_ids(run):
    run.deck.listed = [_session(388, "outreach: Jo A #388"), _session(41, "outreach: Jo B #41")]
    code, _, err = run("jo", "--spawn")
    assert code == 1 and "several sessions ([41, 388])" in err


def test_an_ambiguous_pipeline_name_stops_with_the_ids(run):
    run.named_result = [{"id": 388, "display_name": "Jo A"}, {"id": 41, "display_name": "Jo B"}]
    code, _, err = run("jo", "--spawn")
    assert code == 1 and "[388, 41]" in err


def test_an_id_never_lists_sessions(run):
    run("388")
    assert "sessions" not in run.deck.verbs()


def test_unknown_person_is_a_user_error(run):
    code, _, err = run("nobody@example.com", "--spawn")
    assert code == 1 and "no person matches nobody@example.com" in err


# --- selection -----------------------------------------------------------


def test_next_takes_the_queue_in_rank_order(run, monkeypatch):
    seen = {}

    def queue(conn, *, stage="to_contact", limit=50):
        seen.update(stage=stage, limit=limit)
        return [{"person": JONAH}]

    monkeypatch.setattr(cli.outreach, "queue", queue)
    code, out, _ = run("--next", "3", "--spawn")
    assert code == 0 and seen == {"stage": "to_contact", "limit": 3}
    assert len(run.deck.specs()) == 1


def test_people_and_next_are_exclusive(run):
    code, _, err = run("388", "--next", "2")
    assert code == 1 and "--next" in err


def test_the_cap_is_enforced(run):
    code, _, err = run("--next", "11")
    assert code == 1 and str(cli.MAX_PEOPLE) in err


def test_print_context(run):
    code, out, _ = run("--print-context", "388")
    assert code == 0 and out.strip() == "CONTEXT 388" and run.deck.calls == []


def test_print_context_includes_the_doc(run):
    run.doc_urls[388] = DOC
    code, out, _ = run("--print-context", "388")
    assert code == 0 and out.strip() == "CONTEXT 388 + DOC doc388"


# --- failures ------------------------------------------------------------


def test_registry_unavailable_exits_2(run):
    run.deck.exit_code = 2
    run.deck.stderr = '{"error": "registry unavailable: tmux down"}\n'
    code, out, err = run("388", "--spawn")
    assert code == 2 and "registry unavailable: tmux down — nothing launched" in err


def test_a_missing_deck_sessions_binary_exits_2(run):
    def missing(argv, stdin=None):
        raise FileNotFoundError("deck-sessions")

    out, err = io.StringIO(), io.StringIO()
    code = cli.main_with(
        ["388", "--spawn"], lambda: Conn(), out=out, err=err, today=TODAY, deck=missing, env={}
    )
    assert code == 2 and "deck-sessions not on PATH" in err.getvalue()


# --- prune ---------------------------------------------------------------


def test_prune_archives_people_at_a_terminal_stage(run):
    run.deck.listed = [
        _session(388, deck_id="aaaa0000-1", status="idle"),
        _session(41, deck_id="bbbb0000-1", status="idle"),
        _session(7, deck_id="cccc0000-1", status="idle"),
        _session(8, deck_id="dddd0000-1", status="stopped", archived=True),
        _session(9, deck_id="eeee0000-1", status="idle"),
    ]
    run.stages = {
        388: {"stage": "introduced"},
        41: {"stage": "briefed"},
        7: {"stage": "declined"},
        8: {"stage": "skipped"},
    }  # 9 left the pipeline
    code, out, _ = run("--prune", "--spawn")
    assert code == 0
    assert run.deck.calls[0][0] == ["sessions", "--key-hint", "person_id", "--group", "outreach"]
    assert run.deck.calls[1][0] == ["archive", "--apply", "aaaa0000-1", "cccc0000-1"]
    assert "388\toutreach: x #388\t→ pruned aaaa0000 (introduced)" in out
    assert "7\toutreach: x #7\t→ pruned cccc0000 (declined)" in out


def test_every_terminal_stage_counts(run):
    assert {"introduced", "declined", "skipped", "signed", "lost"} == cli.TERMINAL


def test_prune_dry_run_and_busy(run):
    run.deck.listed = [
        _session(388, deck_id="aaaa0000-1", status="running"),
        _session(7, deck_id="cccc0000-1", status="idle"),
    ]
    run.stages = {388: {"stage": "skipped"}, 7: {"stage": "lost"}}
    _, out, _ = run("--prune")
    assert run.deck.calls[1][0] == ["archive", "aaaa0000-1", "cccc0000-1"]
    assert "→ kept (busy) aaaa0000" in out and "→ would prune cccc0000" in out


def test_prune_with_nothing_terminal_archives_nothing(run):
    run.deck.listed = [_session(388, status="idle")]
    run.stages = {388: {"stage": "briefed"}}
    run("--prune", "--spawn")
    assert run.deck.verbs() == ["sessions"]


# --- migrate -------------------------------------------------------------


class FakeShell:
    """`claude agents --json`, `claude stop`, `agent-deck list/add` for --migrate."""

    def __init__(self, agents=None, deck_rows=None):
        self.agents = agents or []
        self.deck_rows = (
            deck_rows
            if deck_rows is not None
            else [{"id": "c0nd0000-1", "title": "conductor-outreach"}]
        )
        self.calls = []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if argv[:3] == ["claude", "agents", "--json"]:
            return _done(argv, json.dumps(self.agents))
        if argv[:2] == ["claude", "stop"]:
            return _done(argv, "stopped")
        if argv[:3] == ["agent-deck", "list", "--json"]:
            return _done(argv, json.dumps(self.deck_rows))
        if argv[:2] == ["agent-deck", "add"]:
            return _done(argv, json.dumps({"success": True, "id": f"add{len(self.calls):05d}-1"}))
        if argv[:3] == ["agent-deck", "session", "archive"]:
            return _done(argv, "archived")
        raise AssertionError(argv)

    def adds(self):
        return [a for a in self.calls if a[:2] == ["agent-deck", "add"]]


@pytest.fixture
def migrating(run, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONTEXT_DIR", tmp_path / "ctx")
    worktree = tmp_path / "people"
    worktree.mkdir()
    project = tmp_path / "projects" / "-Users-ben-src-people"
    project.mkdir(parents=True)
    records = {}
    for pid in (388, 7, 9, 12):
        sid = f"{pid:08d}-0000-4000-8000-000000000000"
        records[str(pid)] = {"session_id": sid, "cwd": str(worktree), "name": f"outreach: {pid}"}
        if pid != 12:  # 12's transcript is gone
            (project / f"{sid}.jsonl").write_text(json.dumps({"cwd": str(worktree)}) + "\n")
    (tmp_path / "sessions.json").write_text(json.dumps(records))
    run.stages = {
        388: {"id": 388, "display_name": "Jonah Example", "stage": "briefed"},
        7: {"id": 7, "display_name": "Sev", "stage": "replied"},
        12: {"id": 12, "display_name": "Twelve", "stage": "briefed"},
    }  # 9 left
    run.shell = FakeShell()
    run.runner = run.shell
    run.worktree, run.state = worktree, tmp_path / "sessions.json"
    return run


def test_migrate_dry_run_plans_every_record(migrating):
    migrating.shell.agents = [
        {"id": "00000007", "sessionId": "00000007-0000-4000-8000-000000000000", "status": "idle"}
    ]
    code, out, err = migrating("--migrate")
    assert code == 0 and "dry run" in err
    lines = out.splitlines()
    assert len(lines) == 4
    assert "7\toutreach: Sev #7\t→ would stop claude --bg + adopt 00000007" in lines
    assert "9\toutreach: 9\t→ skipped (left the pipeline)" in lines
    assert "12\toutreach: Twelve #12\t→ skipped (transcript gone)" in lines
    assert "388\toutreach: Jonah Example #388\t→ would adopt 00000388" in lines
    assert migrating.shell.adds() == [] and migrating.state.exists()


def test_migrate_apply_adopts_without_starting_and_retires_the_file(migrating):
    migrating.shell.agents = [
        {"id": "00000007", "sessionId": "00000007-0000-4000-8000-000000000000", "status": "idle"}
    ]
    code, out, err = migrating("--migrate", "--apply")
    assert code == 0
    stops = [a for a in migrating.shell.calls if a[:2] == ["claude", "stop"]]
    assert stops == [["claude", "stop", "00000007"]]
    adds = migrating.shell.adds()
    assert len(adds) == 2
    argv = next(a for a in adds if "person_id=388" in a)
    assert argv[argv.index("--resume-session") + 1] == "00000388-0000-4000-8000-000000000000"
    assert argv[argv.index("--parent") + 1] == "conductor-outreach"
    assert argv[argv.index("-g") + 1] == "outreach"
    assert argv[argv.index("-t") + 1] == "outreach: Jonah Example #388"
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--no-identity" in argv and argv[-1] == str(migrating.worktree)
    ctx = cli.CONTEXT_DIR / "388.md"
    assert f"--extra-arg={ctx}" in argv and ctx.read_text() == "CONTEXT 388"
    assert (ctx.stat().st_mode & 0o777) == 0o600
    joined = [" ".join(a) for a in migrating.shell.calls]
    assert not any(" launch " in c or " start " in c for c in joined)
    archives = [a for a in migrating.shell.calls if a[:3] == ["agent-deck", "session", "archive"]]
    assert (
        len(archives) == 2
    )  # never-started sessions read `error`; archived, no conductor wakes them
    assert "(archived)" in out
    assert "(stopped claude --bg)" in out
    assert not migrating.state.exists()
    assert migrating.state.with_name("sessions.json.migrated").exists()


def test_migrate_skips_people_already_in_agent_deck(migrating):
    migrating.deck.listed = [_session(388, deck_id="aaaa0000-1")]
    _, out, _ = migrating("--migrate", "--apply")
    assert "388\toutreach: Jonah Example #388\t→ already in agent-deck aaaa0000" in out
    assert not any("person_id=388" in a for a in migrating.shell.adds())


def test_migrate_leaves_a_busy_bg_session_alone(migrating):
    migrating.shell.agents = [
        {"id": "00000388", "sessionId": "00000388-0000-4000-8000-000000000000", "status": "busy"}
    ]
    _, out, err = migrating("--migrate", "--apply")
    assert "busy in claude --bg 00000388" in out
    assert not any(a[:2] == ["claude", "stop"] for a in migrating.shell.calls)


def test_migrate_keeps_the_file_when_an_add_fails(migrating, monkeypatch):
    real = migrating.shell.__call__

    def failing(argv, stdin=None):
        if argv[:2] == ["agent-deck", "add"]:
            migrating.shell.calls.append(argv)
            return _done(argv, json.dumps({"success": False, "error": "nope"}), 1)
        return real(argv, stdin)

    migrating.runner = failing
    code, out, err = migrating("--migrate", "--apply")
    assert "failed: nope" in out and "kept" in err and migrating.state.exists()


def test_migrate_without_the_conductor_adopts_unparented(migrating):
    migrating.shell.deck_rows = []
    _, _, err = migrating("--migrate", "--apply")
    assert "conductor-outreach not found" in err
    assert all("--no-parent" in a for a in migrating.shell.adds())


def test_migrate_with_no_state_file(run):
    code, _, err = run("--migrate")
    assert code == 0 and "nothing to migrate" in err


# --- write-doc -----------------------------------------------------------


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
    assert "388\toutreach: Jonah Example #388\t→ created" in out


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
