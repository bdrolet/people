import pytest

import clients.google_contacts as gc
import repo.people as people_repo
import services.google_contacts_sync as gsync
from services import person_edit

GROUPS = {
    "myContacts": {"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP"},
    "Inbox": {"resourceName": "contactGroups/inbox1", "groupType": "USER_CONTACT_GROUP"},
    "Family": {"resourceName": "contactGroups/fam1", "groupType": "USER_CONTACT_GROUP"},
    "Colleague": {"resourceName": "contactGroups/col1", "groupType": "USER_CONTACT_GROUP"},
}


@pytest.fixture
def wire(monkeypatch):
    log = []
    row = {
        "email": "a@x.com",
        "eligible": True,
        "google_resource_name": "people/c1",
        "google_etag": "e1",
    }
    monkeypatch.setattr(people_repo, "get", lambda conn, email: row if email == "a@x.com" else None)
    monkeypatch.setattr(gc, "list_groups", lambda: GROUPS)
    monkeypatch.setattr(
        gc,
        "ensure_group",
        lambda name: GROUPS.get(name, {"resourceName": f"contactGroups/new-{name}"})[
            "resourceName"
        ],
    )
    monkeypatch.setattr(
        gc,
        "update_fields",
        lambda rn, etag, fields: log.append(("bio", rn, etag, fields["biographies"][0]["value"])),
    )
    monkeypatch.setattr(
        gc, "modify_group_members", lambda g, add, remove: log.append(("group", g, add, remove))
    )
    monkeypatch.setattr(
        gc,
        "get_person",
        lambda rn: {
            "resourceName": rn,
            "etag": "e1",
            "memberships": [
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/fam1"}},
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/inbox1"}},
            ],
        },
    )
    monkeypatch.setattr(
        gsync,
        "sync_one",
        lambda conn, r: (log.append(("sync", r["email"])), {**r, "synced": True})[1],
    )
    return log


def test_notes_write_through_then_sync(wire):
    out = person_edit.update(None, "a@x.com", notes="met at conf")
    assert wire[0] == ("bio", "people/c1", "e1", "met at conf")
    assert wire[-1] == ("sync", "a@x.com") and out["synced"] is True


def test_label_moves_between_user_groups_but_keeps_inbox(wire):
    person_edit.update(None, "a@x.com", relationship_label="colleague")
    group_calls = [c for c in wire if c[0] == "group"]
    assert ("group", "contactGroups/col1", ["people/c1"], []) in group_calls
    assert ("group", "contactGroups/fam1", [], ["people/c1"]) in group_calls
    assert not any(c[1] == "contactGroups/inbox1" for c in group_calls)


def test_unknown_email_raises(wire):
    with pytest.raises(person_edit.NotFound):
        person_edit.update(None, "nobody@x.com", notes="x")


def test_unlinked_raises(monkeypatch, wire):
    monkeypatch.setattr(
        people_repo,
        "get",
        lambda conn, email: {"email": email, "eligible": False, "google_resource_name": None},
    )
    with pytest.raises(person_edit.NotLinked):
        person_edit.update(None, "a@x.com", notes="x")


def test_label_leaves_unrelated_groups_alone(wire, monkeypatch):
    monkeypatch.setattr(
        gc,
        "get_person",
        lambda rn: {
            "resourceName": rn,
            "etag": "e1",
            "memberships": [
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/fam1"}},
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/inbox1"}},
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/bc1"}},
            ],
        },
    )
    groups = {
        **GROUPS,
        "Book Club": {"resourceName": "contactGroups/bc1", "groupType": "USER_CONTACT_GROUP"},
    }
    monkeypatch.setattr(gc, "list_groups", lambda: groups)
    person_edit.update(None, "a@x.com", relationship_label="colleague")
    group_calls = [c for c in wire if c[0] == "group"]
    assert not any("contactGroups/bc1" in c[3] for c in group_calls)
    assert ("group", "contactGroups/fam1", [], ["people/c1"]) in group_calls


def test_label_matches_existing_group_case_insensitively(wire, monkeypatch):
    def must_not_be_called(name):
        raise AssertionError("must not be called")

    monkeypatch.setattr(gc, "ensure_group", must_not_be_called)
    person_edit.update(None, "a@x.com", relationship_label="FAMILY")
    group_calls = [c for c in wire if c[0] == "group"]
    assert ("group", "contactGroups/fam1", ["people/c1"], []) in group_calls
    assert not any(c[3] for c in group_calls)


def test_notes_uses_live_etag(wire, monkeypatch):
    monkeypatch.setattr(
        gc,
        "get_person",
        lambda rn: {"resourceName": "people/c1", "etag": "e2", "memberships": []},
    )
    person_edit.update(None, "a@x.com", notes="n")
    assert ("bio", "people/c1", "e2", "n") in wire


def test_partial_failure_still_resyncs_and_reraises(wire, monkeypatch):
    def boom(g, add, remove):
        raise RuntimeError("quota")

    monkeypatch.setattr(gc, "modify_group_members", boom)
    with pytest.raises(RuntimeError):
        person_edit.update(None, "a@x.com", notes="n", relationship_label="colleague")
    assert ("sync", "a@x.com") in wire
    assert any(c[0] == "bio" for c in wire)
