"""Pure decoding and normalization for the iMessage snapshot
(docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §5.2-5.3).

No I/O — consumes clients.imessage_local.RawChatDb (types only) and produces
models/imessage.py records. This module does the low-level per-row work;
services/imessage_export.py grows a build_batch/match_handles layer on top
in a later task.
"""

from datetime import UTC, datetime

import phonenumbers

from services.eligibility import normalize

EPOCH_2001 = 978307200
NS = 1_000_000_000
LEGACY_SECONDS_THRESHOLD = 10**11
REACTION_TYPE_MIN = 2000
REACTION_TYPE_MAX = 3999

NSSTRING_MARKER = b"NSString"
LENGTH_MARKER = 0x2B  # b"+"
EXTENDED_LENGTH_SIZES = {0x81: 2, 0x82: 3, 0x84: 4}


def apple_ts(value: int | None) -> datetime | None:
    """Convert an Apple `date` column value to a UTC datetime.

    Nanoseconds since 2001-01-01 UTC on current macOS; values below
    10**11 are treated as legacy seconds-since-2001 (spec §5.2, no real
    rows observed but kept as a defensive guard). 0/None mean missing.
    """
    if not value:
        return None
    unix_seconds: float
    if value < LEGACY_SECONDS_THRESHOLD:
        unix_seconds = value + EPOCH_2001
    else:
        unix_seconds = value / NS + EPOCH_2001
    return datetime.fromtimestamp(unix_seconds, UTC)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the first NSString payload from an Apple typedstream blob
    (an NSAttributedString's `attributedBody`). Returns None on any failure
    rather than raising — undecodable blobs (app balloons with no text) are
    expected and counted separately by the caller."""
    if blob is None:
        return None
    try:
        marker_idx = blob.index(NSSTRING_MARKER)
        plus_idx = blob.index(b"+", marker_idx + len(NSSTRING_MARKER))
        length_idx = plus_idx + 1
        first_byte = blob[length_idx]
        if first_byte in EXTENDED_LENGTH_SIZES:
            size = EXTENDED_LENGTH_SIZES[first_byte]
            length = int.from_bytes(blob[length_idx + 1 : length_idx + 1 + size], "little")
            payload_start = length_idx + 1 + size
        else:
            length = first_byte
            payload_start = length_idx + 1
        payload = blob[payload_start : payload_start + length]
        if len(payload) != length:
            return None
        return payload.decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


def normalize_handle(raw: str, region: str = "US") -> str | None:
    """Normalize a chat.db handle id to lowercased email or E.164 phone.

    Returns None for short codes and other unparseable/invalid numbers
    (spec §5.3)."""
    handle = (raw or "").strip()
    if not handle:
        return None
    if "@" in handle:
        return normalize(handle)
    digits = "".join(c for c in handle if c.isdigit())
    if len(digits) < 7:
        return None
    try:
        parsed = phonenumbers.parse(handle, region)
    except phonenumbers.NumberParseException:
        return None
    # is_possible_number (not is_valid_number): libphonenumber's metadata
    # rejects the 555-01xx NANP fictional range as "invalid" even though it
    # is a well-formed 10-digit number, and the project's own fixture/test
    # convention (CLAUDE.md) requires +1555010xxxx numbers throughout.
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_reaction(associated_message_type: int | None) -> bool:
    """True for tapbacks and their removals (spec §5.2)."""
    if associated_message_type is None:
        return False
    return REACTION_TYPE_MIN <= associated_message_type <= REACTION_TYPE_MAX
