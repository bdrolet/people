"""Classify a path identifier as an email, LinkedIn profile URL, phone number, or
numeric person id (spec §5.1; linkedin-identity design §3). Pure logic, no I/O —
imported by api/routers/people.py and services/person_lookup.py."""

from typing import Literal

from services.eligibility import normalize
from services.imessage_export import normalize_handle
from services.linkedin_export import normalize_profile_url

Kind = Literal["email", "linkedin", "phone", "id"]


def classify(ident: str) -> tuple[Kind, str | int] | None:
    """Email first, then a LinkedIn profile URL, then phone, then id. Returns the
    kind and the normalized value, or None when the value cannot identify anyone
    (the router turns None into a 404)."""
    raw = (ident or "").strip()
    if not raw:
        return None

    if "@" in raw:
        local, _, domain = raw.partition("@")
        if not local.strip() or not domain.strip():
            return None
        return ("email", normalize(raw))

    url = normalize_profile_url(raw)
    if url is not None:
        return ("linkedin", url)

    phone = normalize_handle(raw)
    if phone is not None:
        return ("phone", phone)

    if raw.isdigit():
        return ("id", int(raw))

    return None
