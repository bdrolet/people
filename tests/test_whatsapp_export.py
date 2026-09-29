import logging

from clients import whatsapp_local
from services import whatsapp_export as wa
from tests.fixtures.whatsapp import ALICE_JID, GROUP_JID, LID_JID, MX_JID, build_store

# --- JID classification (§6.3) ------------------------------------------------


def test_group_jid_classifies_as_group_with_no_handle():
    assert wa.jid_to_handle("10000000001-1500000000@g.us") == (None, "group")


def test_phone_jid_classifies_as_direct_with_an_e164_handle():
    assert wa.jid_to_handle("15550100001@s.whatsapp.net") == ("+15550100001", "direct")


def test_lid_jid_keeps_a_prefixed_handle_and_is_never_phone_matched():
    """§5.5: a LID carries no phone number, ever. The 'lid:' prefix keeps the
    handle primary key unambiguous — a LID can never collide with an E.164."""
    assert wa.jid_to_handle("99900000000001@lid") == ("lid:99900000000001", "direct")


def test_status_and_broadcast_jids_are_skipped():
    for jid in ("status@broadcast", "0@status", "12345@abc.status", "STATUS@BROADCAST"):
        assert wa.jid_to_handle(jid) == (None, "skip"), jid


def test_jid_without_an_at_or_with_an_unknown_suffix_is_skipped():
    for jid in ("garbage", "", "   ", "12345@newsletter", "12345@call"):
        assert wa.jid_to_handle(jid) == (None, "skip"), jid


def test_unnormalizable_phone_jid_is_direct_with_no_handle():
    """The suffix is authoritative for the kind; the handle is separately absent,
    which is what handles_unnormalized counts (§6.4)."""
    assert wa.jid_to_handle("5@s.whatsapp.net") == (None, "direct")


# --- The MX/AR legacy-mobile-digit rule (§6.4) --------------------------------


def test_mexican_legacy_mobile_digit_is_stripped():
    """Worth 40% of the match rate (43 -> 60 chats). phonenumbers reports
    is_possible_number=False for the 13-digit form, so it cannot be recovered by
    loosening validation — the digit must go."""
    assert wa.normalize_phone_local("5215555555555") == "+525555555555"


def test_argentinian_thirteen_digit_number_is_preserved():
    """Unlike Mexico's legacy 1, Argentina's leading 9 in a 13-digit local part
    (54 + 9 + 10 digits) is part of the number's international mobile form, not a
    legacy artifact. libphonenumber accepts it directly — is_possible_number is
    already True — so normalize_handle succeeds on the first pass and the
    strip-and-retry in LEGACY_MOBILE_DIGIT never fires. Stripping the 9 here would
    corrupt a valid number; the rule staying inert for AR is the correct outcome,
    not a bug."""
    assert wa.normalize_phone_local("5491155555555") == "+5491155555555"


def test_an_already_twelve_digit_mexican_number_is_not_mangled():
    assert wa.normalize_phone_local("525555555555") == "+525555555555"


def test_a_us_number_is_untouched_by_the_legacy_rule():
    assert wa.normalize_phone_local("15550100001") == "+15550100001"


def test_a_number_that_survives_neither_pass_is_none():
    assert wa.normalize_phone_local("5") is None
    assert wa.normalize_phone_local("") is None
    assert wa.normalize_phone_local("abc") is None


def test_a_thirteen_digit_number_under_another_country_code_is_not_stripped():
    """The rule is scoped to 52/54 — a 13-digit local part elsewhere is left to
    normalize_handle's verdict, not silently shortened."""
    assert wa.normalize_phone_local("4917612345678") == "+4917612345678"


# --- Media (§5.3 item 1, §6.3) ------------------------------------------------


def _media_row(**kw):
    base = {
        "ZMESSAGETYPE": 0,
        "ZMEDIAITEM": 7,
        "ZMEDIALOCALPATH": None,
        "ZMEDIAURL": None,
        "ZVCARDSTRING": None,
    }
    base.update(kw)
    return base


def test_a_media_item_with_no_real_content_is_not_media():
    """The single most dangerous piece of folklore: ZMEDIAITEM is set on 7,762
    plain-text rows, and trusting it reports 9,664 media messages instead of 1,576."""
    assert wa.classify_media(_media_row()) == (False, None)


def test_a_local_path_makes_it_media_and_the_kind_comes_from_the_type():
    assert wa.classify_media(_media_row(ZMESSAGETYPE=1, ZMEDIALOCALPATH="Media/x.jpg")) == (
        True,
        "image",
    )
    assert wa.classify_media(_media_row(ZMESSAGETYPE=2, ZMEDIAURL="https://example.com/v")) == (
        True,
        "video",
    )
    assert wa.classify_media(_media_row(ZMESSAGETYPE=3, ZMEDIALOCALPATH="Media/a.m4a")) == (
        True,
        "audio",
    )
    assert wa.classify_media(_media_row(ZMESSAGETYPE=8, ZMEDIALOCALPATH="Media/d.pdf")) == (
        True,
        "document",
    )


def test_a_vcard_string_wins_over_the_message_type():
    assert wa.classify_media(_media_row(ZMESSAGETYPE=4, ZVCARDSTRING="BEGIN:VCARD")) == (
        True,
        "vcard",
    )


def test_an_unmapped_type_with_real_media_is_other():
    """21 distinct ZMESSAGETYPE values with a long tail; an allowlist would
    silently drop real messages (§5.3)."""
    assert wa.classify_media(_media_row(ZMESSAGETYPE=46, ZMEDIALOCALPATH="Media/x")) == (
        True,
        "other",
    )


def test_session_types_are_a_sanity_check_not_the_authority():
    assert 0 in wa.SESSION_TYPES["direct"]
    assert wa.SESSION_TYPES["group"] == {1, 4}


def batch(tmp_path, **kw):
    raw = whatsapp_local.read(build_store(tmp_path), **kw)
    return wa.build_batch(raw, mode="full")


# --- Sessions -> chats (§4.2, §6.3) -------------------------------------------


def test_sessions_become_chats_and_the_skipped_ones_are_counted(tmp_path):
    b = batch(tmp_path)
    assert {c.chat_jid for c in b.chats} == {ALICE_JID, GROUP_JID, LID_JID, MX_JID}
    assert b.sessions_skipped == 2  # status@broadcast and the JID with no '@'
    assert b.handles_unnormalized == 1  # the 1-digit local part
    assert b.duplicate_jids == 1  # the second row for Alice's JID


def test_a_duplicate_session_jid_collapses_to_one_chat(tmp_path):
    """208 sessions carry 207 distinct JIDs; the extra row holds no messages, so
    collapsing on chat_jid is correct — and deliberate, not discovered (§4.2).
    Review Focus 2: two rows for one key in a batch would otherwise make
    ON CONFLICT DO UPDATE fail with 'cannot affect row a second time'."""
    b = batch(tmp_path)
    assert len([c for c in b.chats if c.chat_jid == ALICE_JID]) == 1


def test_a_group_chat_carries_its_subject_creation_date_and_member_count(tmp_path):
    group = next(c for c in batch(tmp_path).chats if c.chat_jid == GROUP_JID)
    assert group.kind == "group"
    assert group.subject == "Soccer Carpool"
    assert group.created_at is not None
    assert group.handle is None
    assert group.member_count == 4


def test_a_direct_chat_denormalizes_the_other_party_s_handle(tmp_path):
    chats = {c.chat_jid: c for c in batch(tmp_path).chats}
    assert chats[ALICE_JID].kind == "direct"
    assert chats[ALICE_JID].handle == "+15550100001"
    assert chats[ALICE_JID].subject == "Alice Example"
    assert chats[LID_JID].handle == "lid:99900000000001"
    assert chats[MX_JID].handle == "+525555555555"


def test_chat_counters_are_set_from_the_batch_s_messages(tmp_path):
    chats = {c.chat_jid: c for c in batch(tmp_path).chats}
    assert chats[ALICE_JID].message_count == 6  # s1,s2,s3,s9,s10,s13
    assert chats[ALICE_JID].my_message_count == 1
    assert chats[GROUP_JID].message_count == 2  # s4 and Ben's s6
    assert chats[GROUP_JID].my_message_count == 1
    assert chats[ALICE_JID].last_message_at is not None


def test_a_session_type_disagreement_logs_a_warning_without_the_jid(tmp_path, caplog):
    raw = whatsapp_local.read(build_store(tmp_path))
    for session in raw.sessions:
        if session["ZCONTACTJID"] == GROUP_JID:
            session["ZSESSIONTYPE"] = 0  # a group claiming to be 1:1
    with caplog.at_level(logging.WARNING):
        wa.build_batch(raw, mode="full")
    assert any("disagrees" in r.message for r in caplog.records)
    assert GROUP_JID not in caplog.text


# --- Messages (§5.4, §6.3, §4.4) ---------------------------------------------


def test_messages_from_a_skipped_session_are_not_stored(tmp_path):
    assert "s12" not in {m.stanza_id for m in batch(tmp_path).messages}


def test_senderless_group_inbound_is_dropped_and_counted(tmp_path):
    """§5.4: 83 such rows. sender_handle=None means from_me, so keeping them
    would make a message from nobody indistinguishable from Ben's own."""
    b = batch(tmp_path)
    assert "s5" not in {m.stanza_id for m in b.messages}
    assert b.senderless_dropped == 1


def test_ben_s_own_group_message_without_a_group_member_is_kept(tmp_path):
    mine = next(m for m in batch(tmp_path).messages if m.stanza_id == "s6")
    assert mine.from_me is True
    assert mine.sender_handle is None


def test_a_group_message_is_attributed_through_zgroupmember(tmp_path):
    """WhatsApp can attribute group senders where chat.db could not (§5.4)."""
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s4")
    assert msg.sender_handle == "+15550100001"
    assert msg.from_me is False


def test_a_one_to_one_inbound_sender_comes_from_the_session(tmp_path):
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s2")
    assert msg.sender_handle == "+15550100001"


def test_a_duplicate_stanza_id_collapses_and_is_counted(tmp_path):
    """One known pair in 10,329 rows; the only real problem would be collapsing
    it silently (§4.4). The first row wins."""
    b = batch(tmp_path)
    dupes = [m for m in b.messages if m.stanza_id == "s2"]
    assert len(dupes) == 1
    assert dupes[0].text == "Hello"  # the first row, not "Hello again"
    assert b.duplicate_stanza_ids == 1


def test_empty_text_is_stored_as_none_not_empty_string(tmp_path):
    """Review Focus 5 (§6.3)."""
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s3")
    assert msg.text is None


def test_a_message_with_no_stanza_id_is_dropped_and_counted(tmp_path):
    """Review Focus 5: (chat_jid, stanza_id) is the primary key and stanza_id is
    NOT NULL, so an unkeyable row must be dropped, not fed to the insert. No such
    row was measured; this is a guard."""
    raw = whatsapp_local.read(build_store(tmp_path))
    raw.messages.append({**raw.messages[0], "Z_PK": 99, "ZSTANZAID": None})
    raw.messages.append({**raw.messages[0], "Z_PK": 100, "ZSTANZAID": "  "})
    b = wa.build_batch(raw, mode="full")
    assert b.missing_stanza_ids == 2
    assert all(m.stanza_id and m.stanza_id.strip() for m in b.messages)


def test_raw_message_type_is_kept_and_never_used_to_filter(tmp_path):
    """21 distinct values with a long tail; an allowlist would silently drop real
    messages (§5.3)."""
    by_stanza = {m.stanza_id: m for m in batch(tmp_path).messages}
    assert by_stanza["s13"].message_type == 4
    assert by_stanza["s9"].message_type == 1


def test_media_flags_come_from_real_content(tmp_path):
    by_stanza = {m.stanza_id: m for m in batch(tmp_path).messages}
    assert (by_stanza["s9"].has_media, by_stanza["s9"].media_kind) == (True, "image")
    assert (by_stanza["s13"].has_media, by_stanza["s13"].media_kind) == (True, "vcard")
    assert (by_stanza["s10"].has_media, by_stanza["s10"].media_kind) == (False, None)


def test_source_pk_and_watermark_track_the_store(tmp_path):
    b = batch(tmp_path)
    assert b.watermark == 13
    assert max(m.source_pk for m in b.messages) == 13


def test_sent_at_reads_seconds_since_2001(tmp_path):
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s1")
    assert msg.sent_at is not None
    assert msg.sent_at.year == 2025


# --- Handles (§4.1, §6.6) ----------------------------------------------------


def test_only_identities_with_interaction_get_a_handle_row(tmp_path):
    """§4.1: a row per identity seen anywhere would be 6,767 rows, 6,193 of them
    strangers in two 1,200-member community groups. Dana is an active member who
    never posts; she gets a member row and no handle row."""
    b = batch(tmp_path)
    assert {h.handle for h in b.handles} == {
        "+15550100001",
        "lid:99900000000001",
        "+525555555555",
    }
    assert "+15550100002" in {m.handle for m in b.members}


def test_a_handle_keeps_the_raw_jid_and_prefers_the_partner_name(tmp_path):
    """§6.4: the raw JID is stored regardless, so nothing is lost when
    normalization fails. §6.6: ZPARTNERNAME beats ZPUSHNAME."""
    handles = {h.handle: h for h in batch(tmp_path).handles}
    assert handles["+15550100001"].jid == ALICE_JID
    assert handles["+15550100001"].display_name == "Alice Example"
    assert handles["lid:99900000000001"].display_name == "Bob Example"


def test_handles_are_unique_in_the_batch(tmp_path):
    handles = [h.handle for h in batch(tmp_path).handles]
    assert len(handles) == len(set(handles))


def test_partner_names_come_only_from_direct_sessions(tmp_path):
    """The LID name match reads this map and nothing else, so a group member's
    ZCONTACTNAME must never reach it (§6.5)."""
    b = batch(tmp_path)
    assert b.partner_names["lid:99900000000001"] == "Bob Example"
    assert "+15550100002" not in b.partner_names


# --- Members (§4.3) ----------------------------------------------------------


def test_every_group_member_is_recorded_with_admin_and_active_flags(tmp_path):
    members = {m.handle: m for m in batch(tmp_path).members}
    assert set(members) == {
        "+15550100001",
        "+15550100002",
        "+15550100003",
        "lid:99900000000002",
    }
    assert members["+15550100001"].is_admin is True
    assert members["+15550100001"].is_active is True
    assert members["+15550100003"].is_active is False
    assert all(m.chat_jid == GROUP_JID for m in members.values())


def test_members_are_unique_per_chat_and_handle(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    raw.members.append({**raw.members[0], "Z_PK": 99})  # same chat, same JID
    b = wa.build_batch(raw, mode="full")
    keys = [(m.chat_jid, m.handle) for m in b.members]
    assert len(keys) == len(set(keys))


def test_a_member_of_a_skipped_or_missing_chat_is_ignored(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    raw.members.append({**raw.members[0], "Z_PK": 98, "ZCHATSESSION": 4})  # the status session
    raw.members.append({**raw.members[0], "Z_PK": 97, "ZCHATSESSION": 999})  # no such session
    assert len(wa.build_batch(raw, mode="full").members) == 4


def test_mode_is_carried_through(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert wa.build_batch(raw, mode="incremental").mode == "incremental"
