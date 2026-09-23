"""All reads and writes on the people table. Takes an open connection; never
opens one. Rows come back as dicts (both connection flavours in clients/db.py
return dict rows)."""

from datetime import datetime
from typing import Any

_COLUMNS = """
    email, display_name, first_seen, last_seen, last_contacted, message_count,
    my_response_count, relationship_label, notes, eligible, automated,
    google_resource_name, google_etag, google_deleted_at, hubspot_contact_id,
    hubspot_synced_at, updated_at,
    GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction
"""

_LAST_INTERACTION = "GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz))"


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def upsert_inbound(conn: Any, email: str, display_name: str | None, received_at: datetime) -> dict:
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, last_seen, message_count)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT (email) DO UPDATE SET
            display_name  = COALESCE(people.display_name, EXCLUDED.display_name),
            last_seen     = GREATEST(COALESCE(people.last_seen, 'epoch'::timestamptz), EXCLUDED.last_seen),
            message_count = people.message_count + 1,
            updated_at    = now()
        RETURNING {_COLUMNS}
        """,
        (_norm(email), display_name or None, received_at, received_at),
    ).fetchone()


def upsert_outbound(conn: Any, email: str, display_name: str | None, sent_at: datetime) -> dict:
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, last_contacted, my_response_count)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT (email) DO UPDATE SET
            display_name      = COALESCE(people.display_name, EXCLUDED.display_name),
            last_contacted    = GREATEST(COALESCE(people.last_contacted, 'epoch'::timestamptz), EXCLUDED.last_contacted),
            my_response_count = people.my_response_count + 1,
            updated_at        = now()
        RETURNING {_COLUMNS}
        """,
        (_norm(email), display_name or None, sent_at, sent_at),
    ).fetchone()


def set_flags(conn: Any, email: str, *, automated: bool, eligible: bool) -> None:
    conn.execute(
        """
        UPDATE people SET automated = %s, eligible = people.eligible OR %s, updated_at = now()
        WHERE email = %s
        """,
        (automated, eligible, _norm(email)),
    )


def get(conn: Any, email: str) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE email = %s", (_norm(email),)
    ).fetchone()


def get_by_google_resource(conn: Any, resource_name: str) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE google_resource_name = %s", (resource_name,)
    ).fetchone()


def search(conn: Any, q: str, limit: int) -> list[dict]:
    like = f"%{q.strip().lower()}%"
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE email ILIKE %s OR similarity(display_name, %s) > 0.3
        ORDER BY {_LAST_INTERACTION} DESC
        LIMIT %s
        """,
        (like, q.strip(), limit),
    ).fetchall()


def recent(conn: Any, limit: int, eligible_only: bool = True) -> list[dict]:
    where = "WHERE eligible" if eligible_only else ""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people {where} ORDER BY {_LAST_INTERACTION} DESC LIMIT %s",
        (limit,),
    ).fetchall()


def set_google(
    conn: Any,
    email: str,
    *,
    resource_name: str,
    etag: str | None,
    display_name: str | None = None,
    notes: str | None = None,
    relationship_label: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE people SET
            google_resource_name = %s,
            google_etag          = %s,
            display_name         = COALESCE(%s, display_name),
            notes                = COALESCE(%s, notes),
            relationship_label   = COALESCE(%s, relationship_label),
            updated_at           = now()
        WHERE email = %s
        """,
        (resource_name, etag, display_name, notes, relationship_label, _norm(email)),
    )


def update_from_google(
    conn: Any,
    resource_name: str,
    *,
    etag: str | None,
    display_name: str | None,
    notes: str | None,
    relationship_label: str | None,
) -> None:
    """Google is the truth for these fields: overwrite, including with NULL."""
    conn.execute(
        """
        UPDATE people SET google_etag = %s, display_name = COALESCE(%s, display_name),
            notes = %s, relationship_label = %s, updated_at = now()
        WHERE google_resource_name = %s
        """,
        (etag, display_name, notes, relationship_label, resource_name),
    )


def mark_google_deleted(conn: Any, resource_name: str) -> None:
    conn.execute(
        """
        UPDATE people SET google_deleted_at = now(), google_resource_name = NULL,
            google_etag = NULL, updated_at = now()
        WHERE google_resource_name = %s
        """,
        (resource_name,),
    )


def create_from_google(
    conn: Any,
    email: str,
    *,
    display_name: str | None,
    resource_name: str,
    etag: str | None,
    notes: str | None,
    relationship_label: str | None,
) -> dict:
    """A contact Ben made by hand that people has never seen mail from."""
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, eligible, automated,
                            google_resource_name, google_etag, notes, relationship_label)
        VALUES (%s, %s, now(), TRUE, FALSE, %s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE SET
            google_resource_name = EXCLUDED.google_resource_name,
            google_etag = EXCLUDED.google_etag,
            display_name = COALESCE(EXCLUDED.display_name, people.display_name),
            notes = EXCLUDED.notes, relationship_label = EXCLUDED.relationship_label,
            eligible = TRUE, updated_at = now()
        RETURNING {_COLUMNS}
        """,
        (_norm(email), display_name, resource_name, etag, notes, relationship_label),
    ).fetchone()


def set_hubspot(conn: Any, email: str, contact_id: str) -> None:
    conn.execute(
        "UPDATE people SET hubspot_contact_id = %s, hubspot_synced_at = now(), updated_at = now() WHERE email = %s",
        (contact_id, _norm(email)),
    )


def clear_hubspot(conn: Any, email: str) -> None:
    conn.execute(
        "UPDATE people SET hubspot_contact_id = NULL, hubspot_synced_at = NULL, updated_at = now() WHERE email = %s",
        (_norm(email),),
    )


def oldest_managed(conn: Any) -> dict | None:
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people WHERE hubspot_contact_id IS NOT NULL
        ORDER BY {_LAST_INTERACTION} ASC LIMIT 1
        """
    ).fetchone()


def managed(conn: Any) -> list[dict]:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE hubspot_contact_id IS NOT NULL"
    ).fetchall()


def eligible_not_in_hubspot(conn: Any, limit: int) -> list[dict]:
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE eligible AND hubspot_contact_id IS NULL
        ORDER BY {_LAST_INTERACTION} DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()


def names_for_matching(conn: Any) -> list[dict]:
    """Every row's email and display_name, for scripts/import_linkedin.py's soft link."""
    return conn.execute("SELECT email, display_name FROM people").fetchall()


def rows_for_imessage_matching(conn: Any) -> list[dict]:
    """Email, display name, and Google link for scripts/import_imessage.py (spec §5.4)."""
    return conn.execute("SELECT email, display_name, google_resource_name FROM people").fetchall()
