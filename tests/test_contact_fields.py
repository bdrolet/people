import pytest

from services import contact_fields as cf

# --- validate -------------------------------------------------------------


def test_validate_accepts_allowlisted_fields():
    out = cf.validate(
        {"phoneNumbers": [{"value": "+15550100001"}], "names": [{"givenName": "Alice"}]}
    )
    assert out["names"] == [{"givenName": "Alice"}]


def test_validate_wraps_a_bare_object_in_a_list():
    assert cf.validate({"birthdays": {"date": {"month": 4, "day": 2}}}) == {
        "birthdays": [{"date": {"month": 4, "day": 2}}]
    }


def test_validate_allows_empty_list_to_clear_a_field():
    # Review Focus 1: the only way to delete every phone number.
    assert cf.validate({"phoneNumbers": []}) == {"phoneNumbers": []}


def test_validate_rejects_fields_owned_elsewhere():
    with pytest.raises(cf.ValidationError, match="notes"):
        cf.validate({"biographies": [{"value": "hi"}]})
    with pytest.raises(cf.ValidationError, match="labels"):
        cf.validate({"memberships": []})


def test_validate_rejects_unknown_and_unwritable_fields():
    with pytest.raises(cf.ValidationError, match="photos"):
        cf.validate({"photos": []})
    with pytest.raises(cf.ValidationError, match="nope"):
        cf.validate({"nope": []})


def test_validate_rejects_bad_shapes():
    with pytest.raises(cf.ValidationError):
        cf.validate({"phoneNumbers": "+15550100001"})  # scalar
    with pytest.raises(cf.ValidationError):
        cf.validate({"phoneNumbers": [["nested"]]})  # list of non-objects


# --- check_email_addition -------------------------------------------------

LIVE = {"emailAddresses": [{"value": "Alice@Example.com"}, {"value": "a2@example.com"}]}


def test_email_addition_allows_adding():
    cf.check_email_addition(
        LIVE,
        [{"value": "alice@example.com"}, {"value": "a2@example.com"}, {"value": "new@example.com"}],
        "alice@example.com",
    )


def test_email_addition_ignores_case_and_whitespace():
    cf.check_email_addition(
        LIVE, [{"value": " ALICE@example.com "}, {"value": "a2@example.com"}], "alice@example.com"
    )


def test_email_addition_rejects_removal():
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(LIVE, [{"value": "alice@example.com"}], "alice@example.com")


def test_email_addition_rejects_empty_list():
    # Review Focus 2: {} would strip the linkage address.
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(LIVE, [], "alice@example.com")


def test_email_addition_rejects_dropping_the_keyed_address():
    live = {"emailAddresses": [{"value": "alice@example.com"}]}
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(live, [{"value": "other@example.com"}], "alice@example.com")


def test_email_addition_coerces_a_non_string_value_instead_of_raising():
    # Review Focus: a non-string `value` (e.g. an int) must be normalized
    # into a harmless string, not raise AttributeError from .strip().
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(LIVE, [{"value": 12345}], "alice@example.com")


def test_email_addition_allows_the_first_address_for_a_person_with_none():
    # A person with no keyed email: normalize(None) must not become "" in
    # the must-survive set, or every such submission would be rejected.
    cf.check_email_addition({}, [{"value": "new@example.com"}], None)


def test_email_addition_still_blocks_removal_for_a_person_with_one():
    live = {"emailAddresses": [{"value": "alice@example.com"}]}
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(live, [], "alice@example.com")


# --- derive ---------------------------------------------------------------


def test_derive_normalizes_phone_numbers_to_e164():
    got = cf.derive({"phoneNumbers": [{"value": "(555) 010-0001"}, {"value": "+15550100002"}]})
    assert got["phone_numbers"] == ["+15550100001", "+15550100002"]


def test_derive_drops_unparseable_numbers_and_duplicates():
    got = cf.derive(
        {
            "phoneNumbers": [
                {"value": "+15550100001"},
                {"value": "555-010-0001"},
                {"value": "nonsense"},
            ]
        }
    )
    assert got["phone_numbers"] == ["+15550100001"]


def test_derive_prefers_the_primary_organization():
    got = cf.derive(
        {
            "organizations": [
                {"name": "Second Co", "title": "Advisor"},
                {"name": "Example Health", "title": "CTO", "metadata": {"primary": True}},
            ]
        }
    )
    assert (got["company"], got["job_title"]) == ("Example Health", "CTO")


def test_derive_falls_back_to_the_first_organization():
    # Review Focus 4: nothing flagged primary.
    got = cf.derive({"organizations": [{"name": "Only Co", "title": "Lead"}]})
    assert (got["company"], got["job_title"]) == ("Only Co", "Lead")


def test_derive_handles_missing_and_empty_payloads():
    got = cf.derive({})
    assert got == {"phone_numbers": [], "company": None, "job_title": None, "google_fields": {}}
    got = cf.derive({"phoneNumbers": [], "organizations": []})
    assert got["phone_numbers"] == [] and got["company"] is None


def test_derive_keeps_only_allowlisted_keys_in_google_fields():
    got = cf.derive(
        {
            "names": [{"givenName": "Alice"}],
            "metadata": {"sources": []},
            "biographies": [{"value": "hi"}],
            "memberships": [],
        }
    )
    assert "names" in got["google_fields"]
    assert "metadata" not in got["google_fields"]
    assert "biographies" not in got["google_fields"]  # owned by notes
    assert "memberships" not in got["google_fields"]  # owned by labels


# --- identifiers ------------------------------------------------------------


def test_identifiers_returns_normalized_emails_and_phones():
    emails, phones = cf.identifiers(
        {
            "emailAddresses": [{"value": " Alice@Example.COM "}],
            "phoneNumbers": [{"value": "(555) 010-0001"}],
        }
    )
    assert emails == ["alice@example.com"]
    assert phones == ["+15550100001"]


def test_identifiers_drops_unusable_phones():
    # Review Focus 2: a short code is not an identifier.
    _, phones = cf.identifiers({"phoneNumbers": [{"value": "262966"}, {"value": "call me"}]})
    assert phones == []


def test_identifiers_handles_a_contact_with_neither():
    assert cf.identifiers({"names": [{"givenName": "Alice"}]}) == ([], [])


def test_identifiers_ignores_blank_and_non_string_values():
    emails, phones = cf.identifiers(
        {
            "emailAddresses": [{"value": ""}, {"value": None}, {}],
            "phoneNumbers": [{"value": 12345}],
        }
    )
    assert emails == [] and phones == []


def test_identifiers_deduplicates_preserving_order():
    emails, _ = cf.identifiers(
        {
            "emailAddresses": [
                {"value": "a@example.com"},
                {"value": "A@EXAMPLE.COM"},
                {"value": "b@example.com"},
            ]
        }
    )
    assert emails == ["a@example.com", "b@example.com"]
