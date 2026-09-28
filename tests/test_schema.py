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


def test_handle_person_id_must_exist(conn):
    # person_id replaced person_email as the FK column (spec §4.1).
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO imessage_handles (handle, person_id) VALUES (%s, %s)",
            ("+15550100001", 999999),
        )


def test_deleting_person_nulls_links(conn):
    _person(conn)
    pid = conn.execute("select id from people where email = 'alice@example.com'").fetchone()[0]
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_id) VALUES (%s, %s)",
        ("+15550100001", pid),
    )
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_id, snapshot_at)"
        " VALUES (%s, %s, %s, now())",
        ("linkedin.com/in/alice-example", "Alice Example", pid),
    )
    conn.execute("DELETE FROM people WHERE email = 'alice@example.com'")
    assert conn.execute("SELECT person_id FROM imessage_handles").fetchone()[0] is None
    assert conn.execute("SELECT person_id FROM linkedin_connections").fetchone()[0] is None


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


def _apply(conn, sql: str) -> None:
    conn.execute(sql)


def test_people_is_keyed_by_id_and_email_is_nullable_unique(conn):
    pk = conn.execute(
        "select a.attname from pg_index i"
        " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
        " where i.indrelid = 'people'::regclass and i.indisprimary"
    ).fetchall()
    assert [r[0] for r in pk] == ["id"]
    nullable = conn.execute(
        "select is_nullable from information_schema.columns"
        " where table_name = 'people' and column_name = 'email'"
    ).fetchone()[0]
    assert nullable == "YES"
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('a@example.com', now())")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO people (email, first_seen) VALUES ('a@example.com', now())")


def test_identifier_check_rejects_a_person_with_neither(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO people (first_seen) VALUES (now())")


def test_phone_only_people_coexist_with_null_emails(conn):
    # Review Focus 5: many NULLs are allowed under a unique constraint.
    conn.execute(
        "INSERT INTO people (first_seen, phone_numbers) VALUES (now(), %s), (now(), %s)",
        (["+15550100001"], ["+15550100002"]),
    )
    assert conn.execute("select count(*) from people where email is null").fetchone()[0] == 2


def test_two_emailless_people_coexist(conn):
    """Review Focus 1: the case that fails if _norm(None) returns ''."""
    for rn, phone in (("people/c1", "+15550100001"), ("people/c2", "+15550100002")):
        conn.execute(
            "INSERT INTO people (email, first_seen, eligible, automated,"
            " google_resource_name, phone_numbers)"
            " VALUES (NULL, now(), TRUE, FALSE, %s, %s)",
            (rn, [phone]),
        )
    assert conn.execute("select count(*) from people where email is null").fetchone()[0] == 2


def test_empty_string_email_would_collide(conn):
    """Documents WHY NULL matters: two empty strings are not distinct."""
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('', now())")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO people (email, first_seen) VALUES ('', now())")


def test_on_conflict_email_still_upserts(conn):
    # Review Focus 5: the event handlers depend on this.
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('b@example.com', now())")
    conn.execute(
        "INSERT INTO people (email, first_seen, message_count) VALUES ('b@example.com', now(), 1)"
        " ON CONFLICT (email) DO UPDATE SET message_count = people.message_count + 1"
    )
    assert (
        conn.execute("select message_count from people where email = 'b@example.com'").fetchone()[0]
        == 1
    )


def test_child_links_use_person_id_and_null_out_on_delete(conn):
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('c@example.com', now())")
    pid = conn.execute("select id from people where email='c@example.com'").fetchone()[0]
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_id) VALUES ('+15550100003', %s)", (pid,)
    )
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_id, snapshot_at)"
        " VALUES ('linkedin.com/in/c', 'C Example', %s, now())",
        (pid,),
    )
    conn.execute("DELETE FROM people WHERE id = %s", (pid,))
    assert conn.execute("select person_id from imessage_handles").fetchone()[0] is None
    assert conn.execute("select person_id from linkedin_connections").fetchone()[0] is None
    for table in ("imessage_handles", "linkedin_connections"):
        assert (
            conn.execute(
                "select count(*) from information_schema.columns"
                " where table_name = %s and column_name = 'person_email'",
                (table,),
            ).fetchone()[0]
            == 0
        )


def test_migration_is_idempotent(conn):
    _apply(conn, SCHEMA.read_text())  # second full application
    _apply(conn, SCHEMA.read_text())  # third, for good measure


def test_migration_resumes_after_a_partial_apply(conn):
    """Review Focus 1: a crash between steps must not wedge the database."""
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    text = SCHEMA.read_text()
    cut = text.index("-- 3. Now nothing depends on the email PK index")
    _apply(conn, text[:cut])  # stop mid-migration
    _apply(conn, text)  # resume with the whole file
    pk = conn.execute(
        "select a.attname from pg_index i"
        " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
        " where i.indrelid = 'people'::regclass and i.indisprimary"
    ).fetchall()
    assert [r[0] for r in pk] == ["id"]
