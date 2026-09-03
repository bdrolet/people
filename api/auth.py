"""Bearer-token auth for people-api. Single static token (people-api-token
secret); per-client tokens are a later concern. No-op when PEOPLE_API_TOKEN
is unset so local dev and tests run without auth — but only off Cloud Run
(K_SERVICE unset); on Cloud Run an unset token is a misconfiguration, not an
invitation to anonymous access, so it fails closed with 503."""

import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    expected = os.environ.get("PEOPLE_API_TOKEN")
    if not expected:
        if os.environ.get("K_SERVICE"):
            raise HTTPException(status_code=503, detail="service misconfigured: no auth token")
        return
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401)
