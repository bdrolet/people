from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import clients.db as db
import repo.people as people_repo
import services.google_contacts_sync as gsync
import services.person_edit as person_edit
from api.main import app

TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def row(email="alice@x.com", **kw):
    base = {
        "email": email,
        "display_name": "Alice",
        "first_seen": TS,
        "last_seen": TS,
        "last_contacted": None,
        "message_count": 3,
        "my_response_count": 1,
        "relationship_label": "family",
        "notes": None,
        "eligible": True,
        "automated": False,
        "google_resource_name": "people/c1",
        "google_etag": "e",
        "google_deleted_at": None,
        "hubspot_contact_id": "hs1",
        "hubspot_synced_at": TS,
        "updated_at": TS,
        "last_interaction": TS,
    }
    base.update(kw)
    return base


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.delenv("PEOPLE_API_TOKEN", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setattr(db, "get_conn", lambda: Conn())
    monkeypatch.setattr(
        people_repo, "get", lambda conn, email: row() if email == "alice@x.com" else None
    )
    monkeypatch.setattr(people_repo, "search", lambda conn, q, limit: [row()] if "ali" in q else [])
    monkeypatch.setattr(
        people_repo, "recent", lambda conn, limit, eligible_only=True: [row()][:limit]
    )


client = TestClient(app)


def test_get_person_shape():
    r = client.get("/people/alice@x.com")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "alice@x.com" and body["message_count"] == 3
    assert body["in_google_contacts"] is True and body["in_hubspot"] is True
    assert "google_resource_name" not in body and "hubspot_contact_id" not in body


def test_get_person_404():
    assert client.get("/people/nobody@x.com").status_code == 404


def test_get_person_normalizes_case():
    assert client.get("/people/Alice@X.com").status_code == 200


def test_search():
    r = client.post("/search", json={"q": "ali", "limit": 5})
    assert r.status_code == 200 and r.json()["results"][0]["email"] == "alice@x.com"
    assert client.post("/search", json={"q": "zzz"}).json()["results"] == []


def test_recent():
    r = client.get("/people?recent=1")
    assert r.status_code == 200 and len(r.json()["results"]) == 1


def test_patch_write_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        person_edit,
        "update",
        lambda conn, email, **kw: (seen.update(kw), row(notes=kw.get("notes")))[1],
    )
    r = client.patch("/people/alice@x.com", json={"notes": "hi"})
    assert r.status_code == 200 and seen == {"notes": "hi", "relationship_label": None}
    assert r.json()["notes"] == "hi"


def test_patch_blank_label_is_422():
    r = client.patch("/people/alice@x.com", json={"relationship_label": "   "})
    assert r.status_code == 422


def test_patch_not_linked_is_409(monkeypatch):
    def raise_unlinked(conn, email, **kw):
        raise person_edit.NotLinked(email)

    monkeypatch.setattr(person_edit, "update", raise_unlinked)
    assert client.patch("/people/alice@x.com", json={"notes": "hi"}).status_code == 409


def test_sync_one(monkeypatch):
    monkeypatch.setattr(gsync, "sync_one", lambda conn, r: row(display_name="Alice Updated"))
    r = client.post("/people/alice@x.com/sync")
    assert r.status_code == 200 and r.json()["display_name"] == "Alice Updated"


def test_auth_fails_closed_on_cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "people-api")
    assert client.get("/people/alice@x.com").status_code == 503


def test_auth_bearer(monkeypatch):
    monkeypatch.setenv("PEOPLE_API_TOKEN", "t0k")
    assert client.get("/people/alice@x.com").status_code == 401
    assert (
        client.get("/people/alice@x.com", headers={"Authorization": "Bearer t0k"}).status_code
        == 200
    )
