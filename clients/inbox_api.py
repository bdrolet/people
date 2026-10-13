"""inbox-api (Cloud Run, IAM-authenticated), used only by `outreach draft` (outreach
spec §7.3). People never talks to Microsoft Graph from its Cloud Functions; inbox-api is
the mailbox gateway. The caller names the `from` address: inbox-api's own default sender
is not the consulting address, the only identity whose sent mail counts as a brief."""

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


def create_draft(
    to: list[str], subject: str, body: str = "", from_address: str | None = None
) -> dict:
    base = _base()
    payload: dict = {"to": to, "subject": subject, "body": body}
    if from_address:
        # An alias on the primary mailbox, so `shared` is false (inbox-api's `from` block).
        payload["from"] = {"address": from_address, "shared": False}
    resp = httpx.post(
        f"{base}/emails/drafts",
        json=payload,
        headers={"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()
