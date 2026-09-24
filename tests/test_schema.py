"""Schema tests against a real Postgres (spec §4). Skipped unless TEST_DATABASE_URL
is set, e.g. postgresql://localhost/people_schema_test."""

import os
from pathlib import Path

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = Path(__file__).resolve().parent.parent / "repo" / "schema.sql"


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        yield c


def _person(conn, email="alice@example.com"):
    conn.execute(
        "INSERT INTO people (email, first_seen) VALUES (%s, now()) ON CONFLICT DO NOTHING", (email,)
    )


def test_schema_is_idempotent(conn):
    conn.execute(SCHEMA.read_text())  # must not raise: IF NOT EXISTS + DO $$ guard


def test_handle_person_email_must_exist(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO imessage_handles (handle, person_email) VALUES (%s, %s)",
            ("+15550100001", "nobody@example.com"),
        )


def test_deleting_person_nulls_links(conn):
    _person(conn)
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_email) VALUES (%s, %s)",
        ("+15550100001", "alice@example.com"),
    )
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_email, snapshot_at)"
        " VALUES (%s, %s, %s, now())",
        ("linkedin.com/in/alice-example", "Alice Example", "alice@example.com"),
    )
    conn.execute("DELETE FROM people WHERE email = 'alice@example.com'")
    assert conn.execute("SELECT person_email FROM imessage_handles").fetchone()[0] is None
    assert conn.execute("SELECT person_email FROM linkedin_connections").fetchone()[0] is None


def test_people_has_contact_field_columns(conn):
    cols = {
        r[0]: r[1]
        for r in conn.execute(
            "select column_name, data_type from information_schema.columns"
            " where table_name = 'people'"
        ).fetchall()
    }
    assert cols["phone_numbers"] == "ARRAY"
    assert cols["company"] == "text"
    assert cols["job_title"] == "text"
    assert cols["google_fields"] == "jsonb"


def test_contact_field_defaults_and_jsonb_roundtrip(conn):
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('alice@example.com', now())")
    row = conn.execute(
        "select phone_numbers, company, job_title, google_fields from people"
    ).fetchone()
    assert row[0] == [] and row[1] is None and row[2] is None and row[3] == {}

    conn.execute(
        "UPDATE people SET phone_numbers = %s, google_fields = %s WHERE email = %s",
        (
            ["+15550100001"],
            '{"birthdays": [{"date": {"month": 4, "day": 2}}]}',
            "alice@example.com",
        ),
    )
    row = conn.execute("select phone_numbers, google_fields from people").fetchone()
    assert row[0] == ["+15550100001"]
    assert row[1]["birthdays"][0]["date"]["month"] == 4


def test_contact_field_indexes_exist(conn):
    idx = {
        r[0]
        for r in conn.execute(
            "select indexname from pg_indexes where tablename = 'people'"
        ).fetchall()
    }
    assert {
        "people_phone_numbers_idx",
        "people_google_fields_idx",
        "people_company_trgm_idx",
    } <= idx
