from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

import clients.db as db
import repo.imessage as imessage_repo
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


def person_row_with_contact_fields(**kw):
    base = row(
        phone_numbers=["+15550100001"],
        company="Example Health",
        job_title="CTO",
        google_fields={
            "names": [{"givenName": "Alice"}],
            "phoneNumbers": [{"value": "+15550100001"}],
        },
    )
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


def imessage_summary_row(**kw):
    base = {
        "handles": ["+15550100001"],
        "message_count": 212,
        "my_message_count": 98,
        "last_message_at": TS,
        "last_my_message_at": TS,
        "group_message_count": 40,
        "last_group_message_at": TS,
        "imported_at": TS,
    }
    base.update(kw)
    return base


def handle_row(handle="+15550100001", **kw):
    base = {
        "handle": handle,
        "display_name": "Alice",
        "google_resource_name": None,
        "person_email": "alice@x.com",
        "match_method": "email",
        "message_count": 212,
        "my_message_count": 98,
        "last_message_at": TS,
        "last_my_message_at": TS,
        "group_message_count": 40,
        "last_group_message_at": TS,
    }
    base.update(kw)
    return base


def group_row(**kw):
    base = {
        "chat_guid": "chat1",
        "display_name": "Fixture Group",
        "participant_count": 4,
        "message_count": 30,
        "last_message_at": TS,
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
    monkeypatch.setattr(imessage_repo, "summary_for_person", lambda conn, email: None)
    monkeypatch.setattr(
        imessage_repo, "search_handles", lambda conn, q, limit: [handle_row()] if "ali" in q else []
    )
    monkeypatch.setattr(imessage_repo, "handles", lambda conn, **kw: [handle_row()])
    monkeypatch.setattr(
        imessage_repo,
        "handle",
        lambda conn, handle: handle_row(handle) if handle == "+15550100001" else None,
    )
    monkeypatch.setattr(imessage_repo, "handle_groups", lambda conn, handle: [group_row()])
    monkeypatch.setattr(imessage_repo, "latest_import", lambda conn: None)


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
    assert r.status_code == 200
    assert seen == {"notes": "hi", "relationship_label": None, "contact": None}
    assert r.json()["notes"] == "hi"


def test_patch_blank_label_is_422():
    r = client.patch("/people/alice@x.com", json={"relationship_label": "   "})
    assert r.status_code == 422


def test_patch_not_linked_is_409(monkeypatch):
    def raise_unlinked(conn, email, **kw):
        raise person_edit.NotLinked(email)

    monkeypatch.setattr(person_edit, "update", raise_unlinked)
    assert client.patch("/people/alice@x.com", json={"notes": "hi"}).status_code == 409


def test_patch_accepts_contact_fields(monkeypatch):
    monkeypatch.setattr(
        person_edit,
        "update",
        lambda conn, email, **kw: person_row_with_contact_fields(),
    )
    body = client.patch(
        "/people/alice@x.com", json={"contact": {"phoneNumbers": [{"value": "+15550100001"}]}}
    ).json()
    assert body["phone_numbers"] == ["+15550100001"]
    assert body["company"] == "Example Health"
    assert body["contact"]["phoneNumbers"][0]["value"] == "+15550100001"


def test_patch_rejected_field_is_400(monkeypatch):
    def raise_invalid(conn, email, **kw):
        raise person_edit.Invalid("unknown or unwritable field(s): photos")

    monkeypatch.setattr(person_edit, "update", raise_invalid)
    r = client.patch("/people/alice@x.com", json={"contact": {"photos": []}})
    assert r.status_code == 400
    assert "photos" in r.text


def test_patch_email_removal_is_409(monkeypatch):
    def raise_conflict(conn, email, **kw):
        raise person_edit.Conflict("removals and changes go through the Google UI")

    monkeypatch.setattr(person_edit, "update", raise_conflict)
    r = client.patch(
        "/people/alice@x.com", json={"contact": {"emailAddresses": [{"value": "other@x.com"}]}}
    )
    assert r.status_code == 409


def test_stale_etag_is_409(monkeypatch):
    # Review Focus 5 at the transport layer.
    def raise_conflict(conn, email, **kw):
        raise person_edit.Conflict("contact changed meanwhile; retry")

    monkeypatch.setattr(person_edit, "update", raise_conflict)
    r = client.patch("/people/alice@x.com", json={"contact": {"names": [{"givenName": "A"}]}})
    assert r.status_code == 409


def test_person_detail_carries_contact_fields(monkeypatch):
    monkeypatch.setattr(people_repo, "get", lambda conn, email: person_row_with_contact_fields())
    body = client.get("/people/alice@x.com").json()
    assert body["phone_numbers"] == ["+15550100001"]
    assert body["job_title"] == "CTO"
    assert body["contact"]["names"][0]["givenName"] == "Alice"


def test_list_omits_the_contact_blob_but_keeps_typed_columns(monkeypatch):
    monkeypatch.setattr(
        people_repo,
        "recent",
        lambda conn, limit, eligible_only=True: [person_row_with_contact_fields()],
    )
    row_out = client.get("/people?recent=5").json()["results"][0]
    assert row_out["contact"] is None
    assert row_out["company"] == "Example Health"


def test_search_matches_on_company(monkeypatch):
    monkeypatch.setattr(
        people_repo, "search", lambda conn, q, limit: [person_row_with_contact_fields()]
    )
    body = client.post("/search", json={"q": "Example Health"}).json()
    assert body["results"][0]["company"] == "Example Health"


def test_sync_one(monkeypatch):
    monkeypatch.setattr(gsync, "sync_one", lambda conn, r: row(display_name="Alice Updated"))
    r = client.post("/people/alice@x.com/sync")
    assert r.status_code == 200 and r.json()["display_name"] == "Alice Updated"


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


def test_linkedin_connection_detail_percent_encoded_non_ascii_slug(monkeypatch):
    seen = {}

    def get_connection(conn, url):
        seen["url"] = url
        return None

    monkeypatch.setattr(linkedin_repo, "get_connection", get_connection)
    r = client.get("/linkedin/connections/J%C3%B6rg-Example")
    assert r.status_code == 404
    assert seen["url"] == "linkedin.com/in/jörg-example"


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


def test_get_person_linkedin_absent_is_null():
    assert client.get("/people/alice@x.com").json()["linkedin"] is None


def test_get_person_linkedin_present(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        linkedin_repo,
        "connection_for_person",
        lambda conn, email: (seen.setdefault("email", email), li_row())[1],
    )
    body = client.get("/people/Alice@X.com").json()
    assert seen["email"] == "alice@x.com"
    assert body["linkedin"]["profile_url"] == "linkedin.com/in/alice-example"
    assert body["linkedin"]["my_message_count"] == 6
    assert "full_name" not in body["linkedin"] and "person_email" not in body["linkedin"]


def test_patch_person_includes_linkedin(monkeypatch):
    monkeypatch.setattr(person_edit, "update", lambda conn, email, **kw: row(notes="hi"))
    monkeypatch.setattr(linkedin_repo, "connection_for_person", lambda conn, email: li_row())
    body = client.patch("/people/alice@x.com", json={"notes": "hi"}).json()
    assert body["linkedin"]["company"] == "Example Health"


def test_list_responses_have_null_linkedin(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("list responses must not look up linkedin per row")

    monkeypatch.setattr(linkedin_repo, "connection_for_person", fail)
    assert client.get("/people?recent=1").json()["results"][0]["linkedin"] is None
    assert client.post("/search", json={"q": "ali"}).json()["results"][0]["linkedin"] is None


def test_search_linkedin_results():
    body = client.post("/search", json={"q": "ali", "limit": 5}).json()
    assert body["results"][0]["email"] == "alice@x.com"
    assert body["linkedin_results"][0]["full_name"] == "Alice Example"
    assert client.post("/search", json={"q": "zzz"}).json()["linkedin_results"] == []


def test_person_includes_imessage_summary(monkeypatch):
    monkeypatch.setattr(
        imessage_repo, "summary_for_person", lambda conn, email: imessage_summary_row()
    )
    body = client.get("/people/alice@x.com").json()
    assert body["imessage"]["message_count"] == 212
    assert body["imessage"]["handles"] == ["+15550100001"]


def test_person_imessage_is_null_when_unlinked():
    assert client.get("/people/alice@x.com").json()["imessage"] is None


def test_list_people_omits_imessage(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("list responses must not look up imessage per row")

    monkeypatch.setattr(imessage_repo, "summary_for_person", fail)
    assert client.get("/people?recent=5").json()["results"][0]["imessage"] is None
    assert client.post("/search", json={"q": "ali"}).json()["results"][0]["imessage"] is None


def test_patch_person_includes_imessage(monkeypatch):
    monkeypatch.setattr(person_edit, "update", lambda conn, email, **kw: row(notes="hi"))
    monkeypatch.setattr(
        imessage_repo, "summary_for_person", lambda conn, email: imessage_summary_row()
    )
    body = client.patch("/people/alice@x.com", json={"notes": "hi"}).json()
    assert body["imessage"]["message_count"] == 212


def test_search_returns_imessage_results():
    body = client.post("/search", json={"q": "ali"}).json()
    assert body["imessage_results"][0]["handle"] == "+15550100001"
    assert client.post("/search", json={"q": "zzz"}).json()["imessage_results"] == []


def test_handles_endpoint_applies_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        imessage_repo, "handles", lambda conn, **kw: (seen.update(kw), [handle_row()])[1]
    )
    r = client.get("/imessage/handles?replied=true&min_messages=3")
    assert r.status_code == 200
    assert seen["replied"] is True and seen["min_messages"] == 3
    assert r.json()["results"][0]["handle"] == "+15550100001"


def test_handles_limit_bounds():
    assert client.get("/imessage/handles?limit=501").status_code == 422
    assert client.get("/imessage/handles?limit=0").status_code == 422


def test_handle_detail_returns_groups_and_404s():
    body = client.get("/imessage/handles/%2B15550100001").json()
    assert body["groups"][0]["display_name"] == "Fixture Group"
    assert client.get("/imessage/handles/%2B15550100002").status_code == 404


def test_imports_latest_404s_when_never_imported():
    assert client.get("/imessage/imports/latest").status_code == 404


def test_imports_latest(monkeypatch):
    monkeypatch.setattr(
        imessage_repo,
        "latest_import",
        lambda conn: {
            "ran_at": TS,
            "mode": "incremental",
            "max_rowid": 999,
            "messages_upserted": 10,
            "messages_deleted": 0,
            "undecoded": 0,
            "handles": 5,
            "matched_by_email": 2,
            "matched_by_google": 1,
            "linked_to_people": 3,
            "chats": 4,
        },
    )
    r = client.get("/imessage/imports/latest")
    assert r.status_code == 200 and r.json()["messages_upserted"] == 10


def test_no_imessage_response_model_exposes_message_text():
    """Spec §6: people-api never serves iMessage content."""
    from pydantic import BaseModel

    import api.routers.imessage as mod

    banned = {"text", "content", "body", "message", "messages"}
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            assert not (set(obj.model_fields) & banned), f"{name} exposes message content"
