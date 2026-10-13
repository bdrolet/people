"""Background Claude Code sessions for local scripts: spawn one with `claude --bg`,
read back the id it prints, list the live ones, and remember which key owns which
session. I/O only. Mirrors ~/src/tasks/scripts/task_sessions.py's spawn half."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

SHORT_ID = 8

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def spawn_argv(name: str, context: str, prompt: str | None = None) -> list[str]:
    """`claude --bg` picks the session id itself (it ignores --session-id), so the
    id is read back from its output. With a prompt the session starts working at
    once; without one it idles at zero cost until someone attaches."""
    argv = ["claude", "--bg", "-n", name, "--append-system-prompt", context]
    return [*argv, prompt] if prompt else argv


def parse_session_id(output: str) -> str | None:
    """Pull the short id out of `backgrounded · e8af9b02 · <name>`."""
    for line in output.splitlines():
        if line.strip().startswith("backgrounded") and "·" in line:
            parts = [part.strip() for part in line.split("·")]
            if len(parts) >= 2 and parts[1]:
                return parts[1]
    return None


def spawn(
    name: str, context: str, *, cwd: Path, prompt: str | None = None, runner: Runner = _run
) -> str | None:
    """Start a background session in `cwd`; return the short id `claude attach` takes."""
    result = runner(spawn_argv(name, context, prompt), cwd)
    return parse_session_id(result.stdout) if result.returncode == 0 else None


def live_session_ids(*, cwd: Path, runner: Runner = _run) -> set[str]:
    """Short ids of every session `claude agents` currently lists."""
    try:
        listing = json.loads(runner(["claude", "agents", "--json", "--all"], cwd).stdout or "[]")
    except (OSError, json.JSONDecodeError):
        return set()
    ids = set()
    for session in listing:
        short = session.get("id") or (session.get("sessionId") or "")[:SHORT_ID]
        if short:
            ids.add(short)
    return ids


def load_state(path: Path) -> dict[str, str]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))


def reconcile(state: dict[str, str], live: set[str]) -> dict[str, str]:
    """Forget keys whose session is gone, so they can get a new one."""
    return {key: sid for key, sid in state.items() if sid in live}
