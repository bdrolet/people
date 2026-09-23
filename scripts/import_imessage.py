#!/usr/bin/env python3
# scripts/import_imessage.py
"""Import the local Messages database (chat.db) into the imessage_* snapshot
tables (docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §5). Local
only — requires Full Disk Access; people's Cloud Functions never read iMessage.

  python scripts/import_imessage.py [--full] [--dry-run] [--db ~/Library/Messages/chat.db]

Runs against whatever DB clients/db.py resolves from env, like import_contacts.py
and import_linkedin.py. Reads chat.db, builds the Google Contacts phone index,
matches every handle, then writes in one transaction (spec §5.5): --dry-run reads,
builds the index, and matches, but writes nothing. Output is counts only — never
names, numbers, or message content (spec §6).
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients import google_contacts, imessage_local
from models.imessage import IMessageBatch
from repo import imessage as imessage_repo
from repo import people as people_repo
from services import imessage_export

WINDOW_DAYS = 14

FULL_DISK_ACCESS_MSG = (
    "grant Full Disk Access to your terminal: "
    "System Settings → Privacy & Security → Full Disk Access"
)


def run(get_conn: Callable[[], Any], path: Path, *, full: bool, dry_run: bool) -> IMessageBatch:
    since_rowid = 0
    if not full:
        with get_conn() as conn:
            since_rowid = imessage_repo.latest_watermark(conn)

    window_start = None if full else datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    raw = imessage_local.read(path, since_rowid=since_rowid, window_start=window_start, full=full)
    batch = imessage_export.build_batch(raw, mode="full" if full else "incremental")

    # Outside/before the write transaction (spec §5.5): a Google API error must
    # abort before any write, and no handle is written with a partial match set.
    phone_index = google_contacts.list_phone_index()

    with get_conn() as conn:
        people_rows = people_repo.rows_for_imessage_matching(conn)
        imessage_export.match_handles(batch, phone_index, people_rows)

        if dry_run:
            conn.rollback()
        else:
            imessage_repo.upsert_chats(conn, batch.chats)
            imessage_repo.upsert_messages(conn, batch.messages)
            deleted = 0
            if full:
                deleted = imessage_repo.delete_missing(
                    conn, batch.all_guids or set(), batch.all_chat_guids or set()
                )
            imessage_repo.upsert_handles(conn, batch.handles)
            imessage_repo.recompute_handle_stats(conn)
            imessage_repo.record_import(conn, batch, messages_deleted=deleted)

    return batch


def summary(batch: IMessageBatch, *, dry_run: bool, deleted: int) -> str:
    unmatched = len(batch.handles) - batch.matched_by_email - batch.matched_by_google
    group_chats = sum(1 for c in batch.chats if c.is_group)
    lines = ["DRY RUN — nothing written"] if dry_run else []
    lines += [
        f"messages {len(batch.messages):,} upserted (undecoded {batch.undecoded:,}, "
        f"retracted {batch.retracted:,}, deleted {deleted:,})",
        f"handles {len(batch.handles):,} (email {batch.matched_by_email:,}, "
        f"google {batch.matched_by_google:,} [linked to people {batch.linked_to_people:,}], "
        f"unmatched {unmatched:,}; short codes dropped {batch.short_codes_dropped:,})",
        f"chats {len(batch.chats):,} (group {group_chats:,})   "
        f"watermark {batch.max_rowid:,}   mode {batch.mode}",
    ]
    if batch.reactions_skipped or batch.senderless_dropped:
        lines.append(
            f"skipped: reactions {batch.reactions_skipped:,}, "
            f"senderless {batch.senderless_dropped:,}"
        )
    return "\n".join(lines)


def main_with(argv: list[str], get_conn: Callable[[], Any]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("~/Library/Messages/chat.db").expanduser())
    p.add_argument("--full", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    try:
        batch = run(get_conn, args.db.expanduser(), full=args.full, dry_run=args.dry_run)
    except imessage_local.FullDiskAccessError:
        print(FULL_DISK_ACCESS_MSG, file=sys.stderr)
        sys.exit(2)
    except imessage_local.SchemaError as e:
        print(f"chat.db schema error: {e}", file=sys.stderr)
        sys.exit(2)

    deleted = 0
    if not args.dry_run:
        with get_conn() as conn:
            latest = imessage_repo.latest_import(conn)
            if latest is not None:
                deleted = latest["messages_deleted"]

    print(summary(batch, dry_run=args.dry_run, deleted=deleted))


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
