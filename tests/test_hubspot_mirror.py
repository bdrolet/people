from datetime import UTC, datetime

import pytest

import clients.hubspot as hs
import repo.people as people_repo
from services import hubspot_mirror as mirror

TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class FakeHubSpot:
    def __init__(self, total=0, existing=None, contacts=None):
        self.total = total
        self.existing = existing or {}
        self.contacts = contacts or []
        self.created, self.archived, self.logged = [], [], []

    def count_contacts(self):
        return self.total

    def find_by_email(self, email):
        return self.existing.get(email)

    def create_contact(self, email, display, last):
        self.created.append(email)
        self.total += 1
        return f"hs-{email}"

    def archive_contact(self, cid):
        self.archived.append(cid)
        self.total -= 1

    def list_contacts(self):
        return iter(self.contacts)

    def log_email(self, *a, **k):
        self.logged.append(a)

    def update_last_email_date(self, *a):
        pass


class FakeRepo:
    def __init__(self, rows):
        self.rows = {r["email"]: dict(r) for r in rows}

    def set_hubspot(self, conn, email, cid):
        self.rows[email]["hubspot_contact_id"] = cid

    def clear_hubspot(self, conn, email):
        self.rows[email]["hubspot_contact_id"] = None

    def oldest_managed(self, conn):
        managed = [r for r in self.rows.values() if r.get("hubspot_contact_id")]
        return min(managed, key=lambda r: r["last_interaction"], default=None)

    def managed(self, conn):
        return [r for r in self.rows.values() if r.get("hubspot_contact_id")]

    def eligible_not_in_hubspot(self, conn, limit):
        rows = [r for r in self.rows.values() if r["eligible"] and not r.get("hubspot_contact_id")]
        return sorted(rows, key=lambda r: r["last_interaction"], reverse=True)[:limit]

    def get(self, conn, email):
        return self.rows.get(email)


def row(email, eligible=True, cid=None, when=TS):
    return {
        "email": email,
        "display_name": None,
        "eligible": eligible,
        "hubspot_contact_id": cid,
        "last_interaction": when,
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HUBSPOT_WRITES_ENABLED", "true")
    monkeypatch.setenv("HUBSPOT_MAX_CONTACTS", "2")
    monkeypatch.setenv("HUBSPOT_TOKEN", "x")
    monkeypatch.setenv("HUBSPOT_OWNER_ID", "1")


def wire(monkeypatch, fake_hs, fake_repo):
    for n in (
        "count_contacts",
        "find_by_email",
        "create_contact",
        "archive_contact",
        "list_contacts",
        "log_email",
        "update_last_email_date",
    ):
        monkeypatch.setattr(hs, n, getattr(fake_hs, n))
    for n in (
        "set_hubspot",
        "clear_hubspot",
        "oldest_managed",
        "managed",
        "eligible_not_in_hubspot",
        "get",
    ):
        monkeypatch.setattr(people_repo, n, getattr(fake_repo, n))
    mirror._reset_count_cache()


def test_disabled_is_a_noop(monkeypatch, env):
    monkeypatch.setenv("HUBSPOT_WRITES_ENABLED", "false")
    fh, fr = FakeHubSpot(), FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert fh.created == [] and out.get("hubspot_contact_id") is None


def test_create_under_cap(monkeypatch, env):
    fh, fr = FakeHubSpot(total=1), FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert fh.created == ["a@x.com"] and out["hubspot_contact_id"] == "hs-a@x.com"


def test_adopts_existing_hubspot_contact_instead_of_creating(monkeypatch, env):
    fh = FakeHubSpot(total=1, existing={"a@x.com": {"id": "hs-old", "email": "a@x.com"}})
    fr = FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert fh.created == [] and out["hubspot_contact_id"] == "hs-old"


def test_at_cap_evicts_oldest_managed_then_creates(monkeypatch, env):
    old = datetime(2025, 1, 1, tzinfo=UTC)
    fh = FakeHubSpot(total=2)
    fr = FakeRepo(
        [row("old@x.com", cid="hs-old", when=old), row("mid@x.com", cid="hs-mid"), row("new@x.com")]
    )
    wire(monkeypatch, fh, fr)
    mirror.ensure_contact(None, fr.rows["new@x.com"])
    assert fh.archived == ["hs-old"]
    assert fr.rows["old@x.com"]["hubspot_contact_id"] is None
    assert fh.created == ["new@x.com"]


def test_at_cap_with_no_managed_contacts_skips(monkeypatch, env):
    fh, fr = FakeHubSpot(total=2), FakeRepo([row("new@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["new@x.com"])
    assert fh.created == [] and fh.archived == [] and out.get("hubspot_contact_id") is None


def test_hubspot_failure_is_swallowed(monkeypatch, env):
    fh, fr = FakeHubSpot(total=0), FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)

    def boom(*a, **k):
        raise RuntimeError("hubspot down")

    monkeypatch.setattr(hs, "create_contact", boom)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert out.get("hubspot_contact_id") is None


def test_log_email_only_for_mirrored(monkeypatch, env):
    fh, fr = FakeHubSpot(), FakeRepo([row("a@x.com", cid="hs-a"), row("b@x.com")])
    wire(monkeypatch, fh, fr)
    event = {
        "subject": "s",
        "sender": "a@x.com",
        "body": "b",
        "body_html": None,
        "received_at": "2026-09-03T12:00:00Z",
    }
    mirror.log_email(fr.rows["a@x.com"], event)
    mirror.log_email(fr.rows["b@x.com"], event)
    assert len(fh.logged) == 1


def test_reconcile_adopt_heal_enforce_fill(monkeypatch, env):
    old = datetime(2025, 1, 1, tzinfo=UTC)
    fh = FakeHubSpot(
        total=3,
        contacts=[
            {"id": "hs-a", "email": "a@x.com"},  # managed already
            {"id": "hs-b", "email": "b@x.com"},  # in HubSpot, eligible row not linked → adopt
            {"id": "hs-u", "email": "u@x.com"},  # unmanaged, unknown to people → never evicted
        ],
    )
    fr = FakeRepo(
        [
            row("a@x.com", cid="hs-a", when=old),
            row("b@x.com"),
            row("gone@x.com", cid="hs-gone"),  # id no longer in HubSpot → heal
            row("n@x.com"),  # eligible, not in HubSpot
        ]
    )
    wire(monkeypatch, fh, fr)
    counts = mirror.reconcile(None)
    assert counts["adopted"] == 1 and fr.rows["b@x.com"]["hubspot_contact_id"] == "hs-b"
    assert counts["healed"] == 1 and fr.rows["gone@x.com"]["hubspot_contact_id"] is None
    # cap=2, total=3 after adopt/heal → evict the oldest managed (a) once
    assert counts["evicted"] == 1 and fh.archived == ["hs-a"]
    # now total=2 == cap → nothing to fill
    assert counts["filled"] == 0 and fh.created == []


def test_reconcile_fills_when_under_cap(monkeypatch, env):
    monkeypatch.setenv("HUBSPOT_MAX_CONTACTS", "3")
    fh = FakeHubSpot(total=1, contacts=[{"id": "hs-a", "email": "a@x.com"}])
    fr = FakeRepo([row("a@x.com", cid="hs-a"), row("n1@x.com"), row("n2@x.com"), row("n3@x.com")])
    wire(monkeypatch, fh, fr)
    counts = mirror.reconcile(None)
    assert counts["filled"] == 2 and len(fh.created) == 2


def test_reconcile_db_error_propagates(monkeypatch, env):
    fh = FakeHubSpot(total=1, contacts=[{"id": "hs-b", "email": "b@x.com"}])
    fr = FakeRepo([row("b@x.com")])
    wire(monkeypatch, fh, fr)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(people_repo, "set_hubspot", boom)
    with pytest.raises(RuntimeError):
        mirror.reconcile(None)


def test_reconcile_archive_failure_stops_enforce_without_raising(monkeypatch, env):
    old = datetime(2025, 1, 1, tzinfo=UTC)
    monkeypatch.setenv("HUBSPOT_MAX_CONTACTS", "1")
    fh = FakeHubSpot(
        total=2,
        contacts=[{"id": "hs-a", "email": "a@x.com"}, {"id": "hs-b", "email": "b@x.com"}],
    )
    fr = FakeRepo([row("a@x.com", cid="hs-a", when=old), row("b@x.com", cid="hs-b")])
    wire(monkeypatch, fh, fr)

    def boom(*a, **k):
        raise RuntimeError("archive failed")

    monkeypatch.setattr(hs, "archive_contact", boom)
    counts = mirror.reconcile(None)
    assert counts["evicted"] == 0
    assert fr.rows["a@x.com"]["hubspot_contact_id"] == "hs-a"
    assert fr.rows["b@x.com"]["hubspot_contact_id"] == "hs-b"
