"""Google Contacts is the source of truth for identity (spec §4.3, §7).
ensure_contact links or creates a contact for a newly eligible person;
run_sync pulls Google's changes into the derived index by sync token."""

import logging
import os
from typing import Any

import clients.google_contacts as gc
import clients.otel as otel
from repo import people, sync_state

logger = logging.getLogger(__name__)

_SYSTEM = "SYSTEM_CONTACT_GROUP"


def group_name() -> str:
    return os.environ.get("GOOGLE_CONTACT_GROUP", "Inbox")


def primary_email(person: dict) -> str | None:
    for e in person.get("emailAddresses", []):
        v = (e.get("value") or "").strip().lower()
        if v:
            return v
    return None


def display_name(person: dict) -> str | None:
    for n in person.get("names", []):
        if n.get("displayName"):
            return n["displayName"]
    return None


def notes(person: dict) -> str | None:
    for b in person.get("biographies", []):
        if b.get("value"):
            return b["value"]
    return None


def relationship_label(person: dict, groups: dict[str, dict]) -> str | None:
    """First user-defined group the contact belongs to, lowercased, skipping
    system groups and people's own '<GOOGLE_CONTACT_GROUP>' group."""
    by_rn = {g["resourceName"]: (name, g.get("groupType")) for name, g in groups.items()}
    for m in person.get("memberships", []):
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if not rn or rn not in by_rn:
            continue
        name, kind = by_rn[rn]
        if kind == _SYSTEM or name == group_name():
            continue
        return name.lower()
    return None


def _link(conn: Any, email: str, person: dict, groups: dict[str, dict]) -> None:
    people.set_google(
        conn,
        email,
        resource_name=person["resourceName"],
        etag=person.get("etag"),
        display_name=display_name(person),
        notes=notes(person),
        relationship_label=relationship_label(person, groups),
    )


def ensure_contact(conn: Any, row: dict) -> dict:
    if row.get("google_resource_name") or row.get("google_deleted_at") or not row.get("eligible"):
        return row
    try:
        groups = gc.list_groups()
        found = gc.search_by_email(row["email"])
        if found:
            _link(conn, row["email"], found, groups)
        else:
            created = gc.create_contact(
                row.get("display_name"), row["email"], gc.ensure_group(group_name())
            )
            otel.google_contacts_created.add(1)
            _link(conn, row["email"], created, groups)
        return people.get(conn, row["email"]) or row
    except Exception:
        otel.external_errors.add(1, {"system": "google"})
        logger.warning("Google ensure_contact failed for %s", row["email"], exc_info=True)
        return row


def apply_person(conn: Any, person: dict, groups: dict[str, dict]) -> str | None:
    rn = person["resourceName"]
    linked = people.get_by_google_resource(conn, rn)
    if (person.get("metadata") or {}).get("deleted"):
        if linked:
            people.mark_google_deleted(conn, rn)
            return "deleted"
        return None
    label = relationship_label(person, groups)
    if linked:
        people.update_from_google(
            conn,
            rn,
            etag=person.get("etag"),
            display_name=display_name(person),
            notes=notes(person),
            relationship_label=label,
        )
        return "updated"
    email = primary_email(person)
    if not email:
        return None
    row = people.get(conn, email)
    if row and not row.get("google_resource_name") and not row.get("google_deleted_at"):
        _link(conn, email, person, groups)
        return "linked"
    if row is None:
        people.create_from_google(
            conn,
            email,
            display_name=display_name(person),
            resource_name=rn,
            etag=person.get("etag"),
            notes=notes(person),
            relationship_label=label,
        )
        return "created"
    return None


def run_sync(conn: Any) -> dict[str, int]:
    counts = {"updated": 0, "linked": 0, "created": 0, "deleted": 0}
    token = sync_state.get_token(conn)
    try:
        try:
            persons, next_token = gc.list_connections(token)
        except gc.SyncTokenExpired:
            logger.warning("Google sync token expired — full resync")
            persons, next_token = gc.list_connections(None)
        groups = gc.list_groups()
        for p in persons:
            kind = apply_person(conn, p, groups)
            if kind:
                counts[kind] += 1
                otel.google_sync_changes.add(1, {"kind": kind})
        sync_state.set_token(conn, next_token, "ok")
    except Exception as e:
        otel.external_errors.add(1, {"system": "google"})
        sync_state.set_token(conn, token, f"error: {type(e).__name__}")
        raise
    return counts


def sync_one(conn: Any, row: dict) -> dict:
    """Re-pull one linked person (after a PATCH or a manual edit)."""
    rn = row.get("google_resource_name")
    if not rn:
        return row
    apply_person(conn, gc.get_person(rn), gc.list_groups())
    return people.get(conn, row["email"]) or row
