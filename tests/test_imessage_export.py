from datetime import UTC, datetime

from clients import imessage_local
from models.imessage import IMessageBatch, IMessageHandle
from services import imessage_export as ex
from tests.fixtures.imessage import (
    ATTR_BODY,
    apple_ns,
    build_chat_db,
    build_extended_attr_body,
    build_mixed_sender_chat_db,
)

# Longer than 127 bytes so the literal single-byte length can't represent it — forces the
# extended-length (0x81/0x82/0x84) branch.
LONG_PAYLOAD = (
    "Synthetic extended-length payload text padded out past the one hundred twenty seven "
    "byte literal length boundary so the extended marker path is exercised for real."
)


def test_apple_ts_handles_nanoseconds():
    assert ex.apple_ts(apple_ns(1_750_000_000)) == datetime.fromtimestamp(1_750_000_000, UTC)


def test_apple_ts_handles_legacy_seconds():
    assert ex.apple_ts(550_000_000) == datetime.fromtimestamp(550_000_000 + 978307200, UTC)


def test_apple_ts_treats_zero_and_none_as_missing():
    assert ex.apple_ts(0) is None and ex.apple_ts(None) is None


def test_decode_attributed_body_extracts_text():
    assert ex.decode_attributed_body(ATTR_BODY) == "Attr only body"


def test_decode_attributed_body_returns_none_when_undecodable():
    assert ex.decode_attributed_body(b"\x04\x0bstreamtyped\xff\xff") is None


def test_decode_attributed_body_extracts_extended_length_2_byte():
    assert len(LONG_PAYLOAD) > 127
    blob = build_extended_attr_body(0x81, 2, LONG_PAYLOAD)
    assert ex.decode_attributed_body(blob) == LONG_PAYLOAD


def test_decode_attributed_body_extracts_extended_length_3_byte():
    assert len(LONG_PAYLOAD) > 127
    blob = build_extended_attr_body(0x82, 3, LONG_PAYLOAD)
    assert ex.decode_attributed_body(blob) == LONG_PAYLOAD


def test_normalize_handle_formats_e164():
    assert ex.normalize_handle("(555) 010-0001", region="US") == "+15550100001"
    assert ex.normalize_handle("+1 555 010 0001") == "+15550100001"


def test_normalize_handle_lowercases_email():
    assert ex.normalize_handle("Bob@Example.COM ") == "bob@example.com"


def test_normalize_handle_drops_short_codes():
    assert ex.normalize_handle("262966") is None
    assert ex.normalize_handle("") is None


def test_is_reaction_covers_tapback_range():
    assert ex.is_reaction(2000) and ex.is_reaction(3001)
    assert not ex.is_reaction(0) and not ex.is_reaction(None)


def batch(tmp_path, **kw):
    return ex.build_batch(
        imessage_local.read(build_chat_db(tmp_path), full=True), mode="full", **kw
    )


def test_build_batch_skips_reactions_and_short_codes(tmp_path):
    b = batch(tmp_path)
    guids = {m.guid for m in b.messages}
    assert "g5" not in guids and b.reactions_skipped == 1
    assert "g9" not in guids and b.short_codes_dropped == 1
    assert {h.handle for h in b.handles} == {"+15550100001", "bob@example.com", "+15550100002"}


def test_build_batch_decodes_and_flags(tmp_path):
    b = batch(tmp_path)
    by_guid = {m.guid: m for m in b.messages}
    assert by_guid["g3"].text == "Attr only body"
    assert by_guid["g4"].text is None  # undecodable
    assert by_guid["g6"].edited_at is not None
    assert by_guid["g7"].retracted and by_guid["g7"].text is None
    assert by_guid["g10"].has_attachments
    assert by_guid["g2"].from_me and by_guid["g2"].sender_handle is None
    assert b.undecoded == 1  # g4 only
    assert b.retracted == 1  # g7 only


def test_build_batch_drops_senderless_inbound_messages(tmp_path):
    raw = imessage_local.read(build_mixed_sender_chat_db(tmp_path), full=True)
    b = ex.build_batch(raw, mode="full")
    guids = {m.guid for m in b.messages}
    assert "m1" in guids  # valid participant in the mixed chat survives
    assert "m2" not in guids  # short-code sender in the same chat is dropped
    assert b.senderless_dropped == 1


def test_build_batch_classifies_chats(tmp_path):
    chats = {c.chat_guid: c for c in batch(tmp_path).chats}
    assert chats["iMessage;+;chat9"].is_group
    assert chats["iMessage;+;chat9"].participant_handles == [
        "+15550100001",
        "bob@example.com",
        "+15550100002",
    ]
    assert not chats["iMessage;-;+15550100001"].is_group
    assert "SMS;-;262966" not in chats  # short-code chat dropped entirely


def test_match_handles_links_email_to_person():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("bob@example.com")])
    ex.match_handles(
        b, {}, [{"email": "bob@example.com", "display_name": "Bob", "google_resource_name": None}]
    )
    assert b.handles[0].person_email == "bob@example.com"
    assert b.handles[0].match_method == "email"
    assert b.matched_by_email == 1 and b.linked_to_people == 1


def test_match_handles_links_phone_via_unique_google_contact():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {"+15550100001": [{"resource_name": "people/c1", "display_name": "Alice Example"}]}
    ex.match_handles(
        b,
        index,
        [
            {
                "email": "alice@example.com",
                "display_name": "Alice",
                "google_resource_name": "people/c1",
            }
        ],
    )
    h = b.handles[0]
    assert (h.google_resource_name, h.display_name, h.person_email, h.match_method) == (
        "people/c1",
        "Alice Example",
        "alice@example.com",
        "google",
    )
    assert b.matched_by_google == 1 and b.linked_to_people == 1


def test_match_handles_google_contact_without_people_row():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {"+15550100001": [{"resource_name": "people/c9", "display_name": "Carol"}]}
    ex.match_handles(b, index, [])
    h = b.handles[0]
    assert h.google_resource_name == "people/c9" and h.display_name == "Carol"
    assert h.person_email is None and h.match_method == "google"
    assert b.matched_by_google == 1 and b.linked_to_people == 0


def test_match_handles_skips_ambiguous_number():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {
        "+15550100001": [
            {"resource_name": "people/c1", "display_name": "Alice"},
            {"resource_name": "people/c2", "display_name": "Alias"},
        ]
    }
    ex.match_handles(b, index, [])
    assert b.handles[0].match_method is None and b.handles[0].google_resource_name is None
