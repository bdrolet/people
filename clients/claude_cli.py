"""One-shot `claude -p` calls: the prompt goes on stdin and nothing else is shared
(no session, no system prompt), so the caller gets an answer with no context from
whoever wrote the text being checked. I/O only."""

import subprocess
from collections.abc import Callable

Runner = Callable[[list[str], str], subprocess.CompletedProcess]


class ClaudeCliError(RuntimeError):
    pass


def _run(argv: list[str], text: str, timeout_s: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=text, capture_output=True, text=True, check=False, timeout=timeout_s
    )


def prompt(
    text: str,
    *,
    model: str | None = None,
    runner: Runner | None = None,
    timeout_s: int = 120,
) -> str:
    """Run `claude -p` with `text` on stdin and return stdout."""
    argv = ["claude", "-p", "--output-format", "text", *(["--model", model] if model else [])]
    try:
        if runner is None:
            result = _run(argv, text, timeout_s)
        else:
            result = runner(argv, text)
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude -p timed out after {timeout_s}s") from exc
    if result.returncode != 0:
        raise ClaudeCliError(f"claude -p exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout
