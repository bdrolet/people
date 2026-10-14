"""Validation and derivation for arbitrary Google Contacts field edits
(docs/superpowers/specs/2026-09-24-contact-field-edits-design.md §5).

Pure module: no I/O, no DB, no network. Consumed by `clients/google_contacts.py`
(read mask) and `services/person_edit.py` (write path).
"""

from typing import Any

from services.eligibility import normalize
from services.imessage_export import normalize_handle
from services.linkedin_export import normalize_profile_url

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
    "memberships": "labels",
}


class ValidationError(Exception):
    """Unknown/excluded key, or a value that is not a list of objects. -> 400."""


class EmailRuleError(Exception):
    """A submitted `emailAddresses` list drops an existing or keyed address. -> 409."""


class IdentifierRuleError(Exception):
    """A submitted `phoneNumbers` or `urls` list would leave a person with no
    email address, no phone number and no LinkedIn profile URL. -> 409."""


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


def check_email_addition(live: dict, submitted: list[dict], keyed_email: str | None) -> None:
    """Raises EmailRuleError if any existing address, or the keyed address,
    is missing from `submitted`. `keyed_email` is None for a person with no
    email address (e.g. adopted by phone only) — in that case there is no
    keyed address to require, but every address already on the Google
    contact is still required to survive."""
    existing = {normalize(str(e.get("value") or "")) for e in live.get("emailAddresses", [])}
    normalized_keyed = normalize(keyed_email) if keyed_email else ""
    if normalized_keyed:
        existing.add(normalized_keyed)
    submitted_normalized = {normalize(str(e.get("value") or "")) for e in submitted}
    missing = existing - submitted_normalized
    if missing:
        raise EmailRuleError(
            "email addresses cannot be removed or changed via PATCH "
            f"(missing: {', '.join(sorted(missing))}); "
            "removals and changes go through the Google UI"
        )


def check_identifier_kept(email: str | None, live: dict, fields: dict) -> None:
    """Raises IdentifierRuleError when applying `fields` to the live Google contact
    would leave a person with no email, no usable phone and no LinkedIn profile URL
    (people_has_an_identifier; linkedin-identity design §4). A field not being
    submitted keeps its live value. A person with an email address is unaffected."""
    if email:
        return
    phones = fields["phoneNumbers"] if "phoneNumbers" in fields else live.get("phoneNumbers", [])
    if any(normalize_handle(str(p.get("value") or "")) is not None for p in phones):
        return
    if profile_urls({"urls": fields["urls"] if "urls" in fields else live.get("urls", [])}):
        return
    raise IdentifierRuleError(
        "a person must keep an email address, a phone number or a LinkedIn profile URL; "
        "removing the last one goes through the Google UI"
    )


def identifiers(contact: dict) -> tuple[list[str], list[str]]:
    """(emails, phones) a submitted contact map would give a person.
    Emails are normalized; phones are E.164 and exclude anything
    normalize_handle rejects. Either list may be empty."""
    raw_emails = (str(e.get("value") or "") for e in contact.get("emailAddresses", []))
    emails = list(dict.fromkeys(normalized for e in raw_emails if (normalized := normalize(e))))

    raw_phones = (str(p.get("value") or "") for p in contact.get("phoneNumbers", []))
    phones = list(
        dict.fromkeys(handle for p in raw_phones if (handle := normalize_handle(p)) is not None)
    )

    return emails, phones


def profile_urls(contact: dict) -> list[str]:
    """The normalized LinkedIn profile URLs a contact map carries, in order, without
    repeats. Company pages and other URLs are not identifiers and are left out."""
    raw = (normalize_profile_url(str(u.get("value") or "")) for u in contact.get("urls", []))
    return list(dict.fromkeys(u for u in raw if u))


def _primary_organization(organizations: list[dict]) -> dict | None:
    if not organizations:
        return None
    return next(
        (org for org in organizations if org.get("metadata", {}).get("primary")),
        organizations[0],
    )


def derive(person: dict) -> dict:
    """Google person payload -> {"phone_numbers": list[str], "company": str | None,
    "job_title": str | None, "google_fields": dict,
    "linkedin_url": str | None}."""
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

    urls = profile_urls(person)

    return {
        "phone_numbers": phone_numbers,
        "company": company,
        "job_title": job_title,
        "google_fields": google_fields,
        "linkedin_url": urls[0] if urls else None,
    }
