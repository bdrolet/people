"""Referral outreach SQL against a real Postgres (outreach spec §5). Skipped unless
TEST_DATABASE_URL is set; see the plan's Global Constraints for a throwaway container."""

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from repo import imessage as imessage_repo
from repo import outreach as outreach_repo
from repo import whatsapp as whatsapp_repo

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


def test_queue_orders_by_latest_contact_on_any_channel_and_fills_channels(conn):
    stale = _person(conn, email="stale@example.com", last_seen=datetime(2024, 1, 1, tzinfo=UTC))
    texted = _person(conn, email="texted@example.com", last_seen=datetime(2024, 1, 1, tzinfo=UTC))
    never = _person(conn, email=None, phone_numbers=["+15550100003"])
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_id, message_count, my_message_count, last_message_at) "
        "VALUES ('+15550100002', %s, 20, 10, %s)",
        (texted, datetime(2026, 7, 19, tzinfo=UTC)),
    )
    for pid in (stale, texted, never):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.update_contact(conn, pid, {"stage": "to_contact"})
    rows = outreach_repo.queue(conn, stage="to_contact", limit=10)
    assert [r["id"] for r in rows] == [never, stale, texted]  # never-contacted first
    by_id = {r["id"]: r for r in rows}
    assert by_id[texted]["has_imessage"] and not by_id[stale]["has_imessage"]
    assert by_id[texted]["last_interaction"] == datetime(2026, 7, 19, tzinfo=UTC)
    assert by_id[never]["email_last"] is None and by_id[never]["labels"] == []
    assert outreach_repo.queue(conn, stage="briefed", limit=10) == []


def test_follow_ups_and_shared_briefs(conn):
    a, b = _person(conn), _person(conn, email="b@example.com")
    for pid in (a, b):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.update_contact(
            conn, pid, {"stage": "briefed", "follow_up_on": datetime(2026, 10, 20).date()}
        )
        outreach_repo.insert_event(
            conn,
            pipeline="referrer",
            person_id=pid,
            stage="briefed",
            at=TS,
            source="event",
            channel="email",
            graph_message_id="g-shared",
        )
    outreach_repo.update_contact(conn, b, {"follow_up_on": datetime(2026, 10, 25).date()})
    today = datetime(2026, 10, 21).date()
    assert [r["id"] for r in outreach_repo.follow_ups(conn, today=today)] == [a]
    assert outreach_repo.follow_ups_due_count(conn, today) == 1
    assert outreach_repo.shared_briefs(conn) == [{"graph_message_id": "g-shared", "n": 2}]
    pop = {(r["pipeline"], r["stage"]): r["n"] for r in outreach_repo.population(conn)}
    assert pop == {("referrer", "briefed"): 2}


def _brief(conn, pid, week, *, replied=False):
    outreach_repo.insert_contact_if_missing(conn, pid)
    outreach_repo.update_contact(
        conn, pid, {"stage": "replied" if replied else "briefed", "cohort_week": week}
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="briefed",
        at=TS,
        source="cli",
        channel="email",
    )
    if replied:
        outreach_repo.insert_event(
            conn,
            pipeline="referrer",
            person_id=pid,
            stage="replied",
            at=TS,
            source="cli",
            channel="email",
        )


def _opp(conn, referrer, stages, lost_reason=None):
    opp = outreach_repo.insert_opportunity(
        conn, referrer_id=referrer, buyer_id=None, buyer_label="B", at=TS
    )
    for stage in ["intro", *stages]:
        outreach_repo.insert_event(
            conn, pipeline="buyer", opportunity_id=opp["id"], stage=stage, at=TS, source="cli"
        )
    final = stages[-1] if stages else "intro"
    changes = {"stage": final} | (
        {"lost_reason": lost_reason, "lost_at": TS} if final == "lost" else {}
    )
    if final != "intro":
        outreach_repo.update_opportunity(conn, opp["id"], changes)


def test_cohort_counts_derive_from_the_event_log(conn):
    w1, w2 = datetime(2026, 10, 12).date(), datetime(2026, 10, 19).date()
    a, b, c = (
        _person(conn),
        _person(conn, email="b@example.com"),
        _person(conn, email="c@example.com"),
    )
    _brief(conn, a, w1, replied=True)
    _brief(conn, b, w1)
    _brief(conn, c, w2)
    _opp(conn, a, ["conversation"])
    _opp(conn, a, ["lost"], lost_reason="timing")
    ref = {
        r["cohort_week"]: (r["briefed"], r["replied"])
        for r in outreach_repo.referrer_cohort_counts(conn, since=None, until=None)
    }
    assert ref == {w1: (2, 1), w2: (1, 0)}
    buyer = outreach_repo.buyer_cohort_counts(conn, since=None, until=None)
    assert [(r["cohort_week"], r["intro"], r["conversation"], r["proposal"]) for r in buyer] == [
        (w1, 2, 1, 0)
    ]
    assert outreach_repo.lost_reasons(conn, since=None, until=None) == [
        {"lost_reason": "timing", "n": 1}
    ]
    assert [
        r["cohort_week"] for r in outreach_repo.referrer_cohort_counts(conn, since=w2, until=None)
    ] == [w2]
    assert outreach_repo.buyer_cohort_counts(conn, since=w2, until=None) == []


def test_seed_source_queries_return_the_columns_the_bars_read(conn):
    pid = _person(
        conn,
        display_name="Alice",
        eligible=True,
        message_count=2,
        my_response_count=1,
        last_seen=TS,
    )
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_id, message_count, my_message_count, last_message_at, snapshot_at) "
        "VALUES ('https://www.linkedin.com/in/a', 'Alice', %s, 4, 2, %s, %s)",
        (pid, TS, TS),
    )
    conn.execute(
        "INSERT INTO whatsapp_handles (handle, person_id, message_count, my_message_count, last_message_at) "
        "VALUES ('+15550100001', %s, 12, 5, %s)",
        (pid, TS),
    )
    e = outreach_repo.email_seed_rows(conn)[0]
    assert {
        k: e[k]
        for k in (
            "id",
            "eligible",
            "automated",
            "message_count",
            "my_response_count",
            "last_interaction",
        )
    } == {
        "id": pid,
        "eligible": True,
        "automated": False,
        "message_count": 2,
        "my_response_count": 1,
        "last_interaction": TS,
    }
    assert outreach_repo.linkedin_seed_rows(conn)[0]["person_id"] == pid
    assert outreach_repo.whatsapp_seed_rows(conn)[0]["message_count"] == 12
    assert outreach_repo.imessage_seed_rows(conn) == []
    outreach_repo.insert_contact_if_missing(conn, pid)
    assert outreach_repo.pipeline_person_ids(conn) == {pid}


def test_nudge_is_an_event_kind_but_not_a_contact_stage(conn):
    pid = _person(conn)
    conn.execute(
        "INSERT INTO outreach_events (pipeline, person_id, stage, at, source) "
        "VALUES ('referrer', %s, 'nudge', %s, 'cli')",
        (pid, TS),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'nudge')", (pid,)
        )


def test_working_queue_columns_have_their_defaults(conn):
    pid = _person(conn)
    row = conn.execute(
        "INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'to_contact') "
        "RETURNING nudge_count, last_nudged_at, rank",
        (pid,),
    ).fetchone()
    assert row == {"nudge_count": 0, "last_nudged_at": None, "rank": None}


def test_the_stage_check_upgrade_runs_on_a_phase1_database(conn):
    # A phase-1 database has the old inline CHECK, which lacks 'nudge'.
    conn.execute("ALTER TABLE outreach_events DROP CONSTRAINT outreach_events_stage_check")
    conn.execute(
        "ALTER TABLE outreach_events ADD CONSTRAINT outreach_events_stage_check "
        "CHECK (stage IN ('candidate','briefed'))"
    )
    conn.execute(SCHEMA.read_text())
    conn.execute(SCHEMA.read_text())
    pid = _person(conn)
    conn.execute(
        "INSERT INTO outreach_events (pipeline, person_id, stage, at, source) "
        "VALUES ('referrer', %s, 'nudge', %s, 'cli')",
        (pid, TS),
    )
    names = conn.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'outreach_events'::regclass AND conname = 'outreach_events_stage_check'"
    ).fetchall()
    assert len(names) == 1


def test_nudge_columns_and_message_lookup_round_trip(conn):
    pid = _person(conn)
    outreach_repo.insert_contact_if_missing(conn, pid)
    row = outreach_repo.update_contact(
        conn, pid, {"stage": "briefed", "nudge_count": 1, "last_nudged_at": TS, "rank": None}
    )
    assert row["nudge_count"] == 1 and row["last_nudged_at"] == TS and row["rank"] is None
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="nudge",
        at=TS,
        source="event",
        channel="email",
        graph_message_id="g9",
        note="hi",
    )
    assert outreach_repo.referrer_event_with_message(conn, pid, "g9")
    assert not outreach_repo.referrer_event_with_message(conn, pid, "g10")
    assert conn.execute("SELECT note FROM outreach_events").fetchone() == {"note": "hi"}


def test_queue_puts_ranked_rows_first_in_rank_order(conn):
    a = _person(conn)
    b = _person(conn, email="b@example.com")
    c = _person(conn, email="c@example.com")
    for pid in (a, b, c):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.update_contact(conn, pid, {"stage": "to_contact"})
    outreach_repo.update_contact(conn, c, {"rank": 1})
    outreach_repo.update_contact(conn, b, {"rank": 2})
    rows = outreach_repo.queue(conn, stage="to_contact", limit=10)
    assert [r["id"] for r in rows] == [c, b, a]
    assert [r["rank"] for r in rows] == [1, 2, None]
    assert outreach_repo.clear_ranks(conn) == 2
    assert [r["rank"] for r in outreach_repo.queue(conn, stage="to_contact", limit=10)] == [
        None,
        None,
        None,
    ]


def test_follow_ups_carry_the_brief_thread_and_the_last_nudge(conn):
    pid = _person(conn)
    outreach_repo.insert_contact_if_missing(conn, pid)
    outreach_repo.update_contact(
        conn,
        pid,
        {
            "stage": "briefed",
            "briefed_at": TS,
            "channel": "email",
            "nudge_count": 1,
            "follow_up_on": date(2026, 10, 27),
        },
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="briefed",
        at=TS,
        source="event",
        channel="email",
        graph_message_id="g-brief",
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="nudge",
        at=TS + timedelta(days=7),
        source="cli",
        channel="imessage",
    )
    [row] = outreach_repo.follow_ups(conn, today=date(2026, 10, 27))
    assert row["brief_message_id"] == "g-brief" and row["brief_channel"] == "email"
    assert row["brief_at"] == TS and row["last_nudge_channel"] == "imessage"
    assert row["nudge_count"] == 1 and row["email"] == "alice@example.com"
    assert row["has_imessage"] is False
    assert outreach_repo.follow_ups(conn, today=date(2026, 10, 26)) == []


def test_today_counts_use_new_york_dates(conn):
    early = _person(conn)
    late = _person(conn, email="b@example.com")
    # October is EDT (UTC-4): 03:30 UTC on 10-14 is 23:30 on 10-13 in New York;
    # 04:30 UTC is 00:30 on 10-14.
    for pid, at in (
        (early, datetime(2026, 10, 14, 3, 30, tzinfo=UTC)),
        (late, datetime(2026, 10, 14, 4, 30, tzinfo=UTC)),
    ):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.insert_event(
            conn, pipeline="referrer", person_id=pid, stage="briefed", at=at, source="event"
        )
    # A re-brief after a reopen is not a first brief.
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=early,
        stage="briefed",
        at=datetime(2026, 10, 15, 15, tzinfo=UTC),
        source="cli",
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=late,
        stage="nudge",
        at=datetime(2026, 10, 14, 3, 59, tzinfo=UTC),
        source="cli",
    )
    d13, d14, d15 = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
    assert outreach_repo.first_briefs_between(conn, since=d13, until=d13) == 1
    assert outreach_repo.first_briefs_between(conn, since=d14, until=d15) == 1
    assert outreach_repo.first_briefs_between(conn, since=d13, until=d15) == 2
    assert outreach_repo.nudges_between(conn, since=d13, until=d13) == 1
    assert outreach_repo.nudges_between(conn, since=d14, until=d15) == 0
    assert outreach_repo.stage_count(conn, "candidate") == 2
    assert outreach_repo.stage_count(conn, "to_contact") == 0


def test_session_reads_direct_messages_and_the_pipeline_row(conn):
    pid = _person(conn)
    other = _person(conn, email="other@example.com")
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_id) "
        "VALUES ('+15550100001', %s), ('+15550100002', %s)",
        (pid, other),
    )
    conn.execute(
        "INSERT INTO imessage_chats (chat_guid, is_group, participant_handles) VALUES "
        "('direct', FALSE, ARRAY['+15550100001']), "
        "('group', TRUE, ARRAY['+15550100001', '+15550100002'])"
    )
    for i, (chat, from_me, text) in enumerate(
        [("direct", False, "first"), ("group", False, "in a group"), ("direct", True, "second")]
    ):
        conn.execute(
            "INSERT INTO imessage_messages (guid, chat_guid, from_me, sent_at, text) "
            "VALUES (%s, %s, %s, %s, %s)",
            (f"g{i}", chat, from_me, TS + timedelta(minutes=i), text),
        )
    rows = imessage_repo.recent_direct_messages(conn, pid, limit=10)
    assert [r["text"] for r in rows] == ["first", "second"]  # group excluded, oldest first
    newest = imessage_repo.recent_direct_messages(conn, pid, limit=1)
    assert [r["text"] for r in newest] == ["second"]

    conn.execute(
        "INSERT INTO whatsapp_handles (handle, person_id) VALUES ('+15550100001', %s)", (pid,)
    )
    conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, kind, handle) VALUES ('wa1', 'direct', '+15550100001')"
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, sent_at, text, source_pk) "
        "VALUES ('wa1', 's1', TRUE, %s, 'hola', 1)",
        (TS,),
    )
    wa = whatsapp_repo.recent_direct_messages(conn, pid, limit=10)
    assert [(r["text"], r["from_me"]) for r in wa] == [("hola", True)]

    assert outreach_repo.pipeline_row(conn, pid) is None
    outreach_repo.insert_contact_if_missing(conn, pid)
    outreach_repo.update_contact(conn, pid, {"stage": "to_contact", "rank": 2})
    row = outreach_repo.pipeline_row(conn, pid)
    assert row["id"] == pid and row["stage"] == "to_contact" and row["rank"] == 2
    assert row["has_imessage"] and row["has_whatsapp"]


def test_doc_url_round_trips_and_names_find_pipeline_people(conn):
    pid = _person(conn, display_name="Jamie Example")
    other = _person(conn, email="other@example.com", display_name="Jamie Other")
    _person(conn, email="outside@example.com", display_name="Jamie Outside")
    for p in (pid, other):
        outreach_repo.insert_contact_if_missing(conn, p)

    assert outreach_repo.get_contact(conn, pid)["doc_url"] is None
    url = "https://docs.google.com/document/d/doc1/edit"
    assert outreach_repo.update_contact(conn, pid, {"doc_url": url})["doc_url"] == url
    assert outreach_repo.pipeline_row(conn, pid)["doc_url"] == url
    assert outreach_repo.update_contact(conn, pid, {"doc_url": None})["doc_url"] is None

    named = outreach_repo.pipeline_people_named(conn, "JAMIE")
    assert [r["id"] for r in named] == [pid, other]  # the person outside the pipeline is not
    assert outreach_repo.pipeline_people_named(conn, "e exa")[0]["display_name"] == "Jamie Example"
    assert outreach_repo.pipeline_people_named(conn, "outside") == []
