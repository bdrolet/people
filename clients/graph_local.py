"""Local-only Graph reader for scripts/import_contacts.py. Device-code MSAL
with its own cache file (~/.people-token-cache.json) — never the shared
msal-token-cache secret. Public client: CLIENT_ID + TENANT_ID, no secret."""

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import msal
import requests

_CACHE = Path.home() / ".people-token-cache.json"
_SCOPES = ["Mail.Read"]
_GRAPH = "https://graph.microsoft.com/v1.0"


class GraphLocal:
    def __init__(self) -> None:
        self._token: str | None = None

    def authenticate(self) -> None:
        cache = msal.SerializableTokenCache()
        if _CACHE.exists():
            cache.deserialize(_CACHE.read_text())
        app = msal.PublicClientApplication(
            os.environ["CLIENT_ID"],
            authority=f"https://login.microsoftonline.com/{os.environ['TENANT_ID']}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        result = app.acquire_token_silent(_SCOPES, account=accounts[0]) if accounts else None
        if not result:
            flow = app.initiate_device_flow(scopes=_SCOPES)
            print(flow["message"])
            result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(json.dumps(result))
        self._token = result["access_token"]
        if cache.has_state_changed:
            _CACHE.write_text(cache.serialize())

    def iter_messages(self, folder: str, since: datetime) -> Iterator[dict]:
        """Yields normalized dicts: folder, from, from_name, to, cc, received_at, sent_at."""
        url = (
            f"{_GRAPH}/me/mailFolders/{folder}/messages?$top=100"
            f"&$select=from,toRecipients,ccRecipients,receivedDateTime,sentDateTime"
            f"&$filter=receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&$orderby=receivedDateTime desc"
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        while url:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            for m in data.get("value", []):
                frm = (m.get("from") or {}).get("emailAddress") or {}
                yield {
                    "folder": folder,
                    "from": (frm.get("address") or "").lower(),
                    "from_name": frm.get("name"),
                    "to": [
                        r["emailAddress"]["address"].lower()
                        for r in m.get("toRecipients", [])
                        if r.get("emailAddress", {}).get("address")
                    ],
                    "cc": [
                        r["emailAddress"]["address"].lower()
                        for r in m.get("ccRecipients", [])
                        if r.get("emailAddress", {}).get("address")
                    ],
                    "received_at": m.get("receivedDateTime"),
                    "sent_at": m.get("sentDateTime"),
                    "category": "review",  # historical mail was never classified; treat as non-ignore
                }
            url = data.get("@odata.nextLink")
