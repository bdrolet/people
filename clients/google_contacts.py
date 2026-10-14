"""Google People API v1 I/O. Credentials from env (Terraform injects the two
schedule-owned OAuth client secrets and people's own refresh token as env
vars). Built WITHOUT scopes — google-auth would send them on refresh and the
token server rejects scopes it did not explicitly grant (see schedule's
google_calendar.py)."""

import json
import logging
import os
import threading
import time
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.contact_fields import WRITABLE_FIELDS

logger = logging.getLogger(__name__)

PERSON_FIELDS = ",".join(sorted(WRITABLE_FIELDS | {"memberships", "biographies", "metadata"}))
_READ_MASK = PERSON_FIELDS

_RETRIES = 5
_sleep = time.sleep  # patched in tests


def _execute(request: Any, *, idempotent: bool = True) -> Any:
    """Run one People API request, retrying rate limits (linkedin-identity design §5).
    Idempotent requests use googleapiclient's own retry (HTTP 429 and 5xx, with
    backoff). A create or delete retries only HTTP 429, which Google returns before
    doing anything: after a 5xx the contact may already exist, and a retry would
    make a second one."""
    if idempotent:
        return request.execute(num_retries=_RETRIES)
    for attempt in range(_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status != 429 or attempt == _RETRIES:
                raise
            _sleep(2**attempt)
    raise AssertionError("unreachable")


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
        _execute(_svc().people().searchContacts(query="", readMask=_READ_MASK))
        _warmed = True


def search_by_email(email: str) -> dict | None:
    _warmup()
    resp = _execute(_svc().people().searchContacts(query=email, readMask=_READ_MASK, pageSize=5))
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
    return _execute(
        _svc().people().createContact(body=body, personFields=PERSON_FIELDS), idempotent=False
    )


def create_person(body: dict, group_resource_name: str | None) -> dict:
    """createContact with a caller-supplied body plus group membership. The
    eligibility path still uses create_contact (email-only body); this is for
    POST /people, where the caller has already built the full field map."""
    full_body: dict[str, Any] = dict(body)
    if group_resource_name:
        full_body["memberships"] = [
            {"contactGroupMembership": {"contactGroupResourceName": group_resource_name}}
        ]
    return _execute(
        _svc().people().createContact(body=full_body, personFields=PERSON_FIELDS), idempotent=False
    )


def get_person(resource_name: str) -> dict:
    return _execute(_svc().people().get(resourceName=resource_name, personFields=PERSON_FIELDS))


def update_fields(resource_name: str, etag: str, fields: dict) -> dict:
    """One updateContact for every field being changed (spec §5.4). Google
    rejects a stale etag, so callers pass the etag from a fresh get_person."""
    if not fields:
        return {}
    body: dict[str, Any] = {"etag": etag, **fields}
    return _execute(
        _svc()
        .people()
        .updateContact(
            resourceName=resource_name,
            updatePersonFields=",".join(sorted(fields)),
            personFields=PERSON_FIELDS,
            body=body,
        ),
        idempotent=False,
    )


def delete_person(resource_name: str) -> None:
    """Delete a contact outright. Only scripts/merge_duplicate_contacts.py calls
    this — nothing in the nightly sync or the API ever deletes a Google contact."""
    _execute(_svc().people().deleteContact(resourceName=resource_name), idempotent=False)


def list_groups() -> dict[str, dict]:
    out: dict[str, dict] = {}
    token = None
    while True:
        resp = _execute(_svc().contactGroups().list(pageSize=200, pageToken=token))
        for g in resp.get("contactGroups", []):
            out[g.get("formattedName") or g.get("name")] = {
                "resourceName": g["resourceName"],
                "groupType": g.get("groupType"),
            }
        token = resp.get("nextPageToken")
        if not token:
            return out


def list_groups_by_rn() -> dict[str, dict]:
    """Every contact group keyed by resourceName. list_groups() keys by name,
    where a user "Family" label and the built-in "Family" group collide; resource
    names never do (multiple-labels design §3)."""
    out: dict[str, dict] = {}
    token = None
    while True:
        resp = _execute(_svc().contactGroups().list(pageSize=200, pageToken=token))
        for g in resp.get("contactGroups", []):
            out[g["resourceName"]] = {
                "name": g.get("name"),
                "formattedName": g.get("formattedName"),
                "groupType": g.get("groupType"),
            }
        token = resp.get("nextPageToken")
        if not token:
            return out


def ensure_group(name: str) -> str:
    groups = list_groups()
    if name in groups:
        return groups[name]["resourceName"]
    created = _execute(
        _svc().contactGroups().create(body={"contactGroup": {"name": name}}), idempotent=False
    )
    return created["resourceName"]


def modify_group_members(group_resource_name: str, add: list[str], remove: list[str]) -> None:
    body: dict[str, Any] = {}
    if add:
        body["resourceNamesToAdd"] = add
    if remove:
        body["resourceNamesToRemove"] = remove
    if body:
        _execute(
            _svc().contactGroups().members().modify(resourceName=group_resource_name, body=body),
            idempotent=False,
        )


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
        resp = _execute(
            _svc()
            .people()
            .connections()
            .list(
                resourceName="people/me",
                personFields=_PHONE_FIELDS,
                pageSize=1000,
                pageToken=page_token,
            )
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


_URL_FIELDS = "names,urls,metadata"


def list_url_index() -> tuple[dict[str, list[dict]], list[dict]]:
    """(normalized LinkedIn profile URL -> the contacts carrying it, every contact),
    for the seed's and POST /people's duplicate checks (linkedin-identity design §5).
    A full listing like list_phone_index, because searchContacts does not index
    urls. Entries are {"resource_name", "display_name"}. No sync token: the nightly
    sync owns it."""
    # Layer-rule exception, as in list_phone_index: one normalizer, not two.
    from services.linkedin_export import normalize_profile_url

    by_url: dict[str, list[dict]] = {}
    contacts: list[dict] = []
    page_token = None
    while True:
        resp = _execute(
            _svc()
            .people()
            .connections()
            .list(
                resourceName="people/me",
                personFields=_URL_FIELDS,
                pageSize=1000,
                pageToken=page_token,
            )
        )
        for person in resp.get("connections", []):
            entry = {
                "resource_name": person["resourceName"],
                "display_name": (person.get("names") or [{}])[0].get("displayName"),
            }
            contacts.append(entry)
            for u in person.get("urls", []):
                url = normalize_profile_url(str(u.get("value") or ""))
                if url and entry not in by_url.setdefault(url, []):
                    by_url[url].append(entry)
        page_token = resp.get("nextPageToken")
        if not page_token:
            return by_url, contacts


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
            resp = _execute(_svc().people().connections().list(**kwargs))
        except HttpError as e:
            if _is_expired_sync_token(e):
                raise SyncTokenExpired() from e
            raise
        people_out.extend(resp.get("connections", []))
        next_sync = resp.get("nextSyncToken") or next_sync
        page_token = resp.get("nextPageToken")
        if not page_token:
            return people_out, next_sync
