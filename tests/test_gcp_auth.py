import base64
import json
import time

from clients import gcp_auth


def _token(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_tokens_are_cached_per_audience_until_near_expiry(monkeypatch):
    gcp_auth.reset_cache()
    minted = []

    def mint(aud):
        minted.append(aud)
        return _token(time.time() + 3600)

    monkeypatch.setattr(gcp_auth, "_mint", mint)
    gcp_auth.id_token_for("https://a")
    gcp_auth.id_token_for("https://a")
    gcp_auth.id_token_for("https://b")
    assert minted == ["https://a", "https://b"]


def test_a_token_inside_the_refresh_margin_is_reminted(monkeypatch):
    gcp_auth.reset_cache()
    tokens = iter([_token(time.time() + 60), _token(time.time() + 3600)])
    monkeypatch.setattr(gcp_auth, "_mint", lambda aud: next(tokens))
    first = gcp_auth.id_token_for("https://a")
    assert gcp_auth.id_token_for("https://a") != first
