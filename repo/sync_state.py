from typing import Any

KEY = "google_contacts"


def get_token(conn: Any) -> str | None:
    row = conn.execute("SELECT sync_token FROM sync_state WHERE key = %s", (KEY,)).fetchone()
    return row["sync_token"] if row else None


def set_token(conn: Any, token: str | None, status: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (key, sync_token, last_run_at, last_status)
        VALUES (%s, %s, now(), %s)
        ON CONFLICT (key) DO UPDATE SET sync_token = EXCLUDED.sync_token,
            last_run_at = now(), last_status = EXCLUDED.last_status
        """,
        (KEY, token, status),
    )
