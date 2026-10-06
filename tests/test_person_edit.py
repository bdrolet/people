import json

import pytest
from googleapiclient.errors import HttpError

import clients.google_contacts as gc
import repo.people as people_repo
import services.google_contacts_sync as gsync
from services import person_edit

USER = "USER_CONTACT_GROUP"
SYSTEM = "SYSTEM_CONTACT_GROUP"
GROUPS_BY_RN = {
    "contactGroups/myContacts": {
        "name": "myContacts",
        "formattedName": "My Contacts",
        "groupType": SYSTEM,
    },
    "contactGroups/starred": {"name": "starred", "formattedName": "Starred", "groupType": SYSTEM},
    "contactGroups/inbox1": {"name": "Inbox", "formattedName": "Inbox", "groupType": USER},
    "contactGroups/fam1": {"name": "Family", "formattedName": "Family", "groupType": USER},
    "contactGroups/col1": {"name": "Colleague", "formattedName": "Colleague", "groupType": USER},
}


@pytest.fixture
def wire(monkeypatch):
    log = []
    row = {
        "id": 1,
        "email": "a@x.com",
        "eligible": True,
        "google_resource_name": "people/c1",
        "google_etag": "e1",
    }
    monkeypatch.setattr(people_repo, "get_by_id", lambda conn, pid: row if pid == 1 else None)
    monkeypatch.setattr(gc, "list_groups_by_rn", lambda: GROUPS_BY_RN)
    monkeypatch.setattr(
        gc,
        "ensure_group",
        lambda name: (log.append(("create", name)), f"contactGroups/new-{name}")[1],
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
    out = person_edit.update(None, 1, notes="met at conf")
    assert wire[0] == ("bio", "people/c1", "e1", "met at conf")
    assert wire[-1] == ("sync", "a@x.com") and out["synced"] is True


def test_unknown_id_raises(wire):
    with pytest.raises(person_edit.NotFound):
        person_edit.update(None, 999, notes="x")


def test_unlinked_raises(monkeypatch, wire):
    monkeypatch.setattr(
        people_repo,
        "get_by_id",
        lambda conn, pid: {
            "id": pid,
            "email": "a@x.com",
            "eligible": False,
            "google_resource_name": None,
        },
    )
    with pytest.raises(person_edit.NotLinked):
        person_edit.update(None, 1, notes="x")


def group_calls(log):
    return [c for c in log if c[0] == "group"]


def test_add_puts_the_person_in_an_existing_group(wire):
    person_edit.update(None, 1, labels={"add": ["colleague"]})
    assert group_calls(wire) == [("group", "contactGroups/col1", ["people/c1"], [])]
    assert wire[-1] == ("sync", "a@x.com")


def test_add_never_removes_other_labels(wire):
    person_edit.update(None, 1, labels={"add": ["colleague"]})
    assert not any(c[3] for c in group_calls(wire))


def test_add_creates_a_missing_group(wire):
    person_edit.update(None, 1, labels={"add": ["Climbing"]})
    assert ("create", "Climbing") in wire
    assert group_calls(wire) == [("group", "contactGroups/new-Climbing", ["people/c1"], [])]


def test_add_of_a_held_label_is_skipped(wire):
    person_edit.update(None, 1, labels={"add": ["FAMILY"]})
    assert group_calls(wire) == []


def test_remove_takes_the_person_out(wire):
    person_edit.update(None, 1, labels={"remove": ["family"]})
    assert group_calls(wire) == [("group", "contactGroups/fam1", [], ["people/c1"])]


def test_remove_unheld_or_unknown_label_is_a_noop(wire):
    # Review Focus 4.
    person_edit.update(None, 1, labels={"remove": ["colleague", "Never Existed"]})
    assert group_calls(wire) == []
    assert not any(c[0] == "create" for c in wire)


def test_add_and_remove_together(wire):
    person_edit.update(None, 1, labels={"add": ["colleague"], "remove": ["family"]})
    assert group_calls(wire) == [
        ("group", "contactGroups/col1", ["people/c1"], []),
        ("group", "contactGroups/fam1", [], ["people/c1"]),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"add": ["  "]},
        {"add": ["x"], "remove": ["X"]},
        {"add": ["Inbox"]},
        {"remove": ["inbox"]},
        {"add": ["starred"]},
    ],
)
def test_invalid_label_change_rejects_before_any_write(wire, change):
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 1, notes="n", labels=change)
    assert wire == []  # no bio write, no group write, no create, no resync


def test_ambiguous_label_is_a_conflict(wire, monkeypatch):
    monkeypatch.setattr(
        gc,
        "list_groups_by_rn",
        lambda: {
            **GROUPS_BY_RN,
            "contactGroups/v1": {"name": "VIP", "formattedName": "VIP", "groupType": USER},
            "contactGroups/v2": {"name": "vip", "formattedName": "vip", "groupType": USER},
        },
    )
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 1, labels={"add": ["Vip"]})
    assert group_calls(wire) == []


def test_label_google_4xx_is_invalid_and_still_resyncs(wire, monkeypatch):
    def boom(g, add, remove):
        raise http_error(400, "Group is read-only")

    monkeypatch.setattr(gc, "modify_group_members", boom)
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 1, labels={"add": ["colleague"]})
    assert ("sync", "a@x.com") in wire


def test_validate_labels_does_no_google_writes(wire):
    person_edit.validate_labels({"add": ["Climbing", "colleague"], "remove": ["family"]})
    assert wire == []


def test_validate_labels_rejects_blank_name(wire):
    with pytest.raises(person_edit.Invalid):
        person_edit.validate_labels({"add": [" "]})
    assert wire == []


def test_notes_uses_live_etag(wire, monkeypatch):
    monkeypatch.setattr(
        gc,
        "get_person",
        lambda rn: {"resourceName": "people/c1", "etag": "e2", "memberships": []},
    )
    person_edit.update(None, 1, notes="n")
    assert ("bio", "people/c1", "e2", "n") in wire


def test_partial_failure_still_resyncs_and_reraises(wire, monkeypatch):
    def boom(g, add, remove):
        raise RuntimeError("quota")

    monkeypatch.setattr(gc, "modify_group_members", boom)
    with pytest.raises(RuntimeError):
        person_edit.update(None, 1, notes="n", labels={"add": ["colleague"]})
    assert ("sync", "a@x.com") in wire
    assert any(c[0] == "bio" for c in wire)


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def http_error(status, message, error_status=None):
    body: dict = {"message": message}
    if error_status is not None:
        body["status"] = error_status
    return HttpError(FakeResp(status), json.dumps({"error": body}).encode())


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


CONTACT_ROW = {"id": 42, "email": "alice@example.com", "google_resource_name": "people/c1"}


@pytest.fixture
def contact_repo(monkeypatch):
    monkeypatch.setattr(person_edit.people, "get_by_id", lambda conn, pid: dict(CONTACT_ROW))


def test_contact_and_notes_go_in_one_update(contact_gc, contact_repo):
    person_edit.update(
        None,
        42,
        notes="hi",
        contact={"phoneNumbers": [{"value": "+15550100001"}]},
    )
    assert len(contact_gc.updates) == 1
    _, etag, fields = contact_gc.updates[0]
    assert etag == "etag-1"
    assert set(fields) == {"biographies", "phoneNumbers"}


def test_contact_absent_writes_nothing(contact_gc, contact_repo):
    # Review Focus 3.
    person_edit.update(None, 42)
    assert contact_gc.updates == []


def test_contact_empty_dict_writes_nothing(contact_gc, contact_repo):
    # Review Focus 3: present but empty is a no-op, not an error.
    person_edit.update(None, 42, contact={})
    assert contact_gc.updates == []


def test_empty_list_clears_a_field(contact_gc, contact_repo):
    # Review Focus 1.
    person_edit.update(None, 42, contact={"phoneNumbers": []})
    assert contact_gc.updates[0][2] == {"phoneNumbers": []}


def test_rejected_field_raises_invalid_without_writing(contact_gc, contact_repo):
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 42, contact={"biographies": [{"value": "x"}]})
    assert contact_gc.updates == []


def test_email_removal_raises_conflict_without_writing(contact_gc, contact_repo):
    with pytest.raises(person_edit.Conflict):
        person_edit.update(
            None,
            42,
            contact={"emailAddresses": [{"value": "other@example.com"}]},
        )
    assert contact_gc.updates == []


def test_stale_etag_raises_conflict(contact_gc, contact_repo):
    # Review Focus 5.
    contact_gc.raise_on_update = http_error(400, "FAILED_PRECONDITION: etag mismatch")
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 42, contact={"names": [{"givenName": "A"}]})


def test_other_google_4xx_raises_invalid_carrying_the_message(contact_gc, contact_repo):
    contact_gc.raise_on_update = http_error(400, "Invalid birthday")
    with pytest.raises(person_edit.Invalid, match="Invalid birthday"):
        person_edit.update(
            None,
            42,
            contact={"birthdays": [{"date": {"month": 13}}]},
        )


def test_resync_still_runs_after_a_failed_write(contact_gc, contact_repo, monkeypatch):
    seen = []
    monkeypatch.setattr(person_edit.gsync, "sync_one", lambda conn, row: seen.append(1) or row)
    contact_gc.raise_on_update = http_error(400, "Invalid birthday")
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 42, contact={"birthdays": [{}]})
    assert seen == [1]


# --- Fix round 1: stale-etag detection must key off error.status, not the
# message substring (the live API's message doesn't reliably say "etag"). ---


def test_failed_precondition_status_with_no_etag_wording_raises_conflict(contact_gc, contact_repo):
    # This is the case that fails without the error.status check: a real
    # FAILED_PRECONDITION response whose message never says "etag".
    contact_gc.raise_on_update = http_error(
        400, "Precondition failed, try again.", error_status="FAILED_PRECONDITION"
    )
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 42, contact={"names": [{"givenName": "A"}]})


def test_http_412_raises_conflict(contact_gc, contact_repo):
    contact_gc.raise_on_update = http_error(412, "Precondition Failed")
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 42, contact={"names": [{"givenName": "A"}]})


def test_malformed_error_body_does_not_raise_a_parse_error(contact_gc, contact_repo):
    # Guarded parse (same style as _is_expired_sync_token): unparsable content
    # must fall through to Invalid, never raise from inside the mapping.
    contact_gc.raise_on_update = HttpError(FakeResp(400), b"not json at all")
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 42, contact={"names": [{"givenName": "A"}]})


def test_real_message_substring_case_still_raises_conflict(contact_gc, contact_repo):
    # The actual live-API message: no error.status in this fixture, so this
    # exercises the last-resort "etag" substring fallback.
    contact_gc.raise_on_update = http_error(
        400,
        "Request person.etag is different than the current person.etag. "
        "Clear local cache and get the latest person.",
    )
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 42, contact={"names": [{"givenName": "A"}]})


def test_5xx_propagates_untouched(contact_gc, contact_repo):
    contact_gc.raise_on_update = http_error(500, "Internal error")
    with pytest.raises(HttpError):
        person_edit.update(None, 42, contact={"names": [{"givenName": "A"}]})


# --- Task 3: keyed by person_id, so a phone-only person is editable -------


def test_update_is_keyed_by_person_id(contact_gc, monkeypatch):
    seen: dict = {}

    def fake_get_by_id(conn, pid):
        seen["pid"] = pid
        return {"id": pid, "email": "alice@example.com", "google_resource_name": "people/c1"}

    monkeypatch.setattr(person_edit.people, "get_by_id", fake_get_by_id)
    person_edit.update(None, 42, notes="hi")
    assert seen["pid"] == 42


def test_update_raises_not_found_for_an_unknown_id(monkeypatch):
    monkeypatch.setattr(person_edit.people, "get_by_id", lambda conn, pid: None)
    with pytest.raises(person_edit.NotFound):
        person_edit.update(None, 999, notes="hi")


def test_update_works_for_a_person_with_no_email(contact_gc, monkeypatch):
    # Review Focus (Task 3): a phone-only row has row["email"] is None, which
    # must reach check_email_addition without raising on every request.
    monkeypatch.setattr(
        person_edit.people,
        "get_by_id",
        lambda conn, pid: {"id": 7, "email": None, "google_resource_name": "people/c1"},
    )
    person_edit.update(None, 7, contact={"phoneNumbers": [{"value": "+15550100002"}]})
    assert contact_gc.updates and "phoneNumbers" in contact_gc.updates[0][2]


# --- Fix 2: clearing phoneNumbers must not strip a phone-only person's last
# identifier (mirrors the email add-only rule; people_has_an_identifier). ---


def test_clearing_phone_numbers_on_a_phone_only_person_raises_conflict(contact_gc, monkeypatch):
    monkeypatch.setattr(
        person_edit.people,
        "get_by_id",
        lambda conn, pid: {"id": 7, "email": None, "google_resource_name": "people/c1"},
    )
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 7, contact={"phoneNumbers": []})
    assert contact_gc.updates == []


def test_clearing_phone_numbers_on_a_person_with_an_email_is_allowed(contact_gc, contact_repo):
    # CONTACT_ROW has an email, so losing every phone number still leaves an
    # identifier — allowed, same as before this fix.
    person_edit.update(None, 42, contact={"phoneNumbers": []})
    assert contact_gc.updates and contact_gc.updates[0][2] == {"phoneNumbers": []}


def test_replacing_the_phone_number_on_a_phone_only_person_is_allowed(contact_gc, monkeypatch):
    monkeypatch.setattr(
        person_edit.people,
        "get_by_id",
        lambda conn, pid: {"id": 7, "email": None, "google_resource_name": "people/c1"},
    )
    person_edit.update(None, 7, contact={"phoneNumbers": [{"value": "+15550100009"}]})
    assert contact_gc.updates and "phoneNumbers" in contact_gc.updates[0][2]
