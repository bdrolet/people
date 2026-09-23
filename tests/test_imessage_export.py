from datetime import UTC, datetime

from services import imessage_export as ex
from tests.fixtures.imessage import ATTR_BODY, apple_ns


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
