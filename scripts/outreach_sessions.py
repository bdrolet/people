#!/usr/bin/env python3
"""One background Claude Code session per outreach person, and one living Google Doc per
person that outlasts the sessions (docs/superpowers/specs/2026-10-14-outreach-contact-docs-design.md).
Local only.

A fresh session boots with that person's DB snapshot in its system prompt
(services/outreach_session.py: identity, pipeline state, recent 1:1 iMessage, WhatsApp
and LinkedIn messages) plus their contact doc when one exists, and, unless --idle,
starts at once on the working-outreach-queue skill's "Prep a brief" procedure ("Reload"
when a doc exists). A person whose last session has exited gets it back instead: same
id, same conversation, resumed from the directory its transcript says it last ran in.

`scripts/link-skills.sh` puts the `outreach-sessions` wrapper on PATH; it runs
this file under the repo's venv.

    outreach-sessions 388                      # dry run: already open / resume / spawn
    outreach-sessions alex --spawn             # a name: session names, then pipeline names
    outreach-sessions --next 5 --spawn         # the top of the to_contact queue
    outreach-sessions --print-context 388      # the block a session boots with, doc included
    outreach-sessions --write-doc 388 --sections - < sections.md   # or a file path

~/.claude/outreach-sessions.json keeps one record per person ({session_id, cwd, name}),
never dropped when a session exits. The doc link lives in outreach_contacts.doc_url.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

import httpx

from clients import claude_sessions, docs_api
from services import (
    identity,
    outreach,
    outreach_doc,
    outreach_doc_write,
    outreach_session,
    person_lookup,
)

STATE_PATH = Path.home() / ".claude" / "outreach-sessions.json"
DEFAULT_MESSAGES = 60
MAX_PEOPLE = 10


class CliError(Exception):
    pass


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


def _resolve(conn: Any, ident: str) -> dict:
    try:
        return person_lookup.resolve(conn, ident)
    except person_lookup.Ambiguous as e:
        raise CliError(f"{ident} matches several people ({e.candidates}); use an id") from e
    except person_lookup.NotFound as e:
        raise CliError(f"no person matches {ident}") from e


def _by_name(conn: Any, name: str, state: dict[str, dict]) -> dict:
    """Recorded session names first, then pipeline display names (contact-docs spec §4)."""
    needle = name.strip().lower()
    ids = sorted(
        int(key)
        for key, record in state.items()
        if needle and needle in (record.get("name") or "").lower().removeprefix("outreach: ")
    )
    where = "sessions"
    if not ids:
        ids, where = [int(r["id"]) for r in outreach.people_named(conn, name)], "pipeline people"
    if len(ids) > 1:
        raise CliError(f"{name} matches several {where} ({ids}); use an id")
    if not ids:
        raise CliError(f"no person matches {name}")
    return _resolve(conn, str(ids[0]))


def _person(conn: Any, ident: str, state: dict[str, dict]) -> dict:
    """An email, E.164 phone or people id resolves as people-api does; anything else is a name."""
    if identity.classify(ident) is None:
        return _by_name(conn, ident, state)
    return _resolve(conn, ident)


def _unique(people: list[dict]) -> list[dict]:
    """Drop repeats by id, keeping the first of each and the original order."""
    seen: dict[Any, dict] = {}
    for p in people:
        seen.setdefault(p["id"], p)
    return list(seen.values())


def _targets(conn: Any, args: argparse.Namespace, state: dict[str, dict]) -> list[dict]:
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
    resolved = [_person(conn, ident, state) for ident in args.people]
    # The same person named twice (email and name, say) must not spawn twice.
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


def _one(
    conn: Any,
    args: argparse.Namespace,
    person: dict,
    name: str,
    record: dict | None,
    live: dict[str, str],
    *,
    today: date,
) -> tuple[str, dict | None]:
    """One person's row and their new session record (None: keep the old one)."""
    running = claude_sessions.find_live(record["session_id"], live) if record else None
    if running:
        cwd = live[running] or (record or {}).get("cwd") or str(REPO)
        short = running[: claude_sessions.SHORT_ID]
        return f"already open {short}", {"session_id": running, "cwd": cwd, "name": name}

    transcript = claude_sessions.find_transcript(record["session_id"]) if record else None
    cwd = claude_sessions.transcript_cwd(transcript) if transcript else None
    if transcript and cwd and Path(cwd).is_dir():
        if not args.spawn:
            return "resume", None
        prompt = None if args.idle else outreach_session.RELOAD_PROMPT
        short = claude_sessions.resume(transcript.stem, name, cwd=Path(cwd), prompt=prompt)
        if not short:
            return "resume failed", None
        # Resuming a stopped session forks a new id; record the one that's now running.
        running = claude_sessions.find_live(short, claude_sessions.live_sessions(cwd=REPO) or {})
        if not running:
            running = transcript.stem if transcript.stem.startswith(short) else short
        return f"resumed {short}", {"session_id": running, "cwd": cwd, "name": name}

    snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
    if snap is None:
        return "not in the pipeline (run `outreach decide` first)", None
    if not args.spawn:
        return "spawn", None
    url = snap["item"]["doc_url"]
    doc, note = _doc(url)
    if args.idle:
        prompt = None
    else:
        prompt = outreach_session.RELOAD_PROMPT if url else outreach_session.PREP_PROMPT
    short = claude_sessions.spawn(
        name, outreach_session.context_block(snap, today=today, doc=doc), cwd=REPO, prompt=prompt
    )
    if not short:
        return "spawn failed", None
    listed = claude_sessions.live_sessions(cwd=REPO) or {}
    full = claude_sessions.find_live(short, listed) or short
    return f"spawned {short}{note}", {"session_id": full, "cwd": str(REPO), "name": name}


def _write_doc(
    conn: Any, args: argparse.Namespace, out: TextIO, state: dict[str, dict], *, today: date
) -> None:
    if not args.sections:
        raise CliError("--write-doc needs --sections FILE, or --sections - to read stdin")
    person = _person(conn, args.write_doc, state)
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


def run(
    conn: Any,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    *,
    today: date,
    state_path: Path,
) -> None:
    state = claude_sessions.load_state(state_path)
    if args.write_doc:
        _write_doc(conn, args, out, state, today=today)
        return
    if args.print_context:
        person = _person(conn, args.print_context, state)
        snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
        if snap is None:
            raise CliError(f"person {person['id']} is not in the pipeline")
        doc, note = _doc(snap["item"]["doc_url"])
        if note:
            print(f"warning:{note}", file=err)
        print(outreach_session.context_block(snap, today=today, doc=doc), file=out)
        return

    people = _targets(conn, args, state)
    listed = claude_sessions.live_sessions(cwd=REPO)
    if listed is None:
        if args.spawn:
            raise CliError(
                "can't list running Claude sessions (claude agents --json failed); "
                "nothing spawned or resumed"
            )
        print(
            "warning: can't list running Claude sessions (claude agents --json failed); "
            "treating none as running",
            file=err,
        )
    live = listed or {}
    try:
        for person in people:
            key = str(person["id"])
            name = outreach_session.session_name(person)
            outcome, record = _one(conn, args, person, name, state.get(key), live, today=today)
            if record is not None:
                state[key] = record
            print(f"{person['id']}\t{name}\t→ {outcome}", file=out)
    finally:
        # A later person's failure must not lose an earlier person's new session record.
        if args.spawn:
            claude_sessions.save_state(state_path, state)

    if args.spawn:
        print("\nclaude agents          # the view", file=err)
        print("claude attach <id>     # work one (needs a real terminal)", file=err)
    else:
        print("\ndry run — add --spawn to launch these", file=err)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outreach-sessions",
        description="Give each outreach person their own background Claude session and doc.",
    )
    p.add_argument("people", nargs="*", help="email, E.164 phone, LinkedIn URL, people id or name")
    p.add_argument("--next", type=int, help="the top N of the to_contact queue")
    p.add_argument("--spawn", action="store_true", help="actually launch or resume sessions")
    p.add_argument("--idle", action="store_true", help="launch without starting the prep/reload")
    p.add_argument("--messages", type=int, default=DEFAULT_MESSAGES, help="per channel")
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
    state_path: Path = STATE_PATH,
) -> int:
    args = build_parser().parse_args(argv)
    today = today or datetime.now(UTC).astimezone(outreach.TZ).date()
    with get_conn() as conn:
        try:
            run(conn, args, out, err, today=today, state_path=state_path)
        except USER_ERRORS as e:
            print(f"error: {e}", file=err)
            return 1
    return 0


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
