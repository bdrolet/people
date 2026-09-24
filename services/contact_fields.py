"""Validation and derivation for arbitrary Google Contacts field edits
(docs/superpowers/specs/2026-09-24-contact-field-edits-design.md §5).

Pure module: no I/O, no DB, no network. Consumed by `clients/google_contacts.py`
(read mask) and `services/person_edit.py` (write path).
"""

from typing import Any

from services.eligibility import normalize
from services.imessage_export import normalize_handle

# The 24 keys `updateContact` accepts in `updatePersonFields`, verified
# against the live People API (design §5.1). `metadata` and `photos` are
# absent because the API rejects them as invalid mask paths.
WRITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "addresses",
        "biographies",
        "birthdays",
        "calendarUrls",
        "clientData",
        "emailAddresses",
        "events",
        "externalIds",
        "genders",
        "imClients",
        "interests",
        "locales",
        "locations",
        "memberships",
        "miscKeywords",
        "names",
        "nicknames",
        "occupations",
        "organizations",
        "phoneNumbers",
        "relations",
        "sipAddresses",
        "urls",
        "userDefined",
    }
)

# API-accepted, but excluded from what a PATCH caller may send: each is
# already owned by a dedicated PATCH field, which is named in the rejection.
OWNED_ELSEWHERE: dict[str, str] = {
    "biographies": "notes",
    "memberships": "relationship_label",
}


class ValidationError(Exception):
    """Unknown/excluded key, or a value that is not a list of objects. -> 400."""


class EmailRuleError(Exception):
    """A submitted `emailAddresses` list drops an existing or keyed address. -> 409."""


def validate(contact: dict) -> dict:
    """Allowlist + shape check. Returns the normalized map (bare objects wrapped
    in a one-element list). Raises ValidationError."""
    offending: list[str] = []
    for key in contact:
        if key in OWNED_ELSEWHERE:
            offending.append(f"{key} (owned by {OWNED_ELSEWHERE[key]})")
        elif key not in WRITABLE_FIELDS:
            offending.append(key)
    if offending:
        raise ValidationError(f"unknown or unwritable field(s): {', '.join(offending)}")

    normalized: dict[str, Any] = {}
    for key, value in contact.items():
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValidationError(f"{key} must be an object or a list of objects")
        normalized[key] = value
    return normalized


def check_email_addition(live: dict, submitted: list[dict], keyed_email: str) -> None:
    """Raises EmailRuleError if any existing address, or the keyed address,
    is missing from `submitted`."""
    existing = {normalize(str(e.get("value") or "")) for e in live.get("emailAddresses", [])}
    existing.add(normalize(keyed_email))
    submitted_normalized = {normalize(str(e.get("value") or "")) for e in submitted}
    missing = existing - submitted_normalized
    if missing:
        raise EmailRuleError(
            "email addresses cannot be removed or changed via PATCH "
            f"(missing: {', '.join(sorted(missing))}); "
            "removals and changes go through the Google UI"
        )


def _primary_organization(organizations: list[dict]) -> dict | None:
    if not organizations:
        return None
    return next(
        (org for org in organizations if org.get("metadata", {}).get("primary")),
        organizations[0],
    )


def derive(person: dict) -> dict:
    """Google person payload -> {"phone_numbers": list[str], "company": str | None,
    "job_title": str | None, "google_fields": dict}."""
    raw_numbers = [p.get("value", "") for p in person.get("phoneNumbers", [])]
    normalized_numbers = (normalize_handle(n) for n in raw_numbers)
    phone_numbers = list(dict.fromkeys(n for n in normalized_numbers if n is not None))

    org = _primary_organization(person.get("organizations", []))
    company = org.get("name") if org else None
    job_title = org.get("title") if org else None

    google_fields = {
        key: value
        for key, value in person.items()
        if key in WRITABLE_FIELDS and key not in OWNED_ELSEWHERE
    }

    return {
        "phone_numbers": phone_numbers,
        "company": company,
        "job_title": job_title,
        "google_fields": google_fields,
    }
