"""inbox-api (Cloud Run, IAM-authenticated), used only by `outreach draft` (outreach
spec §7.3). People never talks to Microsoft Graph from its Cloud Functions; inbox-api is
the mailbox gateway. Drafts carry no `from` block, so they come from the primary
mailbox, which is the consulting address, the only identity whose sent mail counts as
a brief."""

import os

import httpx

from clients import gcp_auth

TIMEOUT_S = 10


class InboxApiUnavailable(RuntimeError):
    pass


def _base() -> str:
    url = os.environ.get("INBOX_API_URL", "").strip().rstrip("/")
    if not url:
        raise InboxApiUnavailable("INBOX_API_URL unset")
    return url


def create_draft(to: list[str], subject: str, body: str = "") -> dict:
    base = _base()
    resp = httpx.post(
        f"{base}/emails/drafts",
        json={"to": to, "subject": subject, "body": body},
        headers={"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()
