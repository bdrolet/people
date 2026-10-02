"""Schema tests against a real Postgres (spec §4). Skipped unless TEST_DATABASE_URL
is set, e.g. postgresql://localhost/people_schema_test."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from repo import labels as labels_repo
from repo import people as people_repo

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
    """Review Focus 1: the case that fails if _norm(None) returns ''. Driven
    through repo.people.create_from_google (the real adoption write path) so
    this actually exercises _norm, not just the schema's own tolerance for
    two raw NULLs — a regression to _norm(None) == "" would collide the
    second insert against the UNIQUE(email) constraint here, the same way
    test_empty_string_email_would_collide demonstrates for a literal ''."""
    for rn, phone in (("people/c1", "+15550100001"), ("people/c2", "+15550100002")):
        people_repo.create_from_google(
            conn,
            None,
            display_name=None,
            resource_name=rn,
            etag=None,
            notes=None,
            phone_numbers=[phone],
            company=None,
            job_title=None,
            google_fields=json.dumps({}),
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


def test_promoting_to_a_claimed_address_violates_the_unique_index(conn):
    """Review Focus 1: this is what the email_owner guard prevents."""
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('taken@example.com', now())")
    conn.execute(
        "INSERT INTO people (email, first_seen, google_resource_name, phone_numbers)"
        " VALUES (NULL, now(), 'people/c1', %s)",
        (["+15550100001"],),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "UPDATE people SET email = 'taken@example.com' WHERE google_resource_name = 'people/c1'"
        )


# --- WhatsApp snapshot (spec 2026-09-29-whatsapp-snapshot-design.md §4) -------


def _wa_chat(conn, chat_jid="1234-5678@g.us", kind="group"):
    conn.execute("INSERT INTO whatsapp_chats (chat_jid, kind) VALUES (%s, %s)", (chat_jid, kind))


def test_whatsapp_tables_exist(conn):
    names = {
        r[0]
        for r in conn.execute(
            "select table_name from information_schema.tables where table_name like 'whatsapp%%'"
        ).fetchall()
    }
    assert names == {
        "whatsapp_handles",
        "whatsapp_chats",
        "whatsapp_chat_members",
        "whatsapp_messages",
        "whatsapp_imports",
    }


def test_whatsapp_handle_person_id_must_exist(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO whatsapp_handles (handle, person_id) VALUES (%s, %s)",
            ("+15550100001", 999999),
        )


def test_deleting_person_nulls_whatsapp_links(conn):
    _person(conn)
    pid = conn.execute("select id from people where email = 'alice@example.com'").fetchone()[0]
    conn.execute(
        "INSERT INTO whatsapp_handles (handle, person_id) VALUES ('+15550100001', %s)", (pid,)
    )
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_chat_members (chat_jid, handle, person_id)"
        " VALUES ('1234-5678@g.us', '+15550100002', %s)",
        (pid,),
    )
    conn.execute("DELETE FROM people WHERE email = 'alice@example.com'")
    assert conn.execute("SELECT person_id FROM whatsapp_handles").fetchone()[0] is None
    assert conn.execute("SELECT person_id FROM whatsapp_chat_members").fetchone()[0] is None


def test_deleting_chat_cascades_to_members_and_messages(conn):
    """A membership or message row is meaningless without its chat, and both sides
    are import-owned, so CASCADE here is correct rather than destructive (§4.3)."""
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_chat_members (chat_jid, handle) VALUES ('1234-5678@g.us', '+1555010000')"
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, source_pk)"
        " VALUES ('1234-5678@g.us', 's1', false, 1)"
    )
    conn.execute("DELETE FROM whatsapp_chats WHERE chat_jid = '1234-5678@g.us'")
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_chat_members").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_messages").fetchone()[0] == 0


def test_member_handle_is_not_a_foreign_key_to_handles(conn):
    """Most group members have no whatsapp_handles row by design (§4.1/§4.3), so
    the constraint would be wrong, not merely inconvenient."""
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_chat_members (chat_jid, handle) VALUES ('1234-5678@g.us', '+15550109999')"
    )
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_chat_members").fetchone()[0] == 1


def test_message_key_is_chat_jid_and_stanza_id(conn):
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, source_pk)"
        " VALUES ('1234-5678@g.us', 's1', false, 1)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, source_pk)"
            " VALUES ('1234-5678@g.us', 's1', false, 2)"
        )


def test_handle_stats_default_to_zero(conn):
    conn.execute("INSERT INTO whatsapp_handles (handle) VALUES ('lid:123')")
    row = conn.execute(
        "select message_count, my_message_count, group_message_count, group_count,"
        " last_message_at, match_method from whatsapp_handles"
    ).fetchone()
    assert row == (0, 0, 0, 0, None, None)


def test_an_incremental_run_does_not_null_other_chats_last_message_at(conn):
    """Review Focus 3, and the bug the iMessage import shipped once (§6.7): an
    incremental batch legitimately contains no messages for most chats."""
    from models.whatsapp import WhatsAppChat
    from repo import whatsapp as wa_repo

    old, new = "1-1@g.us", "2-2@g.us"
    wa_repo.upsert_chats(
        conn,
        [
            WhatsAppChat(
                chat_jid=old, kind="group", last_message_at=datetime(2025, 1, 1, tzinfo=UTC)
            ),
            WhatsAppChat(chat_jid=new, kind="group"),
        ],
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, sent_at, source_pk)"
        " VALUES (%s, 's1', false, %s, 1)",
        (old, datetime(2025, 1, 1, tzinfo=UTC)),
    )
    wa_repo.recompute_chat_stats(conn)
    # A later incremental run whose window holds only the other chat.
    wa_repo.upsert_chats(conn, [WhatsAppChat(chat_jid=new, kind="group")])
    wa_repo.recompute_chat_stats(conn)
    rows = dict(
        conn.execute(
            "SELECT chat_jid, last_message_at FROM whatsapp_chats ORDER BY chat_jid"
        ).fetchall()
    )
    assert rows[old] is not None
    assert rows[new] is None


def test_handle_stats_recompute_over_real_rows(conn):
    """Pins the two semantics that are easy to get backwards: a 1:1 count includes
    Ben's own messages (whose sender_handle is NULL), and a group count is only
    what that handle sent (§4.1)."""
    from models.whatsapp import WhatsAppChat, WhatsAppChatMember, WhatsAppHandle
    from repo import whatsapp as wa_repo

    handle, direct, group = "+15550100001", "15550100001@s.whatsapp.net", "1-2@g.us"
    wa_repo.upsert_chats(
        conn,
        [
            WhatsAppChat(chat_jid=direct, kind="direct", handle=handle),
            WhatsAppChat(chat_jid=group, kind="group"),
        ],
    )
    wa_repo.upsert_handles(conn, [WhatsAppHandle(handle=handle)])
    wa_repo.replace_members(
        conn, [WhatsAppChatMember(chat_jid=group, handle=handle, is_active=True)]
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, sender_handle, from_me, sent_at,"
        " source_pk) VALUES"
        " (%s, 'a', %s, false, now(), 1),"
        " (%s, 'b', NULL,  true,  now(), 2),"
        " (%s, 'c', %s, false, now(), 3),"
        " (%s, 'd', NULL,  true,  now(), 4)",
        (direct, handle, direct, group, handle, group),
    )
    wa_repo.recompute_handle_stats(conn)
    row = conn.execute(
        "SELECT message_count, my_message_count, group_message_count, group_count"
        " FROM whatsapp_handles WHERE handle = %s",
        (handle,),
    ).fetchone()
    assert row == (2, 1, 1, 1)  # both sides of the 1:1; only what they sent in the group


def test_rematch_stored_handles_relinks_nulls_ambiguity_and_spares_lid(conn):
    """Final-review Finding A, end to end against real Postgres: a group-only
    sender's handle whose stored link went stale gets picked up on the next
    run's rematch, an ambiguous phone links to nothing, and a `lid:` handle's
    name match survives untouched (§6.5)."""
    from repo import whatsapp as wa_repo

    stale, ambiguous, lid = "+15550100010", "+15550100020", "lid:99900000000123"

    # A person the stale handle used to be (wrongly) linked to, with no phone
    # number of its own, and the person whose phone number it actually
    # matches now.
    conn.execute(
        "INSERT INTO people (email, phone_numbers, first_seen) VALUES"
        " ('wrong@example.com', %s, now()),"
        " (NULL, %s, now())",
        ([], [stale]),
    )
    correct_id = conn.execute(
        "SELECT id FROM people WHERE phone_numbers = %s", ([stale],)
    ).fetchone()[0]
    wrong_id = conn.execute("SELECT id FROM people WHERE email = 'wrong@example.com'").fetchone()[0]

    # The number `ambiguous` is shared by two distinct people rows on purpose —
    # 34 real numbers are shared this way in production (CLAUDE.md).
    conn.execute(
        "INSERT INTO people (email, phone_numbers, first_seen) VALUES"
        " (NULL, %s, now()), (NULL, %s, now())",
        ([ambiguous], [ambiguous]),
    )

    # A person the LID handle is matched to by name — this SQL must never touch it.
    conn.execute(
        "INSERT INTO people (email, display_name, first_seen) VALUES"
        " ('lid-match@example.com', 'Zoe Example', now())"
    )
    lid_person_id = conn.execute(
        "SELECT id FROM people WHERE email = 'lid-match@example.com'"
    ).fetchone()[0]

    conn.execute(
        "INSERT INTO whatsapp_handles (handle, person_id, match_method) VALUES"
        " (%s, %s, 'phone'),"  # stale: linked to the wrong person
        " (%s, %s, 'phone'),"  # ambiguous today: was matched once, must be nulled
        " (%s, %s, 'name')",  # lid: must survive untouched
        (stale, wrong_id, ambiguous, wrong_id, lid, lid_person_id),
    )

    wa_repo.rematch_stored_handles(conn)

    all_rows = conn.execute(
        "SELECT handle, person_id, match_method FROM whatsapp_handles"
    ).fetchall()
    rows = {r[0]: (r[1], r[2]) for r in all_rows}
    assert rows[stale] == (correct_id, "phone")
    assert rows[ambiguous] == (None, None)
    assert rows[lid] == (lid_person_id, "name")


def _linked(conn, rn, email):
    return people_repo.create_from_google(
        conn,
        email,
        display_name=None,
        resource_name=rn,
        etag=None,
        notes=None,
        phone_numbers=[],
        company=None,
        job_title=None,
        google_fields=json.dumps({}),
    )


def test_returning_columns_includes_labels(conn):
    # Review Focus 3: correlated subquery inside INSERT ... ON CONFLICT ... RETURNING
    # must be valid SQL on both write paths that use RETURNING {_COLUMNS}.
    _linked(conn, "people/c1", "a@example.com")  # create_from_google: raises if invalid
    people_repo.upsert_inbound(conn, "b@example.com", None, datetime.now(UTC))  # upsert path
    got = conn.execute(
        "SELECT labels FROM (SELECT " + people_repo._COLUMNS + " FROM people) s"
    ).fetchall()
    assert [r[0] for r in got] == [[], []]


def test_labels_join_reflects_a_rename(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "Climbing"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    labels_repo.replace_groups(conn, {"contactGroups/a": "Bouldering"})
    got = conn.execute(
        "SELECT labels FROM (SELECT " + people_repo._COLUMNS + " FROM people) s"
    ).fetchone()
    assert got[0] == ["Bouldering"]


def test_set_contact_labels_skips_unknown_groups(conn):
    # Review Focus 2: a group newer than the last refresh must not FK-fail.
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "Climbing"})
    labels_repo.set_contact_labels(
        conn, "people/c1", ["contactGroups/a", "contactGroups/unknown", "contactGroups/myContacts"]
    )
    assert conn.execute("SELECT count(*) FROM people_labels").fetchone()[0] == 1


def test_set_contact_labels_replaces_rather_than_appends(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A", "contactGroups/b": "B"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/b"])
    rows = conn.execute("SELECT group_resource_name FROM people_labels").fetchall()
    assert [r[0] for r in rows] == ["contactGroups/b"]


def test_deleting_a_group_cascades_memberships(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A", "contactGroups/b": "B"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a", "contactGroups/b"])
    labels_repo.replace_groups(conn, {"contactGroups/b": "B"})
    rows = conn.execute("SELECT group_resource_name FROM people_labels").fetchall()
    assert [r[0] for r in rows] == ["contactGroups/b"]


def test_replace_groups_with_nothing_clears_everything(conn):
    # Review Focus 5.
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    labels_repo.replace_groups(conn, {})
    assert conn.execute("SELECT count(*) FROM contact_groups").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM people_labels").fetchone()[0] == 0


def test_deleting_a_person_cascades_memberships(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    conn.execute("DELETE FROM people")
    assert conn.execute("SELECT count(*) FROM people_labels").fetchone()[0] == 0


def test_list_with_counts_includes_empty_labels(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(
        conn, {"contactGroups/a": "climbing", "contactGroups/b": "Book Club"}
    )
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    got = [tuple(r) for r in labels_repo.list_with_counts(conn)]
    assert got == [("Book Club", 0), ("climbing", 1)]
