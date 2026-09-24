"""PATCH /people/{email}: write to Google Contacts first (it is the truth),
then refresh the DB row from Google. Spec §9, contact fields design §5.4."""

import logging
from typing import Any

from googleapiclient.errors import HttpError

import clients.google_contacts as gc
from repo import people
from services import contact_fields
from services import google_contacts_sync as gsync

logger = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class NotLinked(Exception):
    pass


class Conflict(Exception):
    """Stale etag, or a submitted `emailAddresses` list would remove or
    change an existing/keyed address. -> 409."""


class Invalid(Exception):
    """Validation failure, or a Google 4xx (message carried verbatim). -> 400."""


def _set_label(person_rn: str, live: dict, label: str) -> None:
    """Replace the person's relationship label: leave every unrelated group alone,
    remove only the group currently read as the label, add the target."""
    groups = gc.list_groups()
    wanted = label.strip()
    target = next(
        (
            g["resourceName"]
            for n, g in groups.items()
            if g.get("groupType") != gsync.SYSTEM_GROUP_TYPE and n.lower() == wanted.lower()
        ),
        None,
    ) or gc.ensure_group(wanted)
    current = gsync.relationship_label(live, groups)
    if current is not None and current != wanted.lower():
        old_rn = next(
            g["resourceName"]
            for n, g in groups.items()
            if g.get("groupType") != gsync.SYSTEM_GROUP_TYPE and n.lower() == current
        )
        gc.modify_group_members(old_rn, [], [person_rn])
    gc.modify_group_members(target, [person_rn], [])


def update(
    conn: Any,
    email: str,
    *,
    notes: str | None = None,
    relationship_label: str | None = None,
    contact: dict | None = None,
) -> dict:
    row = people.get(conn, email)
    if row is None:
        raise NotFound(email)
    rn = row.get("google_resource_name")
    if not rn:
        raise NotLinked(email)
    live = gc.get_person(rn)  # fresh etag + memberships: Google rejects a stale etag

    # Validate and check the email rule BEFORE any write, so a rejected
    # request changes nothing (design §5.4 step 3).
    fields: dict[str, Any] = {}
    if contact is not None:
        try:
            fields = contact_fields.validate(contact)
        except contact_fields.ValidationError as e:
            raise Invalid(str(e)) from e
        if "emailAddresses" in fields:
            try:
                contact_fields.check_email_addition(live, fields["emailAddresses"], row["email"])
            except contact_fields.EmailRuleError as e:
                raise Conflict(str(e)) from e
    if notes is not None:
        fields["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]

    try:
        try:
            if fields:
                gc.update_fields(rn, live.get("etag") or "", fields)
        except HttpError as e:
            status = e.resp.status
            message = e.reason or ""
            if status in (400, 412) and (
                "FAILED_PRECONDITION" in message or "etag" in message.lower()
            ):
                raise Conflict(message) from e
            if 400 <= status < 500:
                raise Invalid(message) from e
            raise
        if relationship_label is not None:
            _set_label(rn, live, relationship_label)
    finally:
        # Google may now hold a partial result; refresh the DB from it either way.
        try:
            row = gsync.sync_one(conn, row)
        except Exception:
            logger.warning("post-edit resync failed for %s", email, exc_info=True)
    return row
