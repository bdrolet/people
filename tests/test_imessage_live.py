import sqlite3
import subprocess

import pytest

from clients import imessage_local, imessage_send
from mcp_servers import imessage as server
from services import imessage_live
from tests.fixtures.imessage import apple_ns, build_chat_db

ALICE = "+15550100001"
ALICE_CHAT = "iMessage;-;+15550100001"
GROUP_CHAT = "iMessage;+;chat9"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = build_chat_db(tmp_path)
    monkeypatch.setenv("IMESSAGE_DB", str(path))
    return path


def _insert_from_me(path, rowid, text, chat_id, *, delivered=0, error=0, handle_id=0):
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    if "error" not in cols:
        conn.executescript(
            "ALTER TABLE message ADD COLUMN error INTEGER DEFAULT 0;"
            "ALTER TABLE message ADD COLUMN is_delivered INTEGER DEFAULT 0;"
            "ALTER TABLE message ADD COLUMN is_read INTEGER DEFAULT 0;"
        )
    conn.execute(
        "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me, date, service,"
        " cache_has_attachments, associated_message_type, error, is_delivered)"
        " VALUES (?, ?, ?, ?, 1, ?, 'iMessage', 0, 0, ?, ?)",
        (rowid, f"s{rowid}", text, handle_id, apple_ns(1_760_000_000 + rowid), error, delivered),
    )
    conn.execute(
        "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_id, rowid)
    )
    conn.commit()
    conn.close()


# --- client live reads -----------------------------------------------------------------


def test_chat_index_orders_newest_first_and_survives_missing_columns(db):
    index = imessage_local.chat_index(db)
    assert [c["guid"] for c in index.chats] == ["SMS;-;262966", GROUP_CHAT, ALICE_CHAT]
    assert index.chats[0]["service_name"] is None  # fixture has no service_name column


def test_chat_messages_returns_newest_n_oldest_first(db):
    rows = imessage_local.chat_messages(db, [1], limit=3)
    assert [r["guid"] for r in rows] == ["g5", "g6", "g7"]
    assert rows[0]["handle"] == ALICE


def test_chat_messages_with_no_chats_is_empty(db):
    assert imessage_local.chat_messages(db, [], limit=10) == []


def test_messages_after_and_max_rowid(db):
    assert imessage_local.max_message_rowid(db) == 10
    _insert_from_me(db, 11, "new", 1)
    assert [r["guid"] for r in imessage_local.messages_after(db, 10)] == ["s11"]


# --- pure service ----------------------------------------------------------------------


def test_render_message_decodes_attributed_body_and_skips_reactions(db):
    rows = {r["guid"]: r for r in imessage_local.chat_messages(db, [1], limit=50)}
    assert imessage_live.render_message(rows["g3"])["text"] == "Attr only body"
    assert imessage_live.render_message(rows["g5"]) is None
    mine = imessage_live.render_message(rows["g2"])
    assert mine["from"] == "me" and mine["status"] == "sent"
    assert imessage_live.render_message(rows["g1"])["from"] == ALICE


def test_find_chats_by_handle_excludes_groups(db):
    index = imessage_local.chat_index(db)
    assert [c["guid"] for c in imessage_live.find_chats(index, "(555) 010-0001")] == [ALICE_CHAT]
    assert [c["guid"] for c in imessage_live.find_chats(index, GROUP_CHAT)] == [GROUP_CHAT]
    assert imessage_live.find_chats(index, "nobody@example.com") == []


def test_render_chat_names_a_group_and_a_one_to_one(db):
    index = imessage_local.chat_index(db)
    by_guid = {c["guid"]: c for c in index.chats}
    group = imessage_live.render_chat(index, by_guid[GROUP_CHAT])
    assert group["name"] == "Fixture Group" and group["group"] is True
    assert group["participants"] == [ALICE, "bob@example.com", "+15550100002"]
    assert imessage_live.render_chat(index, by_guid[ALICE_CHAT])["name"] == ALICE


def test_resolve_target(db):
    index = imessage_local.chat_index(db)
    existing = imessage_live.resolve_target(index, ALICE)
    assert existing.chat["guid"] == ALICE_CHAT and existing.handle == ALICE
    new = imessage_live.resolve_target(index, "carol@example.com")
    assert new.chat is None and new.handle == "carol@example.com"
    assert imessage_live.resolve_target(index, GROUP_CHAT).chat["guid"] == GROUP_CHAT
    with pytest.raises(imessage_live.RecipientError):
        imessage_live.resolve_target(index, "any;+;nope")
    with pytest.raises(imessage_live.RecipientError):
        imessage_live.resolve_target(index, "123")


def test_confirm_send_matches_text_and_chat(db):
    _insert_from_me(db, 11, "same text", 2)
    _insert_from_me(db, 12, "same text", 1, delivered=1)
    rows = imessage_local.messages_after(db, 10)
    found = imessage_live.confirm_send(rows, " same text ", chat_rowid=1, handle=ALICE)
    assert found == {"status": "delivered", "id": 12, "chat_id": 1}
    assert imessage_live.confirm_send(rows, "other", chat_rowid=1, handle=ALICE) is None


def test_search_is_case_insensitive_and_reads_attributed_body(db):
    rows = imessage_local.recent_messages(db, limit=100)
    assert [m["text"] for m in imessage_live.search(rows, "ATTR ONLY", limit=5)] == [
        "Attr only body"
    ]


# --- osascript client ------------------------------------------------------------------


def _fake_run(returncode=0, stdout="", stderr=""):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return run, calls


def test_send_passes_text_as_an_argument_never_in_the_script(monkeypatch):
    run, calls = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)
    text = 'hi" & do shell script "rm -rf ~'
    imessage_send.send_to_chat(text, ALICE_CHAT)
    args, kwargs = calls[0]
    assert args == ["osascript", "-", text, ALICE_CHAT]
    assert text not in kwargs["input"]


def test_send_error_and_permission_problems(monkeypatch):
    run, _ = _fake_run(1, stderr="execution error: Can't get chat id. (-1728)")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(imessage_send.SendError) as e:
        imessage_send.send_to_handle("x", ALICE)
    assert not isinstance(e.value, imessage_send.MessagesUnavailable)
    assert imessage_send.chat_known("any;-;nope") is False

    run, _ = _fake_run(1, stderr="Messages got an error: AppleEvent timed out. (-1712)")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(imessage_send.MessagesUnavailable):
        imessage_send.chat_known(ALICE_CHAT)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 45)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(imessage_send.MessagesUnavailable):
        imessage_send.send_to_chat("x", ALICE_CHAT)


# --- MCP tools -------------------------------------------------------------------------


def test_list_chats_and_get_messages(db):
    chats = server.list_chats(limit=2)["chats"]
    assert [c["chat"] for c in chats] == ["SMS;-;262966", GROUP_CHAT]
    got = server.get_messages(ALICE, limit=2)
    assert [c["chat"] for c in got["chats"]] == [ALICE_CHAT]
    assert [m["text"] for m in got["messages"]] == ["Edited text", "Unsent"]


def test_search_messages_names_the_chat(db):
    hits = server.search_messages("group hello")["messages"]
    assert hits[0]["chat"] == GROUP_CHAT


def test_send_message_dry_run_sends_nothing(db, monkeypatch):
    monkeypatch.setattr(imessage_send, "_run", lambda *a: pytest.fail("sent on a dry run"))
    out = server.send_message(ALICE, "hello")
    assert out["status"] == "dry_run"
    assert out["would_send_to"]["kind"] == "existing chat"
    assert out["would_send_to"]["chat"] == ALICE_CHAT


def test_send_message_confirms_from_chat_db(db, monkeypatch):
    def fake_send(text, chat_guid):
        assert chat_guid == ALICE_CHAT
        _insert_from_me(db, 11, text, 1, delivered=1)

    monkeypatch.setattr(imessage_send, "send_to_chat", fake_send)
    out = server.send_message(ALICE, "hello", confirm_send=True)
    assert out["status"] == "delivered" and out["message_id"] == 11


def test_send_message_new_conversation_goes_to_the_handle(db, monkeypatch):
    sent = []
    monkeypatch.setattr(imessage_send, "send_to_handle", lambda t, h: sent.append((t, h)))
    monkeypatch.setattr(server, "CONFIRM_SECONDS", 0.0)
    out = server.send_message("Carol@Example.com", "hello", confirm_send=True)
    assert sent == [("hello", "carol@example.com")]
    assert out["status"] == "unconfirmed"


def test_send_message_reports_failure_without_retrying(db, monkeypatch):
    calls = []

    def fail(text, chat_guid):
        calls.append(text)
        raise imessage_send.SendError("boom")

    monkeypatch.setattr(imessage_send, "send_to_chat", fail)
    out = server.send_message(ALICE, "hello", confirm_send=True)
    assert out["status"] == "error" and calls == ["hello"]


def test_send_message_rejects_empty_and_bad_recipients(db):
    assert server.send_message(ALICE, "  ")["status"] == "error"
    assert server.send_message("12", "hi")["status"] == "error"
