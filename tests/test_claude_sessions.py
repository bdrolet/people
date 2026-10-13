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


def test_live_session_ids_reads_short_ids():
    listing = [{"id": "ab12cd34"}, {"sessionId": "ef56ab78-1234"}, {}]
    runner = lambda argv, cwd: _result(json.dumps(listing))  # noqa: E731
    assert cs.live_session_ids(cwd=CWD, runner=runner) == {"ab12cd34", "ef56ab78"}
    assert cs.live_session_ids(cwd=CWD, runner=lambda argv, cwd: _result("not json")) == set()


def test_state_round_trip_and_reconcile(tmp_path):
    path = tmp_path / "state.json"
    assert cs.load_state(path) == {}
    cs.save_state(path, {"388": "ab12cd34", "1": "gone0000"})
    assert cs.load_state(path) == {"388": "ab12cd34", "1": "gone0000"}
    assert cs.reconcile(cs.load_state(path), {"ab12cd34"}) == {"388": "ab12cd34"}
    path.write_text("[]")
    assert cs.load_state(path) == {}
