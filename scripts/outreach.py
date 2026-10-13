#!/usr/bin/env python3
# scripts/outreach.py
"""Work the referral outreach pipeline
(docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §7.4). Local only.

  python scripts/outreach.py today [--targets PATH] [--json]
  python scripts/outreach.py queue [--stage S] [--limit N] [--json]
  python scripts/outreach.py decide <person> [--skip [--note TEXT]]
  python scripts/outreach.py rank <person> <n> | --file PATH | --clear
  python scripts/outreach.py draft <person> [--subject S]
  python scripts/outreach.py mark <person> briefed|replied|declined|to_contact
                                  [--channel C] [--at T] [--from ADDR] [--force] [--note TEXT]
  python scripts/outreach.py nudge <person> --channel C [--at T] [--from ADDR] [--note TEXT]
  python scripts/outreach.py lead <person> "<name, company>"
  python scripts/outreach.py intro <referrer> "<buyer name, company>" [--buyer <person>] [--at T]
  python scripts/outreach.py conversation|proposal|signed <opportunity-id> [--at T]
  python scripts/outreach.py lost <opportunity-id> --reason R [--at T]
  python scripts/outreach.py followups
  python scripts/outreach.py report [--floors PATH] [--since D] [--until D]
  python scripts/outreach.py sync-asana

<person> is an email, an E.164 phone, or a people id. --at takes YYYY-MM-DD (noon, New
York time) or a full ISO timestamp; it defaults to now. Every stage change is committed
before any tasks-api, inbox-api or Google call, so a failed side effect never loses a
count: `sync-asana` retries the Asana half. `mark --force` reopens a declined or skipped
referrer to any stage but candidate (e.g. `mark replied --force` records a late reply
after a no-response decline). `today` reads its targets from --targets or OUTREACH_TARGETS;
`report` reads floors from --floors or OUTREACH_FLOORS (scripts/fetch-env.sh writes both).
`--note` goes on `decide --skip`, `mark ... declined` and `nudge`, and is never sent anywhere.
Runs against whatever DB clients/db.py
resolves from env, like the import scripts.
"""

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from clients import inbox_api
from models.outreach import CHANNELS, LOST_REASONS, REFERRER_STAGES, Advance
from services import (
    outreach,
    outreach_report,
    outreach_sync,
    outreach_targets,
    outreach_today,
    person_edit,
    person_lookup,
)
from services.ingest import parse_ts

LABEL = "consulting-outreach"  # Google Contacts label marking cohort membership (spec §7.1)
MARK_STAGES = ("briefed", "replied", "declined", "to_contact")


class CliError(Exception):
    pass


USER_ERRORS = (
    CliError,
    outreach.InvalidTransition,
    outreach.NotInPipeline,
    outreach.OpportunityNotFound,
    outreach.NotQueued,
    outreach_targets.TargetsError,
    outreach_report.FloorsError,
    inbox_api.InboxApiUnavailable,
    httpx.HTTPError,
    ValueError,
)


def parse_at(value: str | None, now: datetime) -> datetime:
    if not value:
        return now
    if len(value) == 10:  # a bare date: noon in New York, clear of any week boundary
        return datetime.combine(date.fromisoformat(value), time(12), tzinfo=outreach.TZ)
    return parse_ts(value)


def _label(person: dict) -> str:
    return person.get("display_name") or person.get("email") or f"person {person['id']}"


def _resolve(conn: Any, ident: str) -> dict:
    try:
        return person_lookup.resolve(conn, ident)
    except person_lookup.Ambiguous as e:
        raise CliError(f"{ident} matches several people ({e.candidates}); use an id") from e
    except person_lookup.NotFound as e:
        raise CliError(f"no person matches {ident}") from e


def _sync(conn: Any, res: Advance | None, pipeline: str, stage: str, out: TextIO) -> None:
    if res is None or res.event_id is None or not outreach_sync.produces_task(pipeline, stage):
        return
    result = outreach_sync.sync_events(conn, [res.event_id])
    if result.skipped:
        print("asana: TASKS_API_URL is unset; run `outreach sync-asana` once it is set", file=out)
    elif result.failed:
        print("asana: task not created yet; `outreach sync-asana` retries it", file=out)
    else:
        print("asana: task created and completed", file=out)


def _changed(res: Advance) -> str:
    return "" if res.changed else " (unchanged)"


def cmd_queue(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    items = outreach.queue(conn, stage=args.stage, limit=args.limit)
    if args.json:
        print(json.dumps(items, indent=2), file=out)
    elif items:
        rows = [
            [
                i["person"]["id"],
                i["rank"] or "",
                _label(i["person"]),
                i["person"]["company"] or "",
                i["last_interaction"] or "never",
                " ".join(k for k, v in i["channels"].items() if v),
                i["follow_up_on"] or "",
                "lead" if i["has_lead"] else "",
            ]
            for i in items
        ]
        print(
            outreach_report.table(
                ["id", "rank", "name", "company", "last", "channels", "follow-up", ""], rows
            ),
            file=out,
        )
    else:
        print(f"no contacts at {args.stage}", file=out)
    warn_to = sys.stderr if args.json else out
    for s in outreach.shared_briefs(conn):
        print(
            f"warning: one email ({s['graph_message_id']}) briefed {s['n']} contacts; "
            "a group note is not a brief",
            file=warn_to,
        )


def cmd_decide(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if args.note and not args.skip:
        raise CliError("--note goes with --skip (a reason to skip); put context in the notes")
    person = _resolve(conn, args.person)
    target = "skipped" if args.skip else "to_contact"
    outreach.ensure_contact(conn, person["id"], at=now, source="cli")
    res = outreach.advance(conn, person["id"], target, at=now, source="cli", note=args.note)
    if res is None:  # ensure_contact just made the row; only a concurrent delete gets here
        raise CliError(f"{_label(person)} vanished from the pipeline")
    conn.commit()
    print(f"{_label(person)}: {res.row['stage']}{_changed(res)}", file=out)
    if target != "to_contact":
        return
    try:
        person_edit.update(conn, person["id"], labels={"add": [LABEL]})
        conn.commit()
    except Exception as e:  # the label is a convenience; the stage is already recorded
        conn.rollback()
        why = "no Google contact is linked yet" if isinstance(e, person_edit.NotLinked) else e
        print(f"warning: stage recorded, but the {LABEL} label was not added: {why}", file=out)


def cmd_draft(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    person = _resolve(conn, args.person)
    if not person.get("email"):
        raise CliError(f"{_label(person)} has no email address; brief them by hand and `mark`")
    contact = outreach.get_contact(conn, person["id"])
    stage = contact["stage"] if contact else "not in the pipeline"
    if stage != "to_contact":
        print(f"warning: {_label(person)} is {stage}, not to_contact", file=out)
    sender = outreach.brief_from_address()
    if not sender:
        raise CliError("OUTREACH_FROM_ADDRESSES is unset; no address to send the brief from")
    draft = inbox_api.create_draft([person["email"]], subject=args.subject, from_address=sender)
    print(f"draft created from {sender}: {draft.get('web_link') or draft.get('id')}", file=out)


def cmd_mark(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if args.note and args.stage != "declined":
        raise CliError("--note applies only to declined")
    if args.stage in ("briefed", "replied") and not args.channel:
        raise CliError("--channel is required for briefed and replied")
    if args.from_address and args.channel != "email":
        raise CliError("--from applies only with --channel email")
    person = _resolve(conn, args.person)
    res = outreach.advance(
        conn,
        person["id"],
        args.stage,
        at=parse_at(args.at, now),
        channel=args.channel,
        source="cli",
        from_address=args.from_address,
        force=args.force,
        note=args.note,
    )
    if res is None:
        raise CliError(f"{_label(person)} is not in the pipeline; run `outreach decide` first")
    conn.commit()
    print(f"{_label(person)}: {res.row['stage']}{_changed(res)}", file=out)
    _sync(conn, res, "referrer", args.stage, out)


def cmd_lead(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    person = _resolve(conn, args.person)
    outreach.set_lead(conn, person["id"], args.lead, today=now.astimezone(outreach.TZ).date())
    conn.commit()
    print(f"{_label(person)}: lead noted ({args.lead})", file=out)


def cmd_intro(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    referrer = _resolve(conn, args.referrer)
    buyer_id = _resolve(conn, args.buyer)["id"] if args.buyer else None
    res = outreach.open_opportunity(
        conn,
        referrer["id"],
        args.buyer_label,
        at=parse_at(args.at, now),
        buyer_id=buyer_id,
        source="cli",
    )
    conn.commit()
    print(f"opportunity {res.row['id']}: {res.row['buyer_label']} via {_label(referrer)}", file=out)
    _sync(conn, res, "buyer", "intro", out)


def cmd_buyer(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    stage = args.command
    res = outreach.advance_opportunity(
        conn,
        args.opportunity,
        stage,
        at=parse_at(args.at, now),
        lost_reason=getattr(args, "reason", None),
        source="cli",
    )
    conn.commit()
    print(
        f"opportunity {res.row['id']} ({res.row['buyer_label']}): {res.row['stage']}{_changed(res)}",
        file=out,
    )
    _sync(conn, res, "buyer", stage, out)


def _config_path(flag: str | None, env: str) -> Path | None:
    value = flag or os.environ.get(env)
    return Path(value).expanduser() if value else None


def cmd_today(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    path = _config_path(args.targets, "OUTREACH_TARGETS")
    if path is None:
        raise CliError("no targets file: pass --targets PATH or set OUTREACH_TARGETS")
    targets = outreach_targets.load_targets(path)
    counts = outreach.today_counts(
        conn,
        today=now.astimezone(outreach.TZ).date(),
        per_day=targets.per_day,
        plan_start=targets.start,
        plan_end=targets.end,
    )
    today = outreach_today.build(counts, targets, now=now)
    print(outreach_today.to_json(today) if args.json else outreach_today.render(today), file=out)


def cmd_nudge(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if args.from_address and args.channel != "email":
        raise CliError("--from applies only with --channel email")
    person = _resolve(conn, args.person)
    contact = outreach.get_contact(conn, person["id"])
    if contact is None:
        raise CliError(f"{_label(person)} is not in the pipeline; run `outreach decide` first")
    if contact["stage"] != "briefed":
        raise CliError(
            f"{_label(person)} is at {contact['stage']}; only a briefed contact is nudged"
        )
    event_id = outreach.nudge(
        conn,
        person["id"],
        at=parse_at(args.at, now),
        channel=args.channel,
        source="cli",
        from_address=args.from_address,
        note=args.note,
    )
    if event_id is None:
        raise CliError(
            f"{_label(person)}: a nudge must come at least a day after the brief "
            f"({contact['briefed_at'].astimezone(outreach.TZ).date()})"
        )
    conn.commit()
    row = outreach.get_contact(conn, person["id"]) or {}
    print(
        f"{_label(person)}: nudge {row.get('nudge_count')} recorded; "
        f"next follow-up {row.get('follow_up_on')}",
        file=out,
    )


def _rank_file(conn: Any, path: Path) -> list[int]:
    try:
        text = path.read_text()
    except FileNotFoundError as e:
        raise CliError(f"rank file not found: {path}") from e
    ids: list[int] = []
    seen: dict[int, int] = {}
    bad: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            person = _resolve(conn, line)
        except CliError as e:
            bad.append(f"  line {lineno}: {e}")
            continue
        contact = outreach.get_contact(conn, person["id"])
        stage = contact["stage"] if contact else "not in the pipeline"
        if stage != "to_contact":
            bad.append(f"  line {lineno}: {line} is {stage}, not to_contact")
        elif person["id"] in seen:
            bad.append(f"  line {lineno}: {line} repeats line {seen[person['id']]}")
        else:
            seen[person["id"]] = lineno
            ids.append(person["id"])
    if bad:
        raise CliError("rank file refused; nothing changed:\n" + "\n".join(bad))
    if not ids:
        raise CliError("rank file lists nobody; use `outreach rank --clear` to clear ranks")
    return ids


def cmd_rank(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    forms = sum([args.clear, args.file is not None, args.person is not None])
    if forms != 1:
        raise CliError("give exactly one of: outreach rank <person> <n>, --file PATH, --clear")
    if args.clear:
        n = outreach.clear_ranks(conn)
        conn.commit()
        print(f"cleared {n} ranks", file=out)
        return
    if args.file is not None:
        ids = _rank_file(conn, Path(args.file).expanduser())
        outreach.set_ranks(conn, ids)
        conn.commit()
        print(f"ranked {len(ids)}; every other to_contact rank cleared", file=out)
        return
    if args.n is None:
        raise CliError("give the rank: outreach rank <person> <n>")
    person = _resolve(conn, args.person)
    outreach.set_rank(conn, person["id"], args.n)
    conn.commit()
    print(f"{_label(person)}: rank {args.n}", file=out)


def cmd_followups(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    items = outreach.follow_ups(conn, today=now.astimezone(outreach.TZ).date())
    if not items:
        print("no follow-ups due", file=out)
        return
    print(
        outreach_report.table(
            ["id", "name", "action", "briefed", "due", "channel"],
            [
                [
                    i["person"]["id"],
                    _label(i["person"]),
                    i["kind"],
                    i["brief"]["at"] or "",
                    i["follow_up_on"] + (" (overdue)" if i["overdue"] else ""),
                    _how(i),
                ]
                for i in items
            ],
        ),
        file=out,
    )


def _how(item: dict) -> str:
    brief = item["brief"]["channel"]
    if item["kind"] == "close":
        return f"outreach mark {item['person']['id']} declined"
    via = item["suggested_channel"] or brief or "any channel"
    return f"switch to {via}" if item["kind"] == "nudge 2" and via != brief else via


def cmd_report(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    today = now.astimezone(outreach.TZ).date()
    path = _config_path(args.floors, "OUTREACH_FLOORS")
    floors = outreach_report.load_floors(path) if path else None
    counts = outreach.counts(
        conn,
        today=today,
        since=date.fromisoformat(args.since) if args.since else None,
        until=date.fromisoformat(args.until) if args.until else None,
    )
    verdicts = outreach_report.stage_verdicts(counts, floors, today=today) if floors else None
    print(outreach_report.render(counts, verdicts), file=out)
    below = sum(1 for v in verdicts or [] if v.verdict == "below floor")
    otel.outreach_report_runs.add(1, {"verdict_below": str(below)})


def cmd_sync_asana(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    res = outreach_sync.sync_pending(conn)
    if res.skipped:
        raise CliError("TASKS_API_URL is unset")
    late = ", ".join(f"{bucket} {n}" for bucket, n in sorted(res.late.items()) if bucket != "0d")
    print(
        f"asana: synced {res.synced}, failed {res.failed}" + (f"; late: {late}" if late else ""),
        file=out,
    )


COMMANDS: dict[str, Callable[[Any, argparse.Namespace, TextIO, datetime], None]] = {
    "today": cmd_today,
    "queue": cmd_queue,
    "decide": cmd_decide,
    "draft": cmd_draft,
    "mark": cmd_mark,
    "nudge": cmd_nudge,
    "rank": cmd_rank,
    "lead": cmd_lead,
    "intro": cmd_intro,
    "conversation": cmd_buyer,
    "proposal": cmd_buyer,
    "signed": cmd_buyer,
    "lost": cmd_buyer,
    "followups": cmd_followups,
    "report": cmd_report,
    "sync-asana": cmd_sync_asana,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="outreach", description="Work the referral outreach pipeline.")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("today")
    t.add_argument("--targets")
    t.add_argument("--json", action="store_true")
    q = sub.add_parser("queue")
    q.add_argument("--stage", default="to_contact", choices=REFERRER_STAGES)
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--json", action="store_true")
    d = sub.add_parser("decide")
    d.add_argument("person")
    d.add_argument("--skip", action="store_true")
    d.add_argument("--note")
    dr = sub.add_parser("draft")
    dr.add_argument("person")
    dr.add_argument("--subject", default="")
    m = sub.add_parser("mark")
    m.add_argument("person")
    m.add_argument("stage", choices=MARK_STAGES)
    m.add_argument("--channel", choices=CHANNELS)
    m.add_argument("--at")
    m.add_argument("--from", dest="from_address")
    m.add_argument("--force", action="store_true")
    m.add_argument("--note")
    n = sub.add_parser("nudge")
    n.add_argument("person")
    n.add_argument("--channel", required=True, choices=CHANNELS)
    n.add_argument("--at")
    n.add_argument("--from", dest="from_address")
    n.add_argument("--note")
    rk = sub.add_parser("rank")
    rk.add_argument("person", nargs="?")
    rk.add_argument("n", nargs="?", type=int)
    rk.add_argument("--file")
    rk.add_argument("--clear", action="store_true")
    le = sub.add_parser("lead")
    le.add_argument("person")
    le.add_argument("lead")
    i = sub.add_parser("intro")
    i.add_argument("referrer")
    i.add_argument("buyer_label")
    i.add_argument("--buyer")
    i.add_argument("--at")
    for stage in ("conversation", "proposal", "signed"):
        b = sub.add_parser(stage)
        b.add_argument("opportunity", type=int)
        b.add_argument("--at")
    lo = sub.add_parser("lost")
    lo.add_argument("opportunity", type=int)
    lo.add_argument("--reason", required=True, choices=LOST_REASONS)
    lo.add_argument("--at")
    sub.add_parser("followups")
    r = sub.add_parser("report")
    r.add_argument("--floors")
    r.add_argument("--since")
    r.add_argument("--until")
    sub.add_parser("sync-asana")
    return p


def main_with(
    argv: list[str],
    get_conn: Callable[[], Any],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: datetime | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    now = now or datetime.now(UTC)
    otel.setup_telemetry("people-outreach-local")
    try:
        with get_conn() as conn:
            try:
                with contextlib.redirect_stderr(err):
                    COMMANDS[args.command](conn, args, out, now)
            except USER_ERRORS as e:
                conn.rollback()
                print(f"error: {e}", file=err)
                return 1
            try:
                outreach.record_population(conn, today=now.astimezone(outreach.TZ).date())
            except Exception:
                pass  # telemetry only; never fail a command over a gauge
        return 0
    finally:
        # A short-lived local process exports nothing unless it flushes (spec §12.2).
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
