"""Referral outreach SQL against a real Postgres (outreach spec §5). Skipped unless
TEST_DATABASE_URL is set; see the plan's Global Constraints for a throwaway container."""

import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from repo import outreach as outreach_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = Path(__file__).resolve().parent.parent / "repo" / "schema.sql"
TS = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True, row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        yield c


def _person(conn, email="alice@example.com", **cols) -> int:
    cols = {"email": email, "first_seen": TS, **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    return conn.execute(
        f"INSERT INTO people ({names}) VALUES ({marks}) RETURNING id", tuple(cols.values())
    ).fetchone()["id"]


def test_schema_is_still_idempotent(conn):
    conn.execute(SCHEMA.read_text())


def test_contact_rejects_an_unknown_stage(conn):
    pid = _person(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'pinged')", (pid,)
        )


def test_pipeline_rows_cascade_with_their_person(conn):
    pid = _person(conn)
    conn.execute("INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'briefed')", (pid,))
    oid = conn.execute(
        "INSERT INTO outreach_opportunities (referrer_id, buyer_label, stage, intro_at) "
        "VALUES (%s, 'Buyer, Co', 'intro', %s) RETURNING id",
        (pid, TS),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO outreach_events (pipeline, opportunity_id, stage, at, source) "
        "VALUES ('buyer', %s, 'intro', %s, 'cli')",
        (oid, TS),
    )
    conn.execute("DELETE FROM people WHERE id = %s", (pid,))
    for table in ("outreach_contacts", "outreach_opportunities", "outreach_events"):
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


def test_deleting_the_buyer_keeps_the_opportunity(conn):
    referrer = _person(conn)
    buyer = _person(conn, email="buyer@example.com")
    conn.execute(
        "INSERT INTO outreach_opportunities (referrer_id, buyer_id, buyer_label, stage, intro_at) "
        "VALUES (%s, %s, 'Buyer, Co', 'intro', %s)",
        (referrer, buyer, TS),
    )
    conn.execute("DELETE FROM people WHERE id = %s", (buyer,))
    row = conn.execute("SELECT buyer_id, buyer_label FROM outreach_opportunities").fetchone()
    assert row == {"buyer_id": None, "buyer_label": "Buyer, Co"}


def test_a_referrer_event_needs_a_person_and_no_opportunity(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_events (pipeline, stage, at, source) "
            "VALUES ('referrer', 'briefed', %s, 'cli')",
            (TS,),
        )


def test_lost_needs_a_reason_and_only_lost_has_one(conn):
    pid = _person(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_opportunities (referrer_id, buyer_label, stage, intro_at) "
            "VALUES (%s, 'B', 'lost', %s)",
            (pid, TS),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_opportunities (referrer_id, buyer_label, stage, intro_at, lost_reason) "
            "VALUES (%s, 'B', 'intro', %s, 'price')",
            (pid, TS),
        )


def test_repo_round_trip(conn):
    pid = _person(conn)
    assert outreach_repo.insert_contact_if_missing(conn, pid)["stage"] == "candidate"
    assert outreach_repo.insert_contact_if_missing(conn, pid) is None
    row = outreach_repo.update_contact(conn, pid, {"stage": "to_contact"})
    assert (
        row["stage"] == "to_contact"
        and outreach_repo.lock_contact(conn, pid)["stage"] == "to_contact"
    )
    opp = outreach_repo.insert_opportunity(
        conn, referrer_id=pid, buyer_id=None, buyer_label="B, Co", at=TS
    )
    assert opp["stage"] == "intro" and opp["intro_at"] == TS
    eid = outreach_repo.insert_event(
        conn, pipeline="buyer", opportunity_id=opp["id"], stage="intro", at=TS, source="cli"
    )
    assert isinstance(eid, int)
    lost = outreach_repo.update_opportunity(
        conn, opp["id"], {"stage": "lost", "lost_reason": "price", "lost_at": TS}
    )
    assert lost["lost_reason"] == "price"


def test_repoint_then_delete_keeps_history(conn):
    keeper, loser = _person(conn), _person(conn, email="dup@example.com")
    outreach_repo.insert_contact_if_missing(conn, loser)
    outreach_repo.insert_event(
        conn, pipeline="referrer", person_id=loser, stage="candidate", at=TS, source="seed"
    )
    outreach_repo.insert_opportunity(
        conn, referrer_id=loser, buyer_id=loser, buyer_label="B", at=TS
    )
    assert outreach_repo.repoint_events(conn, loser, keeper) == 1
    assert outreach_repo.repoint_opportunities(conn, loser, keeper) == 2
    outreach_repo.move_contact(conn, loser, keeper)
    conn.execute("DELETE FROM people WHERE id = %s", (loser,))
    assert outreach_repo.get_contact(conn, keeper) is not None
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM outreach_events WHERE person_id = %s", (keeper,)
        ).fetchone()["n"]
        == 1
    )


def test_sync_rows_finds_only_unsynced_task_events(conn):
    pid = _person(conn, display_name="Alice Example")
    outreach_repo.insert_contact_if_missing(conn, pid)
    briefed = outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="briefed",
        at=TS,
        source="cli",
        channel="email",
    )
    outreach_repo.insert_event(
        conn, pipeline="referrer", person_id=pid, stage="replied", at=TS, source="cli"
    )
    opp = outreach_repo.insert_opportunity(
        conn, referrer_id=pid, buyer_id=None, buyer_label="Bo, Co", at=TS
    )
    intro = outreach_repo.insert_event(
        conn, pipeline="buyer", opportunity_id=opp["id"], stage="intro", at=TS, source="cli"
    )
    keys = ["referrer:briefed", "buyer:intro"]
    rows = outreach_repo.sync_rows(conn, keys)
    assert [r["id"] for r in rows] == [briefed, intro]
    assert rows[1]["person_name"] == "Alice Example" and rows[1]["buyer_label"] == "Bo, Co"
    outreach_repo.set_asana_gid(conn, briefed, "g1")
    outreach_repo.set_asana_completed(conn, briefed)
    assert [r["id"] for r in outreach_repo.sync_rows(conn, keys)] == [intro]
    assert [r["id"] for r in outreach_repo.sync_rows(conn, keys, event_id=intro)] == [intro]
