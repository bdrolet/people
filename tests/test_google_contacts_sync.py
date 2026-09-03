import pytest

import clients.google_contacts as gc
import repo.people as people_repo
import repo.sync_state as sync_state
from services import google_contacts_sync as sync

GROUPS = {
    "myContacts": {"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP"},
    "Inbox": {"resourceName": "contactGroups/inbox1", "groupType": "USER_CONTACT_GROUP"},
    "Family": {"resourceName": "contactGroups/fam1", "groupType": "USER_CONTACT_GROUP"},
}


def person(
    rn="people/c1",
    email="alice@example.com",
    name="Alice Example",
    groups=(),
    bio=None,
    deleted=False,
    etag="e1",
):
    p = {
        "resourceName": rn,
        "etag": etag,
        "emailAddresses": [{"value": email}] if email else [],
        "names": [{"displayName": name}] if name else [],
        "memberships": [
            {"contactGroupMembership": {"contactGroupResourceName": g}} for g in groups
        ],
        "metadata": {"deleted": deleted},
    }
    if bio:
        p["biographies"] = [{"value": bio}]
    return p


class FakeRepo:
    def __init__(self, rows=()):
        self.rows = {r["email"]: dict(r) for r in rows}
        self.log = []

    def _by_rn(self, rn):
        return next((r for r in self.rows.values() if r.get("google_resource_name") == rn), None)

    def get(self, conn, email):
        return self.rows.get(email)

    def get_by_google_resource(self, conn, rn):
        return self._by_rn(rn)

    def set_google(self, conn, email, **kw):
        self.rows[email].update(google_resource_name=kw["resource_name"], google_etag=kw["etag"])
        for k in ("display_name", "notes", "relationship_label"):
            if kw.get(k) is not None:
                self.rows[email][k] = kw[k]
        self.log.append(("set_google", email))

    def update_from_google(self, conn, rn, **kw):
        r = self._by_rn(rn)
        r.update(
            google_etag=kw["etag"], notes=kw["notes"], relationship_label=kw["relationship_label"]
        )
        if kw["display_name"]:
            r["display_name"] = kw["display_name"]
        self.log.append(("update", rn))

    def mark_google_deleted(self, conn, rn):
        r = self._by_rn(rn)
        r.update(google_deleted_at="now", google_resource_name=None)
        self.log.append(("deleted", rn))

    def create_from_google(self, conn, email, **kw):
        self.rows[email] = {
            "email": email,
            "eligible": True,
            "google_resource_name": kw["resource_name"],
            **kw,
        }
        self.log.append(("created", email))
        return self.rows[email]


class FakeGC:
    def __init__(self, found=None, connections=(), token="tok2", expire_first=False):
        self.found = found
        self.created = []
        self.connections = list(connections)
        self.token = token
        self.expire_first = expire_first
        self.calls = []

    def search_by_email(self, email):
        return self.found

    def create_contact(self, name, email, group):
        self.created.append((name, email, group))
        return person(rn="people/new", email=email, name=name)

    def ensure_group(self, name):
        return "contactGroups/inbox1"

    def list_groups(self):
        return GROUPS

    def list_connections(self, sync_token):
        self.calls.append(sync_token)
        if self.expire_first and sync_token:
            self.expire_first = False
            raise gc.SyncTokenExpired()
        return self.connections, self.token

    def get_person(self, rn):
        return next(p for p in self.connections if p["resourceName"] == rn)


@pytest.fixture
def wire(monkeypatch):
    def _wire(fake_gc, fake_repo, token="tok1"):
        for n in (
            "search_by_email",
            "create_contact",
            "ensure_group",
            "list_groups",
            "list_connections",
            "get_person",
        ):
            monkeypatch.setattr(gc, n, getattr(fake_gc, n))
        for n in (
            "get",
            "get_by_google_resource",
            "set_google",
            "update_from_google",
            "mark_google_deleted",
            "create_from_google",
        ):
            monkeypatch.setattr(people_repo, n, getattr(fake_repo, n))
        saved = {}
        monkeypatch.setattr(sync_state, "get_token", lambda conn: token)
        monkeypatch.setattr(
            sync_state, "set_token", lambda conn, t, s: saved.update(token=t, status=s)
        )
        monkeypatch.delenv("GOOGLE_CONTACT_GROUP", raising=False)
        return saved

    return _wire


def row(email, **kw):
    base = {
        "email": email,
        "display_name": None,
        "eligible": True,
        "google_resource_name": None,
        "google_deleted_at": None,
    }
    base.update(kw)
    return base


def test_relationship_label_ignores_system_and_inbox_groups():
    p = person(groups=["contactGroups/myContacts", "contactGroups/inbox1", "contactGroups/fam1"])
    assert sync.relationship_label(p, GROUPS) == "family"


def test_relationship_label_none_when_only_system():
    assert sync.relationship_label(person(groups=["contactGroups/myContacts"]), GROUPS) is None


def test_ensure_contact_links_existing(wire):
    fr = FakeRepo([row("alice@example.com")])
    fg = FakeGC(found=person(groups=["contactGroups/fam1"], bio="old friend"))
    wire(fg, fr)
    sync.ensure_contact(None, fr.rows["alice@example.com"])
    r = fr.rows["alice@example.com"]
    assert r["google_resource_name"] == "people/c1" and fg.created == []
    assert (
        r["display_name"] == "Alice Example"
        and r["notes"] == "old friend"
        and r["relationship_label"] == "family"
    )


def test_ensure_contact_creates_in_inbox_group(wire):
    fr = FakeRepo([row("alice@example.com", display_name="Alice")])
    fg = FakeGC(found=None)
    wire(fg, fr)
    sync.ensure_contact(None, fr.rows["alice@example.com"])
    assert fg.created == [("Alice", "alice@example.com", "contactGroups/inbox1")]
    assert fr.rows["alice@example.com"]["google_resource_name"] == "people/new"


def test_ensure_contact_skips_linked_and_deleted(wire):
    fr = FakeRepo(
        [
            row("a@x.com", google_resource_name="people/c9"),
            row("b@x.com", google_deleted_at="2026-01-01"),
        ]
    )
    fg = FakeGC(found=None)
    wire(fg, fr)
    sync.ensure_contact(None, fr.rows["a@x.com"])
    sync.ensure_contact(None, fr.rows["b@x.com"])
    assert fg.created == []


def test_ensure_contact_swallows_google_errors(wire, monkeypatch):
    fr = FakeRepo([row("a@x.com")])
    wire(FakeGC(found=None), fr)

    def boom(email):
        raise RuntimeError("quota")

    monkeypatch.setattr(gc, "search_by_email", boom)
    out = sync.ensure_contact(None, fr.rows["a@x.com"])
    assert out["google_resource_name"] is None


def test_ensure_contact_db_failure_propagates(wire, monkeypatch):
    fr = FakeRepo([row("a@x.com")])
    fg = FakeGC(found=None)
    wire(fg, fr)

    def boom(conn, email, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(people_repo, "set_google", boom)
    with pytest.raises(RuntimeError):
        sync.ensure_contact(None, fr.rows["a@x.com"])
    assert len(fg.created) == 1


def test_ensure_contact_reuses_listed_group(wire, monkeypatch):
    fr = FakeRepo([row("a@x.com")])
    fg = FakeGC(found=None)
    wire(fg, fr)

    def must_not_be_called(name):
        raise AssertionError("must not be called")

    monkeypatch.setattr(gc, "ensure_group", must_not_be_called)
    sync.ensure_contact(None, fr.rows["a@x.com"])
    assert fg.created == [(None, "a@x.com", "contactGroups/inbox1")]


def test_run_sync_updates_links_creates_deletes(wire):
    fr = FakeRepo(
        [
            row("linked@x.com", google_resource_name="people/l1"),
            row("hand@x.com"),
            row("gone@x.com", google_resource_name="people/g1"),
        ]
    )
    fg = FakeGC(
        connections=[
            person(
                rn="people/l1",
                email="linked@x.com",
                name="Linked Person",
                groups=["contactGroups/fam1"],
                bio="n",
            ),
            person(rn="people/h1", email="hand@x.com", name="Hand Made"),
            person(rn="people/u1", email="unknown@x.com", name="Unknown One"),
            person(rn="people/g1", email=None, name=None, deleted=True),
        ]
    )
    saved = wire(fg, fr)
    counts = sync.run_sync(None)
    assert counts == {"updated": 1, "linked": 1, "created": 1, "deleted": 1}
    assert fr.rows["linked@x.com"]["relationship_label"] == "family"
    assert fr.rows["hand@x.com"]["google_resource_name"] == "people/h1"
    assert fr.rows["unknown@x.com"]["eligible"] is True
    assert fr.rows["gone@x.com"]["google_deleted_at"] == "now"
    assert saved == {"token": "tok2", "status": "ok"}


def test_run_sync_falls_back_to_full_on_expired_token(wire):
    fr = FakeRepo()
    fg = FakeGC(connections=[], expire_first=True)
    wire(fg, fr, token="stale")
    sync.run_sync(None)
    assert fg.calls == ["stale", None]


def test_run_sync_records_error_status_and_reraises(wire, monkeypatch):
    fr = FakeRepo()
    fg = FakeGC()

    def boom(sync_token):
        raise RuntimeError("boom")

    monkeypatch.setattr(fg, "list_connections", boom)
    saved = wire(fg, fr)
    with pytest.raises(RuntimeError):
        sync.run_sync(None)
    assert saved == {"token": "tok1", "status": "error: RuntimeError"}


class FakeConn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_run_sync_commits_error_status(wire, monkeypatch):
    fr = FakeRepo()
    fg = FakeGC()

    def boom(sync_token):
        raise RuntimeError("boom")

    monkeypatch.setattr(fg, "list_connections", boom)
    wire(fg, fr)
    conn = FakeConn()
    with pytest.raises(RuntimeError):
        sync.run_sync(conn)
    assert conn.commits == 1
