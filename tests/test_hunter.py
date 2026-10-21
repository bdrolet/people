import httpx
import pytest

from clients import hunter


def _fake_get(monkeypatch, payload):
    seen = {}

    def get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers, timeout=timeout)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(hunter.httpx, "get", get)
    return seen


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "hk")


def test_domain_pattern(monkeypatch):
    seen = _fake_get(monkeypatch, {"data": {"pattern": "{first}"}})
    assert hunter.domain_pattern("acme.com") == "{first}"
    assert seen["url"] == "https://api.hunter.io/v2/domain-search"
    assert seen["params"] == {"domain": "acme.com"}
    assert seen["timeout"] == 15
    assert seen["headers"] == {"X-API-KEY": "hk"}
    _fake_get(monkeypatch, {"data": {"pattern": None}})
    assert hunter.domain_pattern("acme.com") is None


def test_find(monkeypatch):
    seen = _fake_get(monkeypatch, {"data": {"email": "ada@acme.com", "score": 91}})
    assert hunter.find("acme.com", "Ada", "L") == {"email": "ada@acme.com", "score": 91}
    assert seen["url"] == "https://api.hunter.io/v2/email-finder"
    assert seen["params"] == {
        "domain": "acme.com",
        "first_name": "Ada",
        "last_name": "L",
    }
    _fake_get(monkeypatch, {"data": {"email": None, "score": 0}})
    assert hunter.find("acme.com", "Ada", "L") is None


@pytest.mark.parametrize(
    "raw,mapped",
    [
        ("valid", "valid"),
        ("accept_all", "accept_all"),
        ("invalid", "invalid"),
        ("disposable", "invalid"),
        ("webmail", "invalid"),
        ("unknown", "unknown"),
        ("blocked", "unknown"),
        (None, "unknown"),
    ],
)
def test_verify_maps_status(monkeypatch, raw, mapped):
    seen = _fake_get(monkeypatch, {"data": {"status": raw}})
    assert hunter.verify("a@acme.com") == mapped
    assert seen["url"] == "https://api.hunter.io/v2/email-verifier"
    assert seen["params"] == {"email": "a@acme.com"}


def test_unset_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY")
    with pytest.raises(hunter.HunterUnavailable):
        hunter.verify("a@acme.com")


def test_401_error_message_does_not_leak_the_key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "SECRETKEY")

    def get(url, params, headers, timeout):
        req = httpx.Request("GET", url, params=params)
        resp = httpx.Response(401, request=req)
        resp.raise_for_status = lambda: httpx.Response(401, request=req).raise_for_status()
        return resp

    monkeypatch.setattr(hunter.httpx, "get", get)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        hunter.verify("a@acme.com")
    assert "SECRETKEY" not in str(ei.value)
    assert "a%40acme.com" in str(ei.value) or "a@acme.com" in str(ei.value)
