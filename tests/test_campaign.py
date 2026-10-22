from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from models.campaign import CONTACT_STATUSES
from services import campaign as svc

TS = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)


class FakeConn:
    """Never used for SQL here: every repo call goes through FakeRepo. Counts commits so
    a test can prove the service leaves committing to the caller."""

    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class FakeRepo:
    """In-memory stand-in for repo/campaign.py: same function names and shapes."""

    def __init__(self):
        self.runs: list[dict] = []
        self.companies: dict[int, dict] = {}
        self.contacts: dict[int, dict] = {}
        self.events: list[dict] = []
        self.outreach_by_domain: dict[str, list[dict]] = {}
        self.locked: list[int] = []
        self.people_bounced: list[tuple[str, datetime]] = []

    # runs
    def latest_run(self, conn):
        return self.runs[-1] if self.runs else None

    def insert_run(self, conn, *, ran_at, lookback_from, lookback_to):
        row = {
            "id": len(self.runs) + 1,
            "ran_at": ran_at,
            "lookback_from": lookback_from,
            "lookback_to": lookback_to,
        }
        self.runs.append(row)
        return dict(row)

    # companies
    def insert_company_if_missing(self, conn, fields):
        if any(c["domain"].lower() == fields["domain"].lower() for c in self.companies.values()):
            return None
        cid = len(self.companies) + 1
        row = {"status": "new", "fit_score": None, "fit_notes": None, **fields, "id": cid}
        self.companies[cid] = row
        return dict(row)

    def get_company(self, conn, company_id):
        row = self.companies.get(company_id)
        return dict(row) if row else None

    def get_company_by_domain(self, conn, domain):
        for c in self.companies.values():
            if c["domain"].lower() == domain.lower():
                return dict(c)
        return None

    def update_company(self, conn, company_id, changes):
        self.companies[company_id].update(changes)
        return dict(self.companies[company_id])

    # contacts
    def insert_contact_if_missing(self, conn, fields):
        for c in self.contacts.values():
            if (
                c["company_id"] == fields["company_id"]
                and fields.get("email")
                and (c.get("email") or "").lower() == fields["email"].lower()
            ):
                return None
        cid = len(self.contacts) + 1
        row = {
            "status": "candidate",
            "sent_at": None,
            "follow_up_on": None,
            "replied_at": None,
            "graph_message_id": None,
            **fields,
            "id": cid,
        }
        self.contacts[cid] = row
        return dict(row)

    def get_contact_by_email(self, conn, email):
        for c in self.contacts.values():
            if (c.get("email") or "").lower() == email.lower():
                return dict(c)
        return None

    def contacts_by_domain(self, conn, domain):
        ids = {i for i, c in self.companies.items() if c["domain"].lower() == domain.lower()}
        return [dict(c) for c in self.contacts.values() if c["company_id"] in ids]

    def lock_contact(self, conn, contact_id):
        self.locked.append(contact_id)
        row = self.contacts.get(contact_id)
        return dict(row) if row else None

    def update_contact(self, conn, contact_id, changes):
        self.contacts[contact_id].update(changes)
        return dict(self.contacts[contact_id])

    def insert_event(self, conn, **kw):
        self.events.append(kw)
        return len(self.events)

    def people_in_outreach_by_domain(self, conn, domain):
        return self.outreach_by_domain.get(domain, [])

    def set_people_bounced(self, conn, email, at):
        self.people_bounced.append((email, at))


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(svc, "campaign_repo", fake)
    return fake


@pytest.fixture
def people(monkeypatch):
    """Scripted people and outreach lookups for the warm guard."""
    state = SimpleNamespace(by_email={}, by_linkedin={}, in_outreach=set())
    monkeypatch.setattr(svc.people_repo, "get", lambda c, email: state.by_email.get(email))
    monkeypatch.setattr(
        svc.people_repo, "get_by_linkedin_url", lambda c, url: state.by_linkedin.get(url)
    )
    monkeypatch.setattr(
        svc.outreach_repo,
        "get_contact",
        lambda c, pid: {"person_id": pid} if pid in state.in_outreach else None,
    )
    return state


def _company(repo, domain="acme.com", status="active"):
    row, _ = svc.add_company(FakeConn(), {"domain": domain, "name": "Acme"}, run_id=1)
    repo.companies[row["id"]]["status"] = status
    return row["id"]


def _contact(repo, company_id, status, email="cto@acme.com"):
    row = repo.insert_contact_if_missing(
        None, {"company_id": company_id, "full_name": "Cy", "email": email}
    )
    repo.contacts[row["id"]]["status"] = status
    return row["id"]


# --- transition table -----------------------------------------------------------------------

ALLOWED = [
    ("candidate", "unverified"),
    ("candidate", "skipped_warm"),
    ("candidate", "drafted"),
    ("candidate", "skipped"),
    ("unverified", "drafted"),
    ("unverified", "skipped"),
    ("drafted", "approved"),
    ("drafted", "skipped"),
    ("approved", "sent"),
    ("approved", "drafted"),
    ("approved", "skipped"),
    ("sent", "followed_up"),
    ("sent", "replied"),
    ("sent", "opted_out"),
    ("sent", "bounced"),
    ("sent", "booked"),
    ("followed_up", "replied"),
    ("followed_up", "opted_out"),
    ("followed_up", "bounced"),
    ("followed_up", "booked"),
    ("replied", "opted_out"),
    ("replied", "booked"),
]


def test_allowed_table_is_the_brief_verbatim():
    expected: dict[str, set[str]] = {s: set() for s in CONTACT_STATUSES}
    for current, target in ALLOWED:
        expected[current].add(target)
    assert svc._ALLOWED == expected


@pytest.mark.parametrize("current,target", ALLOWED)
def test_moves_in_the_table_advance(current, target):
    assert svc.transition(current, target) == "advance"


@pytest.mark.parametrize(
    "current,target",
    [
        ("drafted", "sent"),
        ("candidate", "approved"),
        ("unverified", "approved"),
        ("candidate", "sent"),
        ("replied", "bounced"),
        ("bounced", "replied"),
        ("skipped", "drafted"),
        ("skipped_warm", "drafted"),
        ("booked", "opted_out"),
    ],
)
def test_moves_outside_the_table_are_invalid(current, target):
    assert svc.transition(current, target) == "invalid"


@pytest.mark.parametrize(
    "current,target",
    [
        ("sent", "sent"),
        ("sent", "approved"),
        ("followed_up", "sent"),
        ("replied", "followed_up"),
        ("opted_out", "replied"),
        ("booked", "replied"),
        ("bounced", "sent"),
        *[(s, s) for s in ("skipped_warm", "skipped", "opted_out", "bounced", "booked")],
    ],
)
def test_reaching_a_status_already_passed_is_a_noop(current, target):
    assert svc.transition(current, target) == "noop"


def test_unknown_status_is_a_programming_error():
    with pytest.raises(ValueError):
        svc.transition("sent", "ghosted")


def test_reached_sets_include_their_own_status():
    for status in CONTACT_STATUSES:
        assert status in svc._REACHED[status]
        assert not (svc._REACHED[status] - {status}) & svc._ALLOWED[status]


# --- advance --------------------------------------------------------------------------------


def test_advance_drafted_to_sent_raises(repo):
    cid = _contact(repo, _company(repo), "drafted")
    with pytest.raises(svc.InvalidTransition) as e:
        svc.advance(FakeConn(), cid, "sent", at=TS, source="cli")
    assert "drafted" in str(e.value) and "sent" in str(e.value)
    assert isinstance(e.value, ValueError)
    assert repo.contacts[cid]["status"] == "drafted"


def test_advance_already_sent_is_a_noop_with_no_event(repo):
    cid = _contact(repo, _company(repo), "sent")
    before = len(repo.events)
    res = svc.advance(FakeConn(), cid, "sent", at=TS, source="event")
    assert res.changed is False and res.event_id is None
    assert res.row["status"] == "sent"
    assert len(repo.events) == before
    assert repo.locked == [cid]


def test_advance_terminal_status_already_held_is_unchanged(repo):
    cid = _contact(repo, _company(repo), "opted_out")
    res = svc.advance(FakeConn(), cid, "opted_out", at=TS, source="event")
    assert res.changed is False


def test_advance_unknown_contact_raises_not_found(repo):
    with pytest.raises(svc.NotFound):
        svc.advance(FakeConn(), 99, "sent", at=TS, source="cli")


def test_advance_to_sent_writes_sent_at_follow_up_and_message_id(repo):
    co = _company(repo)
    cid = _contact(repo, co, "approved")
    conn = FakeConn()
    res = svc.advance(conn, cid, "sent", at=TS, source="cli", graph_message_id="m1")
    assert res.changed is True and res.event_id is not None
    assert res.row["status"] == "sent"
    assert res.row["sent_at"] == TS
    assert res.row["follow_up_on"] == svc.follow_up_date(TS, svc.FOLLOW_UP_BUSINESS_DAYS)
    assert res.row["graph_message_id"] == "m1"
    assert repo.events[-1] == {
        "contact_id": cid,
        "company_id": co,
        "status": "sent",
        "at": TS,
        "source": "cli",
        "graph_message_id": "m1",
        "note": None,
    }
    assert conn.commits == 0


def test_advance_to_sent_takes_extra_columns(repo):
    cid = _contact(repo, _company(repo), "approved")
    res = svc.advance(
        FakeConn(),
        cid,
        "sent",
        at=TS,
        source="cli",
        changes={"draft_id": "d1", "follow_up_on": date(2026, 11, 2)},
    )
    assert res.row["draft_id"] == "d1"
    assert res.row["follow_up_on"] == date(2026, 11, 2)


def test_advance_to_replied_sets_replied_at_once_and_clears_follow_up(repo):
    cid = _contact(repo, _company(repo), "sent")
    repo.contacts[cid]["follow_up_on"] = date(2026, 10, 28)
    res = svc.advance(FakeConn(), cid, "replied", at=TS, source="event", graph_message_id="r1")
    assert res.row["replied_at"] == TS
    assert res.row["follow_up_on"] is None
    # The reply's message id belongs on the event, not on the contact (that is the sent one).
    assert res.row["graph_message_id"] is None
    assert repo.events[-1]["graph_message_id"] == "r1"


def test_advance_to_followed_up_clears_follow_up_on(repo):
    cid = _contact(repo, _company(repo), "sent")
    repo.contacts[cid]["follow_up_on"] = date(2026, 10, 28)
    res = svc.advance(FakeConn(), cid, "followed_up", at=TS, source="cli")
    assert res.row["follow_up_on"] is None


def test_advance_stale_approval_back_to_drafted(repo):
    cid = _contact(repo, _company(repo), "approved")
    res = svc.advance(FakeConn(), cid, "drafted", at=TS, source="cli", note="stale")
    assert res.changed is True and res.row["status"] == "drafted"
    assert repo.events[-1]["note"] == "stale"


# --- follow_up_date -------------------------------------------------------------------------


def test_follow_up_date_skips_the_weekend():
    friday_et = datetime(2026, 10, 23, 19, 0, tzinfo=UTC)  # 15:00 ET
    assert svc.follow_up_date(friday_et, 5) == date(2026, 10, 30)


def test_follow_up_date_counts_from_the_local_date():
    # Monday 2026-10-26 01:00 UTC is still Sunday 21:00 in New York.
    assert svc.follow_up_date(datetime(2026, 10, 26, 1, 0, tzinfo=UTC), 1) == date(2026, 10, 26)
    assert svc.follow_up_date(datetime(2026, 10, 21, 15, 0, tzinfo=UTC), 2) == date(2026, 10, 23)


# --- runs -----------------------------------------------------------------------------------


def test_start_run_first_run_uses_the_first_run_lookback(repo):
    targets = SimpleNamespace(lookback_days_first_run=30)
    run = svc.start_run(FakeConn(), now=TS, targets=targets)
    assert run["lookback_from"] == date(2026, 9, 21)
    assert run["lookback_to"] == date(2026, 10, 21)
    assert run["ran_at"] == TS


def test_start_run_later_run_looks_back_to_the_previous_run(repo):
    targets = SimpleNamespace(lookback_days_first_run=30)
    svc.start_run(FakeConn(), now=TS - timedelta(days=7), targets=targets)
    run = svc.start_run(FakeConn(), now=TS, targets=targets)
    assert run["lookback_from"] == date(2026, 10, 14)
    assert run["lookback_to"] == date(2026, 10, 21)


def test_start_run_lookback_days_overrides_the_previous_run(repo):
    targets = SimpleNamespace(lookback_days_first_run=30)
    svc.start_run(FakeConn(), now=TS - timedelta(days=7), targets=targets)
    run = svc.start_run(FakeConn(), now=TS, targets=targets, lookback_days=10)
    assert run["lookback_from"] == date(2026, 10, 11)
    assert run["lookback_to"] == date(2026, 10, 21)


# --- companies ------------------------------------------------------------------------------


def test_add_company_twice_returns_the_existing_row(repo):
    first, created = svc.add_company(FakeConn(), {"domain": "acme.com", "name": "Acme"}, run_id=1)
    second, created_again = svc.add_company(
        FakeConn(), {"domain": "Acme.com", "name": "Acme Inc"}, run_id=1
    )
    assert created is True and created_again is False
    assert second["id"] == first["id"] and second["name"] == "Acme"
    assert len(repo.companies) == 1


def test_add_company_lower_cases_the_domain_and_sets_the_run(repo):
    row, _ = svc.add_company(FakeConn(), {"domain": " Acme.COM ", "name": "Acme"}, run_id=4)
    assert row["domain"] == "acme.com" and row["run_id"] == 4


@pytest.mark.parametrize("country", ["US", "usa", "United States", "united states", None])
def test_add_company_us_or_unknown_country_is_new(repo, country):
    row, _ = svc.add_company(
        FakeConn(), {"domain": "acme.com", "name": "Acme", "hq_country": country}, run_id=1
    )
    assert row["status"] == "new" and row["fit_notes"] is None


def test_add_company_non_us_is_below_bar(repo):
    row, created = svc.add_company(
        FakeConn(), {"domain": "acme.de", "name": "Acme", "hq_country": "Germany"}, run_id=1
    )
    assert created is True
    assert row["status"] == "below_bar" and row["fit_notes"] == "non-US"
    assert repo.events[-1]["status"] == "below_bar"
    assert repo.events[-1]["company_id"] == row["id"]


@pytest.mark.parametrize("score,status", [(6, "active"), (8, "active"), (5, "below_bar")])
def test_score_company_against_the_threshold(repo, score, status):
    co = _company(repo, status="new")
    row = svc.score_company(FakeConn(), co, score=score, notes="fit", threshold=6)
    assert (row["fit_score"], row["fit_notes"], row["status"]) == (score, "fit", status)
    assert repo.events[-1]["status"] == status and repo.events[-1]["company_id"] == co


def test_score_company_unknown_raises(repo):
    with pytest.raises(svc.NotFound):
        svc.score_company(FakeConn(), 9, score=7, notes="", threshold=6)


def test_promote_company_from_below_bar(repo):
    co = _company(repo, status="below_bar")
    assert svc.promote_company(FakeConn(), co)["status"] == "active"
    assert repo.events[-1]["status"] == "active"


def test_promote_company_rejects_anything_else(repo):
    co = _company(repo, status="opted_out")
    with pytest.raises(svc.InvalidTransition):
        svc.promote_company(FakeConn(), co)


# --- contacts and the warm guard ------------------------------------------------------------


def _fields(co, **kw):
    return {
        "company_id": co,
        "full_name": "Cy Tech",
        "email": "cto@acme.com",
        "linkedin_url": "https://www.linkedin.com/in/cytech/",
        **kw,
    }


def test_add_contact_cold_is_a_candidate(repo, people):
    co = _company(repo)
    row, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "candidate" and row["status"] == "candidate" and row["run_id"] == 2
    assert repo.events[-1]["status"] == "candidate"
    assert repo.companies[co]["status"] == "active"


def test_add_contact_two_way_history_is_warm(repo, people):
    co = _company(repo)
    people.by_email["cto@acme.com"] = {"id": 7, "message_count": 3, "my_response_count": 1}
    conn = FakeConn()
    row, kind = svc.add_contact(conn, _fields(co), run_id=2)
    assert kind == "skipped_warm" and row["status"] == "skipped_warm"
    assert repo.companies[co]["status"] == "warm"
    assert {e["status"] for e in repo.events if e["company_id"] == co} >= {"warm"}
    assert conn.commits == 0


def test_add_contact_one_way_history_is_not_warm(repo, people):
    co = _company(repo)
    people.by_email["cto@acme.com"] = {"id": 7, "message_count": 3, "my_response_count": 0}
    _, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "candidate"


def test_add_contact_linkedin_person_in_outreach_is_warm(repo, people):
    co = _company(repo)
    people.by_linkedin["linkedin.com/in/cytech"] = {"id": 11}
    people.in_outreach.add(11)
    _, kind = svc.add_contact(FakeConn(), _fields(co, email="cy@acme.com"), run_id=2)
    assert kind == "skipped_warm"


def test_add_contact_linkedin_person_not_in_outreach_is_cold(repo, people):
    co = _company(repo)
    people.by_linkedin["linkedin.com/in/cytech"] = {"id": 11}
    _, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "candidate"


def test_add_contact_domain_colleague_in_outreach_is_warm(repo, people):
    co = _company(repo)
    repo.outreach_by_domain["acme.com"] = [{"id": 12, "email": "ceo@acme.com"}]
    _, kind = svc.add_contact(FakeConn(), _fields(co, linkedin_url=None), run_id=2)
    assert kind == "skipped_warm"
    assert repo.companies[co]["status"] == "warm"


@pytest.mark.parametrize("status", ["opted_out", "closed"])
def test_add_contact_at_a_stopped_company_is_opted_out(repo, people, status):
    co = _company(repo, status=status)
    row, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "opted_out" and row["status"] == "opted_out"
    assert repo.events[-1] == {
        "contact_id": row["id"],
        "company_id": co,
        "status": "opted_out",
        "at": repo.events[-1]["at"],
        "source": "cli",
        "note": "company opted out",
    }
    assert repo.companies[co]["status"] == status


@pytest.mark.parametrize("status", ["opted_out", "closed"])
def test_warm_contact_never_reopens_a_stopped_company(repo, people, status):
    co = _company(repo, status=status)
    people.by_email["cto@acme.com"] = {"id": 7, "message_count": 3, "my_response_count": 1}
    row, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "opted_out" and row["status"] == "opted_out"
    assert repo.companies[co]["status"] == status
    assert not [e for e in repo.events if e["status"] == "warm"]


@pytest.mark.parametrize("status", ["new", "below_bar", "active"])
def test_warm_contact_flips_an_open_company(repo, people, status):
    co = _company(repo, status=status)
    people.by_email["cto@acme.com"] = {"id": 7, "message_count": 3, "my_response_count": 1}
    _, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "skipped_warm" and repo.companies[co]["status"] == "warm"


def test_warm_contact_at_a_warm_company_adds_no_company_event(repo, people):
    co = _company(repo, status="warm")
    people.by_email["cto@acme.com"] = {"id": 7, "message_count": 3, "my_response_count": 1}
    _, kind = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    assert kind == "skipped_warm"
    assert not [e for e in repo.events if e["contact_id"] is None]


def test_add_contact_twice_returns_existing(repo, people):
    co = _company(repo)
    first, _ = svc.add_contact(FakeConn(), _fields(co), run_id=2)
    events = len(repo.events)
    again, kind = svc.add_contact(FakeConn(), _fields(co, email="CTO@acme.com"), run_id=3)
    assert kind == "existing" and again["id"] == first["id"]
    assert len(repo.events) == events and len(repo.contacts) == 1


# --- opt-out --------------------------------------------------------------------------------


def test_opt_out_company_marks_every_open_contact(repo):
    co = _company(repo)
    sent = _contact(repo, co, "sent", "a@acme.com")
    drafted = _contact(repo, co, "drafted", "b@acme.com")
    bounced = _contact(repo, co, "bounced", "c@acme.com")
    already = _contact(repo, co, "opted_out", "d@acme.com")
    other = _contact(repo, _company(repo, "other.com"), "sent", "x@other.com")
    conn = FakeConn()
    n = svc.opt_out_company(conn, co, at=TS, source="event", note="No")
    assert n == 2
    assert repo.contacts[sent]["status"] == "opted_out"
    assert repo.contacts[drafted]["status"] == "opted_out"
    assert repo.contacts[bounced]["status"] == "bounced"
    assert repo.contacts[already]["status"] == "opted_out"
    assert repo.contacts[other]["status"] == "sent"
    assert repo.companies[co]["status"] == "opted_out"
    statuses = [(e["contact_id"], e["status"]) for e in repo.events if e["status"] == "opted_out"]
    assert sorted(statuses, key=str) == sorted(
        [(sent, "opted_out"), (drafted, "opted_out"), (None, "opted_out")], key=str
    )
    assert conn.commits == 0


def test_opt_out_company_unknown_raises(repo):
    with pytest.raises(svc.NotFound):
        svc.opt_out_company(FakeConn(), 9, at=TS, source="cli", note=None)


@pytest.mark.parametrize(
    "body",
    [
        "No.\n\nSent from my phone",
        "Not interested, thanks",
        "no",
        "  \n\nUnsubscribe\n",
        "NO THANKS!",
        "Please remove me.",
        "Remove",
        "Not interested in this right now",
        "unsubscribe me please",
        "No, thanks.",
        "No thank you",
        "No thankyou",
        "no, not for us",
        "No - not interested",
        "No need",
        "Hi Ben,\n\nNot interested.",
        "Thanks for reaching out.\n\nNo thanks.",
        "Good morning Ben\nunsubscribe",
    ],
)
def test_is_opt_out_true(body):
    assert svc.is_opt_out(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "No problem, send me the intake",
        "Not now, but maybe in Q1",
        "Nothing to add",
        "Sounds good. Not interested in the others though",
        "",
        "   \n  ",
        "Notice: I'm out of office",
        None,
        "No worries, happy to chat",
        "No rush on this",
        "No doubt you're busy",
        "Hi Ben,\n\nHappy to talk, send the intake",
        "Thanks for reaching out.\n\nSounds great.\n\nNo thanks to the newsletter though",
        "Hi Ben,\nHello again\nSounds good\nNo",
    ],
)
def test_is_opt_out_false(body):
    assert svc.is_opt_out(body) is False


# --- inbox events ---------------------------------------------------------------------------

CAMPAIGN_SENDER = "ben@consulting.example.com"


@pytest.fixture
def sender(monkeypatch):
    monkeypatch.delenv("CAMPAIGN_FROM_ADDRESS", raising=False)
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", f"{CAMPAIGN_SENDER},ben@alias.example.com")
    return CAMPAIGN_SENDER


def test_campaign_from_address_defaults_to_the_brief_sender(sender):
    assert svc.campaign_from_address() == sender


def test_campaign_from_address_env_override(sender, monkeypatch):
    monkeypatch.setenv("CAMPAIGN_FROM_ADDRESS", "campaign@example.com")
    assert svc.campaign_from_address() == "campaign@example.com"


def test_reply_from_a_colleague_marks_the_sent_contact_replied(repo):
    co = _company(repo)
    cid = _contact(repo, co, "sent")
    conn = FakeConn()
    got = svc.record_inbound_from_event(
        conn,
        sender="Colleague@Acme.com",
        body="Looping in our CTO, who owns this.",
        category="respond",
        graph_message_id="r1",
        at=TS,
    )
    assert got == "replied"
    assert repo.contacts[cid]["status"] == "replied" and repo.contacts[cid]["replied_at"] == TS
    assert repo.events[-1]["status"] == "replied"
    assert repo.events[-1]["source"] == "event"
    assert repo.events[-1]["graph_message_id"] == "r1"
    assert conn.commits == 0


def test_domain_fallback_picks_the_most_recent_send(repo):
    co = _company(repo)
    older = _contact(repo, co, "followed_up", "a@acme.com")
    newer = _contact(repo, co, "sent", "b@acme.com")
    _contact(repo, co, "drafted", "c@acme.com")
    repo.contacts[older]["sent_at"] = TS - timedelta(days=9)
    repo.contacts[newer]["sent_at"] = TS - timedelta(days=1)
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="x@acme.com",
        body="Sure",
        category="respond",
        graph_message_id="r1",
        at=TS,
    )
    assert got == "replied"
    assert repo.contacts[newer]["status"] == "replied"
    assert repo.contacts[older]["status"] == "followed_up"


def test_ignored_no_opts_the_company_out(repo):
    co = _company(repo)
    cid = _contact(repo, co, "sent")
    other = _contact(repo, co, "drafted", "ceo@acme.com")
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="cto@acme.com",
        body="No",
        category="ignore",
        graph_message_id="r1",
        at=TS,
    )
    assert got == "opted_out"
    assert repo.contacts[cid]["status"] == "opted_out"
    assert repo.contacts[other]["status"] == "opted_out"
    assert repo.companies[co]["status"] == "opted_out"
    assert repo.events[-1]["note"] == "reply from cto@acme.com"


def test_ignored_mail_that_is_not_an_opt_out_writes_nothing(repo):
    co = _company(repo)
    cid = _contact(repo, co, "sent")
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="cto@acme.com",
        body="I am out of the office until Monday.",
        category="ignore",
        graph_message_id="r1",
        at=TS,
    )
    assert got is None and repo.contacts[cid]["status"] == "sent" and repo.events == []


def test_unknown_sender_writes_nothing(repo):
    co = _company(repo)
    _contact(repo, co, "sent")
    events = len(repo.events)
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="someone@elsewhere.com",
        body="No",
        category="respond",
        graph_message_id="r1",
        at=TS,
    )
    assert got is None and len(repo.events) == events


def test_domain_with_no_sent_contact_writes_nothing(repo):
    co = _company(repo)
    _contact(repo, co, "drafted")
    events = len(repo.events)
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="x@acme.com",
        body="Thanks",
        category="respond",
        graph_message_id="r1",
        at=TS,
    )
    assert got is None and len(repo.events) == events


def test_reply_on_a_bounced_contact_is_skipped(repo):
    co = _company(repo)
    cid = _contact(repo, co, "bounced")
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="cto@acme.com",
        body="Sure",
        category="respond",
        graph_message_id="r1",
        at=TS,
    )
    assert got is None and repo.contacts[cid]["status"] == "bounced" and repo.events == []


def test_confirm_sent_ignores_another_sender(repo, sender):
    co = _company(repo)
    cid = _contact(repo, co, "approved")
    n = svc.confirm_sent_from_event(
        FakeConn(),
        to=["cto@acme.com"],
        from_address="ben@alias.example.com",
        graph_message_id="g1",
        at=TS,
    )
    assert n == 0 and repo.contacts[cid]["status"] == "approved"


def test_confirm_sent_advances_an_approved_contact(repo, sender):
    co = _company(repo)
    cid = _contact(repo, co, "approved")
    n = svc.confirm_sent_from_event(
        FakeConn(),
        to=["CTO@acme.com"],
        from_address=" Ben@Consulting.Example.com",
        graph_message_id="g1",
        at=TS,
    )
    assert n == 1
    row = repo.contacts[cid]
    assert row["status"] == "sent" and row["graph_message_id"] == "g1" and row["sent_at"] == TS
    assert repo.events[-1]["source"] == "event" and repo.events[-1]["status"] == "sent"


def test_confirm_sent_fills_a_missing_message_id(repo, sender):
    co = _company(repo)
    cid = _contact(repo, co, "sent")
    kept = _contact(repo, co, "sent", "ceo@acme.com")
    repo.contacts[kept]["graph_message_id"] = "g0"
    events = len(repo.events)
    n = svc.confirm_sent_from_event(
        FakeConn(),
        to=["cto@acme.com", "ceo@acme.com", "x@nowhere.com"],
        from_address=sender,
        graph_message_id="g1",
        at=TS,
    )
    assert n == 1
    assert repo.contacts[cid]["graph_message_id"] == "g1"
    assert repo.contacts[kept]["graph_message_id"] == "g0"
    assert len(repo.events) == events and repo.contacts[cid]["status"] == "sent"


def test_bounce_with_one_known_recipient(repo):
    co = _company(repo)
    cid = _contact(repo, co, "sent")
    n = svc.record_bounce_from_event(
        FakeConn(),
        failed_recipients=["CTO@acme.com", "nobody@else.com"],
        graph_message_id="n1",
        at=TS,
        status="5.1.1",
        diagnostic="user unknown",
    )
    assert n == 1
    assert repo.contacts[cid]["status"] == "bounced"
    assert repo.events[-1]["source"] == "event" and repo.events[-1]["note"] == "5.1.1 user unknown"
    assert repo.people_bounced == [("cto@acme.com", TS), ("nobody@else.com", TS)]


def test_late_bounce_on_an_opted_out_contact_is_skipped(repo):
    co = _company(repo)
    cid = _contact(repo, co, "opted_out")
    n = svc.record_bounce_from_event(
        FakeConn(),
        failed_recipients=["cto@acme.com"],
        graph_message_id="n1",
        at=TS,
    )
    assert n == 0 and repo.contacts[cid]["status"] == "opted_out" and repo.events == []
    assert repo.people_bounced == [("cto@acme.com", TS)]


def test_colleague_opt_out_after_a_reply_still_stops_the_company(repo):
    co = _company(repo)
    cto = _contact(repo, co, "replied")
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="ceo@acme.com",
        body="No thanks, not interested.",
        category="respond",
        graph_message_id="r2",
        at=TS,
    )
    assert got == "opted_out"
    assert repo.companies[co]["status"] == "opted_out"
    assert repo.contacts[cto]["status"] == "opted_out"


def test_opt_out_resolves_the_company_from_a_terminal_contact(repo):
    co = _company(repo)
    _contact(repo, co, "bounced")
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="ops@acme.com",
        body="Unsubscribe",
        category="ignore",
        graph_message_id="r2",
        at=TS,
    )
    assert got == "opted_out" and repo.companies[co]["status"] == "opted_out"


def test_reply_from_a_drafted_contact_falls_through_to_the_sent_one(repo):
    co = _company(repo)
    cto = _contact(repo, co, "sent", "cto@acme.com")
    ceo = _contact(repo, co, "drafted", "ceo@acme.com")
    got = svc.record_inbound_from_event(
        FakeConn(),
        sender="ceo@acme.com",
        body="Sure, let's talk.",
        category="respond",
        graph_message_id="r3",
        at=TS,
    )
    assert got == "replied"
    assert repo.contacts[cto]["status"] == "replied"
    assert repo.contacts[ceo]["status"] == "drafted"


# --- annotate -------------------------------------------------------------------------------


def test_annotate_appends_an_event_at_the_current_status_without_a_move(repo):
    cid = _contact(repo, _company(repo), "followed_up")
    conn = FakeConn()
    before = len(repo.events)
    event_id = svc.annotate(conn, cid, at=TS, source="cli", note="follow-up: sent (reply)")
    assert repo.contacts[cid]["status"] == "followed_up"
    assert len(repo.events) == before + 1 and event_id == len(repo.events)
    event = repo.events[-1]
    assert event["contact_id"] == cid and event["company_id"] == repo.contacts[cid]["company_id"]
    assert (event["status"], event["at"], event["source"], event["note"]) == (
        "followed_up",
        TS,
        "cli",
        "follow-up: sent (reply)",
    )
    assert conn.commits == 0


def test_annotate_unknown_contact_raises_not_found(repo):
    with pytest.raises(svc.NotFound):
        svc.annotate(FakeConn(), 999, at=TS, source="cli", note="x")
