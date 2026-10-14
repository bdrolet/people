#!/usr/bin/env python3
# scripts/outreach_seed.py
"""Propose referral candidates and insert them at `candidate`
(docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §7.1). Local only.

  python scripts/outreach_seed.py [--window-days 730] [--min-messages 10] [--dry-run]

Idempotent: a person already in the pipeline is never touched, so it never downgrades
anyone. A warm LinkedIn connection with no people row is linked to the row already
holding its URL, or created as a Google Contact labelled `from-linkedin` and entered
(docs/superpowers/specs/2026-10-13-linkedin-identity-design.md §5). Possible
duplicates and contacts Google has but people hasn't synced are listed, never
created. Each person is committed on its own, so one failure never undoes the rest;
the run exits 1 if any create failed. Names print to this terminal only.
"""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.google_contacts as gc
import clients.otel as otel
from repo import linkedin as linkedin_repo
from repo import outreach as outreach_repo
from services import outreach, outreach_seed, person_create

FROM_LINKEDIN_LABEL = "from-linkedin"


@dataclass
class SeedRun:
    plan: outreach_seed.SeedPlan
    entered: int = 0  # candidates newly entered at `candidate`, every source
    linked: int = 0
    created: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)


def _enter(conn: Any, profile_url: str, person_id: int, now: datetime) -> None:
    linkedin_repo.link_connection(conn, profile_url, person_id)
    outreach.ensure_contact(conn, person_id, at=now, source="seed")


def _reason(e: Exception) -> str:
    if isinstance(e, person_create.Duplicate):
        return f"already exists (candidates {e.candidates or 'in Google only'})"
    return f"{type(e).__name__}: {e}"


def run(
    get_conn: Callable[[], Any],
    *,
    now: datetime,
    window_days: int,
    min_messages: int,
    dry_run: bool,
) -> SeedRun:
    google_urls, google_contacts = gc.list_url_index()
    with get_conn() as conn:
        result = SeedRun(
            outreach_seed.plan(
                email_rows=outreach_repo.email_seed_rows(conn),
                linkedin_rows=outreach_repo.linkedin_seed_rows(conn),
                imessage_rows=outreach_repo.imessage_seed_rows(conn),
                whatsapp_rows=outreach_repo.whatsapp_seed_rows(conn),
                existing=outreach_repo.pipeline_person_ids(conn),
                now=now,
                window_days=window_days,
                min_messages=min_messages,
                people_rows=outreach_repo.people_seed_rows(conn),
                google_urls=google_urls,
                google_contacts=google_contacts,
            )
        )
        if dry_run:
            conn.rollback()
            return result
        for person_id in sorted(result.plan.candidates):
            if outreach.ensure_contact(conn, person_id, at=now, source="seed") is not None:
                result.entered += 1
        conn.commit()
        for o in result.plan.linkedin_of("link"):
            assert o.person_id is not None
            _enter(conn, o.row["profile_url"], o.person_id, now)
            conn.commit()
            result.linked += 1
        for o in result.plan.linkedin_of("create"):
            try:
                row = person_create.create(
                    conn,
                    contact=outreach_seed.contact_for(o.row),
                    labels=[FROM_LINKEDIN_LABEL],
                )
                _enter(conn, o.row["profile_url"], row["id"], now)
                conn.commit()
                result.created += 1
            except Exception as e:  # one person's failure never stops the run (design §5)
                conn.rollback()
                result.failures.append((o.row["full_name"], _reason(e)))
    return result


_TITLES = {
    "link": "Link",
    "create": "Create",
    "possible_duplicate": "Possible duplicate (not created)",
    "in_google": "In Google, not synced (not created; the nightly sync adopts it)",
}


def _linkedin_line(o: outreach_seed.LinkedInOutcome) -> str:
    who = f"  {o.row['full_name']}  {o.row['profile_url']}"
    if o.kind == "link":
        return f"{who}  → person {o.person_id}"
    if o.kind == "possible_duplicate":
        found = [f"person {i}" for i in o.candidates]
        found += [f"in Google, no row: {n}" for n in o.google_names]
        return f"{who}  ~ {', '.join(found)}"
    return who


def summary(r: SeedRun, *, dry_run: bool) -> str:
    p = r.plan
    lines = ["DRY RUN — nothing written"] if dry_run else []
    by_source = ", ".join(f"{s} {n}" for s, n in p.by_source().items())
    lines.append(
        f"candidates {len(p.candidates)} ({by_source}); already in the pipeline {len(p.already_in)}"
    )
    li = {k: p.linkedin_of(k) for k in outreach_seed.LINKEDIN_KINDS}
    lines.append(
        "LinkedIn without a people row: "
        f"link {len(li['link'])}, create {len(li['create'])}, "
        f"possible duplicate {len(li['possible_duplicate'])}, "
        f"in Google not synced {len(li['in_google'])}"
    )
    if not dry_run:
        lines.append(
            f"entered {r.entered}; linked {r.linked}; created {r.created}; failed {len(r.failures)}"
        )
    for kind in ("link", "create", "possible_duplicate", "in_google"):
        if li[kind]:
            lines.append(f"{_TITLES[kind]}:")
            lines += [_linkedin_line(o) for o in li[kind]]
    if r.failures:
        lines.append("Failed:")
        lines += [f"  {name}  {reason}" for name, reason in r.failures]
    if p.unlinked:
        lines.append(
            f"{len(p.unlinked)} warm iMessage/WhatsApp contacts have no people row; "
            "add them with creating-person:"
        )
        lines += [f"  {u.source:<9} {u.name or '(no name)'}  {u.detail}" for u in p.unlinked]
    return "\n".join(lines)


def main_with(argv: list[str], get_conn: Callable[[], Any]) -> None:
    ap = argparse.ArgumentParser(prog="outreach_seed")
    ap.add_argument("--window-days", type=int, default=730)
    ap.add_argument("--min-messages", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    otel.setup_telemetry("people-outreach-local")
    try:
        r = run(
            get_conn,
            now=datetime.now(UTC),
            window_days=args.window_days,
            min_messages=args.min_messages,
            dry_run=args.dry_run,
        )
        print(summary(r, dry_run=args.dry_run))
    finally:
        otel.flush()
    if r.failures:
        sys.exit(1)


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
