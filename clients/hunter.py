"""Hunter.io domain pattern, email finder and verifier (CLI-only). I/O only."""

import os

import httpx

TIMEOUT_S = 15
_BASE = "https://api.hunter.io/v2"
_STATUS = {
    "valid": "valid",
    "accept_all": "accept_all",
    "invalid": "invalid",
    "disposable": "invalid",
    "webmail": "invalid",
}


class HunterUnavailable(RuntimeError):
    pass


def _get(path: str, params: dict) -> dict:
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not key:
        raise HunterUnavailable("HUNTER_API_KEY unset")
    resp = httpx.get(f"{_BASE}{path}", params={**params, "api_key": key}, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json().get("data") or {}


def domain_pattern(domain: str) -> str | None:
    return _get("/domain-search", {"domain": domain}).get("pattern") or None


def find(domain: str, first_name: str, last_name: str) -> dict | None:
    data = _get(
        "/email-finder", {"domain": domain, "first_name": first_name, "last_name": last_name}
    )
    if not data.get("email"):
        return None
    return {"email": data["email"], "score": data.get("score")}


def verify(email: str) -> str:
    return _STATUS.get(_get("/email-verifier", {"email": email}).get("status"), "unknown")
