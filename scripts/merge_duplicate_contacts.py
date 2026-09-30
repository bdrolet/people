#!/usr/bin/env python3
# scripts/merge_duplicate_contacts.py
"""Merge duplicate Google contacts — the ones that share a phone number and so
produce several `people` rows for one person.

  python scripts/merge_duplicate_contacts.py            # dry run (the default)
  python scripts/merge_duplicate_contacts.py --apply    # actually merge

Duplicates arose because the nightly sync adopted each copy of a contact
separately, so one person ends up with two Google contacts and two `people`
rows sharing a phone number.

What a merge does, per set:

  1. Pick a survivor — the most populated copy, lowest resourceName on a tie.
  2. Union the multi-valued fields (emails, phones, addresses, urls, ...) onto
     the survivor, so nothing the losers held is lost. Contact-group membership
     moves through modify_group_members rather than a raw `memberships` write,
     which would risk dropping the contact out of `myContacts` altogether; that
     is what preserves the relationship label. Notes (`biographies`) are filled
     in only when the survivor has none.
  3. Delete the loser contacts in Google.
  4. Re-point `imessage_handles` and `linkedin_connections` at the survivor's
     `people` row, **then** delete the loser rows. Those FKs are ON DELETE SET
     NULL, so deleting first would silently drop the links instead of moving
     them. An iMessage handle's own `google_resource_name` match is re-pointed
     too — it is independent of `person_id`, so it would otherwise be left
     naming a contact that no longer exists.

It skips and reports rather than guessing: a set whose copies carry different
birthdays or different notes, and a set whose `people` rows carry more than one distinct email
address (two different people who happen to share a number, or a genuine merge
that needs the mail history decided by hand).

Safety: `--dry-run` is the default and `--apply` is required for any write. In
apply mode every contact about to be deleted is written to a JSON backup file
**before** the first delete, so a bad merge is restorable. Each set is its own
transaction. Output is counts and resourceNames only — never names, numbers,
addresses, or notes — because this repo is public.
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients import google_contacts as gc
from repo import imessage as imessage_repo
from repo import linkedin as linkedin_repo
from repo import people as people_repo
from repo import whatsapp as whatsapp_repo
from services.contact_fields import WRITABLE_FIELDS
from services.imessage_export import normalize_handle

# Fields where a difference between copies means "two people", not "one person
# recorded twice" — a set with one of these is reported, never merged. Each is
# compared on the part a human entered: a birthday carries a `text` rendering
# of its own date ("10/23/1989" vs "1989-10-23"), and comparing that instead of
# the date would report a clash between two contacts that agree.
CLASH_KEYS: dict[str, Callable[[dict], Any]] = {
    "birthdays": lambda v: v.get("date"),
    "biographies": lambda v: (v.get("value") or "").strip(),
}

# Single-valued in practice: the survivor's wins, and a loser's is only taken
# when the survivor has none at all. `biographies` is the contact's notes —
# one visible field in the Google UI, so a union would be two competing notes.
SINGLE_VALUED = frozenset({"birthdays", "genders", "names", "biographies"})

# `memberships` is deliberately not merged as a field: it carries the
# `myContacts` system membership, and a wrong write there removes the contact
# from the address book entirely. Contact groups move through
# `modify_group_members` instead — see _candidate_groups.
MERGE_FIELDS = sorted(WRITABLE_FIELDS - SINGLE_VALUED - {"memberships"})

# Keys Google derives from the others; they differ between copies without the
# copies differing, so they must not count when deduping values.
_DERIVED_KEYS = frozenset({"metadata", "formattedType"})


# --- pure helpers ---------------------------------------------------------


def _content(value: Any) -> Any:
    """A value stripped to the parts a human entered, for equality testing."""
    if isinstance(value, dict):
        return {k: _content(v) for k, v in sorted(value.items()) if k not in _DERIVED_KEYS}
    if isinstance(value, list):
        return [_content(v) for v in value]
    return value


def _key(field: str, value: dict) -> str:
    """The identity of one value within a field, for deduping the union. Phones
    and emails get semantic keys so "(555) 010-0001" and "+15550100001", or two
    casings of an address, collapse instead of both landing on the survivor."""
    if field == "phoneNumbers":
        raw = value.get("value", "")
        return normalize_handle(raw) or raw.strip()
    if field == "emailAddresses":
        return value.get("value", "").strip().lower()
    return json.dumps(_content(value), sort_keys=True)


def _writable(value: dict) -> dict:
    """A field value with Google's server-owned `metadata` removed. Copying a
    value from one contact to another carries that contact's
    `metadata.source.id`, and Google then silently drops the value from the
    write — no error, no field. Verified against the live API."""
    return {k: v for k, v in value.items() if k != "metadata"}


def _phones(person: dict) -> list[str]:
    out = []
    for number in person.get("phoneNumbers", []):
        e164 = normalize_handle(number.get("value", ""))
        if e164:
            out.append(e164)
    return out


def _populated(person: dict) -> int:
    """How much contact data a copy carries — the survivor is the richest.
    Group membership is not contact data, and counting it would hand the merge
    to whichever copy happens to sit in more groups."""
    return sum(len(person.get(f) or []) for f in WRITABLE_FIELDS - {"memberships"})


def find_duplicate_sets(contacts: list[dict]) -> dict[str, list[dict]]:
    """Group contacts into sets that share at least one phone number.

    Sets are transitive: if A and B share one number and B and C share another,
    all three are one set — otherwise merging the first pair would leave a
    duplicate behind. Each set is keyed by its lowest shared number, and only
    sets of two or more distinct contacts are returned."""
    by_phone: dict[str, list[str]] = {}
    by_rn: dict[str, dict] = {}
    for person in contacts:
        if (person.get("metadata") or {}).get("deleted"):
            continue
        rn = person["resourceName"]
        by_rn[rn] = person
        for e164 in _phones(person):
            if rn not in by_phone.setdefault(e164, []):
                by_phone[e164].append(rn)

    # Union-find over resourceNames, joined by every shared number.
    parent: dict[str, str] = {rn: rn for rn in by_rn}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for members in by_phone.values():
        for other in members[1:]:
            parent[find(other)] = find(members[0])

    components: dict[str, list[str]] = {}
    for rn in by_rn:
        components.setdefault(find(rn), []).append(rn)

    out: dict[str, list[dict]] = {}
    for root, members in components.items():
        if len(members) < 2:
            continue
        shared = sorted(p for p, rns in by_phone.items() if find(rns[0]) == root)
        out[shared[0]] = [by_rn[rn] for rn in sorted(members)]
    return dict(sorted(out.items()))


def choose_survivor(copies: list[dict]) -> dict:
    """The most populated copy; the lowest resourceName settles a tie, so the
    choice does not depend on the order Google happened to list them in."""
    return min(copies, key=lambda p: (-_populated(p), p["resourceName"]))


def union_fields(copies: list[dict], survivor: dict) -> dict:
    """The field values to write onto the survivor so nothing is lost when the
    other copies are deleted. Returns only fields that actually change."""
    fields: dict[str, list] = {}
    for field in MERGE_FIELDS:
        merged: dict[str, dict] = {}
        for person in [survivor, *(c for c in copies if c is not survivor)]:
            for value in person.get(field) or []:
                merged.setdefault(_key(field, value), _writable(value))
        if not merged:
            continue
        values = list(merged.values())
        if _content(values) != _content(survivor.get(field) or []):
            fields[field] = values

    # Single-valued fields: only fill a gap, never overwrite the survivor's.
    for field in sorted(SINGLE_VALUED):
        if survivor.get(field):
            continue
        for person in copies:
            if person.get(field):
                fields[field] = [_writable(v) for v in person[field]]
                break
    return fields


def single_valued_clash(copies: list[dict]) -> list[str]:
    """Fields where the copies disagree in a way that means these may not be
    the same person. Such a set is reported and left alone."""
    out = []
    for field, key in CLASH_KEYS.items():
        distinct = {
            json.dumps([key(v) for v in p[field]], sort_keys=True) for p in copies if p.get(field)
        }
        if len(distinct) > 1:
            out.append(field)
    return out


def _candidate_groups(copies: list[dict], survivor: dict) -> set[str]:
    """Contact groups a loser belongs to and the survivor does not. Returned as
    raw resourceNames; the caller drops the system ones, which every contact
    carries and which must never be written by hand."""

    def group_rns(person: dict) -> set[str]:
        return {
            rn
            for m in person.get("memberships", [])
            if (rn := (m.get("contactGroupMembership") or {}).get("contactGroupResourceName"))
        }

    losers = [c for c in copies if c is not survivor]
    return set().union(*(group_rns(c) for c in losers), set()) - group_rns(survivor)


# --- the run --------------------------------------------------------------


def _people_rows(conn: Any, copies: list[dict]) -> list[dict]:
    """Every `people` row reachable from this set's phone numbers."""
    rows: dict[int, dict] = {}
    for person in copies:
        for e164 in _phones(person):
            for row in people_repo.get_by_phone(conn, e164):
                rows[row["id"]] = row
    return list(rows.values())


def _keeper_and_losers(
    conn: Any, survivor: dict, loser_rns: list[str]
) -> tuple[dict | None, list[dict], bool]:
    """The `people` row that survives this set, the rows to fold into it, and
    whether the keeper still has to be re-linked to the surviving contact.

    Usually the keeper is the survivor contact's own row. Sometimes the
    survivor contact has no row at all — it was never adopted, while a duplicate
    of it was — and then the best of the losers' rows is promoted and re-linked.
    Skipping the local side in that case would leave rows pointing at a contact
    this script just deleted, which is the mess it exists to clean up. A row
    with an email is preferred, since that address is how the rest of the
    system addresses the person; the lowest id settles the rest."""
    keeper = people_repo.get_by_google_resource(conn, survivor["resourceName"])
    rows = [r for r in (people_repo.get_by_google_resource(conn, rn) for rn in loser_rns) if r]
    if keeper is not None:
        return keeper, [r for r in rows if r["id"] != keeper["id"]], False
    if not rows:
        return None, [], False
    rows.sort(key=lambda r: (r["email"] is None, r["id"]))
    return rows[0], rows[1:], True


def _preview_rows(conn: Any, survivor: dict, loser_rns: list[str]) -> dict[str, int]:
    """What _collapse_rows would do, counted without writing — so a dry run
    reports the local damage as well as the Google-side one."""
    counts = {"rows_deleted": 0, "links_repointed": 0, "rows_relinked": 0}
    keeper, losers, relink = _keeper_and_losers(conn, survivor, loser_rns)
    if keeper is None:
        return counts
    counts["rows_relinked"] = int(relink)
    for row in losers:
        counts["rows_deleted"] += 1
        summary = imessage_repo.summary_for_person(conn, row["id"])
        counts["links_repointed"] += len(summary["handles"] or []) if summary else 0
        if linkedin_repo.connection_for_person(conn, row["id"]):
            counts["links_repointed"] += 1
        # Approximate, deliberately: repoint_person moves handle rows plus one
        # member row per shared chat, and shared_groups counts only the active
        # memberships. A dry run reports the scale, not an exact rowcount.
        wa = whatsapp_repo.summary_for_person(conn, row["id"])
        if wa:
            counts["links_repointed"] += len(wa["handles"] or []) + (wa["shared_groups"] or 0)
    return counts


def _collapse_rows(conn: Any, survivor: dict, loser_rns: list[str]) -> dict[str, int]:
    """Fold the losers' `people` rows into the keeper's. Links move first: the
    FKs are ON DELETE SET NULL, so deleting the row first would drop them."""
    counts = {"rows_deleted": 0, "links_repointed": 0, "rows_relinked": 0}
    keeper, losers, relink = _keeper_and_losers(conn, survivor, loser_rns)
    if keeper is None:
        return counts
    if relink:
        people_repo.relink_google(
            conn, keeper["id"], resource_name=survivor["resourceName"], etag=survivor.get("etag")
        )
        counts["rows_relinked"] = 1
    for row in losers:
        counts["links_repointed"] += imessage_repo.repoint_person(conn, row["id"], keeper["id"])
        counts["links_repointed"] += linkedin_repo.repoint_person(conn, row["id"], keeper["id"])
        # whatsapp_handles.person_id and whatsapp_chat_members.person_id are both
        # ON DELETE SET NULL too (WhatsApp spec §4, §11 step 5). The next import
        # would re-derive most of them, but moving them here closes the window in
        # which people-api reports no WhatsApp activity for the survivor.
        counts["links_repointed"] += whatsapp_repo.repoint_person(conn, row["id"], keeper["id"])
        people_repo.delete(conn, row["id"])
        counts["rows_deleted"] += 1
    return counts


def run(
    get_conn: Callable, *, apply: bool, backup_path: Path, only: list[str] | None = None
) -> dict:
    """Find the duplicate sets, and — with apply — merge the ones that are
    unambiguous. Returns counts; prints a per-set line naming resourceNames.

    `only` merges one explicit set instead — survivor first, then the contacts
    to fold into it. That is a decision a person has made, so the skip guards
    do not apply to it; everything else, including the backup, still does."""
    if only:
        contacts = [gc.get_person(rn) for rn in only]
        sets = {"explicit": contacts}
    else:
        contacts, _ = gc.list_connections(None)
        sets = find_duplicate_sets(contacts)

    result = {
        "contacts": len(contacts),
        "sets": len(sets),
        "mergeable": 0,
        "skipped": 0,
        "merged": 0,
        "contacts_deleted": 0,
        "rows_deleted": 0,
        "links_repointed": 0,
        "rows_relinked": 0,
        "groups_moved": 0,
        "handles_relinked": 0,
    }
    plan: list[tuple[dict, list[dict], list[str]]] = []
    group_types: dict[str, str] | None = None

    with get_conn() as conn:
        for copies in sets.values():
            survivor = copies[0] if only else choose_survivor(copies)
            losers = [c for c in copies if c is not survivor]

            clash = [] if only else single_valued_clash(copies)
            if clash:
                result["skipped"] += 1
                print(
                    f"skip  {survivor['resourceName']} +{len(losers)}: differing {', '.join(clash)}"
                )
                continue

            emails = (
                set()
                if only
                else {r["email"] for r in _people_rows(conn, copies) if r.get("email")}
            )
            if len(emails) > 1:
                result["skipped"] += 1
                print(
                    f"skip  {survivor['resourceName']} +{len(losers)}: "
                    f"{len(emails)} people rows with distinct email addresses"
                )
                continue

            gained: list[str] = []
            candidates = _candidate_groups(copies, survivor)
            if candidates:
                if group_types is None:
                    group_types = {
                        g["resourceName"]: g["groupType"] for g in gc.list_groups().values()
                    }
                gained = sorted(g for g in candidates if group_types.get(g) == "USER_CONTACT_GROUP")

            result["mergeable"] += 1
            result["contacts_deleted"] += len(losers)
            result["groups_moved"] += len(gained)
            plan.append((survivor, losers, gained))
            print(
                f"merge {survivor['resourceName']} <- "
                f"{', '.join(c['resourceName'] for c in losers)}"
            )

        if not apply:
            for survivor, losers, _ in plan:
                counts = _preview_rows(conn, survivor, [c["resourceName"] for c in losers])
                for k in ("rows_deleted", "links_repointed", "rows_relinked"):
                    result[k] += counts[k]
            print(
                f"\nDRY RUN — nothing written. {result['sets']} duplicate sets over "
                f"{result['contacts']:,} contacts: {result['mergeable']} mergeable, "
                f"{result['skipped']} skipped.\n"
                f"Applying would delete {result['contacts_deleted']} Google contacts and "
                f"{result['rows_deleted']} people rows, re-pointing "
                f"{result['links_repointed']} iMessage/LinkedIn/WhatsApp links, re-linking "
                f"{result['rows_relinked']} rows onto a surviving contact, and moving "
                f"{result['groups_moved']} contact-group memberships."
            )
            return result

        # Every contact about to be deleted, on disk before the first delete.
        backup_path.write_text(json.dumps([c for _, losers, _ in plan for c in losers], indent=2))
        print(f"\nbackup written: {backup_path}")

        for survivor, losers, gained in plan:
            rn = survivor["resourceName"]
            fields = union_fields([survivor, *losers], survivor)
            if fields:
                gc.update_fields(rn, survivor["etag"], fields)
            for group in gained:
                gc.modify_group_members(group, [rn], [])
            for loser in losers:
                gc.delete_person(loser["resourceName"])
                result["handles_relinked"] += imessage_repo.repoint_google_resource(
                    conn, loser["resourceName"], rn
                )
            counts = _collapse_rows(conn, survivor, [c["resourceName"] for c in losers])
            for k in ("rows_deleted", "links_repointed", "rows_relinked"):
                result[k] += counts[k]
            result["merged"] += 1
            conn.commit()

    print(
        f"\nmerged {result['merged']} sets: {result['contacts_deleted']} contacts deleted, "
        f"{result['rows_deleted']} people rows deleted, "
        f"{result['links_repointed']} links re-pointed, "
        f"{result['rows_relinked']} rows re-linked, "
        f"{result['groups_moved']} group memberships moved, "
        f"{result['handles_relinked']} iMessage handle matches re-pointed, "
        f"{result['skipped']} sets skipped."
    )
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="actually merge; default is a dry run")
    p.add_argument("--backup", type=Path, help="where to write the pre-delete JSON backup")
    p.add_argument(
        "--merge",
        help="merge one explicit set instead of the detected ones: comma-separated "
        "resourceNames, survivor first. Bypasses the skip guards, for a set you have "
        "decided by hand.",
    )
    args = p.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = args.backup or Path(f"contact-merge-backup-{stamp}.json")

    from clients.db import get_conn

    only = [rn.strip() for rn in args.merge.split(",")] if args.merge else None
    if only and len(only) < 2:
        sys.exit("--merge needs a survivor and at least one contact to merge into it")
    run(get_conn, apply=args.apply, backup_path=backup, only=only)


if __name__ == "__main__":
    main()
