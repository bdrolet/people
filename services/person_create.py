"""POST /people (design §5): validate, refuse duplicates, create the Google
contact, then let the sync's own apply_person create the row — so a
hand-created person is byte-identical to one the nightly sync adopted."""

from typing import Any

import clients.google_contacts as gc
from repo import people
from services import contact_fields, person_edit
from services import google_contacts_sync as gsync


class Invalid(Exception):
    """Validation failure, no usable identifier, or apply_person declined to
    create a row. -> 400."""


class Duplicate(Exception):
    """The contact already exists, on either side. -> 409.

    `candidates` holds `people.id` values; a Google-only match (a contact the
    sync has not yet adopted) carries an empty list — there is no id to give.
    """

    def __init__(self, message: str, candidates: list[int]):
        super().__init__(message)
        self.candidates = candidates


def create(
    conn: Any,
    *,
    contact: dict,
    notes: str | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Spec §5. Validate, refuse duplicates, create in Google, then let
    apply_person create the row. Returns the row."""
    try:
        fields = contact_fields.validate(contact)
    except contact_fields.ValidationError as e:
        raise Invalid(str(e)) from e

    emails, phones = contact_fields.identifiers(fields)
    if not emails and not phones:
        raise Invalid("a contact needs an email address or a phone number")

    candidates: list[int] = []
    google_only = False
    for email in emails:
        row = people.get(conn, email)
        if row is not None:
            candidates.append(row["id"])
    for phone in phones:
        for row in people.get_by_phone(conn, phone):
            candidates.append(row["id"])
    if candidates:
        raise Duplicate("person exists", candidates=candidates)

    for email in emails:
        if gc.search_by_email(email) is not None:
            google_only = True
    if phones:
        phone_index = gc.list_phone_index()
        for phone in phones:
            if phone_index.get(phone):
                google_only = True
    if google_only:
        raise Duplicate("person exists", candidates=[])

    if labels:
        person_edit.validate_labels({"add": labels})

    groups = gc.list_groups()
    target = (groups.get(gsync.group_name()) or {}).get("resourceName") or gc.ensure_group(
        gsync.group_name()
    )

    created = gc.create_person(fields, target)
    gsync.apply_person(conn, created)
    row = people.get_by_google_resource(conn, created["resourceName"])
    if row is None:
        raise Invalid(
            "the contact was created in Google but no row was made; the next sync will pick it up"
        )

    if notes is not None or labels:
        return person_edit.update(
            conn, row["id"], notes=notes, labels={"add": labels} if labels else None
        )
    return row
