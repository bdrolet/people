from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import dns.resolver
import pytest

from models.campaign import CONTACT_STATUSES
from services import campaign as campaign_svc
from services import campaign_send as send
from services import person_create
from services.campaign_targets import CampaignTargets

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # a Wednesday, 11:00 in New York
TARGETS = CampaignTargets(
    threshold=1,
    slots=5,
    slots_through="this fall",
    per_day=20,
    follow_up_business_days=5,
    lookback_days_first_run=7,
    stale_after_days=7,
)
FROM = "ben@drolet.ai"
POSTAL = "123 Main St, San Francisco, CA 94110"


# --- preflight ------------------------------------------------------------------------------


class FakeResolver:
    def __init__(self, records: dict[str, list[str]]):
        self.records = records
        self.asked: list[tuple[str, str]] = []

    def resolve(self, name, rdtype):
        self.asked.append((name, rdtype))
        if name not in self.records:
            raise dns.resolver.NXDOMAIN()
        if not self.records[name]:
            raise dns.resolver.NoAnswer()
        return [SimpleNamespace(strings=[t.encode()]) for t in self.records[name]]


GOOD_DNS = {
    "drolet.ai": ["google-site-verification=x", "v=spf1 include:spf.protection.outlook.com -all"],
    "_dmarc.drolet.ai": ["v=DMARC1; p=quarantine"],
    "selector1._domainkey.drolet.ai": ["v=DKIM1; k=rsa; p=MIGf"],
}


def _preflight(records=GOOD_DNS, postal=POSTAL, reachable=True):
    send.preflight(
        "drolet.ai",
        postal_address=postal,
        inbox_reachable=lambda: reachable,
        resolver=FakeResolver(records),
    )


def test_preflight_passes_with_spf_dmarc_dkim_postal_and_inbox():
    _preflight()


def test_preflight_names_dmarc_when_it_is_missing():
    records = {k: v for k, v in GOOD_DNS.items() if not k.startswith("_dmarc")}
    with pytest.raises(send.PreflightError, match="dmarc"):
        _preflight(records)


def test_preflight_names_spf_when_the_domain_has_no_spf_record():
    records = {**GOOD_DNS, "drolet.ai": ["google-site-verification=x"]}
    with pytest.raises(send.PreflightError, match="spf"):
        _preflight(records)


def test_preflight_names_dkim_when_no_selector_answers():
    records = {**GOOD_DNS, "selector1._domainkey.drolet.ai": []}
    with pytest.raises(send.PreflightError, match="dkim"):
        _preflight(records)


def test_preflight_accepts_the_second_selector():
    records = {k: v for k, v in GOOD_DNS.items() if not k.startswith("selector1")}
    records["selector2._domainkey.drolet.ai"] = ["v=DKIM1; p=x"]
    _preflight(records)


def test_preflight_selectors_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_DKIM_SELECTORS", " google , s2 ")
    records = {k: v for k, v in GOOD_DNS.items() if not k.startswith("selector1")}
    with pytest.raises(send.PreflightError, match="dkim"):
        _preflight(records)
    records["google._domainkey.drolet.ai"] = ["v=DKIM1; p=x"]
    _preflight(records)


def test_preflight_wants_a_postal_address():
    with pytest.raises(send.PreflightError, match="postal"):
        _preflight(postal="  ")


def test_preflight_wants_inbox_api_reachable():
    with pytest.raises(send.PreflightError, match="inbox"):
        _preflight(reachable=False)


# --- the fake repo, people and inbox --------------------------------------------------------


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeRepo:
    """In-memory stand-in for the repo/campaign.py functions send and advance use."""

    def __init__(self):
        self.runs = [{"id": 1, "ran_at": NOW - timedelta(days=1), "notes": None}]
        self.companies = {1: {"id": 1, "name": "Acme", "domain": "acme.com", "status": "active"}}
        self.rows: dict[int, dict] = {}  # contacts by id
        self.events: list[dict] = []
        self.counts: dict[int, dict] = {}
        self.sent_today_override: int | None = None
        self.on_lock = None  # called with the row before a lock returns it
        self.bounced_at: dict[str, datetime] = {}
        self.outreach_by_domain: dict[str, list[dict]] = {}

    def latest_run(self, conn):
        return self.runs[-1] if self.runs else None

    def get_company(self, conn, company_id):
        return dict(self.companies[company_id])

    def contacts_list(self):
        return [self.rows[i] for i in sorted(self.rows)]

    def contacts(self, conn, *, status=None, run_id=None, follow_up_due_on=None):
        return [
            dict(c)
            for c in self.contacts_list()
            if (status is None or c["status"] == status)
            and (run_id is None or c["run_id"] == run_id)
        ]

    def lock_contact(self, conn, contact_id):
        row = self.rows.get(contact_id)
        if row and self.on_lock:
            self.on_lock(row)
        return dict(row) if row else None

    def email_bounced_at(self, conn, email):
        return self.bounced_at.get(email.lower())

    def people_in_outreach_by_domain(self, conn, domain):
        return self.outreach_by_domain.get(domain, [])

    def update_contact(self, conn, contact_id, changes):
        self.rows[contact_id].update(changes)
        return dict(self.rows[contact_id])

    def insert_event(self, conn, **kw):
        self.events.append(kw)
        return len(self.events)

    def sent_today(self, conn, on, tz="America/New_York"):
        if self.sent_today_override is not None:
            return self.sent_today_override
        return sum(
            1
            for e in self.events
            if e["status"] == "sent" and e["at"].astimezone(campaign_svc.TZ).date() == on
        )

    def last_event_at(self, conn, contact_id, status):
        ats = [
            e["at"] for e in self.events if e["contact_id"] == contact_id and e["status"] == status
        ]
        return max(ats) if ats else None

    def bounced_count_for_run(self, conn, run_id):
        ids = {c["id"] for c in self.rows.values() if c["run_id"] == run_id}
        return sum(1 for e in self.events if e["status"] == "bounced" and e["contact_id"] in ids)

    def status_counts_for_run(self, conn, run_id):
        counts = dict.fromkeys(CONTACT_STATUSES, 0)
        for c in self.rows.values():
            if c["run_id"] == run_id:
                counts[c["status"]] += 1
        return counts

    def update_run_counts(self, conn, run_id, counts):
        self.counts[run_id] = counts

    def add(self, status="approved", **fields):
        cid = len(self.rows) + 1
        self.rows[cid] = {
            "id": cid,
            "company_id": 1,
            "person_id": None,
            "full_name": f"Sam Lee{cid}",
            "title": "CTO",
            "linkedin_url": f"https://www.linkedin.com/in/sam{cid}",
            "email": f"sam{cid}@acme.com",
            "subject": f"Subject {cid}",
            "body": f"Body {cid}",
            "status": status,
            "draft_id": None,
            "graph_message_id": None,
            "sent_at": None,
            "follow_up_on": None,
            "replied_at": None,
            "run_id": 1,
            **fields,
        }
        return cid


class FakeInbox:
    def __init__(
        self,
        fail_send_for: set[str] = frozenset(),
        return_id: bool = True,
        lost_reply_for: set[str] = frozenset(),
    ):
        self.drafts: list[dict] = []
        self.sent: list[str] = []
        self.fail_send_for = fail_send_for
        self.return_id = return_id
        # Graph accepts the send but the response never arrives (read timeout, late 5xx).
        self.lost_reply_for = set(lost_reply_for)
        self.searches: list[tuple[str, str]] = []

    def create_draft(self, to, subject, body="", from_address=None):
        draft_id = f"d{len(self.drafts) + 1}"
        self.drafts.append(
            {"id": draft_id, "to": to, "subject": subject, "body": body, "from": from_address}
        )
        return {"id": draft_id}

    def send_draft(self, draft_id, *, from_address):
        to = next(d["to"][0] for d in self.drafts if d["id"] == draft_id)
        if to in self.fail_send_for:
            raise RuntimeError("graph 503")
        assert from_address == FROM
        self.sent.append(draft_id)
        if to in self.lost_reply_for:
            self.lost_reply_for.discard(to)
            raise RuntimeError("read timeout")
        return {"status": "sent", "id": f"m-{draft_id}"} if self.return_id else {"status": "sent"}

    def search_sent(self, *, to, subject):
        self.searches.append((to, subject))
        return [
            {
                "message_id": f"m-{d['id']}",
                "subject": d["subject"],
                "sender": FROM,
                "is_draft": False,
            }
            for d in self.drafts
            if d["id"] in self.sent and d["to"] == [to] and d["subject"] == subject
        ]


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(send, "campaign_repo", fake)
    monkeypatch.setattr(campaign_svc, "campaign_repo", fake)
    return fake


@pytest.fixture
def people(monkeypatch):
    state = SimpleNamespace(created=[], by_email={}, next_id=100, fail=set())

    def create(conn, *, contact, notes=None, labels=None):
        email = contact["emailAddresses"][0]["value"]
        if email in state.fail:
            raise person_create.Invalid("bad")
        state.created.append({"contact": contact, "labels": labels})
        state.next_id += 1
        row = {"id": state.next_id, "email": email}
        state.by_email[email] = row
        return row

    monkeypatch.setattr(send.person_create, "create", create)

    def get(conn, email):
        row = state.by_email.get(email)
        return {"message_count": 0, "my_response_count": 0, **row} if row else None

    monkeypatch.setattr(send.people_repo, "get", get)
    monkeypatch.setattr(send.people_repo, "get_by_linkedin_url", lambda conn, url: None)
    return state


def _send(conn, repo, inbox, now=NOW, force=False):
    return send.send_batch(
        conn,
        now=now,
        targets=TARGETS,
        from_address=FROM,
        postal_address=POSTAL,
        inbox=inbox,
        pause_s=3.0,
        sleep=lambda s: None,
        force=force,
    )


# --- restage_stale --------------------------------------------------------------------------


def test_restage_stale_moves_an_eight_day_old_approval_back(repo):
    old = repo.add()
    fresh = repo.add()
    repo.insert_event(None, contact_id=old, status="approved", at=NOW - timedelta(days=8))
    repo.insert_event(None, contact_id=fresh, status="approved", at=NOW - timedelta(days=6))
    conn = FakeConn()
    assert send.restage_stale(conn, now=NOW, stale_after_days=7) == 1
    assert repo.rows[old]["status"] == "drafted"
    assert repo.rows[fresh]["status"] == "approved"
    assert repo.events[-1]["note"] == "stale" and repo.events[-1]["status"] == "drafted"
    assert conn.commits >= 1


def test_restage_stale_uses_the_latest_approval(repo):
    cid = repo.add()
    repo.insert_event(None, contact_id=cid, status="approved", at=NOW - timedelta(days=20))
    repo.insert_event(None, contact_id=cid, status="approved", at=NOW - timedelta(days=1))
    assert send.restage_stale(FakeConn(), now=NOW, stale_after_days=7) == 0


# --- send_batch -----------------------------------------------------------------------------


def test_sends_up_to_the_daily_cap_then_the_rest_next_day(repo, people):
    ids = [repo.add() for _ in range(21)]
    inbox, conn = FakeInbox(), FakeConn()
    result = _send(conn, repo, inbox)
    assert result == send.SendResult(sent=20, remaining=1, halted=False)
    assert [repo.rows[i]["status"] for i in ids[:20]] == ["sent"] * 20
    assert repo.rows[ids[20]]["status"] == "approved"
    assert conn.commits >= 20

    again = _send(conn, repo, inbox)  # same day: sent_today is now 20
    assert again == send.SendResult(sent=0, remaining=1, halted=False)

    tomorrow = _send(conn, repo, inbox, now=NOW + timedelta(days=1))
    assert tomorrow == send.SendResult(sent=1, remaining=0, halted=False)
    assert repo.rows[ids[20]]["status"] == "sent"


def test_cap_counts_sends_already_made_today(repo, people):
    repo.add()
    repo.sent_today_override = 20
    assert _send(FakeConn(), repo, FakeInbox()) == send.SendResult(0, 1, False)


def test_a_sent_contact_carries_draft_message_and_follow_up(repo, people):
    cid = repo.add()
    inbox = FakeInbox()
    _send(FakeConn(), repo, inbox)
    row = repo.rows[cid]
    assert inbox.drafts[0] == {
        "id": "d1",
        "to": [f"sam{cid}@acme.com"],
        "subject": f"Subject {cid}",
        "body": f"Body {cid}",
        "from": FROM,
    }
    assert row["draft_id"] == "d1"
    assert row["graph_message_id"] == "m-d1"
    assert row["sent_at"] == NOW
    assert row["follow_up_on"] == date(2026, 10, 28)
    event = repo.events[-1]
    assert (event["status"], event["source"], event["graph_message_id"]) == ("sent", "cli", "m-d1")


def test_an_older_inbox_api_without_an_id_leaves_graph_message_id_empty(repo, people):
    cid = repo.add()
    _send(FakeConn(), repo, FakeInbox(return_id=False))
    assert repo.rows[cid]["status"] == "sent"
    assert repo.rows[cid]["graph_message_id"] is None


def test_creates_the_people_row_with_the_label(repo, people):
    cid = repo.add(full_name="Sam Q Lee")
    _send(FakeConn(), repo, FakeInbox())
    assert len(people.created) == 1
    created = people.created[0]
    assert created["labels"] == ["consulting-campaign"]
    assert created["contact"] == {
        "names": [{"givenName": "Sam", "familyName": "Q Lee"}],
        "emailAddresses": [{"value": f"sam{cid}@acme.com"}],
        "urls": [{"value": f"https://www.linkedin.com/in/sam{cid}", "type": "profile"}],
        "organizations": [{"name": "Acme", "title": "CTO"}],
    }
    assert repo.rows[cid]["person_id"] == 101


def test_people_row_is_not_created_when_person_id_is_set(repo, people):
    cid = repo.add(person_id=7)
    _send(FakeConn(), repo, FakeInbox())
    assert people.created == []
    assert repo.rows[cid]["status"] == "sent"


def test_an_existing_people_row_is_linked_not_duplicated(repo, people):
    cid = repo.add()
    people.by_email[f"sam{cid}@acme.com"] = {"id": 55}
    _send(FakeConn(), repo, FakeInbox())
    assert people.created == []
    assert repo.rows[cid]["person_id"] == 55


def test_a_duplicate_from_person_create_links_the_candidate(repo, people, monkeypatch):
    cid = repo.add()

    def dup(conn, *, contact, notes=None, labels=None):
        raise person_create.Duplicate("person exists", candidates=[66])

    monkeypatch.setattr(send.person_create, "create", dup)
    _send(FakeConn(), repo, FakeInbox())
    assert repo.rows[cid]["person_id"] == 66
    assert repo.rows[cid]["status"] == "sent"


def test_a_partially_created_person_is_still_linked(repo, people, monkeypatch):
    cid = repo.add()

    def partial(conn, *, contact, notes=None, labels=None):
        raise person_create.PartiallyCreated({"id": 77}, "label failed")

    monkeypatch.setattr(send.person_create, "create", partial)
    _send(FakeConn(), repo, FakeInbox())
    assert repo.rows[cid]["person_id"] == 77
    assert repo.rows[cid]["status"] == "sent"


def test_an_invalid_person_skips_the_contact_and_sends_nothing_for_it(repo, people):
    bad, good = repo.add(), repo.add()
    people.fail.add(f"sam{bad}@acme.com")
    inbox = FakeInbox()
    result = _send(FakeConn(), repo, inbox)
    assert repo.rows[bad]["status"] == "approved"
    assert repo.rows[good]["status"] == "sent"
    assert [d["to"] for d in inbox.drafts] == [[f"sam{good}@acme.com"]]
    assert result == send.SendResult(sent=1, remaining=1, halted=False)


def test_a_failed_send_leaves_the_contact_approved_and_continues(repo, people):
    bad, good = repo.add(), repo.add()
    inbox = FakeInbox(fail_send_for={f"sam{bad}@acme.com"})
    conn = FakeConn()
    result = _send(conn, repo, inbox)
    assert repo.rows[bad]["status"] == "approved"
    assert repo.rows[bad]["person_id"] is not None  # the people row is kept
    assert repo.rows[good]["status"] == "sent"
    assert conn.rollbacks == 1
    assert repo.rows[bad]["draft_id"] == "d1"  # recorded before the send was tried
    assert result == send.SendResult(sent=1, remaining=1, halted=False, needs_check=[bad])


def test_three_bounces_in_the_run_halt_before_any_send(repo, people):
    for _ in range(3):
        cid = repo.add(status="bounced")
        repo.insert_event(None, contact_id=cid, status="bounced", at=NOW - timedelta(hours=1))
    waiting = repo.add()
    inbox = FakeInbox()
    result = _send(FakeConn(), repo, inbox)
    assert result == send.SendResult(sent=0, remaining=1, halted=True)
    assert inbox.drafts == [] and people.created == []
    assert repo.rows[waiting]["status"] == "approved"


def test_two_bounces_do_not_halt(repo, people):
    for _ in range(2):
        cid = repo.add(status="bounced")
        repo.insert_event(None, contact_id=cid, status="bounced", at=NOW - timedelta(hours=1))
    repo.add()
    assert _send(FakeConn(), repo, FakeInbox()).halted is False


def test_sleeps_between_sends_only(repo, people):
    for _ in range(3):
        repo.add()
    slept: list[float] = []
    send.send_batch(
        FakeConn(),
        now=NOW,
        targets=TARGETS,
        from_address=FROM,
        postal_address=POSTAL,
        inbox=FakeInbox(),
        pause_s=3.0,
        sleep=slept.append,
    )
    assert slept == [3.0, 3.0]


def test_writes_the_run_counts(repo, people):
    repo.add()
    repo.add(status="drafted")
    _send(FakeConn(), repo, FakeInbox())
    counts = repo.counts[1]
    assert counts["sent"] == 1 and counts["drafted"] == 1 and counts["approved"] == 0
    assert set(counts) == set(CONTACT_STATUSES)


# --- unknown send outcomes ------------------------------------------------------------------


def test_a_send_accepted_but_unanswered_is_reconciled_not_resent(repo, people):
    cid = repo.add()
    inbox = FakeInbox(lost_reply_for={f"sam{cid}@acme.com"})
    first = _send(FakeConn(), repo, inbox)
    assert first == send.SendResult(sent=0, remaining=1, halted=False, needs_check=[cid])
    assert repo.rows[cid]["status"] == "approved"
    assert repo.rows[cid]["draft_id"] == "d1"

    second = _send(FakeConn(), repo, inbox)
    assert len(inbox.drafts) == 1 and inbox.sent == ["d1"]  # never sent twice
    assert inbox.searches == [(f"sam{cid}@acme.com", f"Subject {cid}")]
    assert second == send.SendResult(sent=1, remaining=0, halted=False)
    row = repo.rows[cid]
    assert (row["status"], row["graph_message_id"]) == ("sent", "m-d1")
    assert row["follow_up_on"] == date(2026, 10, 28)
    assert repo.sent_today(None, date(2026, 10, 21)) == 1


def test_a_draft_with_no_sent_message_is_reported_and_left_alone(repo, people):
    cid = repo.add(draft_id="d-old")
    inbox = FakeInbox()
    result = _send(FakeConn(), repo, inbox)
    assert result == send.SendResult(sent=0, remaining=1, halted=False, needs_check=[cid])
    assert inbox.drafts == [] and inbox.sent == []
    assert repo.rows[cid]["status"] == "approved"


def test_advance_failing_after_a_send_leaves_the_draft_recorded_for_reconcile(
    repo, people, monkeypatch
):
    cid = repo.add()
    inbox = FakeInbox()
    real_advance = campaign_svc.advance

    def broken(*a, **kw):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(campaign_svc, "advance", broken)
    with pytest.raises(RuntimeError):
        _send(FakeConn(), repo, inbox)
    assert repo.rows[cid]["draft_id"] == "d1" and repo.rows[cid]["status"] == "approved"

    monkeypatch.setattr(campaign_svc, "advance", real_advance)
    result = _send(FakeConn(), repo, inbox)
    assert inbox.sent == ["d1"]
    assert result.sent == 1 and repo.rows[cid]["status"] == "sent"
    assert repo.rows[cid]["graph_message_id"] == "m-d1"


def test_restage_stale_leaves_a_contact_with_a_draft_for_ben(repo):
    cid = repo.add(draft_id="d9")
    repo.insert_event(None, contact_id=cid, status="approved", at=NOW - timedelta(days=8))
    assert send.restage_stale(FakeConn(), now=NOW, stale_after_days=7) == 0
    assert repo.rows[cid]["status"] == "approved"
    assert repo.rows[cid]["draft_id"] == "d9"


# --- force and the halt ---------------------------------------------------------------------


def _bounces(repo, n):
    for _ in range(n):
        cid = repo.add(status="bounced")
        repo.insert_event(None, contact_id=cid, status="bounced", at=NOW - timedelta(hours=1))


def test_force_sends_past_the_bounce_halt(repo, people):
    _bounces(repo, 3)
    cid = repo.add()
    result = _send(FakeConn(), repo, FakeInbox(), force=True)
    assert result == send.SendResult(sent=1, remaining=0, halted=False)
    assert repo.rows[cid]["status"] == "sent"


def test_a_halted_run_reports_halted_even_when_the_day_is_full(repo, people):
    _bounces(repo, 3)
    repo.add()
    repo.sent_today_override = TARGETS.per_day
    assert _send(FakeConn(), repo, FakeInbox()).halted is True


def test_the_unsent_draft_itself_is_not_taken_as_a_sent_message(repo, people):
    """Graph's /messages search spans Drafts; a sent draft leaves Drafts with a new id."""
    cid = repo.add(draft_id="d-old")
    inbox = FakeInbox()
    inbox.search_sent = lambda *, to, subject: [{"message_id": "d-old", "subject": subject}]
    result = _send(FakeConn(), repo, inbox)
    assert result.needs_check == [cid] and result.sent == 0
    assert repo.rows[cid]["status"] == "approved"


def _hit(cid, **kw):
    return [{"message_id": "m-sent", "subject": f"Subject {cid}", "is_draft": False, **kw}]


@pytest.mark.parametrize(
    "overrides",
    [{"is_draft": True}, {"is_draft": None}, {"subject": "Re: Subject 1"}],
    ids=["draft", "is_draft-unknown", "other-subject"],
)
def test_reconcile_needs_a_confirmed_sent_item_with_the_exact_subject(repo, people, overrides):
    cid = repo.add(draft_id="d-old")
    inbox = FakeInbox()
    inbox.search_sent = lambda *, to, subject: _hit(cid, **overrides)
    result = _send(FakeConn(), repo, inbox)
    assert result.needs_check == [cid] and result.sent == 0
    assert repo.rows[cid]["status"] == "approved"


def test_reconcile_accepts_a_sent_item_with_the_exact_subject(repo, people):
    cid = repo.add(draft_id="d-old")
    inbox = FakeInbox()
    inbox.search_sent = lambda *, to, subject: _hit(cid)
    result = _send(FakeConn(), repo, inbox)
    assert result.sent == 1 and result.needs_check == []
    assert repo.rows[cid]["status"] == "sent"
    assert repo.rows[cid]["graph_message_id"] == "m-sent"


def test_a_lost_send_counts_against_the_daily_cap(repo, people):
    ids = [repo.add() for _ in range(21)]
    inbox = FakeInbox(lost_reply_for={f"sam{ids[0]}@acme.com"})
    result = _send(FakeConn(), repo, inbox)
    assert len(inbox.sent) <= TARGETS.per_day
    assert result.needs_check == [ids[0]]
    assert result.sent == 19


def test_needs_check_is_reported_when_halted(repo, people):
    _bounces(repo, 3)
    cid = repo.add(draft_id="d-old")
    result = _send(FakeConn(), repo, FakeInbox())
    assert result.halted is True and result.needs_check == [cid]


def test_needs_check_is_reported_when_the_day_is_full(repo, people):
    repo.add()
    cid = repo.add(draft_id="d-old")
    repo.sent_today_override = TARGETS.per_day
    result = _send(FakeConn(), repo, FakeInbox())
    assert result.needs_check == [cid] and result.sent == 0


# --- the sendable gate at send time ---------------------------------------------------------


def test_a_contact_opted_out_between_selection_and_lock_is_never_sent(repo, people):
    stopped, fine = repo.add(), repo.add()

    def opt_out(row):
        if row["id"] == stopped and row["status"] == "approved":
            row["status"] = "opted_out"  # the inbox handler won the race
            repo.companies[1]["status"] = "opted_out"

    repo.on_lock = opt_out
    inbox, conn = FakeInbox(), FakeConn()
    result = _send(conn, repo, inbox)
    assert [d["to"] for d in inbox.drafts] == []
    assert repo.rows[stopped]["status"] == "opted_out"
    assert result.sent == 0 and result.skipped[0][0] == stopped
    assert "opted_out" in result.skipped[0][1]
    assert {cid for cid, _ in result.skipped} == {stopped, fine}  # the company stopped too
    assert repo.rows[stopped]["draft_id"] is None


def test_a_contact_whose_address_bounced_is_not_sent(repo, people):
    gone, fine = repo.add(), repo.add()
    repo.bounced_at[f"sam{gone}@acme.com"] = NOW - timedelta(days=2)
    inbox = FakeInbox()
    result = _send(FakeConn(), repo, inbox)
    assert [d["to"] for d in inbox.drafts] == [[f"sam{fine}@acme.com"]]
    assert result.skipped == [(gone, "bounced address")]
    assert result.sent == 1


def test_a_gate_stopped_approval_becomes_skipped_and_leaves_remaining(repo, people):
    gone, waiting = repo.add(), repo.add()
    repo.bounced_at[f"sam{gone}@acme.com"] = NOW
    repo.sent_today_override = 19  # one slot left: the gated contact must not take it
    result = _send(FakeConn(), repo, FakeInbox())
    assert repo.rows[gone]["status"] == "skipped"
    moves = [e for e in repo.events if e["contact_id"] == gone]
    assert moves[-1]["status"] == "skipped" and moves[-1]["note"] == "bounced address"
    assert result.skipped == [(gone, "bounced address")]
    assert result.sent == 1 and result.remaining == 0
    assert repo.rows[waiting]["status"] == "sent"


def test_a_gate_stopped_contact_is_listed_once(repo, people):
    gone = repo.add()
    repo.bounced_at[f"sam{gone}@acme.com"] = NOW
    assert _send(FakeConn(), repo, FakeInbox()).skipped == [(gone, "bounced address")]
    again = _send(FakeConn(), repo, FakeInbox())
    assert again.skipped == [] and again.remaining == 0


def test_a_contact_found_warm_after_approval_is_not_sent(repo, people):
    warm = repo.add()
    people.by_email[f"sam{warm}@acme.com"] = {"id": 9, "message_count": 2, "my_response_count": 1}
    inbox = FakeInbox()
    result = _send(FakeConn(), repo, inbox)
    assert inbox.drafts == [] and result.skipped == [(warm, "warm")]


def test_a_skipped_contact_does_not_use_a_daily_slot(repo, people):
    gone = repo.add()
    ids = [repo.add() for _ in range(20)]
    repo.bounced_at[f"sam{gone}@acme.com"] = NOW
    result = _send(FakeConn(), repo, FakeInbox())
    assert result.sent == 20 and [repo.rows[i]["status"] for i in ids] == ["sent"] * 20
