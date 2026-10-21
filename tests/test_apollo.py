import httpx
import pytest

from clients import apollo


def _fake_post(monkeypatch, payload, status=200):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(apollo.httpx, "post", post)
    return seen


def test_people_search_posts_the_documented_body_and_maps_results(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    seen = _fake_post(
        monkeypatch,
        {
            "people": [
                {
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "title": "CTO",
                    "linkedin_url": "https://linkedin.com/in/ada",
                    "email": "ada@acme.com",
                    "email_status": "verified",
                }
            ]
        },
    )
    out = apollo.people_search("acme.com", ["CTO"])
    assert seen["url"] == "https://api.apollo.io/api/v1/mixed_people/search"
    assert seen["json"] == {
        "q_organization_domains": "acme.com",
        "person_titles": ["CTO"],
        "page": 1,
        "per_page": 10,
    }
    assert seen["headers"]["x-api-key"] == "k"
    assert seen["timeout"] == 15
    assert out == [
        {
            "name": "Ada Lovelace",
            "title": "CTO",
            "linkedin_url": "https://linkedin.com/in/ada",
            "email": "ada@acme.com",
            "email_status": "verified",
        }
    ]


def test_enrich_returns_the_person_or_none(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    seen = _fake_post(
        monkeypatch, {"person": {"first_name": "Ada", "last_name": "L", "email": "a@b.co"}}
    )
    out = apollo.enrich("https://linkedin.com/in/ada", None)
    assert seen["url"] == "https://api.apollo.io/api/v1/people/match"
    assert seen["json"] == {"linkedin_url": "https://linkedin.com/in/ada"}
    assert out["name"] == "Ada L" and out["email"] == "a@b.co"
    _fake_post(monkeypatch, {})
    assert apollo.enrich(None, "a@b.co") is None


def test_unset_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    with pytest.raises(apollo.ApolloUnavailable):
        apollo.people_search("acme.com", [])


def test_forbidden_surfaces_as_http_status_error(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    _fake_post(monkeypatch, {"error": "no"}, status=403)
    with pytest.raises(httpx.HTTPStatusError):
        apollo.people_search("acme.com", [])
