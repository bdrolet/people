from services import identity


def test_email_is_normalized():
    assert identity.classify(" Alice@Example.COM ") == ("email", "alice@example.com")


def test_e164_phone_is_recognised():
    assert identity.classify("+15550100001") == ("phone", "+15550100001")


def test_local_format_phone_is_normalised():
    assert identity.classify("(555) 010-0001") == ("phone", "+15550100001")


def test_digits_are_an_id():
    assert identity.classify("42") == ("id", 42)


def test_short_code_is_not_a_phone():
    # normalize_handle rejects anything under 7 digits, so this is an id.
    assert identity.classify("262966") == ("id", 262966)


def test_unparseable_values_return_none():
    # Review Focus 4: these must 404, never 500.
    for bad in ("alice", "", "   ", "not-an-email@", "@example.com"):
        assert identity.classify(bad) is None


def test_email_wins_over_digits_when_both_present():
    assert identity.classify("12345@example.com") == ("email", "12345@example.com")


def test_profile_urls_are_linkedin():
    # Review Focus 4: whatever Ben pastes from a browser resolves.
    for raw in (
        "linkedin.com/in/jane-example",
        "https://www.linkedin.com/in/Jane-Example/?trk=x",
        "https://uk.linkedin.com/in/jane-example",
    ):
        assert identity.classify(raw) == ("linkedin", "linkedin.com/in/jane-example")


def test_a_numeric_slug_is_linkedin_not_an_id_or_a_phone():
    assert identity.classify("linkedin.com/in/5550100001") == (
        "linkedin",
        "linkedin.com/in/5550100001",
    )


def test_an_email_mentioning_linkedin_is_an_email():
    assert identity.classify("jane@linkedin.com") == ("email", "jane@linkedin.com")


def test_a_company_page_identifies_no_one():
    assert identity.classify("https://www.linkedin.com/company/acme") is None
