from datetime import UTC, date, datetime

import httpx
import pytest

from services import campaign as campaign_svc
from services import campaign_followups as followups
from services.campaign_draft import render_follow_up

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # a Wednesday, 11:00 in New York
TODAY = date(2026, 10, 21)
FROM = "ben@drolet.ai"
SUBJECT = "Production-readiness audit for Acme"
POSTAL = "123 Main St, San Francisco, CA 94110"


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeRepo:
    """In-memory stand-in for the repo/campaign.py functions follow-ups and advance use."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.events: list[dict] = []
        self.updates: list[tuple[int, dict]] = []
        self.on_lock = None  # called with the row before a lock returns it

    def contacts(self, conn, *, status=None, run_id=None, follow_up_due_on=None):
        return [
            dict(self.rows[i])
            for i in sorted(self.rows)
            if (status is None or self.rows[i]["status"] == status)
            and (
                follow_up_due_on is None
                or (
                    self.rows[i]["follow_up_on"] is not None
                    and self.rows[i]["follow_up_on"] <= follow_up_due_on
                )
            )
        ]

    def lock_contact(self, conn, contact_id):
        row = self.rows.get(contact_id)
        if row and self.on_lock:
            self.on_lock(row)
        return dict(row) if row else None

    def update_contact(self, conn, contact_id, changes):
        self.updates.append((contact_id, dict(changes)))
        self.rows[contact_id].update(changes)
        return dict(self.rows[contact_id])

    def insert_event(self, conn, **kw):
        self.events.append(kw)
        return len(self.events)

    def add(self, **fields):
        cid = len(self.rows) + 1
        self.rows[cid] = {
            "id": cid,
            "company_id": 1,
            "full_name": f"Sam Lee{cid}",
            "email": f"sam{cid}@acme.com",
            "subject": SUBJECT,
            "status": "sent",
            "draft_id": f"d{cid}",
            "graph_message_id": f"m{cid}",
            "follow_up_on": TODAY,
            "replied_at": None,
            **fields,
        }
        return cid


class FakeInbox:
    def __init__(self, hits=(), fail_reply_for=frozenset(), reply_error=None, send_error=None):
        self.hits = list(hits)
        self.fail_reply_for = set(fail_reply_for)
        self.reply_error = reply_error  # raised by every reply, after it is recorded
        self.send_error = send_error  # raised by every send_draft, after it is recorded
        self.replies: list[dict] = []
        self.searches: list[tuple[str, str]] = []
        self.drafts: list[dict] = []
        self.sent: list[tuple[str, str]] = []

    def reply(self, message_id, *, body, to, from_address):
        if message_id in self.fail_reply_for:
            raise RuntimeError("graph 503")
        self.replies.append({"id": message_id, "body": body, "to": to, "from": from_address})
        if self.reply_error:
            raise self.reply_error
        return {"status": "sent"}

    def search_sent(self, *, to, subject):
        self.searches.append((to, subject))
        return list(self.hits)

    def create_draft(self, to, subject, body="", from_address=None):
        draft_id = f"fd{len(self.drafts) + 1}"
        self.drafts.append(
            {"id": draft_id, "to": to, "subject": subject, "body": body, "from": from_address}
        )
        return {"id": draft_id}

    def send_draft(self, draft_id, *, from_address):
        self.sent.append((draft_id, from_address))
        if self.send_error:
            raise self.send_error
        return {"status": "sent", "id": f"m-{draft_id}"}


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(followups, "campaign_repo", fake)
    monkeypatch.setattr(campaign_svc, "campaign_repo", fake)
    return fake


def _run(conn, inbox, sleeps=None):
    return followups.run(
        conn,
        now=NOW,
        from_address=FROM,
        postal_address=POSTAL,
        inbox=inbox,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def _status_error(code):
    request = httpx.Request("POST", "http://inbox-api/emails/m1/reply")
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request)
    )


def _hit(**fields):
    return {
        "message_id": "m-found",
        "subject": SUBJECT,
        "sender": FROM,
        "received_at": "2026-10-14T15:00:00Z",
        "is_draft": False,
        **fields,
    }


def test_a_due_contact_with_an_id_gets_a_threaded_reply_and_is_followed_up(repo):
    cid = repo.add(full_name="Dana  Q. Smith")
    conn, inbox = FakeConn(), FakeInbox()
    result = _run(conn, inbox)
    assert inbox.replies == [
        {
            "id": "m1",
            "body": render_follow_up(first_name="Dana", postal_address=POSTAL),
            "to": ["sam1@acme.com"],
            "from": FROM,
        }
    ]
    assert inbox.searches == [] and inbox.drafts == []
    assert repo.rows[cid]["status"] == "followed_up"
    assert repo.rows[cid]["follow_up_on"] is None
    assert [(e["status"], e["note"], e["source"], e["at"]) for e in repo.events] == [
        ("followed_up", "follow-up: sending", "cli", NOW),
        ("followed_up", "follow-up: sent (reply)", "cli", NOW),
    ]
    assert (result.sent, result.fallback, result.skipped, result.failed) == (1, 0, 0, 0)
    assert conn.commits == 2


def test_no_id_and_a_confirmed_sent_hit_replies_on_it_and_stores_the_id(repo):
    cid = repo.add(graph_message_id=None)
    conn = FakeConn()
    inbox = FakeInbox(hits=[_hit(is_draft=None, message_id="m-maybe"), _hit()])
    result = _run(conn, inbox)
    assert inbox.searches == [("sam1@acme.com", SUBJECT)]
    assert [r["id"] for r in inbox.replies] == ["m-found"]
    assert repo.rows[cid]["graph_message_id"] == "m-found"
    assert repo.updates == [
        (cid, {"status": "followed_up", "follow_up_on": None, "graph_message_id": "m-found"})
    ]
    assert repo.rows[cid]["status"] == "followed_up"
    assert inbox.drafts == []
    assert (result.sent, result.fallback) == (1, 0)


@pytest.mark.parametrize(
    "hit",
    [
        _hit(is_draft=None),
        _hit(is_draft=True),
        _hit(subject="Re: " + SUBJECT),
        _hit(message_id="d1"),  # the original draft itself
        _hit(message_id=None),
    ],
)
def test_no_confirmed_hit_falls_back_to_a_fresh_re(repo, hit):
    cid = repo.add(graph_message_id=None)
    conn, inbox = FakeConn(), FakeInbox(hits=[hit])
    result = _run(conn, inbox)
    assert inbox.replies == []
    assert inbox.drafts == [
        {
            "id": "fd1",
            "to": ["sam1@acme.com"],
            "subject": "Re: Production-readiness audit for Acme",
            "body": render_follow_up(first_name="Sam", postal_address=POSTAL),
            "from": FROM,
        }
    ]
    assert inbox.sent == [("fd1", FROM)]
    assert repo.rows[cid]["status"] == "followed_up"
    assert repo.rows[cid]["graph_message_id"] is None
    assert repo.events[-1]["note"] == "follow-up: sent (fallback: fresh Re:)"
    assert (result.sent, result.fallback, result.failed) == (1, 1, 0)


def test_a_contact_due_tomorrow_is_untouched(repo):
    cid = repo.add(follow_up_on=date(2026, 10, 22))
    conn, inbox = FakeConn(), FakeInbox()
    result = _run(conn, inbox)
    assert inbox.replies == [] and inbox.searches == [] and inbox.drafts == []
    assert repo.rows[cid]["status"] == "sent" and repo.events == []
    assert (result.sent, result.fallback, result.skipped, result.failed) == (0, 0, 0, 0)


def test_an_overdue_contact_is_followed_up(repo):
    cid = repo.add(follow_up_on=date(2026, 10, 19))
    _run(FakeConn(), FakeInbox())
    assert repo.rows[cid]["status"] == "followed_up"


def test_today_is_the_new_york_date(repo):
    # 02:00 UTC on the 22nd is still the 21st in New York: tomorrow's contact waits.
    cid = repo.add(follow_up_on=date(2026, 10, 22))
    followups.run(
        FakeConn(),
        now=datetime(2026, 10, 22, 2, 0, tzinfo=UTC),
        from_address=FROM,
        postal_address=POSTAL,
        inbox=FakeInbox(),
        sleep=lambda s: None,
    )
    assert repo.rows[cid]["status"] == "sent"


def test_a_contact_that_replied_before_the_lock_is_skipped(repo):
    cid = repo.add()

    def replied(row):
        row["status"] = "replied"

    repo.on_lock = replied
    conn, inbox = FakeConn(), FakeInbox()
    result = _run(conn, inbox)
    assert inbox.replies == [] and inbox.drafts == []
    assert repo.rows[cid]["status"] == "replied" and repo.events == []
    assert (result.sent, result.skipped) == (0, 1)
    assert conn.rollbacks == 1  # the lock is released


def test_a_failed_reply_is_recorded_and_never_retried(repo):
    first = repo.add()
    second = repo.add()
    conn = FakeConn()
    inbox = FakeInbox(fail_reply_for={"m1"})
    result = _run(conn, inbox)
    assert repo.rows[first]["status"] == "followed_up"
    assert repo.rows[first]["follow_up_on"] is None
    notes = [e["note"] for e in repo.events if e["contact_id"] == first]
    assert notes == [
        "follow-up: sending",
        "follow-up: FAILED RuntimeError: graph 503; check Sent Items",
    ]
    assert repo.rows[second]["status"] == "followed_up"
    assert [r["id"] for r in inbox.replies] == ["m2"]
    assert (result.sent, result.failed) == (1, 1)
    again = _run(FakeConn(), inbox)
    assert [r["id"] for r in inbox.replies] == ["m2"]
    assert (again.sent, again.failed, again.skipped) == (0, 0, 0)


def test_a_reply_whose_response_is_lost_is_not_sent_twice(repo):
    cid = repo.add()
    inbox = FakeInbox(reply_error=httpx.ReadTimeout("timed out"))
    result = _run(FakeConn(), inbox)
    assert result.failed == 1 and result.sent == 0
    assert repo.rows[cid]["status"] == "followed_up"
    assert repo.events[-1]["note"] == ("follow-up: FAILED ReadTimeout: timed out; check Sent Items")
    _run(FakeConn(), inbox)
    assert len(inbox.replies) == 1
    assert inbox.drafts == []


def test_a_5xx_from_reply_is_a_failure_not_a_fallback(repo):
    repo.add()
    inbox = FakeInbox(reply_error=_status_error(503))
    result = _run(FakeConn(), inbox)
    assert (result.failed, result.fallback) == (1, 0)
    assert inbox.drafts == []


@pytest.mark.parametrize("code", [404, 410])
def test_a_gone_original_falls_through_to_a_fresh_re(repo, code):
    cid = repo.add()
    inbox = FakeInbox(reply_error=_status_error(code))
    result = _run(FakeConn(), inbox)
    assert len(inbox.replies) == 1
    assert [d["subject"] for d in inbox.drafts] == ["Re: " + SUBJECT]
    assert inbox.sent == [("fd1", FROM)]
    assert (result.sent, result.fallback, result.failed) == (1, 1, 0)
    assert repo.rows[cid]["status"] == "followed_up"
    assert repo.events[-1]["note"] == "follow-up: sent (fallback: fresh Re:)"


def test_a_failed_fallback_send_is_recorded_and_never_retried(repo):
    cid = repo.add(graph_message_id=None)
    inbox = FakeInbox(send_error=RuntimeError("read timeout"))
    result = _run(FakeConn(), inbox)
    assert (result.sent, result.fallback, result.failed) == (0, 0, 1)
    assert repo.rows[cid]["status"] == "followed_up"
    assert repo.events[-1]["note"] == (
        "follow-up: FAILED RuntimeError: read timeout; check Sent Items"
    )
    _run(FakeConn(), inbox)
    assert len(inbox.drafts) == 1 and len(inbox.sent) == 1


def test_advance_failing_before_the_send_sends_nothing(repo, monkeypatch):
    cid = repo.add()

    def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(campaign_svc, "advance", broken)
    inbox = FakeInbox()
    with pytest.raises(RuntimeError, match="db down"):
        _run(FakeConn(), inbox)
    assert inbox.replies == [] and inbox.drafts == []
    assert repo.rows[cid]["status"] == "sent"


def test_path_b_stores_the_found_id_with_the_sending_record(repo):
    cid = repo.add(graph_message_id=None)
    inbox = FakeInbox(hits=[_hit()], reply_error=RuntimeError("graph 503"))
    conn = FakeConn()
    _run(conn, inbox)
    assert repo.rows[cid]["graph_message_id"] == "m-found"
    sending = [u for c, u in repo.updates if c == cid and u.get("status") == "followed_up"]
    assert sending and sending[0]["graph_message_id"] == "m-found"


def test_a_failed_search_leaves_the_contact_sent_for_the_next_run(repo):
    cid = repo.add(graph_message_id=None)

    class SearchDown(FakeInbox):
        def search_sent(self, *, to, subject):
            raise RuntimeError("inbox-api down")

    conn = FakeConn()
    result = _run(conn, SearchDown())
    assert result.failed == 1
    assert repo.rows[cid]["status"] == "sent" and repo.events == []
    assert conn.rollbacks == 1


def test_pauses_between_sends_only(repo):
    repo.add()
    repo.add()
    repo.add()
    sleeps: list[float] = []
    _run(FakeConn(), FakeInbox(), sleeps)
    assert sleeps == [1.0, 1.0]
