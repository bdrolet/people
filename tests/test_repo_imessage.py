from datetime import UTC, datetime

from models.imessage import IMessageBatch, IMessageChat, IMessageHandle, IMessageMessage
from repo import imessage
from tests.test_repo_people import FakeConn

TS = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_latest_watermark_defaults_to_zero():
    assert imessage.latest_watermark(FakeConn(results=[[]])) == 0


def test_latest_watermark_reads_max_rowid():
    conn = FakeConn(results=[[{"max_rowid": 42}]])
    assert imessage.latest_watermark(conn) == 42
    assert "ORDER BY id DESC" in conn.calls[0][0]


def test_upsert_chats_upserts_on_chat_guid():
    conn = FakeConn()
    imessage.upsert_chats(
        conn,
        [
            IMessageChat(
                chat_guid="c1",
                display_name=None,
                is_group=False,
                participant_handles=["+15550100001"],
            )
        ],
    )
    sql, params = conn.calls[0]
    assert "INSERT INTO imessage_chats" in sql
    assert "ON CONFLICT (chat_guid) DO UPDATE" in sql
    assert "participant_handles = EXCLUDED.participant_handles" in sql
    assert "updated_at = now()" in sql
    assert params[0] == "c1"


def test_upsert_chats_binds_empty_participants_as_text_array():
    conn = FakeConn()
    imessage.upsert_chats(
        conn,
        [IMessageChat(chat_guid="c1", display_name=None, is_group=False, participant_handles=[])],
    )
    sql, _ = conn.calls[0]
    assert "%s::text[]" in sql


def test_upsert_messages_is_idempotent_on_guid():
    conn = FakeConn()
    n = imessage.upsert_messages(
        conn,
        [
            IMessageMessage(
                guid="g1",
                chat_guid="c1",
                sender_handle="+15550100001",
                from_me=False,
                sent_at=TS,
                text="hi",
                service="iMessage",
            )
        ],
    )
    sql, params = conn.calls[0]
    assert "INSERT INTO imessage_messages" in sql
    assert "ON CONFLICT (guid) DO UPDATE" in sql
    assert "text = EXCLUDED.text" in sql and "updated_at = now()" in sql
    assert params[0] == "g1"
    assert n == 1


def test_upsert_handles_preserves_stats_columns():
    conn = FakeConn()
    imessage.upsert_handles(conn, [IMessageHandle("+15550100001", display_name="Alice")])
    sql, _ = conn.calls[0]
    assert "ON CONFLICT (handle) DO UPDATE" in sql
    for stat in (
        "message_count",
        "my_message_count",
        "group_message_count",
        "last_message_at",
        "last_my_message_at",
        "last_group_message_at",
    ):
        assert f"{stat} = EXCLUDED" not in sql  # stats come from recompute, not the upsert


def test_delete_missing_uses_temp_tables_not_a_guid_list():
    # Six statements: CREATE tmp_guids, CREATE tmp_chat_guids, INSERT tmp_guids,
    # INSERT tmp_chat_guids, DELETE imessage_messages (RETURNING 3 rows), DELETE imessage_chats.
    conn = FakeConn(
        results=[
            [],
            [],
            [],
            [],
            [{"guid": "g1"}, {"guid": "g2"}, {"guid": "g3"}],
            [],
        ]
    )
    deleted = imessage.delete_missing(conn, {"g1"}, {"c1"})
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "CREATE TEMP TABLE" in joined and "NOT EXISTS" in joined
    assert "NOT IN (" not in joined
    assert deleted == 3


def test_recompute_handle_stats_splits_one_to_one_and_group():
    conn = FakeConn()
    imessage.recompute_handle_stats(conn)
    sql, _ = conn.calls[0]
    assert "UPDATE imessage_handles" in sql
    assert "is_group = false" in sql and "is_group = true" in sql
    assert "from_me" in sql


def test_record_import_writes_audit_row():
    conn = FakeConn()
    batch = IMessageBatch(
        mode="incremental",
        max_rowid=99,
        matched_by_email=2,
        matched_by_google=5,
        linked_to_people=3,
        undecoded=1,
    )
    imessage.record_import(conn, batch, messages_deleted=0)
    sql, params = conn.calls[0]
    assert "INSERT INTO imessage_imports" in sql
    assert "incremental" in params and 99 in params


def test_handles_filters_build_expected_sql():
    conn = FakeConn(results=[[]])
    imessage.handles(conn, q="ali", min_messages=5, replied=True, unmatched=True, limit=10)
    sql, params = conn.calls[0]
    assert "display_name ILIKE %s OR handle ILIKE %s" in sql
    assert "message_count >= %s" in sql and "my_message_count > 0" in sql
    assert "person_email IS NULL AND google_resource_name IS NULL" in sql
    assert params[-1] == 10


def test_handles_include_groups_changes_ordering():
    conn = FakeConn(results=[[]])
    imessage.handles(conn, include_groups=True)
    assert "GREATEST" in conn.calls[0][0]


def test_handles_no_filters():
    conn = FakeConn(results=[[]])
    imessage.handles(conn)
    sql, params = conn.calls[0]
    assert "WHERE" not in sql
    assert params == (50,)


def test_handle_looks_up_by_pk():
    conn = FakeConn(results=[[{"handle": "+15550100001"}]])
    row = imessage.handle(conn, "+15550100001")
    sql, params = conn.calls[0]
    assert "FROM imessage_handles WHERE handle = %s" in sql
    assert params == ("+15550100001",)
    assert row == {"handle": "+15550100001"}


def test_handle_returns_none_when_missing():
    conn = FakeConn(results=[[]])
    assert imessage.handle(conn, "+15550100001") is None


def test_handle_groups_filters_group_chats_containing_handle():
    conn = FakeConn(results=[[{"chat_guid": "c1"}]])
    rows = imessage.handle_groups(conn, "+15550100001")
    sql, params = conn.calls[0]
    assert "is_group" in sql and "ANY(c.participant_handles)" in sql
    assert params == ("+15550100001",)
    assert rows == [{"chat_guid": "c1"}]


def test_summary_for_person_aggregates_across_handles():
    conn = FakeConn(results=[[{"handles": ["+15550100001"], "message_count": 5}]])
    row = imessage.summary_for_person(conn, "alice@example.com")
    sql, params = conn.calls[0]
    assert "FROM imessage_handles WHERE person_email = %s" in sql
    assert "HAVING COUNT(*) > 0" in sql
    assert params == ("alice@example.com",)
    assert row["message_count"] == 5


def test_summary_for_person_none_when_no_handles():
    conn = FakeConn(results=[[]])
    assert imessage.summary_for_person(conn, "nobody@example.com") is None


def test_search_handles_uses_ilike_and_trigram():
    conn = FakeConn(results=[[]])
    imessage.search_handles(conn, "ali", 20)
    sql, params = conn.calls[0]
    assert "display_name ILIKE %s OR handle ILIKE %s" in sql
    assert "similarity(display_name, %s) > 0.3" in sql
    assert "similarity(handle, %s) > 0.3" in sql
    assert params == ("%ali%", "%ali%", "ali", "ali", 20)


def test_latest_import():
    conn = FakeConn(results=[[{"mode": "full"}]])
    assert imessage.latest_import(conn) == {"mode": "full"}
    sql, _ = conn.calls[0]
    assert "FROM imessage_imports ORDER BY id DESC LIMIT 1" in sql
