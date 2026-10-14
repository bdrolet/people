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
            def execute(self, num_retries=0):
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

        def execute(self, num_retries=0):
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
        def execute(self, num_retries=0):
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


def test_update_fields_does_not_retry_5xx(monkeypatch):
    seen = []

    class FakeReq:
        def execute(self, *args, **kwargs):
            seen.append((args, kwargs))
            return {}

    class FakePeople:
        def updateContact(self, **kw):
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(gc, "_svc", lambda: FakeService())
    gc.update_fields("people/c1", "etag-1", {"names": [{"givenName": "Alice"}]})
    assert seen == [((), {})]  # no num_retries: a 5xx after an applied write would 409


def test_update_fields_with_no_fields_does_not_call_google(monkeypatch):
    def boom():
        raise AssertionError("must not call Google")

    monkeypatch.setattr(gc, "_svc", boom)
    assert gc.update_fields("people/c1", "etag-1", {}) == {}


def test_create_person_sends_the_body_and_group(monkeypatch):
    calls = []

    class FakeReq:
        def execute(self, num_retries=0):
            return {"resourceName": "people/c1"}

    class FakePeople:
        def createContact(self, **kw):
            calls.append(kw)
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(gc, "_svc", lambda: FakeService())
    gc.create_person({"phoneNumbers": [{"value": "+15550100001"}]}, "contactGroups/abc")
    body = calls[0]["body"]
    assert body["phoneNumbers"] == [{"value": "+15550100001"}]
    assert body["memberships"] == [
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}}
    ]
    assert calls[0]["personFields"] == gc.PERSON_FIELDS


def test_create_person_without_a_group_sends_no_membership(monkeypatch):
    calls = []

    class FakeReq:
        def execute(self, num_retries=0):
            return {"resourceName": "people/c1"}

    class FakePeople:
        def createContact(self, **kw):
            calls.append(kw)
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(gc, "_svc", lambda: FakeService())
    gc.create_person({"names": [{"givenName": "Alice"}]}, None)
    assert "memberships" not in calls[0]["body"]


def test_list_groups_by_rn_pages_and_keeps_colliding_names(monkeypatch):
    pages = [
        {
            "contactGroups": [
                {
                    "resourceName": "contactGroups/family",
                    "name": "family",
                    "formattedName": "Family",
                    "groupType": "SYSTEM_CONTACT_GROUP",
                },
            ],
            "nextPageToken": "p2",
        },
        {
            "contactGroups": [
                {
                    "resourceName": "contactGroups/fam1",
                    "name": "Family",
                    "formattedName": "Family",
                    "groupType": "USER_CONTACT_GROUP",
                },
            ]
        },
    ]
    tokens = []

    class Req:
        def __init__(self, page):
            self.page = page

        def execute(self, num_retries=0):
            return self.page

    class Groups:
        def list(self, pageSize, pageToken):
            tokens.append(pageToken)
            return Req(pages[len(tokens) - 1])

    class Svc:
        def contactGroups(self):
            return Groups()

    monkeypatch.setattr(gc, "_svc", lambda: Svc())
    got = gc.list_groups_by_rn()
    assert tokens == [None, "p2"]
    assert set(got) == {"contactGroups/family", "contactGroups/fam1"}
    assert got["contactGroups/fam1"] == {
        "name": "Family",
        "formattedName": "Family",
        "groupType": "USER_CONTACT_GROUP",
    }


class _Req:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def execute(self, num_retries=0):
        self.calls.append(num_retries)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_an_idempotent_request_lets_googleapiclient_retry():
    req = _Req([{"ok": 1}])
    assert gc._execute(req) == {"ok": 1}
    assert req.calls == [gc._RETRIES]


def test_a_create_retries_only_rate_limits(monkeypatch):
    slept = []
    monkeypatch.setattr(gc, "_sleep", slept.append)
    req = _Req([_http_error(429, None), _http_error(429, None), {"resourceName": "people/c1"}])
    assert gc._execute(req, idempotent=False) == {"resourceName": "people/c1"}
    assert req.calls == [0, 0, 0] and slept == [1, 2]


def test_a_create_never_retries_a_server_error(monkeypatch):
    # A 5xx may have created the contact already; retrying could make a second one.
    monkeypatch.setattr(gc, "_sleep", lambda s: pytest.fail("must not retry"))
    with pytest.raises(HttpError):
        gc._execute(_Req([_http_error(500, None)]), idempotent=False)


def test_a_create_gives_up_after_the_last_retry(monkeypatch):
    monkeypatch.setattr(gc, "_sleep", lambda s: None)
    req = _Req([_http_error(429, None)] * (gc._RETRIES + 1))
    with pytest.raises(HttpError):
        gc._execute(req, idempotent=False)
    assert len(req.calls) == gc._RETRIES + 1


def test_list_url_index_normalizes_pages_and_keeps_every_contact(monkeypatch):
    pages = [
        {
            "connections": [
                {
                    "resourceName": "people/c1",
                    "names": [{"displayName": "Jane Example"}],
                    "urls": [
                        {"value": "https://www.linkedin.com/in/Jane-Example/"},
                        {"value": "https://www.linkedin.com/company/acme"},
                    ],
                },
                {"resourceName": "people/c2", "names": [{"displayName": "Lee Example"}]},
            ],
            "nextPageToken": "p2",
        },
        {
            "connections": [
                {"resourceName": "people/c3", "urls": [{"value": "linkedin.com/in/jane-example"}]}
            ]
        },
    ]
    seen = []

    class _List:
        def __init__(self, kw):
            self.kw = kw

        def execute(self, num_retries=0):
            seen.append((self.kw.get("pageToken"), self.kw["personFields"], num_retries))
            return pages[len(seen) - 1]

    class _Connections:
        def list(self, **kw):
            return _List(kw)

    class _People:
        def connections(self):
            return _Connections()

    class _Svc:
        def people(self):
            return _People()

    monkeypatch.setattr(gc, "_svc", lambda: _Svc())
    by_url, contacts = gc.list_url_index()
    assert by_url == {
        "linkedin.com/in/jane-example": [
            {"resource_name": "people/c1", "display_name": "Jane Example"},
            {"resource_name": "people/c3", "display_name": None},
        ]
    }
    assert [c["resource_name"] for c in contacts] == ["people/c1", "people/c2", "people/c3"]
    assert seen == [
        (None, "names,urls,metadata", gc._RETRIES),
        ("p2", "names,urls,metadata", gc._RETRIES),
    ]


def test_a_membership_change_retries_an_aborted_group(monkeypatch):
    # Google answers back-to-back changes to one contact group with 409 ABORTED.
    # Adding or removing a member is safe to repeat, so it is retried.
    slept = []
    monkeypatch.setattr(gc, "_sleep", slept.append)
    req = _Req([_http_error(409, None), _http_error(503, None), {}])

    class _Members:
        def modify(self, **kw):
            return req

    class _Groups:
        def members(self):
            return _Members()

    class _Svc:
        def contactGroups(self):
            return _Groups()

    monkeypatch.setattr(gc, "_svc", lambda: _Svc())
    gc.modify_group_members("contactGroups/abc", ["people/c1"], [])
    assert len(req.calls) == 3 and slept == [1, 2]


def test_a_create_does_not_retry_an_aborted_409(monkeypatch):
    monkeypatch.setattr(gc, "_sleep", lambda s: pytest.fail("must not retry"))
    with pytest.raises(HttpError):
        gc._execute(_Req([_http_error(409, None)]), idempotent=False)
