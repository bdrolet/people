import pytest

from services import person_create


@pytest.fixture
def wired(monkeypatch):
    """No duplicates anywhere; Google creates; apply_person makes a row."""
    state = {"created_body": None, "applied": None, "edits": []}

    monkeypatch.setattr(person_create.people, "get", lambda conn, email: None)
    monkeypatch.setattr(person_create.people, "get_by_phone", lambda conn, ph: [])
    monkeypatch.setattr(person_create.gc, "search_by_email", lambda email: None)
    monkeypatch.setattr(person_create.gc, "list_phone_index", lambda: {})
    monkeypatch.setattr(person_create.gc, "list_groups", lambda: {})
    monkeypatch.setattr(person_create.gc, "ensure_group", lambda name: "contactGroups/abc")

    def fake_create_person(body, group):
        state["created_body"] = body
        return {"resourceName": "people/c1", "etag": "e1"}

    monkeypatch.setattr(person_create.gc, "create_person", fake_create_person)
    monkeypatch.setattr(
        person_create.gsync,
        "apply_person",
        lambda conn, person: state.__setitem__("applied", person) or "created",
    )
    monkeypatch.setattr(
        person_create.people,
        "get_by_google_resource",
        lambda conn, rn: {"id": 7, "email": None, "google_resource_name": rn},
    )
    monkeypatch.setattr(
        person_create.person_edit,
        "update",
        lambda conn, pid, **kw: state["edits"].append((pid, kw)) or {"id": pid},
    )
    return state


PHONE_ONLY = {"phoneNumbers": [{"value": "+15550100001"}]}


def test_creates_a_phone_only_person(wired):
    row = person_create.create(None, contact=PHONE_ONLY)
    assert row["id"] == 7
    assert wired["created_body"]["phoneNumbers"] == [{"value": "+15550100001"}]
    assert wired["applied"]["resourceName"] == "people/c1"
    assert wired["edits"] == []  # no notes or label given


def test_applies_notes_and_labels_when_given(wired):
    person_create.create(None, contact=PHONE_ONLY, notes="hi", labels=["colleague", "Climbing"])
    assert wired["edits"] == [(7, {"notes": "hi", "labels": {"add": ["colleague", "Climbing"]}})]


def test_labels_alone_trigger_the_edit(wired):
    person_create.create(None, contact=PHONE_ONLY, labels=["colleague"])
    assert wired["edits"] == [(7, {"notes": None, "labels": {"add": ["colleague"]}})]


def test_empty_labels_do_not_trigger_an_edit(wired):
    person_create.create(None, contact=PHONE_ONLY, labels=[])
    assert wired["edits"] == []


def test_rejects_a_contact_with_no_usable_identifier(wired):
    # Review Focus 2.
    with pytest.raises(person_create.Invalid):
        person_create.create(None, contact={"phoneNumbers": [{"value": "262966"}]})
    assert wired["created_body"] is None  # nothing reached Google


def test_rejects_a_field_owned_elsewhere(wired):
    with pytest.raises(person_create.Invalid):
        person_create.create(None, contact={"biographies": [{"value": "x"}]})
    assert wired["created_body"] is None


def test_refuses_a_duplicate_person_row_by_email(wired, monkeypatch):
    monkeypatch.setattr(person_create.people, "get", lambda conn, email: {"id": 11, "email": email})
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact={"emailAddresses": [{"value": "a@example.com"}]})
    assert e.value.candidates == [11]
    assert wired["created_body"] is None


def test_refuses_a_duplicate_person_row_by_phone(wired, monkeypatch):
    monkeypatch.setattr(
        person_create.people, "get_by_phone", lambda conn, ph: [{"id": 12}, {"id": 13}]
    )
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact=PHONE_ONLY)
    assert e.value.candidates == [12, 13]
    assert wired["created_body"] is None


def test_refuses_a_google_only_duplicate_with_no_candidates(wired, monkeypatch):
    # Review Focus 3: exists in Google, not yet adopted — no people.id to give.
    monkeypatch.setattr(
        person_create.gc, "search_by_email", lambda email: {"resourceName": "people/c9"}
    )
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact={"emailAddresses": [{"value": "a@example.com"}]})
    assert e.value.candidates == []
    assert wired["created_body"] is None


def test_refuses_a_google_only_duplicate_by_phone(wired, monkeypatch):
    monkeypatch.setattr(
        person_create.gc,
        "list_phone_index",
        lambda: {"+15550100001": [{"resource_name": "people/c9", "display_name": "Alice"}]},
    )
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact=PHONE_ONLY)
    assert e.value.candidates == []
    assert wired["created_body"] is None


def test_raises_invalid_when_no_row_appears(wired, monkeypatch):
    # Review Focus 4: created in Google but no local row — do not report success.
    monkeypatch.setattr(person_create.people, "get_by_google_resource", lambda conn, rn: None)
    with pytest.raises(person_create.Invalid):
        person_create.create(None, contact=PHONE_ONLY)
