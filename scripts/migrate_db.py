#!/usr/bin/env python3
"""One-shot schema migration. Run after the first terraform apply:

  CLOUD_SQL_CONNECTION_NAME=bens-project-462804:us-central1:inbox \
    POSTGRES_USER=people POSTGRES_PASSWORD=<people_db_password> POSTGRES_DB=people \
    .venv/bin/python scripts/migrate_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.db import get_conn

SCHEMA = Path(__file__).parent.parent / "repo" / "schema.sql"


def main() -> None:
    sql = SCHEMA.read_text()
    with get_conn() as conn:
        conn.execute(sql)
        conn.commit()
    print("Migration complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
