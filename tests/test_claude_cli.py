import subprocess

import pytest

from clients import claude_cli


def _result(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_prompt_builds_argv_and_passes_text_on_stdin():
    seen = {}

    def runner(argv, text):
        seen.update(argv=argv, text=text)
        return _result("hello")

    assert claude_cli.prompt("the text", runner=runner) == "hello"
    assert seen["argv"] == ["claude", "-p", "--output-format", "text"]
    assert seen["text"] == "the text"


def test_prompt_adds_model():
    seen = {}

    def runner(argv, text):
        seen["argv"] = argv
        return _result("x")

    claude_cli.prompt("t", model="haiku", runner=runner)
    assert seen["argv"][-2:] == ["--model", "haiku"]


def test_nonzero_exit_raises_with_stderr():
    def runner(argv, text):
        return _result(returncode=1, stderr="not logged in")

    with pytest.raises(claude_cli.ClaudeCliError, match="not logged in"):
        claude_cli.prompt("t", runner=runner)


def test_timeout_raises():
    def runner(argv, text):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(claude_cli.ClaudeCliError, match="timed out"):
        claude_cli.prompt("t", runner=runner)


def test_default_runner_passes_subprocess_options(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return _result("out")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    assert claude_cli.prompt("x", timeout_s=7) == "out"
    assert seen["timeout"] == 7
    assert seen["input"] == "x"
    assert seen["capture_output"] is True
    assert seen["text"] is True


def test_missing_binary_raises_cli_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    with pytest.raises(claude_cli.ClaudeCliError):
        claude_cli.prompt("x")
