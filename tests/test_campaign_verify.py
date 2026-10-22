import logging

import httpx
import pytest

from clients.apollo import ApolloUnavailable
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


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.apollo.io/api/v1/mixed_people/search")
    return httpx.HTTPStatusError(f"{code}", request=req, response=httpx.Response(code, request=req))


class RaisingApollo:
    def __init__(self, exc: Exception):
        self.exc = exc

    def enrich(self, linkedin_url, email):
        raise self.exc

    def people_search(self, domain, titles):
        raise self.exc


class FindHunter(FakeHunter):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.find_calls = 0

    def find(self, domain, first, last):
        self.find_calls += 1
        return self.found


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize(
    "contact",
    [{"full_name": "Cy Tee"}, {"full_name": "Cy Tee", "linkedin_url": "https://li/in/cy"}],
)
def test_apollo_plan_error_falls_through_to_hunter(code, contact, caplog):
    hu = FindHunter({"cy@acme.com": "valid"}, found={"email": "cy@acme.com", "score": 90})
    with caplog.at_level(logging.WARNING):
        out = resolve_email(contact, COMPANY, apollo=RaisingApollo(_status_error(code)), hunter=hu)
    assert out == ("cy@acme.com", "valid", "hunter")
    assert hu.find_calls == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_apollo_unavailable_falls_through_to_hunter():
    hu = FindHunter({"cy@acme.com": "valid"}, found={"email": "cy@acme.com", "score": 90})
    ap = RaisingApollo(ApolloUnavailable("no key"))
    assert resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=ap, hunter=hu) == (
        "cy@acme.com",
        "valid",
        "hunter",
    )


@pytest.mark.parametrize("code", [429, 500])
def test_apollo_other_http_errors_propagate(code):
    with pytest.raises(httpx.HTTPStatusError):
        resolve_email(
            {"full_name": "Cy Tee"},
            COMPANY,
            apollo=RaisingApollo(_status_error(code)),
            hunter=FindHunter(),
        )


class StepHunter(FakeHunter):
    """Raises `exc` from the named step ('find', 'pattern' or 'verify')."""

    def __init__(self, step, exc, **kw):
        super().__init__(**kw)
        self.step, self.exc = step, exc

    def find(self, domain, first, last):
        if self.step == "find":
            raise self.exc
        return self.found

    def domain_pattern(self, domain):
        if self.step == "pattern":
            raise self.exc
        return self.pattern

    def verify(self, email):
        if self.step == "verify":
            raise self.exc
        return super().verify(email)


@pytest.mark.parametrize("code", [400, 404, 422])
def test_hunter_find_4xx_means_no_result_and_continues_to_pattern(code, caplog):
    hu = StepHunter(
        "find", _status_error(code), statuses={"cy@acme.com": "valid"}, pattern="{first}"
    )
    with caplog.at_level(logging.WARNING):
        out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == ("cy@acme.com", "valid", "hunter")
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("code", [400, 404, 422])
def test_hunter_pattern_4xx_means_no_result(code, caplog):
    hu = StepHunter("pattern", _status_error(code))
    with caplog.at_level(logging.WARNING):
        out = resolve_email({"full_name": "Cy Tee"}, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == (None, "unknown", None)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("code", [400, 404, 422])
def test_hunter_verify_4xx_is_unknown_for_that_candidate(code):
    hu = StepHunter("verify", _status_error(code))
    contact = {"full_name": "Cy Tee", "email": "cy@acme.com"}
    out = resolve_email(contact, COMPANY, apollo=FakeApollo(), hunter=hu)
    assert out == ("cy@acme.com", "unknown", "manual")


@pytest.mark.parametrize("step", ["find", "pattern", "verify"])
@pytest.mark.parametrize("code", [401, 403, 429, 500, 503])
def test_hunter_outage_and_key_errors_propagate(step, code):
    hu = StepHunter(step, _status_error(code), found={"email": "cy@acme.com"})
    with pytest.raises(httpx.HTTPStatusError):
        resolve_email(
            {"full_name": "Cy Tee", "email": "cy@acme.com"},
            COMPANY,
            apollo=FakeApollo(),
            hunter=hu,
        )
