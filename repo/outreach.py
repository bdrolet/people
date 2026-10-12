"""Every SQL statement on the outreach_* tables (outreach spec §5). Takes an open
connection; never opens one, never commits, and decides nothing: whether a stage
change is allowed is services/outreach.py's call."""

from datetime import datetime
from typing import Any

_CONTACT_COLUMNS = """
    person_id, stage, cohort_week, briefed_at, replied_at, channel, has_lead,
    follow_up_on, notes, hubspot_synced_at, created_at, updated_at
"""
_OPPORTUNITY_COLUMNS = """
    id, referrer_id, buyer_id, buyer_label, stage, lost_reason, intro_at,
    conversation_at, proposal_at, signed_at, lost_at, hubspot_deal_id, notes,
    created_at, updated_at
"""
_CONTACT_UPDATABLE = frozenset(
    {
        "stage",
        "cohort_week",
        "briefed_at",
        "replied_at",
        "channel",
        "has_lead",
        "follow_up_on",
        "notes",
    }
)
_OPPORTUNITY_UPDATABLE = frozenset(
    {
        "stage",
        "lost_reason",
        "conversation_at",
        "proposal_at",
        "signed_at",
        "lost_at",
        "buyer_id",
        "notes",
    }
)


def _set_clause(changes: dict, allowed: frozenset[str]) -> str:
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ValueError(f"not updatable: {sorted(unknown) or 'nothing to update'}")
    return ", ".join(f"{column} = %s" for column in changes) + ", updated_at = now()"


def insert_contact_if_missing(conn: Any, person_id: int) -> dict | None:
    """The new row at `candidate`, or None when the person already has one (never downgrades)."""
    return conn.execute(
        f"""
        INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'candidate')
        ON CONFLICT (person_id) DO NOTHING
        RETURNING {_CONTACT_COLUMNS}
        """,
        (person_id,),
    ).fetchone()


def get_contact(conn: Any, person_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM outreach_contacts WHERE person_id = %s", (person_id,)
    ).fetchone()


def lock_contact(conn: Any, person_id: int) -> dict | None:
    """Row-locked read: two deliveries of one event cannot both see the old stage."""
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM outreach_contacts WHERE person_id = %s FOR UPDATE",
        (person_id,),
    ).fetchone()


def update_contact(conn: Any, person_id: int, changes: dict) -> dict:
    return conn.execute(
        f"""
        UPDATE outreach_contacts SET {_set_clause(changes, _CONTACT_UPDATABLE)}
        WHERE person_id = %s RETURNING {_CONTACT_COLUMNS}
        """,
        (*changes.values(), person_id),
    ).fetchone()


def insert_opportunity(
    conn: Any, *, referrer_id: int, buyer_id: int | None, buyer_label: str, at: datetime
) -> dict:
    return conn.execute(
        f"""
        INSERT INTO outreach_opportunities (referrer_id, buyer_id, buyer_label, stage, intro_at)
        VALUES (%s, %s, %s, 'intro', %s)
        RETURNING {_OPPORTUNITY_COLUMNS}
        """,
        (referrer_id, buyer_id, buyer_label, at),
    ).fetchone()


def lock_opportunity(conn: Any, opportunity_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_OPPORTUNITY_COLUMNS} FROM outreach_opportunities WHERE id = %s FOR UPDATE",
        (opportunity_id,),
    ).fetchone()


def update_opportunity(conn: Any, opportunity_id: int, changes: dict) -> dict:
    return conn.execute(
        f"""
        UPDATE outreach_opportunities SET {_set_clause(changes, _OPPORTUNITY_UPDATABLE)}
        WHERE id = %s RETURNING {_OPPORTUNITY_COLUMNS}
        """,
        (*changes.values(), opportunity_id),
    ).fetchone()


def insert_event(
    conn: Any,
    *,
    pipeline: str,
    stage: str,
    at: datetime,
    source: str,
    person_id: int | None = None,
    opportunity_id: int | None = None,
    channel: str | None = None,
    graph_message_id: str | None = None,
    from_address: str | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO outreach_events
            (pipeline, person_id, opportunity_id, stage, at, channel, source,
             graph_message_id, from_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            pipeline,
            person_id,
            opportunity_id,
            stage,
            at,
            channel,
            source,
            graph_message_id,
            from_address,
        ),
    ).fetchone()
    return int(row["id"])


def repoint_events(conn: Any, from_id: int, to_id: int) -> int:
    return conn.execute(
        "UPDATE outreach_events SET person_id = %s WHERE person_id = %s", (to_id, from_id)
    ).rowcount


def repoint_opportunities(conn: Any, from_id: int, to_id: int) -> int:
    referrer = conn.execute(
        "UPDATE outreach_opportunities SET referrer_id = %s, updated_at = now() WHERE referrer_id = %s",
        (to_id, from_id),
    ).rowcount
    buyer = conn.execute(
        "UPDATE outreach_opportunities SET buyer_id = %s, updated_at = now() WHERE buyer_id = %s",
        (to_id, from_id),
    ).rowcount
    return referrer + buyer


def move_contact(conn: Any, from_id: int, to_id: int) -> None:
    conn.execute(
        "UPDATE outreach_contacts SET person_id = %s, updated_at = now() WHERE person_id = %s",
        (to_id, from_id),
    )


def delete_contact(conn: Any, person_id: int) -> None:
    conn.execute("DELETE FROM outreach_contacts WHERE person_id = %s", (person_id,))


def sync_rows(
    conn: Any, keys: list[str], *, event_id: int | None = None, limit: int = 200
) -> list[dict]:
    """Events of the kinds in `keys` ("pipeline:stage") whose Asana task is missing or
    not yet completed, oldest first, with what the task name needs. Pass one event_id at
    a time: an all-int list param is mangled by clients/db.py's pgvector shim."""
    by_id = "AND e.id = %s" if event_id is not None else ""
    params: tuple = (keys, event_id, limit) if event_id is not None else (keys, limit)
    return conn.execute(
        f"""
        SELECT e.id, e.pipeline, e.stage, e.at, e.channel, e.asana_task_gid,
               e.asana_completed_at, o.buyer_label,
               COALESCE(p.display_name, p.email, 'person ' || p.id::text) AS person_name
        FROM outreach_events e
        LEFT JOIN outreach_opportunities o ON o.id = e.opportunity_id
        LEFT JOIN people p ON p.id = COALESCE(e.person_id, o.referrer_id)
        WHERE (e.asana_task_gid IS NULL OR e.asana_completed_at IS NULL)
          AND (e.pipeline || ':' || e.stage) = ANY(%s::text[])
          {by_id}
        ORDER BY e.id
        LIMIT %s
        """,
        params,
    ).fetchall()


def set_asana_gid(conn: Any, event_id: int, gid: str) -> None:
    conn.execute("UPDATE outreach_events SET asana_task_gid = %s WHERE id = %s", (gid, event_id))


def set_asana_completed(conn: Any, event_id: int) -> None:
    conn.execute("UPDATE outreach_events SET asana_completed_at = now() WHERE id = %s", (event_id,))
