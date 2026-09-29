"""Tests for scripts/merge_duplicate_contacts.py.

Everything here runs against fakes: no Google API, no database. The script
deletes real contacts when run with --apply, so the ordering guarantees
(backup before delete, re-point before row delete) are pinned explicitly.
"""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "merge_duplicate_contacts", Path("scripts/merge_duplicate_contacts.py")
)
mdc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mdc)


def contact(rn, *, phones=(), emails=(), birthday=None, name=None, addresses=()):
    p = {"resourceName": rn, "etag": f"etag-{rn}"}
    if name:
        p["names"] = [{"displayName": name}]
    if phones:
        p["phoneNumbers"] = [{"value": v} for v in phones]
    if emails:
        p["emailAddresses"] = [{"value": v} for v in emails]
    if addresses:
        p["addresses"] = [{"formattedValue": a} for a in addresses]
    if birthday:
        p["birthdays"] = [{"date": birthday}]
    return p


# --- pure helpers ---------------------------------------------------------


def test_groups_only_sets_with_two_distinct_contacts():
    a = contact("people/a", phones=["+15550100001"])
    b = contact("people/b", phones=["+15550100001"])
    lone = contact("people/c", phones=["+15550100002"])
    sets = mdc.find_duplicate_sets([a, b, lone])
    assert list(sets) == ["+15550100001"]
    assert {c["resourceName"] for c in sets["+15550100001"]} == {"people/a", "people/b"}


def test_grouping_ignores_deleted_contacts_and_unusable_phones():
    live = contact("people/a", phones=["+15550100001"])
    gone = {**contact("people/b", phones=["+15550100001"]), "metadata": {"deleted": True}}
    shortcode = contact("people/c", phones=["262966"])
    assert mdc.find_duplicate_sets([live, gone, shortcode]) == {}


def test_survivor_is_the_most_populated_contact():
    thin = contact("people/a", phones=["+15550100001"])
    rich = contact("people/b", phones=["+15550100001"], emails=["a@example.com"], name="A")
    assert mdc.choose_survivor([thin, rich])["resourceName"] == "people/b"


def test_survivor_choice_is_independent_of_input_order():
    x = contact("people/zzz", phones=["+15550100001"], emails=["a@example.com"])
    y = contact("people/aaa", phones=["+15550100001"], emails=["b@example.com"])
    # Equally populated: the lowest resourceName wins, whichever order they arrive in.
    assert mdc.choose_survivor([x, y])["resourceName"] == "people/aaa"
    assert mdc.choose_survivor([y, x])["resourceName"] == "people/aaa"


def test_union_merges_multi_valued_fields_and_dedupes_by_content():
    survivor = contact("people/a", phones=["+15550100001"], emails=["a@example.com"])
    other = contact("people/b", phones=["+15550100001"], emails=["a@example.com", "b@example.com"])
    fields = mdc.union_fields([survivor, other], survivor)
    values = sorted(e["value"] for e in fields["emailAddresses"])
    assert values == ["a@example.com", "b@example.com"]


def test_union_ignores_google_metadata_when_deduping():
    survivor = contact("people/a", emails=["a@example.com"])
    survivor["emailAddresses"][0]["metadata"] = {"primary": True}
    other = contact("people/b", emails=["a@example.com"])
    fields = mdc.union_fields([survivor, other], survivor)
    assert "emailAddresses" not in fields  # same content, nothing to write


def test_union_returns_nothing_when_the_survivor_already_has_everything():
    survivor = contact("people/a", emails=["a@example.com"], addresses=["1 Example St"])
    other = contact("people/b", emails=["a@example.com"])
    assert mdc.union_fields([survivor, other], survivor) == {}


def test_detects_a_single_valued_clash():
    a = contact("people/a", birthday={"month": 4, "day": 2})
    b = contact("people/b", birthday={"month": 7, "day": 9})
    assert mdc.single_valued_clash([a, b]) == ["birthdays"]


def test_identical_single_valued_data_is_not_a_clash():
    a = contact("people/a", birthday={"month": 4, "day": 2})
    b = contact("people/b", birthday={"month": 4, "day": 2})
    assert mdc.single_valued_clash([a, b]) == []


# --- orchestration --------------------------------------------------------


class FakeGoogle:
    def __init__(self, contacts):
        self.contacts = contacts
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.backup_seen_before_delete: list[bool] = []
        self.backup_path = None

    def list_connections(self, token):
        return list(self.contacts), "tok"

    def get_person(self, rn):
        return next(c for c in self.contacts if c["resourceName"] == rn)

    def update_fields(self, rn, etag, fields):
        self.updated.append((rn, fields))
        return {}

    def delete_person(self, rn):
        # Pinning the safety order: the backup must already exist on disk.
        self.backup_seen_before_delete.append(
            self.backup_path is not None and self.backup_path.exists()
        )
        self.deleted.append(rn)


class FakeConn:
    def __init__(self, rows_by_phone=None, rows_by_rn=None):
        self.rows_by_phone = rows_by_phone or {}
        self.rows_by_rn = rows_by_rn or {}
        self.calls: list[str] = []
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.calls.append("rollback")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """One mergeable set: people/a (survivor) and people/b, sharing a phone."""
    a = contact("people/a", phones=["+15550100001"], emails=["a@example.com"], name="A")
    b = contact("people/b", phones=["+15550100001"], emails=["b@example.com"])
    google = FakeGoogle([a, b])
    conn = FakeConn()
    state = {"repointed": [], "deleted_rows": [], "order": [], "handles": []}

    monkeypatch.setattr(mdc.gc, "list_connections", google.list_connections)
    monkeypatch.setattr(mdc.gc, "get_person", google.get_person)
    monkeypatch.setattr(mdc.gc, "update_fields", google.update_fields)
    monkeypatch.setattr(mdc.gc, "delete_person", google.delete_person)

    monkeypatch.setattr(
        mdc.people_repo,
        "get_by_phone",
        lambda c, ph: [
            {"id": 1, "email": None, "google_resource_name": "people/a"},
            {"id": 2, "email": None, "google_resource_name": "people/b"},
        ],
    )
    monkeypatch.setattr(
        mdc.people_repo,
        "get_by_google_resource",
        lambda c, rn: {"id": 1 if rn == "people/a" else 2, "google_resource_name": rn},
    )

    def repoint_im(c, frm, to):
        state["repointed"].append((frm, to))
        state["order"].append("repoint")
        return 1

    def delete_row(c, pid):
        state["deleted_rows"].append(pid)
        state["order"].append("delete_row")

    monkeypatch.setattr(
        mdc.imessage_repo, "summary_for_person", lambda c, pid: {"handles": ["+15550100001"]}
    )
    monkeypatch.setattr(mdc.linkedin_repo, "connection_for_person", lambda c, pid: None)
    monkeypatch.setattr(
        mdc.imessage_repo,
        "repoint_google_resource",
        lambda c, frm, to: state["handles"].append((frm, to)) or 1,
    )
    monkeypatch.setattr(mdc.imessage_repo, "repoint_person", repoint_im)
    monkeypatch.setattr(mdc.linkedin_repo, "repoint_person", lambda c, frm, to: 0)
    monkeypatch.setattr(mdc.people_repo, "delete", delete_row)

    google.backup_path = tmp_path / "backup.json"
    return {"google": google, "conn": conn, "state": state, "backup": google.backup_path}


def test_dry_run_writes_nothing_anywhere(wired):
    result = mdc.run(lambda: wired["conn"], apply=False, backup_path=wired["backup"])
    assert result["mergeable"] == 1
    assert wired["google"].updated == [] and wired["google"].deleted == []
    assert wired["state"]["repointed"] == [] and wired["state"]["deleted_rows"] == []
    assert not wired["backup"].exists()
    assert wired["conn"].commits == 0
    # A dry run still reports what applying would cost, locally as well as in Google.
    assert result["contacts_deleted"] == 1
    assert result["rows_deleted"] == 1
    assert result["links_repointed"] == 1


def test_apply_merges_google_then_collapses_the_row(wired):
    result = mdc.run(lambda: wired["conn"], apply=True, backup_path=wired["backup"])
    assert result["merged"] == 1
    assert wired["google"].updated[0][0] == "people/a"  # survivor updated
    assert wired["google"].deleted == ["people/b"]  # loser deleted
    assert wired["state"]["repointed"] == [(2, 1)]  # links moved to survivor
    assert wired["state"]["deleted_rows"] == [2]


def test_backup_is_written_before_any_delete(wired):
    mdc.run(lambda: wired["conn"], apply=True, backup_path=wired["backup"])
    assert wired["google"].backup_seen_before_delete == [True]
    saved = json.loads(wired["backup"].read_text())
    assert [c["resourceName"] for c in saved] == ["people/b"]


def test_links_are_repointed_before_the_row_is_deleted(wired):
    # ON DELETE SET NULL means the reverse order silently drops the links.
    mdc.run(lambda: wired["conn"], apply=True, backup_path=wired["backup"])
    assert wired["state"]["order"] == ["repoint", "delete_row"]


def test_a_birthday_clash_is_skipped_with_no_writes(monkeypatch, tmp_path):
    a = contact("people/a", phones=["+15550100001"], birthday={"month": 4, "day": 2})
    b = contact("people/b", phones=["+15550100001"], birthday={"month": 7, "day": 9})
    google = FakeGoogle([a, b])
    monkeypatch.setattr(mdc.imessage_repo, "repoint_google_resource", lambda c, frm, to: 0)
    monkeypatch.setattr(mdc.gc, "list_connections", google.list_connections)
    monkeypatch.setattr(mdc.gc, "update_fields", google.update_fields)
    monkeypatch.setattr(mdc.gc, "delete_person", google.delete_person)
    monkeypatch.setattr(mdc.people_repo, "get_by_phone", lambda c, ph: [])
    result = mdc.run(lambda: FakeConn(), apply=True, backup_path=tmp_path / "b.json")
    assert result["skipped"] == 1 and result["merged"] == 0
    assert google.updated == [] and google.deleted == []


def test_two_rows_with_distinct_emails_are_skipped(monkeypatch, tmp_path):
    a = contact("people/a", phones=["+15550100001"])
    b = contact("people/b", phones=["+15550100001"])
    google = FakeGoogle([a, b])
    monkeypatch.setattr(mdc.imessage_repo, "repoint_google_resource", lambda c, frm, to: 0)
    monkeypatch.setattr(mdc.gc, "list_connections", google.list_connections)
    monkeypatch.setattr(mdc.gc, "update_fields", google.update_fields)
    monkeypatch.setattr(mdc.gc, "delete_person", google.delete_person)
    monkeypatch.setattr(
        mdc.people_repo,
        "get_by_phone",
        lambda c, ph: [
            {"id": 1, "email": "one@example.com"},
            {"id": 2, "email": "two@example.com"},
        ],
    )
    result = mdc.run(lambda: FakeConn(), apply=True, backup_path=tmp_path / "b.json")
    assert result["skipped"] == 1 and result["merged"] == 0
    assert google.deleted == []


def test_a_loser_contact_with_no_people_row_does_not_error(wired, monkeypatch):
    monkeypatch.setattr(
        mdc.people_repo,
        "get_by_google_resource",
        lambda c, rn: {"id": 1, "google_resource_name": rn} if rn == "people/a" else None,
    )
    result = mdc.run(lambda: wired["conn"], apply=True, backup_path=wired["backup"])
    assert result["merged"] == 1
    assert wired["state"]["deleted_rows"] == []  # nothing to collapse locally
    assert wired["google"].deleted == ["people/b"]  # Google side still merged


def test_output_carries_no_personal_data(wired, capsys):
    mdc.run(lambda: wired["conn"], apply=False, backup_path=wired["backup"])
    out = capsys.readouterr().out
    assert "+15550100001" not in out
    assert "a@example.com" not in out
    assert "people/a" in out  # resource names are fine


def _wire_google(monkeypatch, google):
    monkeypatch.setattr(mdc.imessage_repo, "repoint_google_resource", lambda c, frm, to: 0)
    monkeypatch.setattr(mdc.gc, "list_connections", google.list_connections)
    monkeypatch.setattr(mdc.gc, "update_fields", google.update_fields)
    monkeypatch.setattr(mdc.gc, "delete_person", google.delete_person)


def test_a_surviving_contact_with_no_row_adopts_the_best_loser_row(monkeypatch, tmp_path):
    # people/a was never adopted, but its duplicates were. Deleting them without
    # re-linking would leave rows pointing at a contact that no longer exists.
    a = contact("people/a", phones=["+15550100001"], emails=["a@example.com"], name="A")
    b = contact("people/b", phones=["+15550100001"])
    c = contact("people/c", phones=["+15550100001"])
    google = FakeGoogle([a, b, c])
    google.backup_path = tmp_path / "backup.json"
    _wire_google(monkeypatch, google)

    rows = {
        "people/a": None,
        "people/b": {"id": 2, "email": None},
        "people/c": {"id": 3, "email": "a@example.com"},
    }
    relinked: list[tuple] = []
    deleted: list[int] = []
    monkeypatch.setattr(mdc.people_repo, "get_by_phone", lambda conn, ph: [])
    monkeypatch.setattr(mdc.people_repo, "get_by_google_resource", lambda conn, rn: rows[rn])
    monkeypatch.setattr(
        mdc.people_repo,
        "relink_google",
        lambda conn, pid, *, resource_name, etag: relinked.append((pid, resource_name, etag)),
    )
    monkeypatch.setattr(mdc.people_repo, "delete", lambda conn, pid: deleted.append(pid))
    monkeypatch.setattr(mdc.imessage_repo, "repoint_person", lambda conn, frm, to: 1)
    monkeypatch.setattr(mdc.linkedin_repo, "repoint_person", lambda conn, frm, to: 0)

    result = mdc.run(lambda: FakeConn(), apply=True, backup_path=google.backup_path)

    # The row carrying the email is the one promoted onto the surviving contact.
    assert relinked == [(3, "people/a", "etag-people/a")]
    assert deleted == [2]
    assert result["rows_relinked"] == 1 and result["rows_deleted"] == 1
    assert result["links_repointed"] == 1


def test_no_rows_at_all_leaves_the_local_side_alone(monkeypatch, tmp_path):
    a = contact("people/a", phones=["+15550100001"])
    b = contact("people/b", phones=["+15550100001"])
    google = FakeGoogle([a, b])
    google.backup_path = tmp_path / "backup.json"
    _wire_google(monkeypatch, google)
    monkeypatch.setattr(mdc.people_repo, "get_by_phone", lambda conn, ph: [])
    monkeypatch.setattr(mdc.people_repo, "get_by_google_resource", lambda conn, rn: None)

    result = mdc.run(lambda: FakeConn(), apply=True, backup_path=google.backup_path)

    assert result["merged"] == 1 and result["rows_relinked"] == 0
    assert result["rows_deleted"] == 0
    assert google.deleted == ["people/b"]


def _with_groups(person, *group_rns):
    person["memberships"] = [
        {"contactGroupMembership": {"contactGroupResourceName": rn}} for rn in group_rns
    ]
    return person


def test_group_membership_does_not_decide_the_survivor():
    thin = _with_groups(contact("people/a", phones=["+15550100001"]), "contactGroups/myContacts")
    grouped = _with_groups(
        contact("people/b", phones=["+15550100001"]), "contactGroups/myContacts", "contactGroups/1"
    )
    assert mdc.choose_survivor([thin, grouped])["resourceName"] == "people/a"


def test_memberships_are_never_written_as_a_field():
    # A raw memberships write can drop a contact out of myContacts entirely.
    survivor = _with_groups(contact("people/a"), "contactGroups/myContacts")
    other = _with_groups(contact("people/b"), "contactGroups/myContacts", "contactGroups/1")
    assert "memberships" not in mdc.union_fields([survivor, other], survivor)


def test_a_losers_user_group_moves_to_the_survivor(monkeypatch, tmp_path):
    a = _with_groups(contact("people/a", phones=["+15550100001"]), "contactGroups/myContacts")
    b = _with_groups(
        contact("people/b", phones=["+15550100001"]),
        "contactGroups/myContacts",
        "contactGroups/friends",
    )
    google = FakeGoogle([a, b])
    google.backup_path = tmp_path / "backup.json"
    _wire_google(monkeypatch, google)
    monkeypatch.setattr(
        mdc.gc,
        "list_groups",
        lambda: {
            "myContacts": {
                "resourceName": "contactGroups/myContacts",
                "groupType": "SYSTEM_CONTACT_GROUP",
            },
            "friends": {
                "resourceName": "contactGroups/friends",
                "groupType": "USER_CONTACT_GROUP",
            },
        },
    )
    moved: list[tuple] = []
    monkeypatch.setattr(
        mdc.gc, "modify_group_members", lambda g, add, rm: moved.append((g, add, rm))
    )
    monkeypatch.setattr(mdc.people_repo, "get_by_phone", lambda conn, ph: [])
    monkeypatch.setattr(mdc.people_repo, "get_by_google_resource", lambda conn, rn: None)

    result = mdc.run(lambda: FakeConn(), apply=True, backup_path=google.backup_path)

    # The user group follows the person; the system group is left alone.
    assert moved == [("contactGroups/friends", ["people/a"], [])]
    assert result["groups_moved"] == 1


def test_notes_fill_a_gap_but_never_overwrite():
    survivor = contact("people/a")
    other = contact("people/b")
    other["biographies"] = [{"value": "met at the conference"}]
    assert mdc.union_fields([survivor, other], survivor)["biographies"] == other["biographies"]

    kept = contact("people/c")
    kept["biographies"] = [{"value": "the survivor's own note"}]
    assert "biographies" not in mdc.union_fields([kept, kept], kept)


def test_differing_notes_are_a_clash_not_a_merge():
    a = contact("people/a")
    a["biographies"] = [{"value": "one story"}]
    b = contact("people/b")
    b["biographies"] = [{"value": "a different story"}]
    assert mdc.single_valued_clash([a, b]) == ["biographies"]
