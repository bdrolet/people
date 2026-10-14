import json
import subprocess
from pathlib import Path

from clients import claude_sessions as cs

CWD = Path("/tmp/repo")


def _result(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_spawn_argv_puts_the_prompt_last():
    assert cs.spawn_argv("n", "ctx") == [
        "claude",
        "--bg",
        "-n",
        "n",
        "--append-system-prompt",
        "ctx",
    ]
    assert cs.spawn_argv("n", "ctx", "go")[-1] == "go"


def test_parse_session_id():
    out = (
        "backgrounded · e8af9b02 · outreach: Jonah (idle — send a prompt to start)\n  claude agents"
    )
    assert cs.parse_session_id(out) == "e8af9b02"
    assert cs.parse_session_id("something else") is None


def test_spawn_runs_in_cwd_and_returns_the_id():
    seen = {}

    def runner(argv, cwd):
        seen.update(argv=argv, cwd=cwd)
        return _result("backgrounded · ab12cd34 · outreach: X")

    assert cs.spawn("outreach: X", "ctx", cwd=CWD, prompt="go", runner=runner) == "ab12cd34"
    assert seen["cwd"] == CWD and seen["argv"][-1] == "go"


def test_spawn_failure_returns_none():
    assert cs.spawn("n", "ctx", cwd=CWD, runner=lambda argv, cwd: _result("", 1)) is None


SID = "ab12cd34-0000-4000-8000-000000000001"


def test_resume_argv_keeps_the_name_and_puts_the_prompt_last():
    """Without -n a resumed session is renamed after its prompt."""
    assert cs.resume_argv(SID, "outreach: X") == [
        "claude",
        "--bg",
        "--resume",
        SID,
        "-n",
        "outreach: X",
    ]
    assert cs.resume_argv(SID, "outreach: X", "go")[-1] == "go"


def test_resume_runs_in_cwd_and_keeps_the_id():
    seen = {}

    def runner(argv, cwd):
        seen.update(argv=argv, cwd=cwd)
        return _result("resumed in the background")

    assert cs.resume(SID, "outreach: X", cwd=CWD, prompt="go", runner=runner) == "ab12cd34"
    assert seen == {
        "argv": ["claude", "--bg", "--resume", SID, "-n", "outreach: X", "go"],
        "cwd": CWD,
    }
    ok = lambda argv, cwd: _result("backgrounded · ab12cd34 · outreach: X")  # noqa: E731
    assert cs.resume(SID, "n", cwd=CWD, runner=ok) == "ab12cd34"
    assert cs.resume(SID, "n", cwd=CWD, runner=lambda argv, cwd: _result("", 1)) is None


def test_live_sessions_lists_running_sessions_only():
    seen = {}
    listing = [{"sessionId": SID, "cwd": "/a"}, {"id": "ef56ab78", "cwd": "/b"}, {}]

    def runner(argv, cwd):
        seen["argv"] = argv
        return _result(json.dumps(listing))

    assert cs.live_sessions(cwd=CWD, runner=runner) == {SID: "/a", "ef56ab78": "/b"}
    assert seen["argv"] == ["claude", "agents", "--json"]  # --all would list exited sessions
    assert cs.live_sessions(cwd=CWD, runner=lambda argv, cwd: _result("[]")) == {}


def test_live_sessions_is_none_when_the_listing_cannot_be_trusted():
    assert cs.live_sessions(cwd=CWD, runner=lambda argv, cwd: _result("", 1)) is None
    assert cs.live_sessions(cwd=CWD, runner=lambda argv, cwd: _result("not json")) is None
    assert cs.live_sessions(cwd=CWD, runner=lambda argv, cwd: _result('{"a": 1}')) is None

    def boom(argv, cwd):
        raise OSError("no claude")

    assert cs.live_sessions(cwd=CWD, runner=boom) is None


def test_live_sessions_skips_items_that_are_not_objects():
    listing = [{"sessionId": SID, "cwd": "/a"}, "x", 3, None, ["y"]]
    runner = lambda argv, cwd: _result(json.dumps(listing))  # noqa: E731
    assert cs.live_sessions(cwd=CWD, runner=runner) == {SID: "/a"}


def test_find_live_matches_by_prefix_either_way():
    live = {SID: "/a", "ef56ab78": "/b"}
    assert cs.find_live("ab12cd34", live) == SID
    assert cs.find_live(SID, live) == SID
    assert cs.find_live("ef56ab78-1111-4000-8000-000000000000", live) == "ef56ab78"
    assert cs.find_live("", live) is None and cs.find_live("99999999", live) is None


def test_find_transcript_searches_every_project_dir(tmp_path):
    worktree = tmp_path / "-Users-me-src-repo--claude-worktrees-feature"
    worktree.mkdir()
    (tmp_path / "-Users-me-src-repo").mkdir()
    (worktree / f"{SID}.jsonl").write_text("{}\n")
    assert cs.find_transcript("ab12cd34", tmp_path) == worktree / f"{SID}.jsonl"
    assert cs.find_transcript(SID, tmp_path) == worktree / f"{SID}.jsonl"
    assert cs.find_transcript("ffffffff", tmp_path) is None
    assert cs.find_transcript("ab12", tmp_path) is None  # too short to trust
    assert cs.find_transcript("../../etc", tmp_path) is None


def test_find_transcript_refuses_an_ambiguous_prefix(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / f"ab12cd34-{d}.jsonl").write_text("")
    assert cs.find_transcript("ab12cd34", tmp_path) is None


def test_transcript_cwd_is_the_last_one_recorded(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"cwd": "/repo"}),
        "not json",
        json.dumps([1]),
        json.dumps({"type": "summary"}),
        json.dumps({"cwd": "/repo/.claude/worktrees/w"}),
    ]
    path.write_text("\n".join(lines) + "\n")
    assert cs.transcript_cwd(path) == "/repo/.claude/worktrees/w"
    assert cs.transcript_cwd(tmp_path / "missing.jsonl") is None


def test_transcript_cwd_is_none_for_undecodable_bytes(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"cwd": "/repo"}\n\xff\xfe\n')
    assert cs.transcript_cwd(path) is None


def test_state_records_and_legacy_entries(tmp_path):
    path = tmp_path / "state.json"
    assert cs.load_state(path) == {}
    record = {"session_id": SID, "cwd": "/r", "name": "outreach: X"}
    path.write_text(json.dumps({"388": "ab12cd34", "1": "", "2": {"cwd": "/x"}, "3": record}))
    assert cs.load_state(path) == {"388": {"session_id": "ab12cd34"}, "3": record}
    cs.save_state(path, {"3": record})
    assert cs.load_state(path) == {"3": record}
    path.write_text("[1, 2]")
    assert cs.load_state(path) == {}


def test_a_model_is_passed_and_the_prompt_stays_last():
    for argv in (
        cs.spawn_argv("n", "ctx", "go", model="opus"),
        cs.resume_argv(SID, "n", "go", model="opus"),
    ):
        i = argv.index("--model")
        assert argv[i + 1] == "opus" and argv[-1] == "go"
    assert "--model" not in cs.spawn_argv("n", "ctx", "go")
    assert "--model" not in cs.resume_argv(SID, "n")
