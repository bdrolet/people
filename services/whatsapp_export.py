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

from services.imessage_export import normalize_handle

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
