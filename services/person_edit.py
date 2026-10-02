"""PATCH /people/{ident}: write to Google Contacts first (it is the truth),
then refresh the DB row from Google. Spec §9, contact fields design §5.4,
adopt-emailless-contacts design §5.3 (keyed by person_id, not email)."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from googleapiclient.errors import HttpError

import clients.google_contacts as gc
from repo import people
from services import contact_fields
from services import google_contacts_sync as gsync
from services import labels as label_rules

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


def _is_stale_etag(e: HttpError) -> bool:
    """A stale etag is Google's `error.status == "FAILED_PRECONDITION"`
    (HTTP 400) — confirmed against the live API, where the message text is
    NOT guaranteed to mention "etag" at all. HTTP 412 is the same condition.
    Same defensive-parse shape as `_is_expired_sync_token` in
    clients/google_contacts.py: a guarded parse of `e.content`, falling
    through rather than raising on malformed/empty content. The old
    message-substring check is kept only as a last-resort fallback."""
    if e.resp.status == 412:
        return True
    if e.resp.status != 400:
        return False
    try:
        error_status = json.loads(e.content)["error"].get("status")
    except (ValueError, KeyError, TypeError):
        error_status = None
    if error_status == "FAILED_PRECONDITION":
        return True
    return "etag" in (e.reason or "").lower()


@dataclass
class _LabelPlan:
    add: list[str] = field(default_factory=list)  # group rns to add the person to
    create: list[str] = field(default_factory=list)  # names with no group yet
    remove: list[str] = field(default_factory=list)  # group rns to remove the person from


def _plan_labels(live: dict, change: dict) -> _LabelPlan:
    """Resolve a labels change against Google's live groups before any write
    (multiple-labels design §5.1), so a rejected request changes nothing.
    Adding a held label and removing an unheld one are both no-ops."""
    try:
        add, remove = label_rules.normalize_change(
            change.get("add") or [], change.get("remove") or []
        )
        by_rn = gc.list_groups_by_rn()
        held = set(label_rules.membership_rns(live))
        plan = _LabelPlan()
        for name in add:
            rn = label_rules.resolve(by_rn, name, gsync.group_name())
            if rn is None:
                plan.create.append(name)
            elif rn not in held:
                plan.add.append(rn)
        for name in remove:
            rn = label_rules.resolve(by_rn, name, gsync.group_name())
            if rn is not None and rn in held:
                plan.remove.append(rn)
    except label_rules.AmbiguousLabel as e:
        raise Conflict(str(e)) from e
    except label_rules.LabelError as e:
        raise Invalid(str(e)) from e
    return plan


def _apply_labels(person_rn: str, plan: _LabelPlan) -> None:
    for name in plan.create:
        plan.add.append(gc.ensure_group(name))
    for g in plan.add:
        gc.modify_group_members(g, [person_rn], [])
    for g in plan.remove:
        gc.modify_group_members(g, [], [person_rn])


def update(
    conn: Any,
    person_id: int,
    *,
    notes: str | None = None,
    labels: dict | None = None,
    contact: dict | None = None,
) -> dict:
    row = people.get_by_id(conn, person_id)
    if row is None:
        raise NotFound(str(person_id))
    rn = row.get("google_resource_name")
    if not rn:
        raise NotLinked(str(person_id))
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
        if "phoneNumbers" in fields:
            try:
                contact_fields.check_phone_removal(row["email"], fields["phoneNumbers"])
            except contact_fields.IdentifierRuleError as e:
                raise Conflict(str(e)) from e
    if notes is not None:
        fields["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
    label_plan = _plan_labels(live, labels) if labels is not None else None

    try:
        try:
            if fields:
                gc.update_fields(rn, live.get("etag") or "", fields)
            if label_plan is not None:
                _apply_labels(rn, label_plan)
        except HttpError as e:
            if _is_stale_etag(e):
                raise Conflict(e.reason or "") from e
            status = e.resp.status
            if 400 <= status < 500:
                raise Invalid(e.reason or "") from e
            raise
    finally:
        # Google may now hold a partial result; refresh the DB from it either way.
        try:
            row = gsync.sync_one(conn, row)
        except Exception:
            logger.warning("post-edit resync failed for %s", person_id, exc_info=True)
    return row
