"""Background Claude Code sessions for local scripts: spawn one with `claude --bg`,
resume an exited one with `claude --bg --resume`, list the running ones, find a
session's transcript, and keep the per-key session records. I/O only. Mirrors
~/src/tasks/scripts/task_sessions.py's spawn half."""

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

SHORT_ID = 8
PROJECTS_DIR = Path.home() / ".claude" / "projects"
_SESSION_ID = re.compile(r"[0-9a-f-]{8,36}")

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


def resume_argv(session_id: str, name: str, prompt: str | None = None) -> list[str]:
    """`-n` again on resume: without it Claude renames the session after its prompt."""
    argv = ["claude", "--bg", "--resume", session_id, "-n", name]
    return [*argv, prompt] if prompt else argv


def resume(
    session_id: str, name: str, *, cwd: Path, prompt: str | None = None, runner: Runner = _run
) -> str | None:
    """Bring an exited session back under its own id. `cwd` must be the directory it last
    ran in (transcript_cwd): the transcript is looked up under that directory's project.
    An exited session comes back under its own id, so the short id stands even if no
    `backgrounded` line prints; a session ended with `claude stop` comes back under a new
    id, which the printed line carries — the caller records whichever id is running."""
    result = runner(resume_argv(session_id, name, prompt), cwd)
    if result.returncode != 0:
        return None
    return parse_session_id(result.stdout) or session_id[:SHORT_ID]


def live_sessions(*, cwd: Path, runner: Runner = _run) -> dict[str, str] | None:
    """Session id (full when listed, else short) → cwd for every running session, or
    None when the listing can't be trusted (the command failed, or its output isn't a
    list): an empty dict means "nothing is running", None means "don't know".
    `claude agents --json` without --all: with it, exited sessions are listed too."""
    try:
        result = runner(["claude", "agents", "--json"], cwd)
        if result.returncode != 0:
            return None
        listing = json.loads(result.stdout or "[]")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(listing, list):
        return None
    live = {}
    for session in listing:
        if not isinstance(session, dict):
            continue
        sid = session.get("sessionId") or session.get("id")
        if sid:
            live[sid] = session.get("cwd") or ""
    return live


def find_live(session_id: str, live: dict[str, str]) -> str | None:
    """The running session `session_id` (full or short) names, by prefix either way."""
    if not session_id:
        return None
    for sid in live:
        if sid.startswith(session_id) or session_id.startswith(sid):
            return sid
    return None


def find_transcript(session_id: str, projects_dir: Path | None = None) -> Path | None:
    """The session's transcript in any project directory: a session that entered a
    worktree writes under that worktree's directory from then on. None when missing,
    ambiguous, or `session_id` doesn't look like an id."""
    if not _SESSION_ID.fullmatch(session_id):
        return None
    hits = sorted((projects_dir or PROJECTS_DIR).glob(f"*/{session_id}*.jsonl"))
    return hits[0] if len(hits) == 1 else None


def transcript_cwd(path: Path) -> str | None:
    """The last working directory the transcript records. The project-directory name
    can't be decoded instead: it writes `/` and `.` both as `-`."""
    cwd = None
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    value = json.loads(line).get("cwd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(value, str) and value:
                    cwd = value
    except (OSError, UnicodeDecodeError):
        return None
    return cwd


def load_state(path: Path) -> dict[str, dict]:
    """Key → {"session_id", "cwd", "name"}. A PR #26 entry (a bare short id) reads as
    {"session_id": <short id>}; every lookup matches ids by prefix, so it still works."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, dict] = {}
    for key, value in raw.items():
        if isinstance(value, str) and value:
            state[key] = {"session_id": value}
        elif isinstance(value, dict) and value.get("session_id"):
            state[key] = dict(value)
    return state


def save_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))
