"""Google Contacts is the source of truth for identity (spec §4.3, §7).
ensure_contact links or creates a contact for a newly eligible person;
run_sync pulls Google's changes into the derived index by sync token."""

import logging
import os
from typing import Any

import clients.google_contacts as gc
import clients.otel as otel
from repo import labels as labels_repo
from repo import people, sync_state
from services import contact_fields, labels

logger = logging.getLogger(__name__)


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


def refresh_groups(conn: Any) -> None:
    """Make contact_groups match Google's labels (multiple-labels design §4.2).
    Runs before apply_person, so a person's memberships can reference them."""
    labels_repo.replace_groups(conn, labels.user_groups(gc.list_groups_by_rn(), group_name()))


def _write_labels(conn: Any, person: dict) -> None:
    labels_repo.set_contact_labels(conn, person["resourceName"], labels.membership_rns(person))


def _claim_url(conn: Any, derived: dict, own_id: int | None) -> bool:
    """Drop a profile URL that a different row already holds (linkedin-identity
    design §4): writing it would violate people_linkedin_url_key and abort the whole
    sync. `own_id` is the row being written, which may already hold its own URL.
    Returns True when a URL was dropped."""
    url = derived["linkedin_url"]
    if url is None:
        return False
    owner = people.linkedin_owner(conn, url)
    if owner is None or owner == own_id:
        return False
    derived["linkedin_url"] = None
    return True


def _link(conn: Any, row: dict, person: dict) -> None:
    derived = contact_fields.derive(person)
    _claim_url(conn, derived, row.get("id"))
    people.set_google(
        conn,
        row["email"],
        resource_name=person["resourceName"],
        etag=person.get("etag"),
        display_name=display_name(person),
        notes=notes(person),
        **derived,
    )
    _write_labels(conn, person)


def ensure_contact(conn: Any, row: dict) -> dict:
    if row.get("google_resource_name") or row.get("google_deleted_at") or not row.get("eligible"):
        return row
    try:
        groups = gc.list_groups()
        person = gc.search_by_email(row["email"])
        if person is None:
            target = (groups.get(group_name()) or {}).get("resourceName") or gc.ensure_group(
                group_name()
            )
            person = gc.create_contact(row.get("display_name"), row["email"], target)
            otel.google_contacts_created.add(1)
    except Exception:
        otel.external_errors.add(1, {"system": "google"})
        logger.warning("Google ensure_contact failed for %s", row["email"], exc_info=True)
        return row
    _link(conn, row, person)  # DB write — a failure here propagates
    return people.get(conn, row["email"]) or row


def apply_person(conn: Any, person: dict) -> str | None:
    rn = person["resourceName"]
    linked = people.get_by_google_resource(conn, rn)
    if (person.get("metadata") or {}).get("deleted"):
        if linked:
            # Clear first: set_contact_labels finds the row by
            # google_resource_name, which mark_google_deleted NULLs.
            labels_repo.set_contact_labels(conn, rn, [])
            people.mark_google_deleted(conn, rn)
            return "deleted"
        return None
    derived = contact_fields.derive(person)
    if linked:
        _claim_url(conn, derived, linked.get("id"))
        if (
            linked.get("email") is None
            and not derived["phone_numbers"]
            and not derived["linkedin_url"]
        ):
            # An email-less row's phones and LinkedIn URL are its only identifiers —
            # never overwrite them with nothing (people_has_an_identifier).
            return "skipped"
        promote = None
        if linked.get("email") is None:
            # A row that already has an email is never touched here — Google
            # is the truth for most fields, but silently changing a person's
            # identity key on a nightly path is not something to do by
            # accident (spec §6.2).
            candidate = primary_email(person)
            if candidate and people.email_owner(conn, candidate) is None:
                # Only promote an unclaimed address: writing a claimed one
                # violates people_email_key and aborts the entire sync.
                promote = candidate
        people.update_from_google(
            conn,
            rn,
            etag=person.get("etag"),
            display_name=display_name(person),
            notes=notes(person),
            email=promote,
            **derived,
        )
        _write_labels(conn, person)
        return "promoted" if promote else "updated"
    email = primary_email(person)
    if not email:
        # Adopt a contact that has no email address, provided it has a phone we
        # can normalize or a LinkedIn profile URL no other row holds —
        # people_has_an_identifier requires one of the three (linkedin-identity
        # design §3). Contacts with none are counted, never written.
        dropped = _claim_url(conn, derived, None)
        if not derived["phone_numbers"] and not derived["linkedin_url"]:
            return "linkedin_conflict" if dropped else "skipped"
        people.create_from_google(
            conn,
            None,
            display_name=display_name(person),
            resource_name=rn,
            etag=person.get("etag"),
            notes=notes(person),
            **derived,
        )
        _write_labels(conn, person)
        return "created"
    row = people.get(conn, email)
    if row and not row.get("google_resource_name") and not row.get("google_deleted_at"):
        _link(conn, row, person)
        return "linked"
    if row is None:
        _claim_url(conn, derived, None)
        people.create_from_google(
            conn,
            email,
            display_name=display_name(person),
            resource_name=rn,
            etag=person.get("etag"),
            notes=notes(person),
            **derived,
        )
        _write_labels(conn, person)
        return "created"
    return None


def run_sync(conn: Any) -> dict[str, int]:
    counts = {
        "updated": 0,
        "linked": 0,
        "created": 0,
        "deleted": 0,
        "skipped": 0,
        "promoted": 0,
        "linkedin_conflict": 0,
    }
    token = sync_state.get_token(conn)
    try:
        try:
            persons, next_token = gc.list_connections(token)
        except gc.SyncTokenExpired:
            logger.warning("Google sync token expired — full resync")
            persons, next_token = gc.list_connections(None)
        refresh_groups(conn)
        for p in persons:
            kind = apply_person(conn, p)
            if kind:
                counts[kind] += 1
                otel.google_sync_changes.add(1, {"kind": kind})
        sync_state.set_token(conn, next_token, "ok")
    except Exception as e:
        otel.external_errors.add(1, {"system": "google"})
        try:
            sync_state.set_token(conn, token, f"error: {type(e).__name__}")
            # Controller ruling: commit the error-status row explicitly here.
            # The surrounding transaction is about to be rolled back by
            # design (we're re-raising), which would otherwise discard this
            # diagnostic write along with everything else.
            conn.commit()
        except Exception:
            logger.warning("could not record sync error status", exc_info=True)
        raise
    return counts


def sync_one(conn: Any, row: dict) -> dict:
    """Re-pull one linked person (after a PATCH or a manual edit)."""
    rn = row.get("google_resource_name")
    if not rn:
        return row
    refresh_groups(conn)
    apply_person(conn, gc.get_person(rn))
    return people.get_by_id(conn, row["id"]) or row
