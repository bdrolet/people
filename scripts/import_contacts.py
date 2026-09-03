#!/usr/bin/env python3
# scripts/import_contacts.py
"""Backfill people from Inbox + Sent Items history (spec §12). Local only.

  python scripts/import_contacts.py --days 365 [--dry-run] [--reset-counters]

Runs against whatever DB clients/db.py resolves from env (Cloud SQL connector
in .env by default). Counters are not idempotent: run once on an empty table,
or with --reset-counters to zero them first.

Commits after every message, so an interrupted run keeps what it processed;
re-running is safe with --reset-counters.
"""

import argparse
import os
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from services import google_contacts_sync, hubspot_mirror, ingest  # noqa: E402


def run(conn, messages: Iterable[dict], *, own: set[str], dry_run: bool) -> dict[str, int]:
    counts = {"inbound": 0, "outbound": 0, "newly_eligible": 0, "skipped": 0}
    for m in messages:
        if m["folder"] == "sentitems":
            sent_at = m.get("sent_at")
            if not sent_at:
                counts["skipped"] += 1
                continue
            results = ingest.record_outbound(
                conn,
                recipients=m["to"] + m["cc"],
                display_by_email=None,
                sent_at=ingest.parse_ts(sent_at),
            )
            counts["outbound"] += 1
        else:
            if not m["from"] or m["from"] in own:
                counts["skipped"] += 1
                continue
            received_at = m.get("received_at")
            if not received_at:
                counts["skipped"] += 1
                continue
            results = [
                ingest.record_inbound(
                    conn,
                    sender=m["from"],
                    display=m.get("from_name"),
                    received_at=ingest.parse_ts(received_at),
                    category=m["category"],
                )
            ]
            counts["inbound"] += 1
        for res in results:
            if res.newly_eligible:
                counts["newly_eligible"] += 1
            if not dry_run and res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
        if not dry_run:
            conn.commit()
    return counts


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reset-counters", action="store_true")
    args = p.parse_args()

    from clients.db import get_conn
    from clients.graph_local import GraphLocal

    g = GraphLocal()
    g.authenticate()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    own = {a.strip().lower() for a in os.environ.get("OWN_ADDRESSES", "").split(",") if a.strip()}

    def stream():
        yield from g.iter_messages("inbox", since)
        yield from g.iter_messages("sentitems", since)

    with get_conn() as conn:
        if args.reset_counters and not args.dry_run:
            conn.execute("UPDATE people SET message_count = 0, my_response_count = 0")
            conn.commit()
        counts = run(conn, stream(), own=own, dry_run=args.dry_run)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print(counts)


if __name__ == "__main__":
    main()
