#!/usr/bin/env python3
# scripts/import_whatsapp.py
"""Import WhatsApp's local store into the whatsapp_* snapshot tables
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §6). Local only —
people's Cloud Functions never read WhatsApp.

  python scripts/import_whatsapp.py [--full] [--dry-run] [--db PATH]

Unlike the iMessage import, this needs **no Full Disk Access**: the WhatsApp group
container is readable by the user (§5.1). The store is copied (with its -wal and
-shm sidecars) to a temp directory and read from the copy, because the app is live;
the copy is deleted in a finally.

Runs against whatever DB clients/db.py resolves from env, like import_contacts.py
and import_imessage.py. Reads the store, matches every handle and member against
`people`, then writes in one transaction (§6.7): --dry-run reads and matches but
writes nothing. Output is counts only — never JIDs, numbers, names or message text
(§6.8).
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

import clients.otel as otel
from clients import whatsapp_local
from models.whatsapp import WhatsAppBatch
from repo import people as people_repo
from repo import whatsapp as whatsapp_repo
from services import whatsapp_export

WINDOW_DAYS = 14


def run(
    get_conn: Callable[[], Any], path: Path, *, full: bool, dry_run: bool
) -> tuple[WhatsAppBatch, int]:
    started_at = datetime.now(UTC)
    since_pk = 0
    if not full:
        with get_conn() as conn:
            since_pk = whatsapp_repo.latest_watermark(conn)

    # §6.2: the watermark plus a 14-day re-scan window, so a late-arriving or edited
    # message below the watermark is re-read rather than missed.
    window_start = None if full else datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    raw = whatsapp_local.read(path, since_pk=since_pk, window_start=window_start, full=full)
    batch = whatsapp_export.build_batch(raw, mode="full" if full else "incremental")

    deleted = 0
    with get_conn() as conn:
        # Matching runs inside the transaction but before any write, and it is redone
        # from scratch every run, so a merge or a corrected phone number is picked up
        # without a full rebuild (§6.5).
        whatsapp_export.match_handles(batch, people_repo.rows_for_whatsapp_matching(conn))

        if dry_run:
            conn.rollback()
        else:
            whatsapp_repo.upsert_chats(conn, batch.chats)
            whatsapp_repo.upsert_handles(conn, batch.handles)
            whatsapp_repo.replace_members(conn, batch.members)
            whatsapp_repo.upsert_messages(conn, batch.messages)
            if full:
                deleted = whatsapp_repo.delete_missing(
                    conn,
                    {(m.chat_jid, m.stanza_id) for m in batch.messages},
                    {c.chat_jid for c in batch.chats},
                )
            whatsapp_repo.recompute_chat_stats(conn)
            whatsapp_repo.recompute_handle_stats(conn)
            whatsapp_repo.record_import(
                conn, batch, started_at=started_at, messages_deleted=deleted
            )
            conn.commit()

    _record_metrics(batch, deleted=deleted, started_at=started_at)
    return batch, deleted


def _record_metrics(batch: WhatsAppBatch, *, deleted: int, started_at: datetime) -> None:
    mode = {"mode": batch.mode}
    otel.whatsapp_import_messages.add(len(batch.messages), {**mode, "outcome": "upserted"})
    otel.whatsapp_import_messages.add(deleted, {**mode, "outcome": "deleted"})
    otel.whatsapp_import_messages.add(
        batch.senderless_dropped, {**mode, "outcome": "senderless_dropped"}
    )
    otel.whatsapp_import_messages.add(batch.duplicate_stanza_ids, {**mode, "outcome": "duplicate"})
    unmatched = len(batch.handles) - batch.matched_by_phone - batch.matched_by_name
    otel.whatsapp_import_handles.add(batch.matched_by_phone, {"match_method": "phone"})
    otel.whatsapp_import_handles.add(batch.matched_by_name, {"match_method": "name"})
    otel.whatsapp_import_handles.add(unmatched, {"match_method": "none"})
    otel.whatsapp_import_handles.add(batch.handles_unnormalized, {"match_method": "unnormalized"})
    otel.whatsapp_import_duration.record((datetime.now(UTC) - started_at).total_seconds(), mode)


def summary(batch: WhatsAppBatch, *, dry_run: bool, deleted: int) -> str:
    """Counts only (§6.8). Never a JID, a number, a name, or message text."""
    direct = sum(1 for c in batch.chats if c.kind == "direct")
    groups = sum(1 for c in batch.chats if c.kind == "group")
    unmatched = len(batch.handles) - batch.matched_by_phone - batch.matched_by_name
    identities = len({m.handle for m in batch.members})
    members_matched = len({m.handle for m in batch.members if m.person_id})
    lines = ["DRY RUN — nothing written"] if dry_run else []
    lines += [
        f"chats {len(batch.chats):,} (direct {direct:,}, group {groups:,}, "
        f"skipped {batch.sessions_skipped:,}, duplicate jid {batch.duplicate_jids:,})",
        f"messages {len(batch.messages):,} upserted, {deleted:,} deleted, "
        f"{batch.senderless_dropped:,} senderless dropped, "
        f"{batch.duplicate_stanza_ids:,} duplicate id",
        f"handles {len(batch.handles):,} (phone-matched {batch.matched_by_phone:,}, "
        f"name-matched {batch.matched_by_name:,}, unmatched {unmatched:,}, "
        f"unnormalized {batch.handles_unnormalized:,})",
        f"members {len(batch.members):,} rows / {identities:,} identities across "
        f"{groups:,} groups (matched {members_matched:,})",
        f"watermark {batch.watermark:,}   mode {batch.mode}",
    ]
    if batch.missing_stanza_ids:
        lines.append(
            f"note: {batch.missing_stanza_ids:,} messages had no stanza id and were dropped"
        )
    return "\n".join(lines)


def main_with(argv: list[str], get_conn: Callable[[], Any]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=whatsapp_local.DEFAULT_STORE)
    p.add_argument("--full", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    otel.setup_telemetry("people-import-whatsapp")
    try:
        try:
            batch, deleted = run(
                get_conn, args.db.expanduser(), full=args.full, dry_run=args.dry_run
            )
        except whatsapp_local.StoreUnreadableError as e:
            print(f"could not read ChatStorage.sqlite: {e}", file=sys.stderr)
            sys.exit(2)
        except whatsapp_local.SchemaError as e:
            print(f"ChatStorage.sqlite schema error: {e}", file=sys.stderr)
            sys.exit(2)
        print(summary(batch, dry_run=args.dry_run, deleted=deleted))
    finally:
        # A short-lived local process exports nothing unless it flushes (§10).
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
