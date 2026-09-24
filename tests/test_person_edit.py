import json

import pytest
from googleapiclient.errors import HttpError

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


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def http_error(status, message):
    return HttpError(FakeResp(status), json.dumps({"error": {"message": message}}).encode())


@pytest.fixture
def contact_gc(monkeypatch):
    """Fake clients.google_contacts as person_edit sees it, for the
    contact-field write path."""

    class GC:
        def __init__(self):
            self.updates = []
            self.live = {
                "etag": "etag-1",
                "emailAddresses": [{"value": "alice@example.com"}],
                "memberships": [],
            }
            self.raise_on_update = None

        def get_person(self, rn):
            return self.live

        def update_fields(self, rn, etag, fields):
            if self.raise_on_update:
                raise self.raise_on_update
            self.updates.append((rn, etag, fields))
            return self.live

    fake = GC()
    monkeypatch.setattr(person_edit, "gc", fake)
    monkeypatch.setattr(person_edit.gsync, "sync_one", lambda conn, row: row)
    return fake


CONTACT_ROW = {"email": "alice@example.com", "google_resource_name": "people/c1"}


@pytest.fixture
def contact_repo(monkeypatch):
    monkeypatch.setattr(person_edit.people, "get", lambda conn, email: dict(CONTACT_ROW))


def test_contact_and_notes_go_in_one_update(contact_gc, contact_repo):
    person_edit.update(
        None,
        "alice@example.com",
        notes="hi",
        contact={"phoneNumbers": [{"value": "+15550100001"}]},
    )
    assert len(contact_gc.updates) == 1
    _, etag, fields = contact_gc.updates[0]
    assert etag == "etag-1"
    assert set(fields) == {"biographies", "phoneNumbers"}


def test_contact_absent_writes_nothing(contact_gc, contact_repo):
    # Review Focus 3.
    person_edit.update(None, "alice@example.com")
    assert contact_gc.updates == []


def test_contact_empty_dict_writes_nothing(contact_gc, contact_repo):
    # Review Focus 3: present but empty is a no-op, not an error.
    person_edit.update(None, "alice@example.com", contact={})
    assert contact_gc.updates == []


def test_empty_list_clears_a_field(contact_gc, contact_repo):
    # Review Focus 1.
    person_edit.update(None, "alice@example.com", contact={"phoneNumbers": []})
    assert contact_gc.updates[0][2] == {"phoneNumbers": []}


def test_rejected_field_raises_invalid_without_writing(contact_gc, contact_repo):
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, "alice@example.com", contact={"biographies": [{"value": "x"}]})
    assert contact_gc.updates == []


def test_email_removal_raises_conflict_without_writing(contact_gc, contact_repo):
    with pytest.raises(person_edit.Conflict):
        person_edit.update(
            None,
            "alice@example.com",
            contact={"emailAddresses": [{"value": "other@example.com"}]},
        )
    assert contact_gc.updates == []


def test_stale_etag_raises_conflict(contact_gc, contact_repo):
    # Review Focus 5.
    contact_gc.raise_on_update = http_error(400, "FAILED_PRECONDITION: etag mismatch")
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, "alice@example.com", contact={"names": [{"givenName": "A"}]})


def test_other_google_4xx_raises_invalid_carrying_the_message(contact_gc, contact_repo):
    contact_gc.raise_on_update = http_error(400, "Invalid birthday")
    with pytest.raises(person_edit.Invalid, match="Invalid birthday"):
        person_edit.update(
            None,
            "alice@example.com",
            contact={"birthdays": [{"date": {"month": 13}}]},
        )


def test_resync_still_runs_after_a_failed_write(contact_gc, contact_repo, monkeypatch):
    seen = []
    monkeypatch.setattr(person_edit.gsync, "sync_one", lambda conn, row: seen.append(1) or row)
    contact_gc.raise_on_update = http_error(400, "Invalid birthday")
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, "alice@example.com", contact={"birthdays": [{}]})
    assert seen == [1]
