"""Google-signed ID tokens for calling the other Cloud Run APIs.

On Cloud Run / Cloud Functions (K_SERVICE set) the metadata server mints a
token for this workload's own service account. Off it, gcloud's user
credential is used, so local runs hit the real APIs as the developer.
Tokens are cached per audience and refreshed five minutes before `exp`;
ID tokens last an hour and the metadata server rate-limits."""

import base64
import json
import os
import subprocess
import time

REFRESH_MARGIN_S = 300
_cache: dict[str, tuple[str, float]] = {}


def _exp(token: str) -> float:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])


def _mint(audience: str) -> str:
    if os.environ.get("K_SERVICE"):
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(request, audience)
    return subprocess.check_output(["gcloud", "auth", "print-identity-token"], text=True).strip()


def id_token_for(audience: str) -> str:
    """A bearer token accepted by the Cloud Run service whose URL is `audience`."""
    cached = _cache.get(audience)
    if cached and cached[1] - time.time() > REFRESH_MARGIN_S:
        return cached[0]
    token = _mint(audience)
    _cache[audience] = (token, _exp(token))
    return token


def reset_cache() -> None:
    _cache.clear()
