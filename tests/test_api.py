from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

import clients.db as db
import repo.linkedin as linkedin_repo
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


def li_row(slug="alice-example", **kw):
    base = {
        "profile_url": f"linkedin.com/in/{slug}",
        "full_name": "Alice Example",
        "email": None,
        "company": "Example Health",
        "position": "CTO",
        "connected_on": date(2021, 4, 2),
        "person_email": "alice@x.com",
        "match_method": "name",
        "message_count": 14,
        "my_message_count": 6,
        "last_message_at": TS,
        "last_my_message_at": TS,
        "snapshot_at": TS,
    }
    base.update(kw)
    return base


def msg_row(conversation_id, sent_at, content="hi", **kw):
    base = {
        "conversation_id": conversation_id,
        "conversation_title": None,
        "sender_name": "Alice Example",
        "sender_profile_url": "linkedin.com/in/alice-example",
        "recipient_names": "Me Example",
        "sent_at": sent_at,
        "subject": None,
        "content": content,
        "folder": "INBOX",
        "from_me": False,
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
    monkeypatch.setattr(linkedin_repo, "connection_for_person", lambda conn, email: None)
    monkeypatch.setattr(
        linkedin_repo, "search_connections", lambda conn, q, limit: [li_row()] if "ali" in q else []
    )
    monkeypatch.setattr(linkedin_repo, "list_connections", lambda conn, **kw: [li_row()])
    monkeypatch.setattr(
        linkedin_repo,
        "get_connection",
        lambda conn, url: li_row() if url == "linkedin.com/in/alice-example" else None,
    )
    monkeypatch.setattr(linkedin_repo, "messages_for", lambda conn, url, limit: [])
    monkeypatch.setattr(linkedin_repo, "recommendations_for", lambda conn, url: [])
    monkeypatch.setattr(linkedin_repo, "latest_import", lambda conn: None)


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


def test_linkedin_connections_passes_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        linkedin_repo, "list_connections", lambda conn, **kw: (seen.update(kw), [li_row()])[1]
    )
    r = client.get(
        "/linkedin/connections?company=Example&min_messages=3&replied=false"
        "&quiet_since=2026-01-01&unmatched=true&limit=10"
    )
    assert r.status_code == 200
    assert seen == {
        "q": None,
        "company": "Example",
        "position": None,
        "min_messages": 3,
        "replied": False,
        "quiet_since": date(2026, 1, 1),
        "unmatched": True,
        "limit": 10,
    }
    body = r.json()["results"][0]
    assert body["profile_url"] == "linkedin.com/in/alice-example"
    assert body["person_email"] == "alice@x.com" and body["message_count"] == 14


def test_linkedin_connections_limit_bounds():
    assert client.get("/linkedin/connections?limit=501").status_code == 422
    assert client.get("/linkedin/connections?limit=0").status_code == 422


def test_linkedin_connection_detail_groups_conversations(monkeypatch):
    t1, t2, t3 = (datetime(2026, 1, d, tzinfo=UTC) for d in (1, 2, 3))
    seen = {}

    def messages_for(conn, url, limit):
        seen["args"] = (url, limit)
        return [
            msg_row("c2", t3, "newest"),
            msg_row("c1", t2, "middle"),
            msg_row("c2", t1, "oldest"),
        ]

    monkeypatch.setattr(linkedin_repo, "messages_for", messages_for)
    monkeypatch.setattr(
        linkedin_repo,
        "recommendations_for",
        lambda conn, url: [
            {"direction": "given", "full_name": "Alice Example", "company": None,
             "job_title": None, "text": "Great.", "status": "VISIBLE", "created_on": date(2025, 6, 1)}
        ],
    )  # fmt: skip
    r = client.get("/linkedin/connections/Alice-Example?messages_limit=5")
    assert r.status_code == 200
    assert seen["args"] == ("linkedin.com/in/alice-example", 5)
    body = r.json()
    assert body["full_name"] == "Alice Example" and body["recommendations"][0]["text"] == "Great."
    assert [c["conversation_id"] for c in body["conversations"]] == ["c2", "c1"]
    assert [m["content"] for m in body["conversations"][0]["messages"]] == ["newest", "oldest"]


def test_linkedin_connection_detail_404():
    assert client.get("/linkedin/connections/nobody").status_code == 404


def test_linkedin_imports_latest(monkeypatch):
    assert client.get("/linkedin/imports/latest").status_code == 404
    monkeypatch.setattr(
        linkedin_repo,
        "latest_import",
        lambda conn: {
            "snapshot_at": TS,
            "source": "Basic_Export",
            "connections": 6,
            "messages": 7,
            "recommendations_given": 2,
            "recommendations_received": 1,
            "matched_by_email": 1,
            "matched_by_name": 1,
        },
    )
    r = client.get("/linkedin/imports/latest")
    assert r.status_code == 200 and r.json()["connections"] == 6
