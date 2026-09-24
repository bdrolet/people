"""Google People API v1 I/O. Credentials from env (Terraform injects the two
schedule-owned OAuth client secrets and people's own refresh token as env
vars). Built WITHOUT scopes — google-auth would send them on refresh and the
token server rejects scopes it did not explicitly grant (see schedule's
google_calendar.py)."""

import json
import logging
import os
import threading
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.contact_fields import WRITABLE_FIELDS

logger = logging.getLogger(__name__)

PERSON_FIELDS = ",".join(sorted(WRITABLE_FIELDS | {"memberships", "biographies", "metadata"}))
_READ_MASK = PERSON_FIELDS


class SyncTokenExpired(Exception):
    pass


_credentials: Credentials | None = None
_local = threading.local()
_warmed = False


def _creds() -> Credentials:
    global _credentials
    if _credentials is None:
        _credentials = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        )
    return _credentials


def _svc() -> Any:
    svc = getattr(_local, "svc", None)
    if svc is None:
        svc = build("people", "v1", credentials=_creds(), cache_discovery=False)
        _local.svc = svc
    return svc


def _warmup() -> None:
    """searchContacts requires a warm-up request with an empty query before
    the first real search in a session, else results may be stale/empty."""
    global _warmed
    if not _warmed:
        _svc().people().searchContacts(query="", readMask=_READ_MASK).execute()
        _warmed = True


def search_by_email(email: str) -> dict | None:
    _warmup()
    resp = _svc().people().searchContacts(query=email, readMask=_READ_MASK, pageSize=5).execute()
    for r in resp.get("results", []):
        person = r.get("person", {})
        for e in person.get("emailAddresses", []):
            if (e.get("value") or "").strip().lower() == email.lower():
                return person
    return None


def create_contact(display_name: str | None, email: str, group_resource_name: str | None) -> dict:
    body: dict[str, Any] = {"emailAddresses": [{"value": email}]}
    if display_name:
        parts = display_name.strip().split(" ", 1)
        name = {"givenName": parts[0]}
        if len(parts) > 1:
            name["familyName"] = parts[1]
        body["names"] = [name]
    if group_resource_name:
        body["memberships"] = [
            {"contactGroupMembership": {"contactGroupResourceName": group_resource_name}}
        ]
    return _svc().people().createContact(body=body, personFields=PERSON_FIELDS).execute()


def get_person(resource_name: str) -> dict:
    return _svc().people().get(resourceName=resource_name, personFields=PERSON_FIELDS).execute()


def update_fields(resource_name: str, etag: str, fields: dict) -> dict:
    """One updateContact for every field being changed (spec §5.4). Google
    rejects a stale etag, so callers pass the etag from a fresh get_person."""
    if not fields:
        return {}
    body: dict[str, Any] = {"etag": etag, **fields}
    return (
        _svc()
        .people()
        .updateContact(
            resourceName=resource_name,
            updatePersonFields=",".join(sorted(fields)),
            personFields=PERSON_FIELDS,
            body=body,
        )
        .execute()
    )


def list_groups() -> dict[str, dict]:
    out: dict[str, dict] = {}
    token = None
    while True:
        resp = _svc().contactGroups().list(pageSize=200, pageToken=token).execute()
        for g in resp.get("contactGroups", []):
            out[g.get("formattedName") or g.get("name")] = {
                "resourceName": g["resourceName"],
                "groupType": g.get("groupType"),
            }
        token = resp.get("nextPageToken")
        if not token:
            return out


def ensure_group(name: str) -> str:
    groups = list_groups()
    if name in groups:
        return groups[name]["resourceName"]
    created = _svc().contactGroups().create(body={"contactGroup": {"name": name}}).execute()
    return created["resourceName"]


def modify_group_members(group_resource_name: str, add: list[str], remove: list[str]) -> None:
    body: dict[str, Any] = {}
    if add:
        body["resourceNamesToAdd"] = add
    if remove:
        body["resourceNamesToRemove"] = remove
    if body:
        _svc().contactGroups().members().modify(
            resourceName=group_resource_name, body=body
        ).execute()


def _is_expired_sync_token(e: HttpError) -> bool:
    """The People API signals an expired syncToken with HTTP 400 and
    reason=EXPIRED_SYNC_TOKEN, not the 410 GONE that the Calendar API uses for
    the same condition. Accept both: 410 costs nothing and only ever means
    this."""
    if e.resp.status == 410:
        return True
    if e.resp.status != 400:
        return False
    try:
        details = json.loads(e.content)["error"].get("details", [])
    except (ValueError, KeyError, TypeError):
        return False
    return any(d.get("reason") == "EXPIRED_SYNC_TOKEN" for d in details)


_PHONE_FIELDS = "names,phoneNumbers,metadata"


def list_phone_index(region: str = "US") -> dict[str, list[dict]]:
    """Every contact's phone numbers, E.164-normalized, for the iMessage import
    (spec §5.4). Deliberately does NOT request or use a sync token: the nightly
    sync in repo/sync_state.py owns that token. This is a full listing every
    time, on its own field mask, independent of list_connections."""
    # Layer-rule exception, adjudicated: clients/ normally never imports
    # services/, but duplicating E.164 normalization here would be worse than
    # this one function-local import.
    from services.imessage_export import normalize_handle

    index: dict[str, list[dict]] = {}
    page_token = None
    while True:
        resp = (
            _svc()
            .people()
            .connections()
            .list(
                resourceName="people/me",
                personFields=_PHONE_FIELDS,
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        for person in resp.get("connections", []):
            entry = {
                "resource_name": person["resourceName"],
                "display_name": (person.get("names") or [{}])[0].get("displayName"),
            }
            for number in person.get("phoneNumbers", []):
                e164 = normalize_handle(number.get("value", ""), region=region)
                if e164:
                    index.setdefault(e164, []).append(entry)
        page_token = resp.get("nextPageToken")
        if not page_token:
            return index


def list_connections(sync_token: str | None) -> tuple[list[dict], str]:
    """Full or incremental listing. Raises SyncTokenExpired when the token has
    aged out, which run_sync recovers from by retrying with no token."""
    people_out: list[dict] = []
    page_token = None
    next_sync = ""
    while True:
        kwargs: dict[str, Any] = {
            "resourceName": "people/me",
            "personFields": PERSON_FIELDS,
            "pageSize": 1000,
            "requestSyncToken": True,
        }
        if sync_token:
            kwargs["syncToken"] = sync_token
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = _svc().people().connections().list(**kwargs).execute()
        except HttpError as e:
            if _is_expired_sync_token(e):
                raise SyncTokenExpired() from e
            raise
        people_out.extend(resp.get("connections", []))
        next_sync = resp.get("nextSyncToken") or next_sync
        page_token = resp.get("nextPageToken")
        if not page_token:
            return people_out, next_sync
