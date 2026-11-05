"""`outreach linkedin …`: the LinkedIn send queue's CLI
(docs/superpowers/specs/2026-11-05-linkedin-send-queue-design.md, "CLI"). Registered by
scripts/outreach.py, which owns the connection, the commit-or-rollback and the errors.

  outreach linkedin queue  (--person P | --username SLUG) --kind K --message-file F|-
                           [--send-now] [--not-before T] [--thread-id ID] [--profile-urn URN]
  outreach linkedin list   [--status S] [--json] [--show-text]
  outreach linkedin cancel <id>
  outreach linkedin edit   <id> --message-file F|-          (needs Ben's new approval)
  outreach linkedin result <id> (--mcp-result JSON|@FILE | --status S [--thread-id] [--reason])
  outreach linkedin record (--person P | --username SLUG) --kind K --message-file F|- [--at T]
  outreach linkedin requeue <id>
  outreach linkedin pause  [--reason R] | resume | status [--json]

Message text is printed only by `list --show-text` and by a send-now `queue` (the exact
send_message arguments the session must pass)."""

import argparse
import json
import os
import random
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, TextIO

from models.linkedin_outbox import KINDS, STATUSES
from services import linkedin_outbox, linkedin_pacing, outreach, person_lookup
from services.ingest import parse_ts
from services.linkedin_export import normalize_profile_url
from services.linkedin_pacing import InvalidMessage

OPEN_STATUSES = ("queued", "sending", "set_aside", "check_linkedin")
SHOW_TZ = linkedin_pacing.SendConfig().window.tz

__all__ = ["ERRORS", "InvalidMessage", "LinkedInCliError", "cmd_linkedin", "register"]


class LinkedInCliError(Exception):
    pass


ERRORS = (
    LinkedInCliError,
    linkedin_outbox.CeilingReached,
    linkedin_outbox.OutboxNotFound,
    linkedin_outbox.NotEditable,
    linkedin_outbox.NotResolvable,
    person_lookup.NotFound,
    person_lookup.Ambiguous,
)


def _cfg() -> linkedin_pacing.SendConfig:
    return linkedin_pacing.load_config(os.environ)


def _at(value: str | None, now: datetime) -> datetime:
    if not value:
        return now
    if len(value) == 10:  # a bare date: noon in New York, like `outreach mark --at`
        return datetime.combine(date.fromisoformat(value), time(12), tzinfo=outreach.TZ)
    return parse_ts(value)


def _show(ts: datetime | None) -> str:
    return ts.astimezone(SHOW_TZ).strftime("%a %H:%M %Z") if ts else "-"


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).expanduser().read_text()
    except OSError as e:
        raise LinkedInCliError(f"can't read {path}: {e.strerror}") from e


def _slug(value: str | None) -> str | None:
    if not value:
        return None
    url = normalize_profile_url(value)
    if url:
        return url.rsplit("/", 1)[1]
    return value.strip().strip("/") or None


def _recipient(conn: Any, args: argparse.Namespace) -> tuple[int | None, str, str]:
    """(person_id, username, label) from --person and/or --username."""
    if not args.person and not args.username:
        raise LinkedInCliError("pass --person or --username")
    person = None
    if args.person:
        try:
            person = person_lookup.resolve(conn, args.person)
        except person_lookup.Ambiguous as e:
            raise LinkedInCliError(f"{args.person} matches several people ({e.candidates})") from e
        except person_lookup.NotFound as e:
            raise LinkedInCliError(f"no person matches {args.person}") from e
    username = _slug(args.username) or _slug(person.get("linkedin_url") if person else None)
    if not username:
        raise LinkedInCliError(
            f"{args.person} has no LinkedIn profile URL; pass --username <slug> "
            "(and add the URL with editing-person)"
        )
    label = (person.get("display_name") or person.get("email")) if person else None
    return (person["id"] if person else None), username, label or username


def _brief(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "message"}


def cmd_queue(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    cfg = _cfg()
    person_id, username, label = _recipient(conn, args)
    row = linkedin_outbox.queue(
        conn,
        person_id=person_id,
        username=username,
        message=_read(args.message_file),
        kind=args.kind,
        send_now=args.send_now,
        now=now,
        cfg=cfg,
        not_before=_at(args.not_before, now) if args.not_before else None,
        thread_id=args.thread_id,
        profile_urn=args.profile_urn,
        session_hint=args.session_hint or os.environ.get("AGENTDECK_INSTANCE_ID"),
    )
    conn.commit()
    if args.send_now:
        send_args: dict[str, Any] = {
            "linkedin_username": row["linkedin_username"],
            "message": row["message"],
            "confirm_send": True,
        }
        if row["profile_urn"]:
            send_args["profile_urn"] = row["profile_urn"]
        print(f"#{row['id']} to {label} recorded as sending (send now).", file=out)
        print("Call mcp__linkedin__send_message with exactly:", file=out)
        print(json.dumps(send_args, indent=2, ensure_ascii=False), file=out)
        print(
            f"then report what it returned: outreach linkedin result {row['id']} "
            "--mcp-result '<the JSON it returned>'",
            file=out,
        )
        return
    est = linkedin_outbox.estimate_send_times(conn, now=now, cfg=cfg).get(row["id"])
    when = _show(est) if est else "when the queue resumes (outreach linkedin status)"
    print(f"#{row['id']} to {label} queued ({row['kind']}); expected to send {when}", file=out)


def cmd_list(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    statuses = (args.status,) if args.status else OPEN_STATUSES
    rows = linkedin_outbox.list_rows(conn, statuses)
    est = linkedin_outbox.estimate_send_times(conn, now=now, cfg=_cfg())
    if args.json:
        data = [
            {**(row if args.show_text else _brief(row)), "expected_at": est.get(row["id"])}
            for row in rows
        ]
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False), file=out)
        return
    if not rows:
        print("Nothing in the LinkedIn queue.", file=out)
        return
    for row in rows:
        detail = (
            f"expected {_show(est.get(row['id']))}"
            if row["status"] == "queued"
            else (row["reason"] or "")
        )
        print(
            f"{row['id']:>5}  {row['status']:<14} {row['kind']:<6} {row['mode']:<8} "
            f"{row['linkedin_username']:<28} approved {_show(row['approved_at'])}  {detail}",
            file=out,
        )
        if args.show_text:
            for line in row["message"].splitlines() or [""]:
                print(f"         | {line}", file=out)


def cmd_cancel(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    linkedin_outbox.cancel(conn, args.id)
    conn.commit()
    print(f"#{args.id} cancelled", file=out)


def cmd_edit(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    linkedin_outbox.edit(conn, args.id, _read(args.message_file), now=now)
    conn.commit()
    print(f"#{args.id} updated; approval reset to now (back of the queue)", file=out)


def _mcp_result(value: str) -> dict:
    text = _read(value[1:]) if value.startswith("@") else value
    try:
        data = json.loads(text)
    except ValueError as e:
        raise LinkedInCliError(f"--mcp-result is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise LinkedInCliError("--mcp-result must be a JSON object")
    return data


def cmd_result(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if bool(args.status) == bool(args.mcp_result):
        raise LinkedInCliError("pass exactly one of --status or --mcp-result")
    rng = random.SystemRandom()
    if args.mcp_result:
        outcome = linkedin_outbox.apply_result(
            conn, args.id, _mcp_result(args.mcp_result), now=now, cfg=_cfg(), rng=rng
        )
        conn.commit()
        msg = f"#{args.id}: {outcome.row_status}"
        if outcome.reason:
            msg += f" ({outcome.reason})"
        if outcome.thread_mismatch:
            msg += "; sent into a different thread than the one it answered"
        if outcome.pause:
            msg += "; the queue is paused (outreach linkedin status)"
        print(msg, file=out)
        return
    linkedin_outbox.resolve(
        conn,
        args.id,
        args.status,
        now=now,
        cfg=_cfg(),
        rng=rng,
        thread_id=args.thread_id,
        reason=args.reason,
    )
    conn.commit()
    print(f"#{args.id}: {args.status}", file=out)


def cmd_record(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    person_id, username, label = _recipient(conn, args)
    row = linkedin_outbox.record_manual(
        conn,
        person_id=person_id,
        username=username,
        message=_read(args.message_file),
        kind=args.kind,
        at=_at(args.at, now),
        now=now,
        cfg=_cfg(),
        rng=random.SystemRandom(),
    )
    conn.commit()
    event = " and its pipeline event" if row.get("outreach_event_id") else ""
    print(f"#{row['id']}: recorded your LinkedIn {args.kind} to {label}{event}", file=out)


def cmd_requeue(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    linkedin_outbox.requeue(conn, args.id)
    conn.commit()
    print(f"#{args.id} back in the queue", file=out)


def cmd_pause(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    linkedin_outbox.pause(conn, args.reason)
    conn.commit()
    print("LinkedIn queue paused; `outreach linkedin resume` restarts it", file=out)


def cmd_resume(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    linkedin_outbox.resume(conn)
    conn.commit()
    print("LinkedIn queue resumed", file=out)


def cmd_status(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    st = linkedin_outbox.status(conn, now=now, cfg=_cfg())
    if args.json:
        print(json.dumps(st, indent=2, default=str), file=out)
        return
    lines = [
        f"Ceiling  today {st['sent_today']}/{st['cap_today']} · "
        f"week {st['sent_week']}/{st['cap_week']}" + ("  (reached)" if st["reached"] else ""),
        f"Queue    {st['queued']} queued · {st['check_linkedin']} to check on LinkedIn · "
        f"{st['set_aside']} set aside",
    ]
    if st["paused_reason"]:
        lines.append(f"PAUSED   {st['paused_reason']}: fix it, then `outreach linkedin resume`")
    elif st["paused_until"]:
        lines.append(f"PAUSED   rate limited until {_show(st['paused_until'])} (clears itself)")
    lines.append(f"Next     {_show(st['next_send_at'])}")
    lines.append(f"Last     sent {_show(st['last_sent_at'])}; last error {st['last_error'] or '-'}")
    if st["thread_mismatch"]:
        lines.append(f"Threads  {st['thread_mismatch']} sent into a new DM this week")
    print("\n".join(lines), file=out)


COMMANDS = {
    "queue": cmd_queue,
    "list": cmd_list,
    "cancel": cmd_cancel,
    "edit": cmd_edit,
    "result": cmd_result,
    "record": cmd_record,
    "requeue": cmd_requeue,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "status": cmd_status,
}


def cmd_linkedin(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    COMMANDS[args.linkedin_command](conn, args, out, now)


def _recipient_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--person")
    p.add_argument("--username", help="profile slug or URL")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--message-file", required=True, help="path, or - for stdin")


def register(sub: Any) -> None:
    li = sub.add_parser("linkedin", help="the LinkedIn send queue")
    lsub = li.add_subparsers(dest="linkedin_command", required=True)
    q = lsub.add_parser("queue")
    _recipient_args(q)
    q.add_argument("--send-now", action="store_true")
    q.add_argument("--not-before")
    q.add_argument("--thread-id")
    q.add_argument("--profile-urn")
    q.add_argument("--session-hint")
    ls = lsub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--show-text", action="store_true")
    for name in ("cancel", "requeue"):
        lsub.add_parser(name).add_argument("id", type=int)
    e = lsub.add_parser("edit")
    e.add_argument("id", type=int)
    e.add_argument("--message-file", required=True)
    r = lsub.add_parser("result")
    r.add_argument("id", type=int)
    r.add_argument("--mcp-result", help="the JSON send_message returned, or @FILE")
    r.add_argument("--status", choices=linkedin_outbox.RESOLVE_STATUSES)
    r.add_argument("--thread-id")
    r.add_argument("--reason")
    rec = lsub.add_parser("record")
    _recipient_args(rec)
    rec.add_argument("--at")
    pa = lsub.add_parser("pause")
    pa.add_argument("--reason")
    lsub.add_parser("resume")
    st = lsub.add_parser("status")
    st.add_argument("--json", action="store_true")
