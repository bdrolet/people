"""Client-level tests for clients/google_contacts.py.

The sync-token expiry translation is the important one: run_sync's recovery
path is keyed on gc.SyncTokenExpired, so if the client fails to translate
Google's real error the nightly sync dies instead of falling back to a full
resync. tests/test_google_contacts_sync.py raises SyncTokenExpired from a
stub, which tests the handler but never this translation.
"""

import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

import clients.google_contacts as gc

# Verbatim body the People API returns for an expired syncToken, captured from
# a real people/me/connections call (HTTP 400, not the 410 the Calendar API
# uses for the same condition).
EXPIRED_BODY = {
    "error": {
        "code": 400,
        "message": (
            "Sync token is expired. Clear local cache and retry call without the sync token."
        ),
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "EXPIRED_SYNC_TOKEN",
                "domain": "people.googleapis.com",
            }
        ],
    }
}

OTHER_400_BODY = {
    "error": {
        "code": 400,
        "message": "Invalid personFields mask path: 'nope'.",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "INVALID_ARGUMENT",
                "domain": "people.googleapis.com",
            }
        ],
    }
}


def _http_error(status: int, body: dict | None) -> HttpError:
    content = json.dumps(body).encode() if body is not None else b""
    return HttpError(httplib2.Response({"status": status}), content, uri="https://example/test")


@pytest.fixture
def raising_svc(monkeypatch):
    """Stub _svc() so connections().list().execute() raises the given error."""

    def _install(error: HttpError):
        class _List:
            def execute(self):
                raise error

        class _Connections:
            def list(self, **kwargs):
                return _List()

        class _People:
            def connections(self):
                return _Connections()

        class _Svc:
            def people(self):
                return _People()

        monkeypatch.setattr(gc, "_svc", lambda: _Svc())

    return _install


def test_list_connections_translates_expired_sync_token_400(raising_svc):
    raising_svc(_http_error(400, EXPIRED_BODY))
    with pytest.raises(gc.SyncTokenExpired):
        gc.list_connections("stale-token")


def test_list_connections_translates_expired_sync_token_410(raising_svc):
    raising_svc(_http_error(410, None))
    with pytest.raises(gc.SyncTokenExpired):
        gc.list_connections("stale-token")


def test_list_connections_propagates_unrelated_400(raising_svc):
    raising_svc(_http_error(400, OTHER_400_BODY))
    with pytest.raises(HttpError) as exc:
        gc.list_connections("stale-token")
    assert not isinstance(exc.value, gc.SyncTokenExpired)


def test_list_phone_index_groups_contacts_by_normalized_number(monkeypatch):
    pages = [
        {
            "connections": [
                {
                    "resourceName": "people/c1",
                    "names": [{"displayName": "Alice Example"}],
                    "phoneNumbers": [{"value": "(555) 010-0001"}, {"value": "+1 555 010 0003"}],
                },
                {
                    "resourceName": "people/c2",
                    "names": [{"displayName": "Alias Example"}],
                    "phoneNumbers": [{"value": "555-010-0001"}],
                },
            ],
            "nextPageToken": "p2",
        },
        {
            "connections": [
                {"resourceName": "people/c3", "names": [{"displayName": "No Phone"}]},
            ]
        },
    ]
    calls = []

    class FakeReq:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class FakeConnections:
        def list(self, **kw):
            calls.append(kw)
            return FakeReq(pages[len(calls) - 1])

    class FakePeople:
        def connections(self):
            return FakeConnections()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(gc, "_svc", lambda: FakeService())

    index = gc.list_phone_index()
    assert sorted(e["resource_name"] for e in index["+15550100001"]) == ["people/c1", "people/c2"]
    assert index["+15550100003"] == [
        {"resource_name": "people/c1", "display_name": "Alice Example"}
    ]
    assert all("syncToken" not in c and not c.get("requestSyncToken") for c in calls)
    assert calls[0]["personFields"] == "names,phoneNumbers,metadata"
    assert calls[1]["pageToken"] == "p2"


def test_person_fields_covers_the_writable_allowlist():
    from services.contact_fields import OWNED_ELSEWHERE, WRITABLE_FIELDS

    mask = set(gc.PERSON_FIELDS.split(","))
    assert WRITABLE_FIELDS <= mask
    assert {"memberships", "biographies", "metadata"} <= mask  # still read
    assert "photos" not in mask
    assert "biographies" in OWNED_ELSEWHERE


def test_update_fields_sends_one_call_with_a_joined_mask(monkeypatch):
    calls = []

    class FakeReq:
        def execute(self):
            return {"resourceName": "people/c1"}

    class FakePeople:
        def updateContact(self, **kw):
            calls.append(kw)
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(gc, "_svc", lambda: FakeService())
    gc.update_fields(
        "people/c1",
        "etag-1",
        {"phoneNumbers": [{"value": "+15550100001"}], "names": [{"givenName": "Alice"}]},
    )
    assert len(calls) == 1
    assert calls[0]["updatePersonFields"] == "names,phoneNumbers"  # sorted, comma-joined
    assert calls[0]["body"]["etag"] == "etag-1"
    assert calls[0]["body"]["names"] == [{"givenName": "Alice"}]
    assert calls[0]["personFields"] == gc.PERSON_FIELDS


def test_update_fields_with_no_fields_does_not_call_google(monkeypatch):
    def boom():
        raise AssertionError("must not call Google")

    monkeypatch.setattr(gc, "_svc", boom)
    assert gc.update_fields("people/c1", "etag-1", {}) == {}
