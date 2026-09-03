"""PATCH /people/{email}: write to Google Contacts first (it is the truth),
then refresh the DB row from Google. Spec §9."""

from typing import Any

import clients.google_contacts as gc
from repo import people
from services import google_contacts_sync as gsync


class NotFound(Exception):
    pass


class NotLinked(Exception):
    pass


def _set_label(person_rn: str, label: str) -> None:
    groups = gc.list_groups()
    target = gc.ensure_group(label.strip().capitalize())
    current = gc.get_person(person_rn)
    keep = {gsync.group_name()}
    for m in current.get("memberships", []):
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if not rn or rn == target:
            continue
        name_kind = next(
            ((n, g.get("groupType")) for n, g in groups.items() if g["resourceName"] == rn), None
        )
        if name_kind is None or name_kind[1] == "SYSTEM_CONTACT_GROUP" or name_kind[0] in keep:
            continue
        gc.modify_group_members(rn, [], [person_rn])
    gc.modify_group_members(target, [person_rn], [])


def update(
    conn: Any, email: str, *, notes: str | None = None, relationship_label: str | None = None
) -> dict:
    row = people.get(conn, email)
    if row is None:
        raise NotFound(email)
    rn = row.get("google_resource_name")
    if not rn:
        raise NotLinked(email)
    if notes is not None:
        gc.update_biography(rn, row.get("google_etag") or "", notes)
    if relationship_label is not None:
        _set_label(rn, relationship_label)
    return gsync.sync_one(conn, row)
