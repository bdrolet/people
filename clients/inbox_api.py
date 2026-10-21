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


def _from_block(from_address: str | None) -> dict:
    # An alias on the primary mailbox, so `shared` is false (inbox-api's `from` block).
    return {"from": {"address": from_address, "shared": False}} if from_address else {}


def _post(path: str, payload: dict) -> dict:
    base = _base()
    resp = httpx.post(
        f"{base}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def create_draft(
    to: list[str], subject: str, body: str = "", from_address: str | None = None
) -> dict:
    return _post(
        "/emails/drafts", {"to": to, "subject": subject, "body": body} | _from_block(from_address)
    )


def send_draft(draft_id: str, *, from_address: str | None) -> dict:
    """`{"status": "sent", "id": <sent message id>}`; older inbox-api deployments omit `id`."""
    return _post(f"/emails/drafts/{draft_id}/send", _from_block(from_address))


def reply(message_id: str, *, body: str, to: list[str], from_address: str | None) -> dict:
    """Reply on the thread to `to` only (never reply-all), sent at once."""
    payload = {"body": body, "to": to, "reply_all": False, "send": True}
    return _post(f"/emails/{message_id}/reply", payload | _from_block(from_address))


def search_sent(*, to: str, subject: str) -> list[dict]:
    """Graph search of the primary mailbox for a message to `to` with `subject`. Each hit
    carries `message_id`, `subject`, `sender` and `received_at`."""
    phrase = subject.replace('"', "")  # a quote inside would end the KQL phrase early
    payload = {
        "query": f'to:{to} subject:"{phrase}"',
        "mode": "graph",
        "mailboxes": ["me"],
        "limit": 10,
    }
    return _post("/search", payload).get("results", [])
