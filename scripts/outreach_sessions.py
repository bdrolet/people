#!/usr/bin/env python3
"""One background Claude Code session per outreach person. Local only.

Each session boots with that person's DB snapshot in its system prompt
(services/outreach_session.py: identity, pipeline state, recent 1:1 iMessage,
WhatsApp and LinkedIn messages) and, unless --idle, starts at once on the
working-outreach-queue skill's "Prep a brief" procedure: an email search, a
web news search, and talking points, ready when Ben attaches.

`scripts/link-skills.sh` puts the `outreach-sessions` wrapper on PATH; it runs
this file under the repo's venv.

    outreach-sessions 388                     # dry run: what it would spawn
    outreach-sessions jonah@example.com --spawn
    outreach-sessions --next 5 --spawn        # the top of the to_contact queue
    outreach-sessions --print-context 388     # the block a session would boot with

A person who already has a live session is left alone:
~/.claude/outreach-sessions.json maps person id to session id, reconciled
against `claude agents --json` on every run.
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

from clients import claude_sessions
from services import outreach, outreach_session, person_lookup

STATE_PATH = Path.home() / ".claude" / "outreach-sessions.json"
DEFAULT_MESSAGES = 60
MAX_PEOPLE = 10


class CliError(Exception):
    pass


def _resolve(conn: Any, ident: str) -> dict:
    try:
        return person_lookup.resolve(conn, ident)
    except person_lookup.Ambiguous as e:
        raise CliError(f"{ident} matches several people ({e.candidates}); use an id") from e
    except person_lookup.NotFound as e:
        raise CliError(f"no person matches {ident}") from e


def _targets(conn: Any, args: argparse.Namespace) -> list[dict]:
    if args.people and args.next:
        raise CliError("give people or --next N, not both")
    if args.next:
        if args.next > MAX_PEOPLE:
            raise CliError(f"--next is capped at {MAX_PEOPLE} sessions per run")
        return [
            item["person"] for item in outreach.queue(conn, stage="to_contact", limit=args.next)
        ]
    if not args.people:
        raise CliError("name at least one person, or use --next N")
    if len(args.people) > MAX_PEOPLE:
        raise CliError(f"at most {MAX_PEOPLE} people per run")
    return [_resolve(conn, ident) for ident in args.people]


def run(
    conn: Any,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    *,
    today: date,
    state_path: Path,
) -> None:
    if args.print_context:
        person = _resolve(conn, args.print_context)
        snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
        if snap is None:
            raise CliError(f"person {person['id']} is not in the pipeline")
        print(outreach_session.context_block(snap, today=today), file=out)
        return

    people = _targets(conn, args)
    state = claude_sessions.reconcile(
        claude_sessions.load_state(state_path), claude_sessions.live_session_ids(cwd=REPO)
    )
    for person in people:
        key = str(person["id"])
        name = outreach_session.session_name(person)
        if key in state:
            outcome = f"already open {state[key]}"
        else:
            snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
            if snap is None:
                outcome = "not in the pipeline (run `outreach decide` first)"
            elif not args.spawn:
                outcome = "spawn"
            else:
                session_id = claude_sessions.spawn(
                    name,
                    outreach_session.context_block(snap, today=today),
                    cwd=REPO,
                    prompt=None if args.idle else outreach_session.PREP_PROMPT,
                )
                if session_id:
                    state[key] = session_id
                    outcome = f"spawned {session_id}"
                else:
                    outcome = "spawn failed"
        print(f"{person['id']}\t{name}\t→ {outcome}", file=out)

    if args.spawn:
        claude_sessions.save_state(state_path, state)
        print("\nclaude agents          # the view", file=err)
        print("claude attach <id>     # work one (needs a real terminal)", file=err)
    else:
        print("\ndry run — add --spawn to launch these", file=err)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outreach-sessions",
        description="Give each outreach person their own background Claude session.",
    )
    p.add_argument("people", nargs="*", help="email, E.164 phone or people id")
    p.add_argument("--next", type=int, help="the top N of the to_contact queue")
    p.add_argument("--spawn", action="store_true", help="actually launch sessions")
    p.add_argument("--idle", action="store_true", help="spawn without starting the prep")
    p.add_argument("--messages", type=int, default=DEFAULT_MESSAGES, help="per channel")
    p.add_argument("--print-context", metavar="PERSON", help="print one person's context block")
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
        except CliError as e:
            print(f"error: {e}", file=err)
            return 1
    return 0


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
