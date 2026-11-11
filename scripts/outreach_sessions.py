#!/usr/bin/env python3
"""One agent-deck Claude session per outreach person, and one living Google Doc per
person that outlasts the sessions (docs/superpowers/specs/2026-10-14-outreach-contact-docs-design.md,
docs/superpowers/specs/2026-11-02-outreach-sessions-on-agent-deck-design.md). Local only.

A fresh session boots with that person's DB snapshot in its system prompt
(services/outreach_session.py: identity, pipeline state, recent 1:1 iMessage, WhatsApp
and LinkedIn messages) plus their contact doc when one exists, and, unless --idle,
starts at once on the working-outreach-queue skill's "Prep a brief" procedure ("Reload"
when a doc exists). A person whose session is stopped or archived gets it back instead
(same conversation) and is sent the Reload prompt, because a resumed conversation keeps
its original, stale system prompt.

The agent-deck mechanics live in `deck-sessions` (~/src/scripts/deck-sessions; umbrella
spec ~/src/scripts/docs/superpowers/specs/2026-11-02-deck-sessions-design.md): this
script builds one spec per person and maps the results back. agent-deck's registry is
the only state: each session carries the hint `person_id=<id>`, sits in group `outreach`
and is parented under `conductor-outreach`.

`scripts/link-skills.sh` puts the `outreach-sessions` wrapper on PATH; it runs
this file under the repo's venv.

    outreach-sessions 388                      # dry run: already open / would reopen / would launch
    outreach-sessions alex --spawn             # a name: session titles, then pipeline names
    outreach-sessions --next 5 --spawn         # the top of the to_contact queue
    outreach-sessions --prune --spawn          # archive sessions of people at a terminal stage
    outreach-sessions --migrate --apply        # one-time: adopt the old `claude --bg` sessions
    outreach-sessions --print-context 388      # the block a session boots with, doc included
    outreach-sessions --write-doc 388 --sections - < sections.md   # or a file path

Group: --group, else $OUTREACH_SESSIONS_GROUP, else `outreach`.
Parent: --parent none|self|<session>, else $OUTREACH_SESSIONS_PARENT, else
`conductor-outreach` (none, with a warning, when that session doesn't exist).
The doc link lives in outreach_contacts.doc_url.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

import httpx

from clients import docs_api
from repo import outreach as outreach_repo
from services import (
    identity,
    outreach,
    outreach_doc,
    outreach_doc_write,
    outreach_session,
    person_lookup,
)

DEFAULT_MESSAGES = 60
DEFAULT_MODEL = "opus"  # research, judgment and writing; OUTREACH_SESSION_MODEL overrides
DEFAULT_GROUP = "outreach"
DEFAULT_PARENT = "conductor-outreach"
MAX_PEOPLE = 10
SHORT_ID = 8
KEY_HINT = "person_id"
# Prune archives a session once its person can make no more progress. declined and
# skipped count (Ben, 2026-11-02): they reopen only with `outreach mark --force`, and an
# archived session comes back on the next run for that person anyway.
TERMINAL = {"introduced", "declined", "skipped", "signed", "lost"}
# The old `claude --bg` launcher's records ({people id: {session_id, cwd, name}}).
STATE_PATH = Path.home() / ".claude" / "outreach-sessions.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
# deck-sessions' default context path for this key hint; the migration writes there too
# so a later reopen rewrites the same file.
CONTEXT_DIR = Path.home() / ".claude" / "deck-context" / KEY_HINT
_TITLE = re.compile(r"^outreach: (?P<label>.*?)(?: #(?P<id>\d+))?$")

Runner = Callable[..., subprocess.CompletedProcess]


class CliError(Exception):
    pass


class DeckUnavailable(Exception):
    """deck-sessions could not read agent-deck's registry (or isn't installed)."""


USER_ERRORS = (
    CliError,
    outreach.NotInPipeline,
    outreach.InvalidDocUrl,
    outreach_doc.SectionsError,
    outreach_doc_write.DocConflict,
    outreach_doc_write.DocNotRecorded,
    docs_api.DocsApiUnavailable,
    docs_api.FolderNotFound,
    httpx.HTTPError,
)


# --- deck-sessions -------------------------------------------------------


def _deck(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["deck-sessions", *argv], input=stdin, capture_output=True, text=True, check=False
    )


def _run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    """`claude` and `agent-deck` for the one-time migration."""
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)


def _call_deck(argv: list[str], stdin: str | None, deck: Runner, err: TextIO) -> str:
    """Run deck-sessions and return its stdout; forward its warnings, raise when the
    registry can't be read."""
    try:
        result = deck(argv, stdin)
    except OSError as e:
        raise DeckUnavailable(f"deck-sessions not on PATH ({e})") from e
    for line in (result.stderr or "").splitlines():
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and body.get("error"):
            raise DeckUnavailable(str(body["error"]).removeprefix("registry unavailable: "))
        print(line, file=err)
    if result.returncode != 0:
        raise DeckUnavailable(f"deck-sessions exit {result.returncode}")
    return result.stdout


class Sessions:
    """The outreach sessions agent-deck knows, read once and only when needed."""

    def __init__(self, deck: Runner, err: TextIO):
        self.deck, self.err = deck, err
        self._rows: list[dict] | None = None

    def rows(self) -> list[dict]:
        if self._rows is None:
            out = _call_deck(["sessions", "--key-hint", KEY_HINT], None, self.deck, self.err)
            self._rows = json.loads(out)
        return self._rows


def _short(session_id: str) -> str:
    return session_id[:SHORT_ID]


def outcome_text(result: dict) -> str:
    """deck-sessions' outcome in the words this CLI prints (spec D4)."""
    outcome, detail, sid = result.get("outcome"), result.get("detail"), result.get("deck_id")
    short = _short(sid) if sid else ""
    if outcome == "launched":
        return f"launched {short}"
    if outcome == "already open":
        if not sid:
            return f"already open ({detail})"
        adopted = " · adopted" if result.get("adopted") else ""
        return f"already open {short} ({result.get('status')}){adopted}"
    if outcome in ("reopened", "refreshed", "would reopen", "would refresh"):
        return f"{outcome} {short}"
    if outcome == "duplicate":
        return "duplicate " + ",".join(_short(i) for i in (detail or "").split(","))
    if outcome == "would launch":
        return outcome
    return f"failed: {detail}"


# --- configuration -------------------------------------------------------


@dataclass
class Placement:
    group: str
    parent: str | None  # None = no parent
    parent_is_default: bool


def _placement(args: argparse.Namespace, env: Mapping[str, str]) -> Placement:
    explicit = args.parent or env.get("OUTREACH_SESSIONS_PARENT")
    parent: str | None = explicit or DEFAULT_PARENT
    if parent == "none":
        parent = None
    elif parent == "self":
        parent = env.get("AGENTDECK_INSTANCE_ID")
        if not parent:
            raise CliError("--parent self needs to run inside an agent-deck session")
    group = args.group or env.get("OUTREACH_SESSIONS_GROUP") or DEFAULT_GROUP
    return Placement(group=group, parent=parent, parent_is_default=not explicit)


def _session_model(env: Mapping[str, str]) -> str:
    """Pinned so sessions don't follow Ben's interactive default model."""
    return env.get("OUTREACH_SESSION_MODEL", "").strip() or DEFAULT_MODEL


# --- who -----------------------------------------------------------------


def _resolve(conn: Any, ident: str) -> dict:
    try:
        return person_lookup.resolve(conn, ident)
    except person_lookup.Ambiguous as e:
        raise CliError(f"{ident} matches several people ({e.candidates}); use an id") from e
    except person_lookup.NotFound as e:
        raise CliError(f"no person matches {ident}") from e


def _title_label(title: str | None) -> str:
    """`outreach: Jonah Example #388` → `jonah example`."""
    match = _TITLE.match(title or "")
    return (match.group("label") if match else (title or "")).lower()


def _by_name(conn: Any, name: str, sessions: Sessions) -> dict:
    """Session titles first, then pipeline display names (contact-docs spec §4)."""
    needle = name.strip().lower()
    ids = sorted(
        {
            int(row["key"])
            for row in sessions.rows()
            if needle and needle in _title_label(row["title"])
        }
    )
    where = "sessions"
    if not ids:
        ids, where = [int(r["id"]) for r in outreach.people_named(conn, name)], "pipeline people"
    if len(ids) > 1:
        raise CliError(f"{name} matches several {where} ({ids}); use an id")
    if not ids:
        raise CliError(f"no person matches {name}")
    return _resolve(conn, str(ids[0]))


def _person(conn: Any, ident: str, sessions: Sessions) -> dict:
    """An email, E.164 phone or people id resolves as people-api does; anything else is a name."""
    if identity.classify(ident) is None:
        return _by_name(conn, ident, sessions)
    return _resolve(conn, ident)


def _unique(people: list[dict]) -> list[dict]:
    """Drop repeats by id, keeping the first of each and the original order."""
    seen: dict[Any, dict] = {}
    for p in people:
        seen.setdefault(p["id"], p)
    return list(seen.values())


def _targets(conn: Any, args: argparse.Namespace, sessions: Sessions) -> list[dict]:
    if args.people and args.next:
        raise CliError("give people or --next N, not both")
    if args.next:
        if args.next > MAX_PEOPLE:
            raise CliError(f"--next is capped at {MAX_PEOPLE} sessions per run")
        queued = [
            item["person"] for item in outreach.queue(conn, stage="to_contact", limit=args.next)
        ]
        return _unique(queued)
    if not args.people:
        raise CliError("name at least one person, or use --next N")
    if len(args.people) > MAX_PEOPLE:
        raise CliError(f"at most {MAX_PEOPLE} people per run")
    resolved = [_person(conn, ident, sessions) for ident in args.people]
    # The same person named twice (email and name, say) must not launch twice.
    return _unique(resolved)


def _doc(url: str | None) -> tuple[str | None, str]:
    """The contact doc's markdown, and a note for the row when it couldn't be fetched (the
    session starts anyway; RELOAD_PROMPT has it fetch the doc itself)."""
    if not url:
        return None, ""
    try:
        return docs_api.get_doc(outreach.doc_id(url)).get("content") or "", ""
    except Exception as e:  # noqa: BLE001 — never block a session on the doc
        return None, f" (doc unavailable: {type(e).__name__})"


# --- ensure --------------------------------------------------------------


def spec_for(
    person: dict,
    snap: dict,
    context: str,
    args: argparse.Namespace,
    placement: Placement,
    env: Mapping[str, str],
) -> dict:
    """The deck-sessions spec for one person (spec D2)."""
    url = snap["item"]["doc_url"]
    if args.idle:
        prompt = reopen = None
    else:
        prompt = outreach_session.RELOAD_PROMPT if url else outreach_session.PREP_PROMPT
        reopen = outreach_session.RELOAD_PROMPT
    return {
        "key_hint": KEY_HINT,
        "key": str(person["id"]),
        "title": outreach_session.session_name(person),
        "adopt_prefix": None,
        "context": context,
        "cwd": str(REPO),
        "group": placement.group,
        "parent": placement.parent or "none",
        "parent_is_default": placement.parent_is_default,
        "hints": {},
        "model": _session_model(env),
        "idle_timeout": None,
        "prompt": prompt,
        "reopen_prompt": reopen,
        "refresh": False,
    }


def _ensure(
    conn: Any,
    args: argparse.Namespace,
    people: list[dict],
    placement: Placement,
    deck: Runner,
    out: TextIO,
    err: TextIO,
    env: Mapping[str, str],
    *,
    today: date,
) -> None:
    rows: list[tuple[dict, str, str]] = []  # person, name, outcome-or-"" (pending)
    specs, notes = [], {}
    for person in people:
        name = outreach_session.session_name(person)
        snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
        if snap is None:
            rows.append((person, name, "not in the pipeline (run `outreach decide` first)"))
            continue
        # The doc is only fetched when something may launch; a dry run never needs it.
        doc, note = _doc(snap["item"]["doc_url"]) if args.spawn else (None, "")
        notes[str(person["id"])] = note
        context = outreach_session.context_block(snap, today=today, doc=doc)
        specs.append(spec_for(person, snap, context, args, placement, env))
        rows.append((person, name, ""))

    results: dict[str, dict] = {}
    if specs:
        stdin = "".join(json.dumps(s) + "\n" for s in specs)
        argv = ["ensure"] + (["--apply"] if args.spawn else [])
        lines = _call_deck(argv, stdin, deck, err).splitlines()
        for spec, line in zip(specs, [ln for ln in lines if ln.strip()], strict=True):
            results[spec["key"]] = json.loads(line)

    for person, name, outcome in rows:
        if not outcome:
            result = results[str(person["id"])]
            detail = result.get("detail") or ""
            if result.get("outcome") == "failed" and detail.startswith("parent "):
                raise CliError(detail)
            outcome = outcome_text(result)
            if result.get("outcome") == "launched":
                outcome += notes.get(str(person["id"]), "")
        print(f"{person['id']}\t{name}\t→ {outcome}", file=out)


# --- prune ---------------------------------------------------------------


def _prune(
    conn: Any,
    args: argparse.Namespace,
    placement: Placement,
    deck: Runner,
    out: TextIO,
    err: TextIO,
) -> None:
    """Archive the group's sessions whose person is at a terminal stage (spec D6)."""
    argv = ["sessions", "--key-hint", KEY_HINT, "--group", placement.group]
    listed = json.loads(_call_deck(argv, None, deck, err))
    done = []
    for session in listed:
        if session.get("archived"):
            continue
        row = outreach_repo.pipeline_row(conn, int(session["key"]))
        if row is not None and row["stage"] in TERMINAL:
            done.append(session | {"stage": row["stage"]})
    if not done:
        return
    by_id = {s["deck_id"]: s for s in done}
    argv = ["archive"] + (["--apply"] if args.spawn else []) + [s["deck_id"] for s in done]
    for line in _call_deck(argv, None, deck, err).splitlines():
        result = json.loads(line)
        session = by_id.get(result["deck_id"], {})
        sid = _short(result["deck_id"])
        outcome = {
            "archived": f"pruned {sid}",
            "kept (busy)": f"kept (busy) {sid}",
            "would archive": f"would prune {sid}",
        }.get(result["outcome"], f"prune failed: {result.get('detail')}")
        print(
            f"{session.get('key')}\t{session.get('title')}\t→ {outcome} ({session.get('stage')})",
            file=out,
        )


# --- migrate (one-time, spec D5) -----------------------------------------


def _transcript(session_id: str, projects_dir: Path) -> Path | None:
    hits = sorted(projects_dir.glob(f"*/{session_id}.jsonl"))
    return hits[0] if len(hits) == 1 else None


def _transcript_cwd(path: Path) -> str | None:
    """The last working directory the transcript records; its project directory is where
    `claude --resume` looks for it."""
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


def _json_out(result: subprocess.CompletedProcess) -> Any:
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def _first_error(result: subprocess.CompletedProcess) -> str:
    body = _json_out(result)
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"]).splitlines()[0]
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[0] if text else f"exit {result.returncode}"


def add_argv(
    title: str,
    key: str,
    sid: str,
    cwd: str,
    context_file: Path,
    placement: Placement,
    model: str,
) -> list[str]:
    argv = ["agent-deck", "add", "--json", "--trust-folder", "-c", "claude", "--no-identity"]
    argv += ["--parent", placement.parent] if placement.parent else ["--no-parent"]
    argv += ["-g", placement.group, "-t", title, "--hint", f"{KEY_HINT}={key}"]
    argv += ["--model", model, "--resume-session", sid]
    argv += ["--extra-arg=--append-system-prompt-file", f"--extra-arg={context_file}", cwd]
    return argv


def _migrate(
    conn: Any,
    args: argparse.Namespace,
    placement: Placement,
    deck: Runner,
    runner: Runner,
    out: TextIO,
    err: TextIO,
    env: Mapping[str, str],
    *,
    today: date,
    state_path: Path,
    projects_dir: Path,
) -> None:
    """Adopt each old `claude --bg` conversation into agent-deck without starting it."""
    try:
        records = json.loads(state_path.read_text())
    except FileNotFoundError:
        print(f"nothing to migrate: {state_path} not found", file=err)
        return
    except (OSError, json.JSONDecodeError) as e:
        raise CliError(f"cannot read {state_path}: {e}") from e

    adopted = {row["key"]: row for row in Sessions(deck, err).rows()}
    listing = runner(["agent-deck", "list", "--json"])
    deck_rows = _json_out(listing)
    if listing.returncode != 0 or not isinstance(deck_rows, list):
        raise DeckUnavailable(_first_error(listing))
    if placement.parent and not any(
        placement.parent in (r.get("id"), r.get("title")) for r in deck_rows
    ):
        if not placement.parent_is_default:
            raise CliError(f"parent {placement.parent} not found in agent-deck")
        print(f"warning: parent {placement.parent} not found; adopting without a parent", file=err)
        placement = Placement(placement.group, None, placement.parent_is_default)

    agents = runner(["claude", "agents", "--json"])
    running = _json_out(agents)
    if agents.returncode != 0 or not isinstance(running, list):
        raise CliError("can't list running Claude sessions (claude agents --json failed)")
    live = {a.get("sessionId"): a for a in running if isinstance(a, dict) and a.get("sessionId")}

    model = _session_model(env)
    failures = 0
    for key, record in sorted(records.items(), key=lambda kv: int(kv[0])):
        sid = record.get("session_id") if isinstance(record, dict) else record
        pid = int(key)
        person = (outreach_repo.pipeline_row(conn, pid) or {}) if sid else {}
        title = outreach_session.session_name({**person, "id": pid}) if person else ""
        label = title or (record.get("name") if isinstance(record, dict) else None) or key

        def say(outcome: str, label: str = label, key: str = key) -> None:
            print(f"{key}\t{label}\t→ {outcome}", file=out)

        if key in adopted:
            say(f"already in agent-deck {_short(adopted[key]['deck_id'])}")
            continue
        if not person:
            say("skipped (left the pipeline)")
            continue
        transcript = _transcript(sid, projects_dir) if sid else None
        cwd = _transcript_cwd(transcript) if transcript else None
        cwd = cwd if cwd and Path(cwd).is_dir() else None
        if not transcript or not cwd:
            say("skipped (transcript gone)")
            continue
        bg = live.get(sid)
        if bg and bg.get("status") == "busy":
            say(f"skipped (busy in claude --bg {bg.get('id')}; rerun later)")
            continue
        if not args.spawn:
            say(("would stop claude --bg + adopt" if bg else "would adopt") + f" {_short(sid)}")
            continue
        if bg:
            stopped = runner(["claude", "stop", bg.get("id") or sid])
            if stopped.returncode != 0:
                failures += 1
                say(f"failed: claude stop: {_first_error(stopped)}")
                continue
        snap = outreach_session.snapshot(conn, pid, limit=args.messages)
        context = outreach_session.context_block(snap, today=today) if snap else ""
        context_file = CONTEXT_DIR / f"{pid}.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.touch(mode=0o600, exist_ok=True)
        context_file.chmod(0o600)
        context_file.write_text(context)
        result = runner(add_argv(title, key, sid, cwd, context_file, placement, model))
        body = _json_out(result)
        if result.returncode == 0 and isinstance(body, dict) and body.get("id"):
            # A never-started session reads `error` / `unknown-exit`, which a conductor
            # restarting errored children would wake. It can't be stopped (not running),
            # but it can be archived; ensure reopens archived sessions with RELOAD.
            archived = runner(["agent-deck", "session", "archive", body["id"]])
            note = "" if archived.returncode == 0 else f"; archive failed: {_first_error(archived)}"
            failures += 1 if note else 0
            say(
                f"adopted {_short(body['id'])} (archived{note})"
                + (" (stopped claude --bg)" if bg else "")
            )
        else:
            failures += 1
            say(f"failed: {_first_error(result)}")

    if args.spawn:
        if failures:
            print(f"\n{failures} failed; {state_path} kept — fix and rerun", file=err)
        else:
            done = state_path.with_name(state_path.name + ".migrated")
            state_path.rename(done)
            print(f"\nmigrated; {state_path.name} renamed to {done.name}", file=err)
    else:
        print("\ndry run — add --apply to migrate", file=err)


# --- write-doc -----------------------------------------------------------


def _write_doc(
    conn: Any, args: argparse.Namespace, out: TextIO, sessions: Sessions, *, today: date
) -> None:
    if not args.sections:
        raise CliError("--write-doc needs --sections FILE, or --sections - to read stdin")
    person = _person(conn, args.write_doc, sessions)
    if args.sections == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.sections).read_text(encoding="utf-8")
        except OSError as e:
            raise CliError(f"cannot read {args.sections}: {e.strerror}") from e
    sections = outreach_doc.parse_sections(text)
    result = outreach_doc_write.write(conn, person["id"], sections, today=today)
    print(f"{person['id']}\t{outreach_session.session_name(person)}\t→ {result}", file=out)


# --- cli -----------------------------------------------------------------


def run(
    conn: Any,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    *,
    today: date,
    deck: Runner = _deck,
    runner: Runner = _run,
    env: Mapping[str, str] = os.environ,
    state_path: Path = STATE_PATH,
    projects_dir: Path = PROJECTS_DIR,
) -> None:
    sessions = Sessions(deck, err)
    if args.write_doc:
        _write_doc(conn, args, out, sessions, today=today)
        return
    if args.print_context:
        person = _person(conn, args.print_context, sessions)
        snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
        if snap is None:
            raise CliError(f"person {person['id']} is not in the pipeline")
        doc, note = _doc(snap["item"]["doc_url"])
        if note:
            print(f"warning:{note}", file=err)
        print(outreach_session.context_block(snap, today=today, doc=doc), file=out)
        return

    placement = _placement(args, env)
    if args.migrate:
        _migrate(
            conn,
            args,
            placement,
            deck,
            runner,
            out,
            err,
            env,
            today=today,
            state_path=state_path,
            projects_dir=projects_dir,
        )
        return
    if args.prune:
        _prune(conn, args, placement, deck, out, err)
        if not args.people and not args.next:
            return

    people = _targets(conn, args, sessions)
    _ensure(conn, args, people, placement, deck, out, err, env, today=today)

    if args.spawn:
        print("\nagent-deck session attach <id>   # work one (needs a real terminal)", file=err)
        print(f"agent-deck -g {placement.group}   # the view", file=err)
    else:
        print("\ndry run — add --spawn to launch these", file=err)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outreach-sessions",
        description="Give each outreach person their own agent-deck Claude session and doc.",
    )
    p.add_argument("people", nargs="*", help="email, E.164 phone, LinkedIn URL, people id or name")
    p.add_argument("--next", type=int, help="the top N of the to_contact queue")
    p.add_argument(
        "--spawn",
        "--apply",
        dest="spawn",
        action="store_true",
        help="actually launch, reopen, prune or migrate; dry run otherwise",
    )
    p.add_argument("--idle", action="store_true", help="launch without starting the prep/reload")
    p.add_argument("--messages", type=int, default=DEFAULT_MESSAGES, help="per channel")
    p.add_argument(
        "--group", help=f"agent-deck group (env OUTREACH_SESSIONS_GROUP, default {DEFAULT_GROUP})"
    )
    p.add_argument(
        "--parent",
        help=f"none|self|<session> (env OUTREACH_SESSIONS_PARENT, default {DEFAULT_PARENT})",
    )
    p.add_argument(
        "--prune", action="store_true", help="archive sessions of people at a terminal stage"
    )
    p.add_argument(
        "--migrate",
        action="store_true",
        help=f"one-time: adopt the {STATE_PATH.name} sessions into agent-deck",
    )
    p.add_argument("--print-context", metavar="PERSON", help="print one person's context block")
    p.add_argument("--write-doc", metavar="PERSON", help="create or merge the person's doc")
    p.add_argument(
        "--sections", metavar="FILE", help="with --write-doc: the session's sections; - reads stdin"
    )
    return p


def main_with(
    argv: list[str],
    get_conn: Callable[[], Any],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    today: date | None = None,
    deck: Runner = _deck,
    runner: Runner = _run,
    env: Mapping[str, str] = os.environ,
    state_path: Path = STATE_PATH,
    projects_dir: Path = PROJECTS_DIR,
) -> int:
    args = build_parser().parse_args(argv)
    if outreach.writes_disabled():
        # Retired (OUTREACH_WRITES_DISABLED): sessions, docs and prunes belong to pipelines now.
        print(outreach.RETIRED_MESSAGE, file=err)
        return 1
    today = today or datetime.now(UTC).astimezone(outreach.TZ).date()
    with get_conn() as conn:
        try:
            run(
                conn,
                args,
                out,
                err,
                today=today,
                deck=deck,
                runner=runner,
                env=env,
                state_path=state_path,
                projects_dir=projects_dir,
            )
        except DeckUnavailable as e:
            print(f"registry unavailable: {e} — nothing launched", file=err)
            return 2
        except USER_ERRORS as e:
            print(f"error: {e}", file=err)
            return 1
    return 0


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
