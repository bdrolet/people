"""All reads and writes on the people table. Takes an open connection; never
opens one. Rows come back as dicts (both connection flavours in clients/db.py
return dict rows)."""

from datetime import datetime
from typing import Any

_COLUMNS = """
    id, email, display_name, first_seen, last_seen, last_contacted, message_count,
    my_response_count, relationship_label, notes, eligible, automated,
    google_resource_name, google_etag, google_deleted_at, hubspot_contact_id,
    hubspot_synced_at, phone_numbers, company, job_title, google_fields, updated_at,
    GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction
"""

_LAST_INTERACTION = "GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz))"


def _norm(email: str | None) -> str | None:
    """None for a missing or blank address. NOT "" — an empty string passes the
    people_has_an_identifier CHECK and then collides on the UNIQUE index, so the
    second email-less adoption would abort the sync (spec §5.2.1)."""
    normalized = (email or "").strip().lower()
    return normalized or None


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


def get_by_id(conn: Any, person_id: int) -> dict | None:
    return conn.execute(f"SELECT {_COLUMNS} FROM people WHERE id = %s", (person_id,)).fetchone()


def get_by_phone(conn: Any, e164: str) -> list[dict]:
    """A phone number can belong to several people (a household landline), so
    this returns every match rather than picking one — ambiguity is an error,
    not a guess (spec §5.1). Ordered by last interaction descending so the
    most recently interacted-with candidate comes first."""
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE %s = ANY(phone_numbers)
        ORDER BY {_LAST_INTERACTION} DESC
        """,
        (e164,),
    ).fetchall()


def get_by_google_resource(conn: Any, resource_name: str) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE google_resource_name = %s", (resource_name,)
    ).fetchone()


def search(conn: Any, q: str, limit: int) -> list[dict]:
    like = f"%{q.strip().lower()}%"
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE email ILIKE %s OR similarity(display_name, %s) > 0.3 OR company ILIKE %s
        ORDER BY {_LAST_INTERACTION} DESC
        LIMIT %s
        """,
        (like, q.strip(), like, limit),
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
    phone_numbers: list[str],
    company: str | None,
    job_title: str | None,
    google_fields: dict,
) -> None:
    conn.execute(
        """
        UPDATE people SET
            google_resource_name = %s,
            google_etag          = %s,
            display_name         = COALESCE(%s, display_name),
            notes                = COALESCE(%s, notes),
            relationship_label   = COALESCE(%s, relationship_label),
            phone_numbers        = %s::text[],
            company              = %s,
            job_title            = %s,
            google_fields        = %s::jsonb,
            updated_at           = now()
        WHERE email = %s
        """,
        (
            resource_name,
            etag,
            display_name,
            notes,
            relationship_label,
            phone_numbers,
            company,
            job_title,
            google_fields,
            _norm(email),
        ),
    )


def update_from_google(
    conn: Any,
    resource_name: str,
    *,
    etag: str | None,
    display_name: str | None,
    notes: str | None,
    relationship_label: str | None,
    phone_numbers: list[str],
    company: str | None,
    job_title: str | None,
    google_fields: dict,
    email: str | None = None,
) -> None:
    """Google is the truth for these fields: overwrite, including with NULL.

    `email` is the one exception: it is written only when explicitly passed
    (email promotion, see services/google_contacts_sync.py::apply_person).
    This runs for every linked contact on every sync, so every other caller
    must keep getting exactly the SQL it got before this parameter existed."""
    set_clauses = [
        "google_etag = %s",
        "display_name = COALESCE(%s, display_name)",
        "notes = %s",
        "relationship_label = %s",
        "phone_numbers = %s::text[]",
        "company = %s",
        "job_title = %s",
        "google_fields = %s::jsonb",
        "updated_at = now()",
    ]
    params: list[Any] = [
        etag,
        display_name,
        notes,
        relationship_label,
        phone_numbers,
        company,
        job_title,
        google_fields,
    ]
    if email is not None:
        set_clauses.append("email = %s")
        params.append(_norm(email))
    params.append(resource_name)
    conn.execute(
        f"UPDATE people SET {', '.join(set_clauses)} WHERE google_resource_name = %s",
        tuple(params),
    )


def email_owner(conn: Any, email: str) -> int | None:
    """The id of the person holding this address, or None. Guards promotion:
    writing a claimed address violates people_email_key and aborts the sync."""
    row = conn.execute("SELECT id FROM people WHERE email = %s", (_norm(email),)).fetchone()
    return row["id"] if row else None


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
    email: str | None,
    *,
    display_name: str | None,
    resource_name: str,
    etag: str | None,
    notes: str | None,
    relationship_label: str | None,
    phone_numbers: list[str],
    company: str | None,
    job_title: str | None,
    google_fields: dict,
) -> dict:
    """A contact Ben made by hand that people has never seen mail from."""
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, eligible, automated,
                            google_resource_name, google_etag, notes, relationship_label,
                            phone_numbers, company, job_title, google_fields)
        VALUES (%s, %s, now(), TRUE, FALSE, %s, %s, %s, %s, %s::text[], %s, %s, %s::jsonb)
        ON CONFLICT (email) DO UPDATE SET
            google_resource_name = EXCLUDED.google_resource_name,
            google_etag = EXCLUDED.google_etag,
            display_name = COALESCE(EXCLUDED.display_name, people.display_name),
            notes = EXCLUDED.notes, relationship_label = EXCLUDED.relationship_label,
            phone_numbers = EXCLUDED.phone_numbers, company = EXCLUDED.company,
            job_title = EXCLUDED.job_title, google_fields = EXCLUDED.google_fields,
            eligible = TRUE, updated_at = now()
        RETURNING {_COLUMNS}
        """,
        (
            _norm(email),
            display_name,
            resource_name,
            etag,
            notes,
            relationship_label,
            phone_numbers,
            company,
            job_title,
            google_fields,
        ),
    ).fetchone()


def relink_google(conn: Any, person_id: int, *, resource_name: str, etag: str | None) -> None:
    """Point an existing row at a different Google contact. Used when duplicate
    contacts are merged and the surviving contact has no row of its own — the
    best of the losers' rows is re-linked rather than orphaned."""
    conn.execute(
        """
        UPDATE people SET google_resource_name = %s, google_etag = %s,
                          google_deleted_at = NULL, updated_at = now()
        WHERE id = %s
        """,
        (resource_name, etag, person_id),
    )


def delete(conn: Any, person_id: int) -> None:
    """Remove a row outright. Child tables reference people(id) ON DELETE SET
    NULL, so callers that care about those links must re-point them first."""
    conn.execute("DELETE FROM people WHERE id = %s", (person_id,))


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
    # clients/hubspot.py searches and creates contacts by email address, so a
    # phone-only person cannot be mirrored (spec §5.2) — exclude them here so
    # the nightly reconcile never burns the bounded cap on failed creates.
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE eligible AND hubspot_contact_id IS NULL AND email IS NOT NULL
        ORDER BY {_LAST_INTERACTION} DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()


def names_for_matching(conn: Any) -> list[dict]:
    """Every row's id, email and display_name, for scripts/import_linkedin.py's soft link."""
    return conn.execute("SELECT id, email, display_name FROM people").fetchall()


def rows_for_imessage_matching(conn: Any) -> list[dict]:
    """Id, email, display name, and Google link for scripts/import_imessage.py (spec §5.4)."""
    return conn.execute(
        "SELECT id, email, display_name, google_resource_name FROM people"
    ).fetchall()


def rows_for_whatsapp_matching(conn: Any) -> list[dict]:
    """Id, display name and phone numbers for scripts/import_whatsapp.py's soft
    link (WhatsApp spec §6.5). No email column: WhatsApp has no email addresses."""
    return conn.execute("SELECT id, display_name, phone_numbers FROM people").fetchall()
