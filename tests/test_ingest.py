from datetime import UTC, datetime

import pytest

import repo.people as people_repo
from services import ingest

TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OWN_ADDRESSES", "me@example.com")
    monkeypatch.delenv("AUTOMATED_SENDER_DOMAINS", raising=False)


class FakeRepo:
    def __init__(self, existing=None):
        self.rows = dict(existing or {})
        self.flags = []

    def upsert_inbound(self, conn, email, display, ts):
        row = self.rows.setdefault(email, {"email": email, "eligible": False, "message_count": 0})
        row["message_count"] += 1
        return dict(row)

    def upsert_outbound(self, conn, email, display, ts):
        row = self.rows.setdefault(
            email, {"email": email, "eligible": False, "my_response_count": 0}
        )
        row["my_response_count"] += 1
        return dict(row)

    def set_flags(self, conn, email, *, automated, eligible):
        self.flags.append((email, automated, eligible))
        row = self.rows[email]
        row["automated"] = automated
        row["eligible"] = row["eligible"] or eligible

    def get(self, conn, email):
        return dict(self.rows[email])


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    for name in ("upsert_inbound", "upsert_outbound", "set_flags", "get"):
        monkeypatch.setattr(people_repo, name, getattr(fake, name))
    return fake


def test_inbound_review_makes_new_sender_eligible(repo):
    res = ingest.record_inbound(
        conn=None, sender="Alice@Example.com", display="Alice", received_at=TS, category="review"
    )
    assert res.newly_eligible is True
    assert res.row["eligible"] is True
    assert repo.flags == [("alice@example.com", False, True)]


def test_inbound_ignore_does_not_make_eligible_but_counts(repo):
    res = ingest.record_inbound(
        conn=None, sender="alice@example.com", display=None, received_at=TS, category="ignore"
    )
    assert res.newly_eligible is False
    assert res.row["eligible"] is False
    assert res.row["message_count"] == 1


def test_inbound_already_eligible_is_not_newly_eligible(repo):
    ingest.record_inbound(
        conn=None, sender="alice@example.com", display=None, received_at=TS, category="review"
    )
    res = ingest.record_inbound(
        conn=None, sender="alice@example.com", display=None, received_at=TS, category="review"
    )
    assert res.newly_eligible is False and res.row["eligible"] is True


def test_inbound_automated_never_eligible(repo):
    res = ingest.record_inbound(
        conn=None, sender="no-reply@x.com", display=None, received_at=TS, category="urgent"
    )
    assert res.row["eligible"] is False and res.row["automated"] is True


def test_outbound_skips_own_address_and_marks_eligible(repo):
    results = ingest.record_outbound(
        conn=None,
        recipients=["me@example.com", "Bob@x.com", "bob@x.com"],
        display_by_email=None,
        sent_at=TS,
    )
    assert [r.row["email"] for r in results] == ["bob@x.com", "bob@x.com"]
    assert results[0].newly_eligible is True
    assert results[1].newly_eligible is False
    assert repo.rows["bob@x.com"]["my_response_count"] == 2


def test_outbound_to_automated_address_counts_but_not_eligible(repo):
    results = ingest.record_outbound(
        conn=None, recipients=["support@x.com"], display_by_email=None, sent_at=TS
    )
    assert results[0].row["eligible"] is False


def test_parse_ts_accepts_iso_with_offset_and_z():
    assert ingest.parse_ts("2026-09-03T12:00:00+00:00") == TS
    assert ingest.parse_ts("2026-09-03T12:00:00Z") == TS
