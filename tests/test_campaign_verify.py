from services.campaign_verify import resolve_email

COMPANY = {"domain": "acme.com"}


class FakeApollo:
    def __init__(self, enrich=None, search=None):
        self._enrich, self._search = enrich, search or []

    def enrich(self, linkedin_url, email):
        return self._enrich

    def people_search(self, domain, titles):
        return self._search


class FakeHunter:
    def __init__(self, statuses=None, found=None, pattern=None):
        self.statuses, self.found, self.pattern = statuses or {}, found, pattern
        self.verified = []

    def verify(self, email):
        self.verified.append(email)
        return self.statuses.get(email, "unknown")

    def find(self, domain, first, last):
        return self.found

    def domain_pattern(self, domain):
        return self.pattern


def test_apollo_email_verified_valid():
    ap = FakeApollo(enrich={"email": "cto@acme.com"})
    hu = FakeHunter({"cto@acme.com": "valid"})
    c = {"full_name": "Cy Tee", "linkedin_url": "https://li/in/cy"}
    assert resolve_email(c, COMPANY, apollo=ap, hunter=hu) == ("cto@acme.com", "valid", "apollo")


def test_apollo_search_matches_by_name_without_linkedin():
    ap = FakeApollo(
        search=[
            {"name": "Other P", "email": "o@acme.com"},
            {"name": "Cy Tee", "email": "cy@acme.com"},
        ]
    )
    hu = FakeHunter({"cy@acme.com": "valid"})
    out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=ap, hunter=hu)
    assert out == ("cy@acme.com", "valid", "apollo")


def test_hunter_find_accept_all():
    hu = FakeHunter({"cy@acme.com": "accept_all"}, found={"email": "cy@acme.com", "score": 80})
    out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == ("cy@acme.com", "accept_all", "hunter")


def test_nothing_found():
    out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=FakeHunter())
    assert out == (None, "unknown", None)


def test_invalid_apollo_email_falls_through_to_hunter():
    ap = FakeApollo(enrich={"email": "bad@acme.com"})
    hu = FakeHunter(
        {"bad@acme.com": "invalid", "cy@acme.com": "valid"},
        found={"email": "cy@acme.com", "score": 90},
    )
    c = {"full_name": "Cy Tee", "linkedin_url": "https://li/in/cy"}
    assert resolve_email(c, COMPANY, apollo=ap, hunter=hu) == ("cy@acme.com", "valid", "hunter")


def test_pattern_candidate_used_and_manual_source():
    hu = FakeHunter({"cy.tee@acme.com": "valid"}, pattern="{first}.{last}")
    out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == ("cy.tee@acme.com", "valid", "hunter")
    hu = FakeHunter({"m@acme.com": "valid"})
    out = resolve_email(
        {"full_name": "Cy Tee", "email": "m@acme.com"}, COMPANY, apollo=FakeApollo(), hunter=hu
    )
    assert out == ("m@acme.com", "valid", "manual")


def test_accept_all_beats_unknown_and_invalid_is_dropped():
    hu = FakeHunter(
        {"cy@acme.com": "unknown", "cy.tee@acme.com": "accept_all", "ctee@acme.com": "invalid"},
        found={"email": "cy@acme.com", "score": 50},
        pattern="{first}.{last}",
    )
    out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == ("cy.tee@acme.com", "accept_all", "hunter")
    hu = FakeHunter({"cy@acme.com": "invalid"}, found={"email": "cy@acme.com", "score": 50})
    out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == (None, "unknown", None)


def test_multi_word_name_uses_first_and_last_token():
    hu = FakeHunter({"mary.smith@x.com": "valid"}, pattern="{first}.{last}")
    out = resolve_email(
        {"full_name": "Mary Ann Smith"}, {"domain": "x.com"}, apollo=FakeApollo(), hunter=hu
    )
    assert out == ("mary.smith@x.com", "valid", "hunter")
    assert hu.verified == ["mary.smith@x.com"]


def test_single_name_skips_find_and_last_patterns():
    class H(FakeHunter):
        def find(self, *a):
            raise AssertionError("find must not be called")

    hu = H({"cher@x.com": "valid"}, pattern="{first}.{last}")
    assert resolve_email(
        {"full_name": "Cher"}, {"domain": "x.com"}, apollo=FakeApollo(), hunter=hu
    ) == (
        None,
        "unknown",
        None,
    )
    assert hu.verified == []
    hu = H({"cher@x.com": "valid"}, pattern="{first}")
    out = resolve_email({"full_name": "Cher"}, {"domain": "x.com"}, apollo=FakeApollo(), hunter=hu)
    assert out == ("cher@x.com", "valid", "hunter")


def test_accents_and_punctuation_are_normalized():
    hu = FakeHunter({"jobrien@x.com": "valid"}, pattern="{f}{last}")
    out = resolve_email(
        {"full_name": "José O'Brien"}, {"domain": "x.com"}, apollo=FakeApollo(), hunter=hu
    )
    assert out == ("jobrien@x.com", "valid", "hunter")


def test_malformed_candidates_are_never_verified():
    hu = FakeHunter({}, found={"email": "a b@x.com", "score": 1})
    out = resolve_email(
        {"full_name": "Cy Tee", "email": "bad@@x.com"},
        {"domain": "x.com"},
        apollo=FakeApollo(),
        hunter=hu,
    )
    assert out == (None, "unknown", None)
    assert hu.verified == []
