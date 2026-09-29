"""Pure JID parsing, classification, media derivation, batch building and matching
for the WhatsApp snapshot
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §6).

No I/O — consumes clients.whatsapp_local.RawStore (types only) and produces
models/whatsapp.py records. Importing services.imessage_export is a
services-to-services import, which the layer rules permit; duplicating apple_ts
or normalize_handle would be worse (§3).
"""

import logging
from typing import Any

from clients.whatsapp_local import RawStore
from models.whatsapp import (
    WhatsAppBatch,
    WhatsAppChat,
    WhatsAppChatMember,
    WhatsAppHandle,
    WhatsAppMessage,
)
from services.imessage_export import apple_ts, normalize_handle
from services.linkedin_export import normalize_name

logger = logging.getLogger(__name__)

GROUP_SUFFIX = "@g.us"
LID_SUFFIX = "@lid"
PHONE_SUFFIX = "@s.whatsapp.net"
STATUS_JIDS = {"status@broadcast", "0@status"}
STATUS_SUFFIX = ".status"

# Legacy mobile digit inserted after the country code in older JIDs: Mexico's 1 and
# Argentina's 9 (§6.4). Keyed by country code, valued by the digit to strip.
#
# The Argentine entry can never actually fire on a well-formed number: a 13-digit AR
# local part (54 + 9 + 10 digits) is already is_possible_number = True, so
# normalize_handle succeeds on the first pass above and the retry loop never reaches
# it — unlike Mexico, where the 13-digit form is is_possible_number = False and the
# strip is the only way to recover it. The leading 9 is part of AR's international
# mobile form, not a legacy artifact like Mexico's 1, so stripping it would corrupt a
# valid number if it ever did fire. It stays anyway: the design spec §6.4 states the
# rule symmetrically by country, it costs one line, and the outcome — never firing on
# real AR numbers — is exactly the correct behavior.
LEGACY_MOBILE_DIGIT = {"52": "1", "54": "9"}
NATIONAL_DIGITS = 10

MEDIA_KIND_BY_TYPE = {1: "image", 2: "video", 3: "audio", 8: "document"}
MEDIA_CONTENT_COLUMNS = ("ZMEDIALOCALPATH", "ZMEDIAURL", "ZVCARDSTRING")

# The JID suffix is authoritative; ZSESSIONTYPE is only a sanity check, and a
# disagreement is logged rather than trusted (§6.3).
SESSION_TYPES = {"direct": {0}, "group": {1, 4}}


def normalize_phone_local(local: str) -> str | None:
    """E.164 for a @s.whatsapp.net local part, or None.

    Tries normalize_handle first, then retries once with the MX/AR legacy mobile
    digit stripped (§6.4). That retry is worth 40% of the match rate and is not
    recoverable by relaxing validation: phonenumbers reports is_possible_number
    = False for the 13-digit forms.
    """
    digits = "".join(c for c in local if c.isdigit())
    if not digits:
        return None
    e164 = normalize_handle("+" + digits)
    if e164 is not None:
        return e164
    for code, extra in LEGACY_MOBILE_DIGIT.items():
        if digits.startswith(code + extra) and len(digits) == len(code) + 1 + NATIONAL_DIGITS:
            return normalize_handle("+" + code + digits[len(code) + 1 :])
    return None


def jid_to_handle(jid: str) -> tuple[str | None, str]:
    """Return (handle, kind). handle is E.164, 'lid:<id>', or None; kind is
    'direct', 'group' or 'skip' (§6.3, §6.4).

    A 'direct' kind with a None handle is a real chat whose number would not
    normalize — the caller counts it in handles_unnormalized and skips it.
    """
    raw = (jid or "").strip()
    lowered = raw.lower()
    if not raw or "@" not in raw:
        return None, "skip"
    if lowered in STATUS_JIDS or lowered.endswith(STATUS_SUFFIX):
        return None, "skip"
    if lowered.endswith(GROUP_SUFFIX):
        return None, "group"
    local, _, domain = raw.partition("@")
    domain = "@" + domain.lower()
    if domain == LID_SUFFIX:
        # §5.5: no amount of normalization recovers a phone number from a LID.
        return f"lid:{local}", "direct"
    if domain == PHONE_SUFFIX:
        return normalize_phone_local(local), "direct"
    return None, "skip"


def classify_media(row: dict[str, Any]) -> tuple[bool, str | None]:
    """(has_media, media_kind) from the ZWAMEDIAITEM join (§5.3 item 1, §6.3).

    ZMEDIAITEM being set means nothing — it is set on 7,762 plain-text rows.
    Media is real content: a local path, a URL, or a vCard string.
    """
    if not any(row.get(column) for column in MEDIA_CONTENT_COLUMNS):
        return False, None
    if row.get("ZVCARDSTRING"):
        return True, "vcard"
    message_type = row.get("ZMESSAGETYPE")
    kind = (
        MEDIA_KIND_BY_TYPE.get(message_type, "other") if isinstance(message_type, int) else "other"
    )
    return True, kind


def check_session_type(_chat_jid: str, kind: str, session_type: int | None) -> None:
    """Log a warning when ZSESSIONTYPE disagrees with the JID suffix (§6.3).

    Counts only, never the JID itself — the repo is public and this lands in a
    terminal. The suffix wins either way.
    """
    if session_type is None or kind not in SESSION_TYPES:
        return
    if session_type not in SESSION_TYPES[kind]:
        logger.warning(
            "whatsapp session type %s disagrees with a %s JID suffix", session_type, kind
        )


def build_batch(raw: RawStore, *, mode: str) -> WhatsAppBatch:
    """Turn a RawStore into a WhatsAppBatch (§6.3-§6.6).

    Every list is deduplicated on the primary key its table uses, because a
    multi-row INSERT ... ON CONFLICT DO UPDATE fails outright when one statement
    touches a key twice — and the store really does contain a duplicate session
    JID and a duplicate stanza id (§4.2, §4.4).

    Counter semantics for a chat are batch-local: an incremental batch holds only
    its window's messages, so repo/whatsapp.py recomputes the stored totals in SQL
    rather than trusting these (§6.7).
    """
    batch = WhatsAppBatch(mode=mode, watermark=raw.max_pk)

    chats_by_jid: dict[str, WhatsAppChat] = {}
    chat_by_session: dict[int, WhatsAppChat] = {}
    handles: dict[str, WhatsAppHandle] = {}

    for session in raw.sessions:
        jid = session["ZCONTACTJID"] or ""
        handle, kind = jid_to_handle(jid)
        if kind == "skip":
            batch.sessions_skipped += 1
            continue
        if kind == "direct" and handle is None:
            # A real chat whose number will not normalize: one such chat, with a
            # 1-digit local part and no messages (§6.4).
            batch.handles_unnormalized += 1
            continue
        check_session_type(jid, kind, session.get("ZSESSIONTYPE"))
        existing = chats_by_jid.get(jid)
        if existing is not None:
            batch.duplicate_jids += 1
            chat_by_session[session["Z_PK"]] = existing
            continue
        chat = WhatsAppChat(
            chat_jid=jid,
            kind=kind,
            subject=session.get("ZPARTNERNAME"),
            handle=handle,
            created_at=apple_ts(session.get("ZGROUPCREATIONDATE")),
        )
        chats_by_jid[jid] = chat
        chat_by_session[session["Z_PK"]] = chat
        batch.chats.append(chat)
        if kind == "direct" and handle is not None:
            name = session.get("ZPARTNERNAME")
            handles[handle] = WhatsAppHandle(handle=handle, jid=jid, display_name=name)
            if name:
                batch.partner_names[handle] = name

    members_seen: set[tuple[str, str]] = set()
    member_names: dict[str, str] = {}
    for row in raw.members:
        member_chat = chat_by_session.get(row["ZCHATSESSION"])
        if member_chat is None or member_chat.kind != "group":
            continue
        member_handle, _kind = jid_to_handle(row["ZMEMBERJID"] or "")
        if member_handle is None:
            batch.handles_unnormalized += 1
            continue
        key = (member_chat.chat_jid, member_handle)
        if key in members_seen:
            continue
        members_seen.add(key)
        batch.members.append(
            WhatsAppChatMember(
                chat_jid=member_chat.chat_jid,
                handle=member_handle,
                is_admin=bool(row.get("ZISADMIN")),
                is_active=bool(row.get("ZISACTIVE")),
            )
        )
        member_chat.member_count += 1
        if row.get("ZCONTACTNAME"):
            member_names.setdefault(member_handle, row["ZCONTACTNAME"])

    # ZWAMESSAGE.ZGROUPMEMBER -> ZWAGROUPMEMBER.Z_PK -> the member's handle/raw JID
    # (§5.4). Read across every group member regardless of chat validity, since a
    # message's ZGROUPMEMBER always points at a ZWAGROUPMEMBER row, not at the
    # (already-filtered) batch.members list.
    member_handle_by_pk: dict[int, str | None] = {}
    member_jid_by_pk: dict[int, str | None] = {}
    for row in raw.members:
        member_handle_by_pk[row["Z_PK"]] = jid_to_handle(row["ZMEMBERJID"] or "")[0]
        member_jid_by_pk[row["Z_PK"]] = row.get("ZMEMBERJID")

    messages_seen: set[tuple[str, str]] = set()
    for row in raw.messages:
        msg_chat = chat_by_session.get(row["ZCHATSESSION"])
        if msg_chat is None:
            continue  # a skipped session, or one whose handle did not normalize

        stanza_id = (row.get("ZSTANZAID") or "").strip()
        if not stanza_id:
            batch.missing_stanza_ids += 1
            continue

        group_member_pk: int | None = row.get("ZGROUPMEMBER")
        from_me = bool(row.get("ZISFROMME"))
        if from_me:
            sender_handle = None
        elif msg_chat.kind == "direct":
            sender_handle = msg_chat.handle
        else:
            sender_handle = (
                member_handle_by_pk.get(group_member_pk) if group_member_pk is not None else None
            )
            if sender_handle is None:
                # sender_handle=None is documented to mean from_me, so an inbound
                # message attributed to nobody would be indistinguishable from Ben's
                # own and cannot be ranked. Same ruling as iMessage (§5.4). ZFROMJID
                # cannot rescue this: on the real store it holds the group's own JID
                # on every senderless inbound, never a sender's.
                batch.senderless_dropped += 1
                continue

        key = (msg_chat.chat_jid, stanza_id)
        if key in messages_seen:
            batch.duplicate_stanza_ids += 1
            continue
        messages_seen.add(key)

        has_media, media_kind = classify_media(row)
        text = row.get("ZTEXT")
        sent_at = apple_ts(row.get("ZMESSAGEDATE"))
        batch.messages.append(
            WhatsAppMessage(
                chat_jid=msg_chat.chat_jid,
                stanza_id=stanza_id,
                sender_handle=sender_handle,
                from_me=from_me,
                sent_at=sent_at,
                text=text if (text or "").strip() else None,
                message_type=row.get("ZMESSAGETYPE"),
                has_media=has_media,
                media_kind=media_kind,
                source_pk=row["Z_PK"],
            )
        )

        msg_chat.message_count += 1
        if from_me:
            msg_chat.my_message_count += 1
        if sent_at is not None and (
            msg_chat.last_message_at is None or sent_at > msg_chat.last_message_at
        ):
            msg_chat.last_message_at = sent_at

        # §4.1: a group sender earns a handle row on the run that sees it talk, so
        # the interaction rule needs no backfill. The raw jid comes from the group
        # member's own ZMEMBERJID, not the message's ZFROMJID — on a group row
        # ZFROMJID holds the group's own JID, not the sender's, so using it here
        # would put a @g.us JID on a person's handle row.
        if sender_handle is not None and msg_chat.kind == "group":
            handle_row = handles.get(sender_handle)
            if handle_row is None:
                handles[sender_handle] = WhatsAppHandle(
                    handle=sender_handle,
                    jid=(
                        member_jid_by_pk.get(group_member_pk)
                        if group_member_pk is not None
                        else None
                    ),
                    display_name=row.get("ZPUSHNAME") or member_names.get(sender_handle),
                )

    batch.handles = list(handles.values())
    return batch


LID_PREFIX = "lid:"


def match_handles(batch: WhatsAppBatch, people_rows: list[dict[str, Any]]) -> None:
    """Link handles and group members to `people` rows (§6.5).

    Two rules and no others. By phone: an E.164 handle matching exactly one row
    links to it; more than one links to nothing, because ambiguity is an error,
    not a guess. By name: a LID handle — and only a LID handle, since a LID
    carries no phone number by construction — links to a row whose display name
    is exactly (normalized-case, trimmed) its ZPARTNERNAME, and only when that
    name is unique on both sides.

    A handle with a phone that does not match means "no match": falling back to
    the name there would manufacture false links. Mutates the batch in place, and
    always rewrites person_id/match_method so a stale link is cleared.
    """
    ids_by_phone: dict[str, set[int]] = {}
    ids_by_name: dict[str, set[int]] = {}
    for row in people_rows:
        for phone in row.get("phone_numbers") or []:
            ids_by_phone.setdefault(phone, set()).add(row["id"])
        name = normalize_name(row.get("display_name"))
        if name:
            ids_by_name.setdefault(name, set()).add(row["id"])

    links: dict[str, tuple[int, str]] = {}
    targets = {h.handle for h in batch.handles} | {m.handle for m in batch.members}
    for handle in sorted(targets):
        if handle.startswith(LID_PREFIX):
            name = normalize_name(batch.partner_names.get(handle))
            candidates = ids_by_name.get(name, set()) if name else set()
            method = "name"
        else:
            candidates = ids_by_phone.get(handle, set())
            method = "phone"
        if len(candidates) == 1:
            links[handle] = (next(iter(candidates)), method)

    batch.matched_by_phone = batch.matched_by_name = 0
    for handle_row in batch.handles:
        link = links.get(handle_row.handle)
        handle_row.person_id, handle_row.match_method = link if link else (None, None)
        if link and link[1] == "phone":
            batch.matched_by_phone += 1
        elif link:
            batch.matched_by_name += 1
    for member in batch.members:
        link = links.get(member.handle)
        member.person_id = link[0] if link else None
