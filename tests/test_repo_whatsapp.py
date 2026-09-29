from datetime import UTC, datetime

from models.whatsapp import (
    WhatsAppBatch,
    WhatsAppChat,
    WhatsAppChatMember,
    WhatsAppHandle,
    WhatsAppMessage,
)
from repo import whatsapp
from tests.test_repo_people import FakeConn

TS = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def chat(**kw):
    base = {"chat_jid": "1-2@g.us", "kind": "group", "subject": "Carpool"}
    base.update(kw)
    return WhatsAppChat(**base)


def message(**kw):
    base = {
        "chat_jid": "1-2@g.us",
        "stanza_id": "s1",
        "sender_handle": "+15550100001",
        "from_me": False,
        "sent_at": TS,
        "text": "hi",
        "message_type": 0,
        "source_pk": 7,
    }
    base.update(kw)
    return WhatsAppMessage(**base)


# --- Watermark ---------------------------------------------------------------


def test_latest_watermark_reads_max_source_pk_from_messages():
    """§6.2: the watermark is MAX(source_pk) from whatsapp_messages, so a run that
    wrote rows and died before its audit row does not re-read them all."""
    conn = FakeConn(results=[[{"watermark": 10329}]])
    assert whatsapp.latest_watermark(conn) == 10329
    sql, _ = conn.calls[0]
    assert "MAX(source_pk) " in sql and "FROM whatsapp_messages" in sql


def test_latest_watermark_defaults_to_zero():
    assert whatsapp.latest_watermark(FakeConn(results=[[{"watermark": None}]])) == 0
    assert whatsapp.latest_watermark(FakeConn(results=[[]])) == 0


# --- Chats -------------------------------------------------------------------


def test_upsert_chats_conflicts_on_chat_jid():
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat()])
    sql, params = conn.calls[0]
    assert "INSERT INTO whatsapp_chats" in sql
    assert "ON CONFLICT (chat_jid) DO UPDATE" in sql
    assert "updated_at = now()" in sql
    assert params[0] == "1-2@g.us"


def test_upsert_chats_never_writes_counters_from_the_batch():
    """Review Focus 3: an incremental batch's chat counters cover its window only.
    recompute_chat_stats owns the stored totals."""
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat(message_count=2, my_message_count=1)])
    sql, _ = conn.calls[0]
    for column in ("message_count", "my_message_count", "member_count"):
        assert f"{column} = EXCLUDED.{column}" not in sql


def test_upsert_chats_last_message_at_is_null_safe_on_conflict():
    """The iMessage import shipped this bug once: a bare EXCLUDED overwrote 939 of
    971 stored values with NULL on an incremental run (§6.7)."""
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat()])
    sql, _ = conn.calls[0]
    assert "last_message_at = EXCLUDED.last_message_at" not in sql
    assert (
        "last_message_at = GREATEST(EXCLUDED.last_message_at, whatsapp_chats.last_message_at)"
        in sql
    )


def test_upsert_chats_is_one_statement_per_chunk():
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat(chat_jid=f"c{i}@g.us") for i in range(7)], chunk=3)
    assert len(conn.calls) == 3


# --- Handles -----------------------------------------------------------------


def test_upsert_handles_writes_link_fields_only():
    """Stats columns belong exclusively to recompute_handle_stats (§6.7 step 2):
    ON CONFLICT must not reset a counter."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001", jid="x@s.whatsapp.net")])
    sql, _ = conn.calls[0]
    assert (
        "INSERT INTO whatsapp_handles (handle, jid, display_name, person_id, match_method)" in sql
    )
    assert "ON CONFLICT (handle) DO UPDATE" in sql
    for column in ("message_count", "my_message_count", "group_message_count", "group_count"):
        assert column not in sql


def test_upsert_handles_keeps_a_known_display_name():
    """A handle seen this run only as a group sender with no ZPUSHNAME carries no
    name; that must not erase the one a direct session gave it earlier."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001")])
    sql, _ = conn.calls[0]
    assert "display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)" in sql


def test_upsert_handles_rewrites_the_link_even_to_null():
    """Re-matched from scratch every run, so a link that no longer holds must
    clear rather than persist (§6.5)."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001")])
    sql, _ = conn.calls[0]
    assert "person_id = EXCLUDED.person_id" in sql
    assert "match_method = EXCLUDED.match_method" in sql


# --- Members -----------------------------------------------------------------


def test_replace_members_deletes_the_batch_s_chats_then_inserts():
    """Membership is a full snapshot with no per-row id, so it is delete-and-replace
    per chat (§6.7 step 3) — a departed member must actually disappear."""
    conn = FakeConn()
    whatsapp.replace_members(
        conn,
        [
            WhatsAppChatMember(chat_jid="1-2@g.us", handle="+15550100001", is_admin=True),
            WhatsAppChatMember(chat_jid="3-4@g.us", handle="+15550100002"),
        ],
    )
    delete_sql, delete_params = conn.calls[0]
    assert delete_sql.startswith("DELETE FROM whatsapp_chat_members WHERE chat_jid IN")
    assert set(delete_params) == {"1-2@g.us", "3-4@g.us"}
    insert_sql, _ = conn.calls[1]
    assert "INSERT INTO whatsapp_chat_members" in insert_sql
    assert "ON CONFLICT" not in insert_sql  # the delete makes it unnecessary


def test_replace_members_with_nothing_writes_nothing():
    conn = FakeConn()
    assert whatsapp.replace_members(conn, []) == 0
    assert conn.calls == []


# --- Messages ----------------------------------------------------------------


def test_upsert_messages_conflicts_on_the_two_column_key():
    conn = FakeConn()
    n = whatsapp.upsert_messages(conn, [message()])
    sql, params = conn.calls[0]
    assert "INSERT INTO whatsapp_messages" in sql
    assert "ON CONFLICT (chat_jid, stanza_id) DO UPDATE" in sql
    assert params[0] == "1-2@g.us" and params[1] == "s1"
    assert n == 1


def test_upsert_messages_chunks_at_five_hundred():
    conn = FakeConn()
    whatsapp.upsert_messages(conn, [message(stanza_id=f"s{i}") for i in range(1001)])
    assert len(conn.calls) == 3


def test_upsert_messages_updates_text_and_media_on_conflict():
    conn = FakeConn()
    whatsapp.upsert_messages(conn, [message()])
    sql, _ = conn.calls[0]
    for column in ("text", "has_media", "media_kind", "source_pk", "sent_at"):
        assert f"{column} = EXCLUDED.{column}" in sql


# --- delete_missing (--full only) --------------------------------------------


def test_delete_missing_uses_temp_tables_and_the_two_column_key():
    conn = FakeConn(results=[[], [], [], [], [{"chat_jid": "x", "stanza_id": "y"}], []])
    deleted = whatsapp.delete_missing(conn, {("1-2@g.us", "s1")}, {"1-2@g.us"})
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "CREATE TEMP TABLE tmp_wa_keys (chat_jid TEXT, stanza_id TEXT" in joined
    assert "CREATE TEMP TABLE tmp_wa_chats (chat_jid TEXT PRIMARY KEY" in joined
    assert "DELETE FROM whatsapp_messages" in joined
    assert "DELETE FROM whatsapp_chats" in joined
    assert deleted == 1


# --- Recomputes --------------------------------------------------------------


def test_recompute_chat_stats_derives_counters_from_the_tables():
    conn = FakeConn()
    whatsapp.recompute_chat_stats(conn)
    sql, _ = conn.calls[0]
    assert "UPDATE whatsapp_chats" in sql
    assert "FROM whatsapp_messages" in sql
    assert "FROM whatsapp_chat_members" in sql
    assert "message_count = COALESCE" in sql and "member_count = COALESCE" in sql


def test_recompute_handle_stats_counts_one_to_one_by_chat_and_groups_by_sender():
    """1:1 counts both sides' messages, so they come from the chat's handle —
    Ben's own rows have sender_handle NULL. Group counts are messages the handle
    SENT, which is where this diverges from iMessage (§4.1)."""
    conn = FakeConn()
    whatsapp.recompute_handle_stats(conn)
    sql, _ = conn.calls[0]
    assert "UPDATE whatsapp_handles" in sql
    assert "kind = 'direct'" in sql and "kind = 'group'" in sql
    assert "sender_handle" in sql
    assert "group_count = COALESCE" in sql
    assert "is_active" in sql


def test_recompute_handle_stats_zeroes_a_handle_with_no_messages():
    """A full re-run after deletions must not leave a stale count behind, which an
    inner-join UPDATE would."""
    conn = FakeConn()
    whatsapp.recompute_handle_stats(conn)
    sql, _ = conn.calls[0]
    assert "LEFT JOIN" in sql
    assert "COALESCE(d.one_to_one, 0)" in sql


# --- Audit and re-point ------------------------------------------------------


def test_record_import_stores_every_counter():
    conn = FakeConn()
    b = WhatsAppBatch(mode="full", watermark=10329)
    b.chats = [chat()]
    b.handles = [WhatsAppHandle(handle="+15550100001")]
    b.members = [WhatsAppChatMember(chat_jid="1-2@g.us", handle="+15550100001")]
    b.messages = [message()]
    b.senderless_dropped, b.duplicate_stanza_ids = 83, 1
    b.sessions_skipped, b.handles_unnormalized = 7, 1
    b.matched_by_phone, b.matched_by_name = 69, 0
    whatsapp.record_import(conn, b, started_at=TS, messages_deleted=4)
    sql, params = conn.calls[0]
    assert "INSERT INTO whatsapp_imports" in sql
    assert params[2] == "full" and params[3] == 10329
    assert 83 in params and 69 in params and 4 in params


def test_repoint_person_moves_handles_and_members():
    """scripts/merge_duplicate_contacts.py deletes people rows; the FKs are
    ON DELETE SET NULL, so links must move before the delete or they vanish (§4)."""
    conn = FakeConn()
    moved = whatsapp.repoint_person(conn, 5, 9)
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "UPDATE whatsapp_handles SET person_id" in joined
    assert "UPDATE whatsapp_chat_members SET person_id" in joined
    assert isinstance(moved, int)
