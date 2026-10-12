import pytest

from services import person_lookup


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setattr(
        person_lookup.people, "get", lambda c, e: {"id": 1} if e == "alice@example.com" else None
    )
    monkeypatch.setattr(
        person_lookup.people, "get_by_id", lambda c, i: {"id": i} if i == 7 else None
    )
    monkeypatch.setattr(
        person_lookup.people,
        "get_by_phone",
        lambda c, p: (
            [{"id": 3}, {"id": 4}]
            if p == "+15550100009"
            else [{"id": 2}]
            if p == "+15550100001"
            else []
        ),
    )


@pytest.mark.parametrize(
    "ident,expected", [("Alice@Example.com", 1), ("7", 7), ("+1 555 010 0001", 2)]
)
def test_resolves_email_id_and_phone(people, ident, expected):
    assert person_lookup.resolve(None, ident)["id"] == expected


def test_a_shared_phone_is_ambiguous(people):
    with pytest.raises(person_lookup.Ambiguous) as e:
        person_lookup.resolve(None, "+15550100009")
    assert e.value.candidates == [3, 4]


@pytest.mark.parametrize("ident", ["nobody@example.com", "8", "", "not a person"])
def test_no_match(people, ident):
    with pytest.raises(person_lookup.NotFound):
        person_lookup.resolve(None, ident)
