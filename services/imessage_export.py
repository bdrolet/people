"""Pure decoding, normalization, batch building and matching for the iMessage
snapshot (docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md
§5.2-5.4).

No I/O — consumes clients.imessage_local.RawChatDb (types only) and produces
models/imessage.py records.
"""

from datetime import UTC, datetime

import phonenumbers

from clients.imessage_local import RawChatDb
from models.imessage import IMessageBatch, IMessageChat, IMessageHandle, IMessageMessage
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
        plus_idx = blob.index(bytes([LENGTH_MARKER]), marker_idx + len(NSSTRING_MARKER))
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


def build_batch(raw: RawChatDb, *, mode: str) -> IMessageBatch:
    """Turn a RawChatDb into an IMessageBatch (spec §5.2-5.3).

    Normalizes every handle, drops chats whose participants all normalize
    away (e.g. short-code-only chats) along with their messages, drops any
    remaining inbound message whose own sender handle didn't survive
    normalization (counted in senderless_dropped), and decodes each
    surviving message's text.
    """
    batch = IMessageBatch(
        mode=mode,
        max_rowid=raw.max_rowid,
        all_guids=raw.all_guids,
        all_chat_guids=raw.all_chat_guids,
    )

    normalized_by_rowid: dict[int, str | None] = {}
    dropped_raw: set[str] = set()
    seen_handles: dict[str, None] = {}
    for rowid, raw_handle in raw.handles.items():
        normalized = normalize_handle(raw_handle)
        normalized_by_rowid[rowid] = normalized
        if normalized is None:
            dropped_raw.add(raw_handle)
        elif normalized not in seen_handles:
            seen_handles[normalized] = None
            batch.handles.append(IMessageHandle(normalized))
    batch.short_codes_dropped = len(dropped_raw)

    chats_by_rowid: dict[int, IMessageChat] = {}
    for raw_chat in raw.chats:
        handle_ids = raw.chat_handles.get(raw_chat["ROWID"], [])
        participants = [
            normalized_by_rowid[hid]
            for hid in handle_ids
            if normalized_by_rowid.get(hid) is not None
        ]
        if not participants:
            continue
        imessage_chat = IMessageChat(
            chat_guid=raw_chat["guid"],
            display_name=raw_chat["display_name"],
            is_group=len(participants) > 1,
            participant_handles=[p for p in participants if p is not None],
        )
        chats_by_rowid[raw_chat["ROWID"]] = imessage_chat
        batch.chats.append(imessage_chat)

    for row in raw.messages:
        if is_reaction(row["associated_message_type"]):
            batch.reactions_skipped += 1
            continue

        chat = chats_by_rowid.get(row["chat_id"])
        if chat is None:
            continue

        is_from_me = bool(row["is_from_me"])
        sender_handle = None if is_from_me else normalized_by_rowid.get(row["handle_id"])

        # sender_handle=None is documented (spec §4.3) to mean from_me. An inbound
        # message whose sender handle didn't survive normalization (short code, or a
        # handle_id missing from the map) would be indistinguishable from a message
        # Ben sent if kept, so it's dropped rather than stored — matching spec §5.3's
        # treatment of dropped handles ("their messages are not imported") and
        # preserving the §4.3 invariant for the stats recompute downstream.
        if not is_from_me and sender_handle is None:
            batch.senderless_dropped += 1
            continue

        text = row["text"]
        if text is None:
            text = decode_attributed_body(row["attributedBody"])
            if text is None:
                batch.undecoded += 1

        is_retracted = apple_ts(row["date_retracted"]) is not None
        if is_retracted:
            text = None
            batch.retracted += 1

        sent_at = apple_ts(row["date"])
        message = IMessageMessage(
            guid=row["guid"],
            chat_guid=chat.chat_guid,
            sender_handle=sender_handle,
            from_me=is_from_me,
            sent_at=sent_at,  # type: ignore[arg-type]
            text=text,
            service=row["service"],
            has_attachments=bool(row["cache_has_attachments"]),
            edited_at=apple_ts(row["date_edited"]),
            retracted=is_retracted,
        )
        batch.messages.append(message)
        if sent_at is not None and (chat.last_message_at is None or sent_at > chat.last_message_at):
            chat.last_message_at = sent_at

    return batch


def match_handles(
    batch: IMessageBatch,
    phone_index: dict[str, list[dict[str, str]]],
    people_rows: list[dict[str, str | None]],
) -> None:
    """Link each handle to a Google contact and/or a people row (spec §5.4).

    Mutates batch.handles in place and increments batch's matching counters.
    """
    people_by_email: dict[str, dict[str, str | None]] = {}
    people_by_resource: dict[str, dict[str, str | None]] = {}
    for row in people_rows:
        email = row.get("email")
        if email:
            people_by_email[email.lower()] = row
        resource_name = row.get("google_resource_name")
        if resource_name:
            people_by_resource[resource_name] = row

    for handle in batch.handles:
        if "@" in handle.handle:
            person = people_by_email.get(handle.handle)
            if person is None:
                continue
            handle.person_email = person["email"]
            handle.display_name = person["display_name"]
            handle.match_method = "email"
            batch.matched_by_email += 1
            batch.linked_to_people += 1
            continue

        entries = phone_index.get(handle.handle, [])
        if len(entries) != 1:
            continue
        entry = entries[0]
        handle.google_resource_name = entry["resource_name"]
        handle.display_name = entry["display_name"]
        handle.match_method = "google"
        batch.matched_by_google += 1

        person = people_by_resource.get(entry["resource_name"])
        if person is not None:
            handle.person_email = person["email"]
            batch.linked_to_people += 1
