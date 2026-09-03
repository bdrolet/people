"""Translate upstream HTTP failures (Google People API, HubSpot) into FastAPI
responses. Client-mistake statuses (400/403/404) pass through with the
upstream's message; everything else — 5xx, 429, timeouts — surfaces as 502."""

from contextlib import contextmanager
from typing import Iterator

import httpx
from fastapi import HTTPException


@contextmanager
def translate_upstream_errors(system: str) -> Iterator[None]:
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text[:200]
        if status in (400, 403, 404):
            raise HTTPException(status_code=status, detail=f"{system}: {detail}") from exc
        raise HTTPException(status_code=502, detail=f"{system} error {status}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{system} unreachable: {exc}") from exc
