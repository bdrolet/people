#!/usr/bin/env python3
# scripts/clear_sync_token.py
"""Clear the stored Google sync token so the next nightly sync runs a full pass
(docs/superpowers/specs/2026-09-28-adopt-emailless-contacts-design.md §6.2).
Local only.

  python scripts/clear_sync_token.py

Clears the sync token in sync_state table and reports what was cleared.
The next run_sync will take the full-listing path instead of an incremental one.
"""

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from repo import sync_state


def run(get_conn: Callable) -> None:
    with get_conn() as conn:
        old_token = sync_state.get_token(conn)
        sync_state.set_token(conn, None, "cleared for full resync")
        conn.commit()

    if old_token:
        print("Cleared sync token. Next run will do a full listing pass.")
    else:
        print("No sync token was set. Next run may still be a full pass if overdue.")


def main() -> None:
    from clients.db import get_conn

    run(get_conn)


if __name__ == "__main__":
    main()
