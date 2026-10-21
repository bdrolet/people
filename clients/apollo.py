"""Apollo.io people search and enrichment (CLI-only; Cloud Functions never call this).
I/O only: services/campaign_verify.py decides. HTTP errors (401/403 on a plan without
API access) surface as httpx.HTTPStatusError."""

import os

import httpx

TIMEOUT_S = 15
_BASE = "https://api.apollo.io/api/v1"


class ApolloUnavailable(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not key:
        raise ApolloUnavailable("APOLLO_API_KEY unset")
    return key


def _shape(p: dict) -> dict:
    name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
    return {
        "name": name,
        "title": p.get("title"),
        "linkedin_url": p.get("linkedin_url"),
        "email": p.get("email"),
        "email_status": p.get("email_status"),
    }


def _post(path: str, body: dict) -> dict:
    resp = httpx.post(
        f"{_BASE}{path}",
        json=body,
        headers={"x-api-key": _key(), "Content-Type": "application/json"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def people_search(domain: str, titles: list[str]) -> list[dict]:
    data = _post(
        "/mixed_people/search",
        {"q_organization_domains": domain, "person_titles": titles, "page": 1, "per_page": 10},
    )
    return [_shape(p) for p in data.get("people") or []]


def enrich(linkedin_url: str | None, email: str | None) -> dict | None:
    body = {}
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    if email:
        body["email"] = email
    person = _post("/people/match", body).get("person")
    return _shape(person) if person else None
