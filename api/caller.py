"""Log the authenticated caller on every request.

Cloud Run IAM has already verified the ID token's signature and audience
before the request reached the container. This decodes the payload only to
record who called; it never rejects a request — that is IAM's job, and doing
it twice means two things to keep in sync. Off Cloud Run (K_SERVICE unset)
nothing is installed."""

import base64
import json
import logging
import os

from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)


def caller_email(authorization: str | None) -> str:
    """The `email` claim of a bearer JWT, or `-` when absent or undecodable."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return "-"
    parts = authorization.split(" ", 1)[1].strip().split(".")
    if len(parts) != 3:
        return "-"
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:  # binascii.Error, JSONDecodeError and UnicodeDecodeError all subclass it
        return "-"
    if not isinstance(claims, dict):
        return "-"
    return str(claims.get("email") or claims.get("sub") or "-")


def install(app: FastAPI) -> None:
    if not os.environ.get("K_SERVICE"):
        return

    @app.middleware("http")
    async def log_caller(request: Request, call_next):
        response = await call_next(request)
        logger.info(
            "%s %s %s caller=%s",
            request.method,
            request.url.path,
            response.status_code,
            caller_email(request.headers.get("authorization")),
        )
        return response
