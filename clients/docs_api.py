"""docs-api (Cloud Run, IAM-authenticated): fetch, create and replace the outreach
contact docs (contact-docs spec §3) and look up a filing-catalog folder. Every call
raises on failure. Local only: services/outreach_doc_write.py, run by
scripts/outreach_sessions.py, is the one caller. The URL is read per call so tests can
set it late; it is also the ID-token audience."""

import os

import httpx

from clients import gcp_auth

TIMEOUT_S = 60  # a live export of a doc holding 300 messages is slow


class DocsApiUnavailable(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    """409: the doc changed since the version the merge started from."""


class FolderNotFound(LookupError):
    pass


def configured() -> bool:
    return bool(os.environ.get("DOCS_API_URL", "").strip())


def _base() -> str:
    url = os.environ.get("DOCS_API_URL", "").strip().rstrip("/")
    if not url:
        raise DocsApiUnavailable("DOCS_API_URL unset (scripts/fetch-env.sh writes it)")
    return url


def _headers(base: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"}


def get_doc(doc_id: str) -> dict:
    """A live export: `content` (markdown) and `version` (often empty for a Google Doc)."""
    base = _base()
    resp = httpx.get(f"{base}/docs/gdrive/{doc_id}", headers=_headers(base), timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def create_doc(title: str, markdown: str, parent: str) -> dict:
    base = _base()
    resp = httpx.post(
        f"{base}/docs",
        json={"title": title, "markdown": markdown, "parent": parent},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def replace_doc(doc_id: str, markdown: str, if_version: str) -> dict:
    """A full-body replace. Deletes the doc's Google comments (docs-api has no comment
    endpoint); the doc says so in Ben's notes."""
    base = _base()
    resp = httpx.put(
        f"{base}/docs/gdrive/{doc_id}",
        json={"markdown": markdown, "if_version": if_version},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    if resp.status_code == 409:
        raise VersionConflict(doc_id)
    resp.raise_for_status()
    return resp.json()


def folder_id(entry_id: str) -> str:
    """The Drive folder behind a filing-catalog entry, so no Drive id is hard-coded."""
    base = _base()
    resp = httpx.get(f"{base}/folders", headers=_headers(base), timeout=TIMEOUT_S)
    resp.raise_for_status()
    for folder in resp.json().get("folders", []):
        if folder.get("id") == entry_id and folder.get("folder_id"):
            return str(folder["folder_id"])
    raise FolderNotFound(
        f"the filing catalog has no '{entry_id}' folder (docs repo filing/manifest.yaml)"
    )
