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
