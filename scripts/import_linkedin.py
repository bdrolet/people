#!/usr/bin/env python3
# scripts/import_linkedin.py
"""Load a LinkedIn data export into the linkedin_* snapshot tables
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §5). Local only.

  python scripts/import_linkedin.py <export-dir-or-zip> [--me <profile-url>] [--dry-run]

Runs against whatever DB clients/db.py resolves from env, like import_contacts.py.
Parses the whole export before opening the DB, so a malformed file writes
nothing. The replace is one transaction: an error leaves the previous snapshot
intact. --dry-run still reads people names so match counts are real, but
writes nothing. Output is counts only — never names or message content.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from models.linkedin import LinkedInSnapshot
from repo import linkedin as linkedin_repo
from repo import people as people_repo
from services import linkedin_export


def run(get_conn: Callable, path: Path, *, me: str | None, dry_run: bool) -> LinkedInSnapshot:
    snapshot = linkedin_export.parse_export(path, me=me)
    with get_conn() as conn:
        linkedin_export.match_people(snapshot, people_repo.names_for_matching(conn))
        if dry_run:
            conn.rollback()
        else:
            linkedin_repo.replace_snapshot(conn, snapshot)
    return snapshot


def summary(s: LinkedInSnapshot, *, dry_run: bool) -> str:
    n = len(s.connections)
    unmatched = n - s.matched_by_email - s.matched_by_name
    given = [r for r in s.recommendations if r.direction == "given"]
    received = [r for r in s.recommendations if r.direction == "received"]
    lines = ["DRY RUN — nothing written"] if dry_run else []
    lines += [
        f"connections {n:,} (email-matched {s.matched_by_email:,}, "
        f"name-matched {s.matched_by_name:,}, unmatched {unmatched:,})",
        f"messages {len(s.messages):,} in {s.conversations:,} conversations (me={s.me}; "
        f"by-url {s.messages_by_url:,}, by-name {s.messages_by_name:,}, "
        f"unmatched {s.messages_unmatched:,}, skipped {s.messages_skipped:,})",
        f"recommendations given {len(given):,} (linked {sum(1 for r in given if r.profile_url):,}), "
        f"received {len(received):,} (linked {sum(1 for r in received if r.profile_url):,})",
        f"snapshot_at {s.snapshot_at:%Y-%m-%dT%H:%M:%SZ}",
    ]
    if s.missing_files:
        lines.append("missing (treated as empty): " + ", ".join(s.missing_files))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("export", type=Path, help="unzipped export directory or the .zip")
    p.add_argument("--me", help="Ben's LinkedIn profile URL; inferred from messages if omitted")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    from clients.db import get_conn

    try:
        snapshot = run(get_conn, args.export.expanduser(), me=args.me, dry_run=args.dry_run)
    except linkedin_export.ExportError as e:
        print(f"export error: {e}", file=sys.stderr)
        sys.exit(2)
    print(summary(snapshot, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
