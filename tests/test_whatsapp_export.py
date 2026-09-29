from services import whatsapp_export as wa

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
