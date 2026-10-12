#!/usr/bin/env python3
# scripts/outreach_seed.py
"""Propose referral candidates and insert them at `candidate`
(docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §7.1). Local only.

  python scripts/outreach_seed.py [--window-days 730] [--min-messages 10] [--dry-run]

Idempotent: a person already in the pipeline is never touched, so it never downgrades
anyone. Candidates are counted, not named (`outreach queue --stage candidate` lists
them). People found on LinkedIn, iMessage or WhatsApp with no people row are listed
by name for Ben to add through the creating-person skill; they print to this terminal
only.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from repo import outreach as outreach_repo
from services import outreach, outreach_seed


def run(
    get_conn: Callable[[], Any],
    *,
    now: datetime,
    window_days: int,
    min_messages: int,
    dry_run: bool,
) -> outreach_seed.SeedPlan:
    with get_conn() as conn:
        p = outreach_seed.plan(
            email_rows=outreach_repo.email_seed_rows(conn),
            linkedin_rows=outreach_repo.linkedin_seed_rows(conn),
            imessage_rows=outreach_repo.imessage_seed_rows(conn),
            whatsapp_rows=outreach_repo.whatsapp_seed_rows(conn),
            existing=outreach_repo.pipeline_person_ids(conn),
            now=now,
            window_days=window_days,
            min_messages=min_messages,
        )
        if dry_run:
            conn.rollback()
        else:
            for person_id in sorted(p.candidates):
                outreach.ensure_contact(conn, person_id, at=now, source="seed")
    return p


def summary(p: outreach_seed.SeedPlan, *, dry_run: bool) -> str:
    lines = ["DRY RUN — nothing written"] if dry_run else []
    by_source = ", ".join(f"{s} {n}" for s, n in p.by_source().items())
    lines.append(
        f"candidates {len(p.candidates)} ({by_source}); already in the pipeline {len(p.already_in)}"
    )
    if p.unlinked:
        lines.append(
            f"{len(p.unlinked)} warm contacts have no people row; add them with creating-person:"
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
        p = run(
            get_conn,
            now=datetime.now(UTC),
            window_days=args.window_days,
            min_messages=args.min_messages,
            dry_run=args.dry_run,
        )
        print(summary(p, dry_run=args.dry_run))
    finally:
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
