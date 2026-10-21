"""Every SQL statement on the campaign_* tables (campaign spec §7.1). Takes an open
connection; never opens one, never commits, and decides nothing: whether a status
change is allowed is services/campaign.py's call."""

import json
from datetime import date, datetime
from typing import Any

from models.campaign import CONTACT_STATUSES

TZ = "America/New_York"  # the zone the campaign counts days and weeks in

_RUN_COLUMNS = "id, ran_at, lookback_from, lookback_to, counts, notes"
_COMPANY_COLUMNS = """
    id, domain, name, round, amount, announced_on, source_url, product_summary,
    announcement_excerpt, hq_country, fit_score, fit_notes, status, run_id,
    created_at, updated_at
"""
_CONTACT_COLUMNS = """
    id, company_id, person_id, full_name, title, linkedin_url, email, email_source,
    verification, status, subject, body, opener_generic, draft_id, graph_message_id,
    sent_at, follow_up_on, replied_at, run_id, created_at, updated_at
"""
_COMPANY_INSERTABLE = frozenset(
    {
        "domain",
        "name",
        "round",
        "amount",
        "announced_on",
        "source_url",
        "product_summary",
        "announcement_excerpt",
        "hq_country",
        "fit_score",
        "fit_notes",
        "status",
        "run_id",
    }
)
_COMPANY_UPDATABLE = frozenset(
    {"fit_score", "fit_notes", "status", "announcement_excerpt", "product_summary"}
)
_CONTACT_UPDATABLE = frozenset(
    {
        "person_id",
        "full_name",
        "title",
        "linkedin_url",
        "email",
        "email_source",
        "verification",
        "status",
        "subject",
        "body",
        "opener_generic",
        "draft_id",
        "graph_message_id",
        "sent_at",
        "follow_up_on",
        "replied_at",
        "run_id",
    }
)
_CONTACT_INSERTABLE = _CONTACT_UPDATABLE | {"company_id"}


def _set_clause(changes: dict, allowed: frozenset[str]) -> str:
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ValueError(f"not updatable: {sorted(unknown) or 'nothing to update'}")
    return ", ".join(f"{column} = %s" for column in changes) + ", updated_at = now()"


def _insert_parts(fields: dict, allowed: frozenset[str]) -> tuple[str, str, tuple]:
    unknown = set(fields) - allowed
    if unknown or not fields:
        raise ValueError(f"not insertable: {sorted(unknown) or 'nothing to insert'}")
    return ", ".join(fields), ", ".join(["%s"] * len(fields)), tuple(fields.values())


def _where(conditions: list[str]) -> str:
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


# --- runs ---------------------------------------------------------------------------------


def insert_run(conn: Any, *, ran_at: datetime, lookback_from: date, lookback_to: date) -> dict:
    return conn.execute(
        f"""
        INSERT INTO campaign_runs (ran_at, lookback_from, lookback_to)
        VALUES (%s, %s, %s)
        RETURNING {_RUN_COLUMNS}
        """,
        (ran_at, lookback_from, lookback_to),
    ).fetchone()


def latest_run(conn: Any) -> dict | None:
    return conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM campaign_runs ORDER BY ran_at DESC, id DESC LIMIT 1"
    ).fetchone()


def update_run_counts(conn: Any, run_id: int, counts: dict) -> None:
    conn.execute(
        "UPDATE campaign_runs SET counts = %s::jsonb WHERE id = %s",
        (json.dumps(counts), run_id),
    )


# --- companies ----------------------------------------------------------------------------


def insert_company_if_missing(conn: Any, fields: dict) -> dict | None:
    """The new row, or None when the lower-cased domain already exists."""
    columns, marks, params = _insert_parts(fields, _COMPANY_INSERTABLE)
    return conn.execute(
        f"""
        INSERT INTO campaign_companies ({columns}) VALUES ({marks})
        ON CONFLICT (lower(domain)) DO NOTHING
        RETURNING {_COMPANY_COLUMNS}
        """,
        params,
    ).fetchone()


def get_company(conn: Any, company_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_COMPANY_COLUMNS} FROM campaign_companies WHERE id = %s", (company_id,)
    ).fetchone()


def get_company_by_domain(conn: Any, domain: str) -> dict | None:
    return conn.execute(
        f"SELECT {_COMPANY_COLUMNS} FROM campaign_companies WHERE lower(domain) = lower(%s)",
        (domain,),
    ).fetchone()


def update_company(conn: Any, company_id: int, changes: dict) -> dict:
    set_clause = _set_clause(changes, _COMPANY_UPDATABLE)
    return conn.execute(
        f"UPDATE campaign_companies SET {set_clause} WHERE id = %s RETURNING {_COMPANY_COLUMNS}",
        (*changes.values(), company_id),
    ).fetchone()


def companies(conn: Any, *, status: str | None = None, run_id: int | None = None) -> list[dict]:
    conditions, params = [], []
    if status is not None:
        conditions.append("status = %s")
        params.append(status)
    if run_id is not None:
        conditions.append("run_id = %s")
        params.append(run_id)
    return conn.execute(
        f"SELECT {_COMPANY_COLUMNS} FROM campaign_companies{_where(conditions)} ORDER BY id",
        tuple(params),
    ).fetchall()


# --- contacts -----------------------------------------------------------------------------


def insert_contact_if_missing(conn: Any, fields: dict) -> dict | None:
    """The new row, or None when the company already has a contact with this email."""
    columns, marks, params = _insert_parts(fields, _CONTACT_INSERTABLE)
    return conn.execute(
        f"""
        INSERT INTO campaign_contacts ({columns}) VALUES ({marks})
        ON CONFLICT (company_id, lower(email)) DO NOTHING
        RETURNING {_CONTACT_COLUMNS}
        """,
        params,
    ).fetchone()


def get_contact(conn: Any, contact_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM campaign_contacts WHERE id = %s", (contact_id,)
    ).fetchone()


def get_contact_by_email(conn: Any, email: str) -> dict | None:
    return conn.execute(
        f"""
        SELECT {_CONTACT_COLUMNS} FROM campaign_contacts
        WHERE lower(email) = lower(%s) ORDER BY id LIMIT 1
        """,
        (email,),
    ).fetchone()


def contacts_by_domain(conn: Any, domain: str) -> list[dict]:
    columns = ", ".join(f"c.{c.strip()}" for c in _CONTACT_COLUMNS.split(","))
    return conn.execute(
        f"""
        SELECT {columns} FROM campaign_contacts c
        JOIN campaign_companies co ON co.id = c.company_id
        WHERE lower(co.domain) = lower(%s) ORDER BY c.id
        """,
        (domain,),
    ).fetchall()


def lock_contact(conn: Any, contact_id: int) -> dict:
    """Row-locked read: two deliveries of one event cannot both see the old status."""
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM campaign_contacts WHERE id = %s FOR UPDATE",
        (contact_id,),
    ).fetchone()


def update_contact(conn: Any, contact_id: int, changes: dict) -> dict:
    set_clause = _set_clause(changes, _CONTACT_UPDATABLE)
    return conn.execute(
        f"UPDATE campaign_contacts SET {set_clause} WHERE id = %s RETURNING {_CONTACT_COLUMNS}",
        (*changes.values(), contact_id),
    ).fetchone()


def contacts(
    conn: Any,
    *,
    status: str | None = None,
    run_id: int | None = None,
    follow_up_due_on: date | None = None,
) -> list[dict]:
    conditions, params = [], []
    if status is not None:
        conditions.append("status = %s")
        params.append(status)
    if run_id is not None:
        conditions.append("run_id = %s")
        params.append(run_id)
    if follow_up_due_on is not None:
        conditions.append("follow_up_on <= %s")
        params.append(follow_up_due_on)
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM campaign_contacts{_where(conditions)} ORDER BY id",
        tuple(params),
    ).fetchall()


# --- events and counts --------------------------------------------------------------------


def insert_event(
    conn: Any,
    *,
    contact_id: int | None,
    company_id: int | None,
    status: str,
    at: datetime,
    source: str,
    graph_message_id: str | None = None,
    note: str | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO campaign_events
            (contact_id, company_id, status, at, source, graph_message_id, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (contact_id, company_id, status, at, source, graph_message_id, note),
    ).fetchone()
    return row["id"]


def sent_today(conn: Any, on: date, tz: str = TZ) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS n FROM campaign_events
        WHERE status = 'sent' AND (at AT TIME ZONE %s)::date = %s
        """,
        (tz, on),
    ).fetchone()
    return row["n"]


def counts_by_week(conn: Any, since: date | None, until: date | None) -> list[dict]:
    """One dict per ISO week in America/New_York (`week` is the Monday), oldest first: `companies_found`
    (companies by created_at), `above_bar` (company events at `active`) and one key per
    contact status (contact events). `since`/`until` bound the event/creation date, inclusive, in the same zone."""

    def bounds(column: str) -> tuple[str, list]:
        conditions, params = [], []
        if since is not None:
            conditions.append(f"({column} AT TIME ZONE '{TZ}')::date >= %s")
            params.append(since)
        if until is not None:
            conditions.append(f"({column} AT TIME ZONE '{TZ}')::date <= %s")
            params.append(until)
        return "".join(f" AND {c}" for c in conditions), params

    co_sql, co_params = bounds("created_at")
    ev_sql, ev_params = bounds("at")
    contact_list = ", ".join(f"'{s}'" for s in CONTACT_STATUSES)
    rows = conn.execute(
        f"""
        SELECT date_trunc('week', created_at AT TIME ZONE '{TZ}')::date AS week, 'companies_found' AS kind,
               count(*) AS n
        FROM campaign_companies WHERE true{co_sql} GROUP BY 1
        UNION ALL
        SELECT date_trunc('week', at AT TIME ZONE '{TZ}')::date, 'above_bar', count(*)
        FROM campaign_events
        WHERE company_id IS NOT NULL AND status = 'active'{ev_sql} GROUP BY 1
        UNION ALL
        SELECT date_trunc('week', at AT TIME ZONE '{TZ}')::date, status, count(*)
        FROM campaign_events
        WHERE contact_id IS NOT NULL AND status IN ({contact_list}){ev_sql} GROUP BY 1, 2
        """,
        tuple(co_params + ev_params + ev_params),
    ).fetchall()
    blank = {"companies_found": 0, "above_bar": 0, **dict.fromkeys(CONTACT_STATUSES, 0)}
    weeks: dict[date, dict] = {}
    for r in rows:
        weeks.setdefault(r["week"], {"week": r["week"], **blank})[r["kind"]] = r["n"]
    return [weeks[w] for w in sorted(weeks)]


def set_people_bounced(conn: Any, email: str, at: datetime) -> None:
    """Mark the address (not the contact) as hard-bounced; the first bounce time stays."""
    conn.execute(
        """
        UPDATE people SET email_bounced_at = COALESCE(email_bounced_at, %s)
        WHERE lower(email) = lower(%s)
        """,
        (at, email),
    )
