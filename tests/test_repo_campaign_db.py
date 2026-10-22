"""repo/campaign.py against a real Postgres. Skipped unless TEST_DATABASE_URL is set."""

import os
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from repo import campaign

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = Path(__file__).resolve().parent.parent / "repo" / "schema.sql"

# Sunday 20:30 ET is already Monday in UTC; the week must still be the one starting 10-12.
SUNDAY_ET = datetime(2026, 10, 19, 0, 30, tzinfo=UTC)
TUESDAY = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
WEEK_A, WEEK_B = date(2026, 10, 12), date(2026, 10, 19)


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True, row_factory=dict_row) as c:
        c.execute("SET TIME ZONE 'UTC'")
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        yield c


@pytest.fixture
def seeded(conn):
    run = campaign.insert_run(
        conn, ran_at=TUESDAY, lookback_from=date(2026, 10, 1), lookback_to=date(2026, 10, 19)
    )
    a = campaign.insert_company_if_missing(
        conn, {"domain": "a.io", "name": "A", "run_id": run["id"]}
    )
    b = campaign.insert_company_if_missing(
        conn, {"domain": "b.io", "name": "B", "run_id": run["id"]}
    )
    conn.execute(
        "UPDATE campaign_companies SET created_at = %s WHERE id = %s", (SUNDAY_ET, a["id"])
    )
    conn.execute("UPDATE campaign_companies SET created_at = %s WHERE id = %s", (TUESDAY, b["id"]))
    ca = campaign.insert_contact_if_missing(
        conn, {"company_id": a["id"], "full_name": "Ann", "email": "ann@a.io"}
    )
    cb = campaign.insert_contact_if_missing(
        conn, {"company_id": b["id"], "full_name": "Bob", "email": "bob@b.io"}
    )
    campaign.insert_event(
        conn, contact_id=ca["id"], company_id=None, status="sent", at=SUNDAY_ET, source="cli"
    )
    campaign.insert_event(
        conn, contact_id=cb["id"], company_id=None, status="sent", at=TUESDAY, source="cli"
    )
    campaign.insert_event(
        conn, contact_id=None, company_id=b["id"], status="active", at=TUESDAY, source="cli"
    )
    return {"a": a, "b": b, "ca": ca, "cb": cb}


def test_counts_by_week_buckets_in_new_york(conn, seeded):
    weeks = {r["week"]: r for r in campaign.counts_by_week(conn, None, None)}
    assert set(weeks) == {WEEK_A, WEEK_B}
    assert (weeks[WEEK_A]["sent"], weeks[WEEK_A]["companies_found"]) == (1, 1)
    assert (weeks[WEEK_B]["sent"], weeks[WEEK_B]["companies_found"]) == (1, 1)
    assert weeks[WEEK_A]["above_bar"] == 0 and weeks[WEEK_B]["above_bar"] == 1
    assert weeks[WEEK_B]["bounced"] == 0


def test_counts_by_week_bounds_apply_in_new_york(conn, seeded):
    # 2026-10-18 is the ET date of the Sunday-night event (10-19 in UTC).
    only_a = campaign.counts_by_week(conn, None, date(2026, 10, 18))
    assert [r["week"] for r in only_a] == [WEEK_A]
    only_b = campaign.counts_by_week(conn, date(2026, 10, 19), None)
    assert [r["week"] for r in only_b] == [WEEK_B]


def test_sent_today_uses_the_given_zone(conn, seeded):
    assert campaign.sent_today(conn, date(2026, 10, 18), "America/New_York") == 1
    assert campaign.sent_today(conn, date(2026, 10, 19), "UTC") == 1
    assert campaign.sent_today(conn, date(2026, 10, 19), "America/New_York") == 0


def test_insert_company_if_missing_ignores_case_duplicates(conn, seeded):
    assert campaign.insert_company_if_missing(conn, {"domain": "A.IO", "name": "A2"}) is None


def test_lock_contact_returns_the_row(conn, seeded):
    with conn.transaction():
        row = campaign.lock_contact(conn, seeded["ca"]["id"])
    assert row["email"] == "ann@a.io"


def _person(conn, email):
    return conn.execute(
        "INSERT INTO people (email, first_seen) VALUES (%s, %s) RETURNING id", (email, TUESDAY)
    ).fetchone()["id"]


def test_people_in_outreach_by_domain(conn):
    in_pipeline = _person(conn, "ceo@acme.com")
    _person(conn, "cfo@acme.com")  # same domain, not in outreach
    elsewhere = _person(conn, "x@other.com")
    for pid in (in_pipeline, elsewhere):
        conn.execute(
            "INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'candidate')", (pid,)
        )
    rows = campaign.people_in_outreach_by_domain(conn, "ACME.com")
    assert [(r["id"], r["email"]) for r in rows] == [(in_pipeline, "ceo@acme.com")]
    assert campaign.people_in_outreach_by_domain(conn, "nobody.io") == []


def test_run_helpers_count_bounces_and_statuses_of_the_runs_contacts(conn, seeded):
    run = campaign.latest_run(conn)
    other = campaign.insert_run(
        conn, ran_at=TUESDAY, lookback_from=date(2026, 10, 1), lookback_to=date(2026, 10, 19)
    )
    campaign.update_contact(conn, seeded["ca"]["id"], {"run_id": run["id"], "status": "bounced"})
    campaign.update_contact(conn, seeded["cb"]["id"], {"run_id": other["id"], "status": "bounced"})
    for c in (seeded["ca"], seeded["cb"]):
        campaign.insert_event(
            conn, contact_id=c["id"], company_id=None, status="bounced", at=TUESDAY, source="cli"
        )
    assert campaign.bounced_count_for_run(conn, run["id"]) == 1
    counts = campaign.status_counts_for_run(conn, run["id"])
    assert counts["bounced"] == 1 and counts["candidate"] == 0
    assert campaign.last_event_at(conn, seeded["ca"]["id"], "sent") == SUNDAY_ET
    assert campaign.last_event_at(conn, seeded["ca"]["id"], "approved") is None


def test_attention_contacts(conn, seeded):
    co = seeded["a"]["id"]

    def contact(name, status, draft_id=None, notes=()):
        c = campaign.insert_contact_if_missing(
            conn, {"company_id": co, "full_name": name, "email": f"{name}@a.io"}
        )
        campaign.update_contact(conn, c["id"], {"status": status, "draft_id": draft_id})
        for i, note in enumerate(notes):
            campaign.insert_event(
                conn,
                contact_id=c["id"],
                company_id=None,
                status=status,
                at=datetime(2026, 10, 20, 12, i, tzinfo=UTC),
                source="cli",
                note=note,
            )
        return c["id"]

    unknown = contact("unknown", "approved", "d1")
    contact("clean", "approved")
    sending = contact("sending", "followed_up", None, ["follow-up: sending"])
    failed = contact("failed", "followed_up", None, ["follow-up: FAILED: 500"])
    contact("fine", "followed_up", None, ["follow-up: FAILED: 500", "follow-up: sent"])
    rows = campaign.attention_contacts(conn)
    assert [r["id"] for r in rows] == [unknown, sending, failed]
    assert rows[0]["draft_id"] == "d1" and rows[0]["company_name"] == "A"
    assert rows[1]["last_note"] == "follow-up: sending"
    assert set(rows[0]) == {
        "id",
        "email",
        "full_name",
        "company_name",
        "status",
        "draft_id",
        "last_note",
    }
