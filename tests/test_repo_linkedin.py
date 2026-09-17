from datetime import UTC, date, datetime

from models.linkedin import (
    LinkedInConnection,
    LinkedInMessage,
    LinkedInRecommendation,
    LinkedInSnapshot,
)
from repo import linkedin, people
from tests.test_repo_people import FakeConn

NOW = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)


def connection(slug="alice-example", **kw):
    base = dict(
        profile_url=f"linkedin.com/in/{slug}",
        first_name="Alice",
        last_name="Example",
        full_name="Alice Example",
        email=None,
        company="Example Health",
        position="CTO",
        connected_on=date(2021, 4, 2),
    )
    base.update(kw)
    return LinkedInConnection(**base)


def snapshot(connections=None, messages=None, recommendations=None):
    return LinkedInSnapshot(
        source="Basic_Export",
        snapshot_at=NOW,
        me="linkedin.com/in/me-example",
        connections=[connection()] if connections is None else connections,
        messages=messages or [],
        recommendations=recommendations or [],
        matched_by_email=1,
        matched_by_name=0,
    )


MESSAGE = LinkedInMessage(
    conversation_id="c1",
    conversation_title=None,
    sender_name="Alice Example",
    sender_profile_url="linkedin.com/in/alice-example",
    recipient_names="Me Example",
    recipient_profile_urls=["linkedin.com/in/me-example"],
    sent_at=NOW,
    subject=None,
    content="Hi",
    folder="INBOX",
    from_me=False,
)
REC = LinkedInRecommendation(
    direction="given",
    first_name="Alice",
    last_name="Example",
    full_name="Alice Example",
    company=None,
    job_title=None,
    text="Great.",
    status="VISIBLE",
    created_on=date(2025, 6, 1),
    profile_url="linkedin.com/in/alice-example",
)


def test_names_for_matching():
    conn = FakeConn(results=[[{"email": "a@b.c", "display_name": "A"}]])
    assert people.names_for_matching(conn) == [{"email": "a@b.c", "display_name": "A"}]
    assert conn.calls[0] == ("SELECT email, display_name FROM people", None)


def test_replace_snapshot_order_and_params():
    conn = FakeConn()
    linkedin.replace_snapshot(conn, snapshot(messages=[MESSAGE], recommendations=[REC]))
    sqls = [sql for sql, _ in conn.calls]
    assert sqls[:3] == [
        "DELETE FROM linkedin_messages",
        "DELETE FROM linkedin_recommendations",
        "DELETE FROM linkedin_connections",
    ]
    assert sqls[3].startswith("INSERT INTO linkedin_connections (profile_url, first_name,")
    assert sqls[4].startswith("INSERT INTO linkedin_messages (conversation_id,")
    assert "%s::text[]" in sqls[4]
    assert sqls[5].startswith("INSERT INTO linkedin_recommendations (direction,")
    assert sqls[6].startswith("INSERT INTO linkedin_imports (snapshot_at, source,")
    assert len(sqls) == 7

    conn_params = conn.calls[3][1]
    assert conn_params[0] == "linkedin.com/in/alice-example" and conn_params[-1] == NOW
    msg_params = conn.calls[4][1]
    assert ["linkedin.com/in/me-example"] in msg_params and msg_params[-1] == NOW
    assert conn.calls[6][1] == (NOW, "Basic_Export", 1, 1, 1, 0, 1, 0)


def test_replace_snapshot_skips_empty_inserts():
    conn = FakeConn()
    linkedin.replace_snapshot(conn, snapshot(connections=[]))
    sqls = [sql for sql, _ in conn.calls]
    assert len(sqls) == 4 and sqls[3].startswith("INSERT INTO linkedin_imports")
    assert conn.calls[3][1] == (NOW, "Basic_Export", 0, 0, 0, 0, 1, 0)


def test_replace_snapshot_batches():
    conn = FakeConn()
    many = [connection(slug=f"p{i}") for i in range(1201)]
    linkedin.replace_snapshot(conn, snapshot(connections=many), chunk=500)
    inserts = [
        (sql, p) for sql, p in conn.calls if sql.startswith("INSERT INTO linkedin_connections")
    ]
    assert [len(p) // 15 for _, p in inserts] == [500, 500, 201]


def test_connection_for_person():
    conn = FakeConn(results=[[{"profile_url": "linkedin.com/in/alice-example"}]])
    row = linkedin.connection_for_person(conn, "alice@example.com")
    sql, params = conn.calls[0]
    assert "FROM linkedin_connections WHERE person_email = %s" in sql
    assert "ORDER BY last_message_at DESC NULLS LAST" in sql and sql.endswith("LIMIT 1")
    assert params == ("alice@example.com",) and row["profile_url"].endswith("alice-example")


def test_search_connections():
    conn = FakeConn(results=[[]])
    linkedin.search_connections(conn, " Health ", 20)
    sql, params = conn.calls[0]
    for col in ("full_name", "company", "position"):
        assert f"{col} ILIKE %s" in sql and f"similarity({col}, %s) > 0.3" in sql
    assert params == ("%health%",) * 3 + ("Health",) * 3 + (20,)


def test_list_connections_no_filters():
    conn = FakeConn(results=[[]])
    linkedin.list_connections(conn)
    sql, params = conn.calls[0]
    assert "WHERE" not in sql
    assert sql.endswith(
        "ORDER BY last_message_at DESC NULLS LAST, connected_on DESC NULLS LAST LIMIT %s"
    )
    assert params == (50,)


def test_list_connections_all_filters():
    conn = FakeConn(results=[[]])
    linkedin.list_connections(
        conn,
        q="ali",
        company="Example",
        position="cto",
        min_messages=3,
        replied=False,
        quiet_since=date(2026, 1, 1),
        unmatched=True,
        limit=10,
    )
    sql, params = conn.calls[0]
    assert (
        "WHERE full_name ILIKE %s AND company ILIKE %s AND position ILIKE %s"
        " AND message_count >= %s AND my_message_count = 0 AND last_message_at < %s"
        " AND person_email IS NULL ORDER BY"
    ) in sql
    assert params == ("%ali%", "%Example%", "%cto%", 3, date(2026, 1, 1), 10)


def test_list_connections_true_false_variants():
    conn = FakeConn(results=[[]])
    linkedin.list_connections(conn, replied=True, unmatched=False)
    sql, _ = conn.calls[0]
    assert "my_message_count > 0 AND person_email IS NOT NULL" in sql


def test_get_connection():
    conn = FakeConn(results=[[]])
    assert linkedin.get_connection(conn, "linkedin.com/in/nobody") is None
    sql, params = conn.calls[0]
    assert "WHERE profile_url = %s" in sql and params == ("linkedin.com/in/nobody",)


def test_messages_for_uses_participant_index_expression():
    conn = FakeConn(results=[[]])
    linkedin.messages_for(conn, "linkedin.com/in/alice-example", 100)
    sql, params = conn.calls[0]
    assert "(recipient_profile_urls || ARRAY[sender_profile_url]) @> ARRAY[%s]::text[]" in sql
    assert sql.endswith("ORDER BY sent_at DESC LIMIT %s")
    assert params == ("linkedin.com/in/alice-example", 100)


def test_recommendations_for():
    conn = FakeConn(results=[[]])
    linkedin.recommendations_for(conn, "linkedin.com/in/alice-example")
    sql, params = conn.calls[0]
    assert "FROM linkedin_recommendations WHERE profile_url = %s" in sql
    assert params == ("linkedin.com/in/alice-example",)


def test_latest_import():
    conn = FakeConn(results=[[{"source": "Basic_Export"}]])
    assert linkedin.latest_import(conn) == {"source": "Basic_Export"}
    sql, _ = conn.calls[0]
    assert "FROM linkedin_imports ORDER BY id DESC LIMIT 1" in sql
