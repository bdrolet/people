import io
import json
from datetime import UTC, date, datetime

import pytest

from clients.claude_cli import ClaudeCliError
from models.campaign import Advance
from scripts import campaign as cli
from services.campaign_factcheck import FactCheck
from services.campaign_followups import FollowUpResult
from services.campaign_send import SendResult
from services.campaign_targets import CampaignTargets

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
TARGETS = CampaignTargets(
    threshold=7,
    slots=3,
    slots_through="November",
    per_day=20,
    follow_up_business_days=5,
    lookback_days_first_run=14,
    stale_after_days=3,
)
EXCERPT = "Acme today announced a $12M Series A to build widgets."


class Conn:
    def __init__(self):
        self.log: list = []

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Store:
    """In-memory stand-in for the repo reads the CLI makes, plus a record of service calls."""

    def __init__(self):
        self.run = {
            "id": 3,
            "ran_at": NOW,
            "lookback_from": date(2026, 10, 7),
            "lookback_to": date(2026, 10, 21),
            "counts": {"sent": 2},
            "notes": None,
        }
        self.companies = {
            1: {
                "id": 1,
                "domain": "acme.com",
                "name": "Acme",
                "round": "a",
                "status": "active",
                "fit_score": 8,
                "fit_notes": "k8s",
                "product_summary": "widgets",
                "announcement_excerpt": EXCERPT,
            }
        }
        self.contacts = {
            5: {
                "id": 5,
                "company_id": 1,
                "full_name": "Jane Doe",
                "title": "CTO",
                "email": "jane@acme.com",
                "verification": "valid",
                "status": "candidate",
                "opener_generic": False,
                "body": "Hi Jane",
                "subject": "s",
            }
        }
        self.notes: dict[int, str] = {}
        self.advances: list[dict] = []
        self.updates: list[tuple[int, dict]] = []
        self.people: dict[str, dict] = {}  # people rows by email, for the warm guard
        self.bounced_at: dict[str, datetime] = {}
        self.people_bounced: list[tuple[str, datetime]] = []
        self.company_events: list[dict] = []
        self.sent_today = 0


@pytest.fixture
def store(monkeypatch):
    s = Store()
    repo = cli.campaign_repo
    monkeypatch.setattr(repo, "latest_run", lambda c: s.run)
    monkeypatch.setattr(repo, "get_company", lambda c, i: s.companies.get(i))
    monkeypatch.setattr(repo, "get_contact", lambda c, i: s.contacts.get(i))
    monkeypatch.setattr(
        repo,
        "get_contact_by_email",
        lambda c, e: next((r for r in s.contacts.values() if r["email"] == e), None),
    )
    monkeypatch.setattr(repo, "latest_event_note", lambda c, i: s.notes.get(i))

    def contacts(c, *, status=None, run_id=None, follow_up_due_on=None):
        return [r for r in s.contacts.values() if status is None or r["status"] == status]

    def companies(c, *, status=None, run_id=None):
        return [r for r in s.companies.values() if status is None or r["status"] == status]

    monkeypatch.setattr(repo, "contacts", contacts)
    monkeypatch.setattr(repo, "companies", companies)

    def update_contact(c, cid, changes):
        s.updates.append((cid, changes))
        s.contacts[cid].update(changes)
        return dict(s.contacts[cid])

    monkeypatch.setattr(repo, "update_contact", update_contact)

    def update_company(c, cid, changes):
        s.companies[cid].update(changes)
        return dict(s.companies[cid])

    monkeypatch.setattr(repo, "update_company", update_company)
    monkeypatch.setattr(repo, "insert_event", lambda c, **kw: s.company_events.append(kw) or 97)
    monkeypatch.setattr(repo, "email_bounced_at", lambda c, e: s.bounced_at.get(e.lower()))
    monkeypatch.setattr(repo, "people_in_outreach_by_domain", lambda c, d: [])
    monkeypatch.setattr(
        repo, "set_people_bounced", lambda c, e, at: s.people_bounced.append((e, at))
    )
    monkeypatch.setattr(repo, "sent_today", lambda c, on, tz="America/New_York": s.sent_today)
    monkeypatch.setattr(cli.campaign.people_repo, "get", lambda c, e: s.people.get(e))
    monkeypatch.setattr(cli.campaign.people_repo, "get_by_linkedin_url", lambda c, u: None)

    def advance(c, cid, status, **kw):
        s.advances.append({"id": cid, "status": status, **kw})
        row = s.contacts[cid]
        if row["status"] == status:
            return Advance(dict(row), False, None)
        row.update(kw.get("changes") or {}, status=status)
        return Advance(dict(row), True, 99)

    monkeypatch.setattr(cli.campaign, "advance", advance)

    def annotate(c, cid, *, at, source, note):
        s.notes[cid] = note
        return 98

    monkeypatch.setattr(cli.campaign, "annotate", annotate)
    return s


@pytest.fixture
def run(monkeypatch, tmp_path, store):
    # scripts/campaign.py loads .env at import; never let the real values leak into tests.
    monkeypatch.setenv("CAMPAIGN_TARGETS", str(tmp_path / "targets.toml"))
    monkeypatch.setenv("CAMPAIGN_POSTAL_ADDRESS", "1 Main St, Springfield")
    monkeypatch.setenv("CAMPAIGN_FROM_ADDRESS", "ben@drolet.ai")
    monkeypatch.setenv("INBOX_API_URL", "https://inbox.example")
    monkeypatch.setattr(cli, "CAMPAIGN_DIR", tmp_path / ".campaign")
    monkeypatch.setattr(cli.campaign_targets, "load_targets", lambda path: TARGETS)
    monkeypatch.setattr(cli.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(cli.otel, "flush", lambda: None)
    conn = Conn()

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(list(argv), lambda: conn, out=out, err=err, now=NOW)
        return code, out.getvalue(), err.getvalue()

    _run.conn = conn
    _run.tmp = tmp_path
    return _run


# --- run ------------------------------------------------------------------------------------


def test_run_start_prints_the_id_and_window(monkeypatch, run):
    seen = {}

    def start_run(c, *, now, targets, lookback_days=None):
        seen.update(now=now, targets=targets, lookback_days=lookback_days)
        return {"id": 4, "lookback_from": date(2026, 10, 7), "lookback_to": date(2026, 10, 21)}

    monkeypatch.setattr(cli.campaign, "start_run", start_run)
    code, out, _ = run("run", "start")
    assert code == 0 and "run 4" in out and "2026-10-07" in out and "2026-10-21" in out
    assert seen == {"now": NOW, "targets": TARGETS, "lookback_days": None}
    assert "commit" in run.conn.log


def test_run_start_passes_the_lookback_through(monkeypatch, run):
    seen = {}

    def start_run(c, *, now, targets, lookback_days=None):
        seen["lookback_days"] = lookback_days
        return {"id": 4, "lookback_from": date(2026, 9, 21), "lookback_to": date(2026, 10, 21)}

    monkeypatch.setattr(cli.campaign, "start_run", start_run)
    assert run("run", "start", "--lookback-days", "30")[0] == 0
    assert seen == {"lookback_days": 30}


def test_run_start_without_targets_is_an_error(monkeypatch, run):
    monkeypatch.delenv("CAMPAIGN_TARGETS")
    code, _, err = run("run", "start")
    assert code == 1 and "CAMPAIGN_TARGETS" in err


def test_run_show_text_and_json(run):
    code, out, _ = run("run", "show")
    assert code == 0 and "run 3" in out and "sent" in out
    code, out, _ = run("run", "show", "--json")
    assert code == 0 and json.loads(out)["id"] == 3


# --- companies ------------------------------------------------------------------------------


def test_company_add_twice_prints_added_then_exists(monkeypatch, run):
    excerpt = run.tmp / "excerpt.txt"
    excerpt.write_text(EXCERPT)
    seen = []

    def add_company(c, fields, *, run_id):
        seen.append((fields, run_id))
        return {"id": 11}, len(seen) == 1

    monkeypatch.setattr(cli.campaign, "add_company", add_company)
    argv = [
        "company", "add", "--domain", "acme.com", "--name", "Acme", "--round", "a",
        "--announced-on", "2026-10-20", "--source-url", "https://news/x",
        "--product", "widgets", "--excerpt-file", str(excerpt), "--amount", "12M",
    ]  # fmt: skip
    assert run(*argv)[1].strip() == "added 11"
    assert run(*argv)[1].strip() == "exists 11"
    fields, run_id = seen[0]
    assert run_id == 3
    assert fields["announcement_excerpt"] == EXCERPT
    assert fields["hq_country"] == "US" and fields["amount"] == 12_000_000
    assert fields["announced_on"] == date(2026, 10, 20) and fields["product_summary"] == "widgets"


def test_company_score_prints_the_status(monkeypatch, run):
    seen = {}

    def score_company(c, cid, *, score, notes, threshold):
        seen.update(cid=cid, score=score, notes=notes, threshold=threshold)
        return {"id": cid, "status": "below_bar"}

    monkeypatch.setattr(cli.campaign, "score_company", score_company)
    code, out, _ = run("company", "score", "1", "--score", "5", "--notes", "no k8s")
    assert code == 0 and "below_bar" in out
    assert seen == {"cid": 1, "score": 5, "notes": "no k8s", "threshold": 7}


def test_company_list_json(run):
    code, out, _ = run("company", "list", "--status", "active", "--json")
    assert code == 0 and [c["id"] for c in json.loads(out)] == [1]


# --- contacts -------------------------------------------------------------------------------


def test_contact_add_prints_the_outcome(monkeypatch, run):
    seen = {}

    def add_contact(c, fields, *, run_id):
        seen.update(fields=fields, run_id=run_id)
        return {"id": 6}, "skipped_warm"

    monkeypatch.setattr(cli.campaign, "add_contact", add_contact)
    code, out, _ = run(
        "contact", "add", "--company", "1", "--name", "Jo Lee", "--title", "CTO",
        "--email", "jo@acme.com",
    )  # fmt: skip
    assert code == 0 and out.strip() == "skipped_warm 6"
    assert seen["fields"]["email_source"] == "manual" and seen["run_id"] == 3


def test_contact_verify_stores_the_result_and_parks_accept_all(monkeypatch, run, store):
    store.contacts[5].update(verification=None, email=None)
    monkeypatch.setattr(
        cli.campaign_verify,
        "resolve_email",
        lambda contact, company, *, apollo, hunter: ("jane@acme.com", "accept_all", "hunter"),
    )
    code, out, _ = run("contact", "verify", "5")
    assert code == 0 and "accept_all" in out
    assert store.updates[0] == (
        5,
        {"email": "jane@acme.com", "email_source": "hunter", "verification": "accept_all"},
    )
    assert store.advances[0]["status"] == "unverified" and store.advances[0]["source"] == "cli"


def test_contact_verify_valid_stays_candidate(monkeypatch, run, store):
    monkeypatch.setattr(
        cli.campaign_verify,
        "resolve_email",
        lambda contact, company, *, apollo, hunter: ("jane@acme.com", "valid", "apollo"),
    )
    assert run("contact", "verify", "--all")[0] == 0
    assert store.advances == [] and store.contacts[5]["verification"] == "valid"


def test_contact_draft_needs_a_valid_verification(run, store):
    store.contacts[5].update(status="unverified", verification="accept_all")
    code, _, err = run("contact", "draft", "5", "--opener", "Congrats on the Series A.")
    assert code == 1 and "verification" in err
    assert store.advances == []


def test_contact_draft_on_a_valid_unverified_contact_succeeds(monkeypatch, run, store):
    store.contacts[5].update(status="unverified")
    monkeypatch.setattr(cli.campaign_factcheck, "check", lambda o, e: FactCheck(True, [], "{}"))
    code, out, _ = run("contact", "draft", "5", "--opener", "Congrats on the Series A.")
    assert code == 0 and "drafted 5" in out
    adv = store.advances[0]
    assert adv["status"] == "drafted" and adv["source"] == "cli" and adv.get("note") is None
    assert adv["changes"]["opener_generic"] is False
    assert "Congrats on the Series A." in adv["changes"]["body"]
    assert "1 Main St, Springfield" in adv["changes"]["body"]
    assert "Acme" in adv["changes"]["subject"]


def test_a_failing_fact_check_exits_2_and_prints_the_claim(monkeypatch, run, store):
    monkeypatch.setattr(
        cli.campaign_factcheck, "check", lambda o, e: FactCheck(False, ["Series B"], "{}")
    )
    code, _, err = run("contact", "draft", "5", "--opener", "Congrats on the Series B.")
    assert code == 2 and "Series B" in err
    assert store.advances == []
    assert store.notes[5].startswith("fact check failed")  # kept for the regenerated badge
    assert "commit" in run.conn.log


def test_a_fact_check_that_could_not_run_exits_3(monkeypatch, run, store):
    def boom(o, e):
        raise ClaudeCliError("claude exited 1")

    monkeypatch.setattr(cli.campaign_factcheck, "check", boom)
    code, _, err = run("contact", "draft", "5", "--opener", "Congrats.")
    assert code == 3 and "fact check could not run: claude exited 1" in err
    assert store.advances == []
    assert 5 not in store.notes  # not a failed check: no regenerated badge on the retry


def test_an_empty_fact_check_reply_exits_3(monkeypatch, run, store):
    monkeypatch.setattr(
        cli.campaign_factcheck, "check", lambda o, e: FactCheck(False, ["unparseable"], "  ")
    )
    code, _, err = run("contact", "draft", "5", "--opener", "Congrats.")
    assert code == 3 and "fact check could not run" in err


def test_a_rejected_opener_still_exits_2_not_3(monkeypatch, run, store):
    monkeypatch.setattr(
        cli.campaign_factcheck, "check", lambda o, e: FactCheck(False, ["Series B"], "{}")
    )
    assert run("contact", "draft", "5", "--opener", "Congrats on the Series B.")[0] == 2


def test_the_subject_is_fact_checked_with_the_opener(monkeypatch, run, store):
    seen = []

    def check(text, excerpt):
        seen.append(text)
        ok = "Series B" not in text
        return FactCheck(ok, [] if ok else ["$40M Series B"], "{}")

    monkeypatch.setattr(cli.campaign_factcheck, "check", check)
    code, _, err = run(
        "contact", "draft", "5", "--opener", "Congrats on the Series A.",
        "--subject", "Acme's $40M Series B",
    )  # fmt: skip
    assert code == 2 and "$40M Series B" in err
    assert seen == ["Acme's $40M Series B\nCongrats on the Series A."]
    assert store.advances == []


def test_without_a_subject_only_the_opener_is_checked(monkeypatch, run, store):
    seen = []
    monkeypatch.setattr(
        cli.campaign_factcheck, "check", lambda t, e: seen.append(t) or FactCheck(True, [], "{}")
    )
    assert run("contact", "draft", "5", "--opener", "Congrats on the Series A.")[0] == 0
    assert seen == ["Congrats on the Series A."]


def test_a_draft_after_a_failed_check_is_annotated_regenerated(monkeypatch, run, store):
    store.notes[5] = "fact check failed (prompt 2026-10-21): Series B"
    monkeypatch.setattr(cli.campaign_factcheck, "check", lambda o, e: FactCheck(True, [], "{}"))
    assert run("contact", "draft", "5", "--opener", "Congrats on the Series A.")[0] == 0
    # The move itself carries no note (it counts as drafted); the badge rides an annotation.
    assert store.advances[0]["status"] == "drafted" and store.advances[0].get("note") is None
    assert store.notes[5].startswith("fact check regenerated")


def test_generic_skips_the_check_and_sets_opener_generic(monkeypatch, run, store):
    store.companies[1]["announcement_excerpt"] = None

    def never(o, e):
        raise AssertionError("no fact check for --generic")

    monkeypatch.setattr(cli.campaign_factcheck, "check", never)
    code, _, _ = run("contact", "draft", "5", "--generic")
    assert code == 0
    changes = store.advances[0]["changes"]
    assert changes["opener_generic"] is True
    assert cli.campaign_draft.GENERIC_OPENER in changes["body"]


def test_draft_without_an_excerpt_needs_generic(run, store):
    store.companies[1]["announcement_excerpt"] = None
    code, _, err = run("contact", "draft", "5", "--opener", "Congrats.")
    assert code == 1 and "excerpt" in err


def test_a_failed_then_regenerated_redraft_of_a_drafted_row_keeps_the_badge(
    monkeypatch, run, store
):
    store.contacts[5]["status"] = "drafted"
    annotations = []
    real_annotate = cli.campaign.annotate

    def annotate(c, cid, **kw):
        annotations.append(kw["note"])
        return real_annotate(c, cid, **kw)

    monkeypatch.setattr(cli.campaign, "annotate", annotate)
    monkeypatch.setattr(
        cli.campaign_factcheck, "check", lambda o, e: FactCheck(False, ["Series B"], "{}")
    )
    assert run("contact", "draft", "5", "--opener", "Congrats on the Series B.")[0] == 2
    monkeypatch.setattr(cli.campaign_factcheck, "check", lambda o, e: FactCheck(True, [], "{}"))
    assert run("contact", "draft", "5", "--opener", "Congrats on the Series A.")[0] == 0
    assert store.contacts[5]["status"] == "drafted"
    assert "Congrats on the Series A." in store.contacts[5]["body"]
    assert annotations[0].startswith("fact check failed")
    assert annotations[1].startswith("fact check regenerated")
    drafted, _ = cli.review_entries(run.conn, TARGETS.threshold)
    assert drafted[0]["flags"] == ["fact check regenerated"]


# --- review and send ------------------------------------------------------------------------


def _fake_send(monkeypatch, result=None, calls=None):
    calls = calls if calls is not None else []
    monkeypatch.setattr(cli.campaign_send, "restage_stale", lambda c, *, now, stale_after_days: 0)
    monkeypatch.setattr(cli.campaign_send, "preflight", lambda domain, **kw: calls.append("pre"))

    def send_batch(c, **kw):
        calls.append(("send", kw))
        return result or SendResult(sent=1, remaining=0, halted=False)

    monkeypatch.setattr(cli.campaign_send, "send_batch", send_batch)
    return calls


def test_review_serves_then_applies_approvals_and_sends(monkeypatch, run, store):
    store.contacts[5]["status"] = "drafted"
    store.notes[5] = "fact check regenerated (prompt 2026-10-21)"
    store.contacts[6] = {
        **store.contacts[5], "id": 6, "status": "drafted", "full_name": "Sam Roe",
    }  # fmt: skip
    store.contacts[7] = {**store.contacts[5], "id": 7, "status": "unverified"}
    store.companies[2] = {**store.companies[1], "id": 2, "status": "below_bar", "fit_score": 4}
    calls = _fake_send(monkeypatch)
    seen = {}

    def serve(*, html, key, drafted_ids, on_submit, **kw):
        seen.update(html=html, key=key, drafted_ids=drafted_ids)
        calls.append("serve")
        on_submit([(5, "approved", "Edited body"), (6, "skipped", None)])

    monkeypatch.setattr(cli.campaign_review, "serve", serve)
    code, out, _ = run("review")
    assert code == 0, out
    assert seen["drafted_ids"] == [5, 6] and len(seen["key"]) == 32
    assert "fact check regenerated" in seen["html"]
    assert "Unverified email" in seen["html"] and "fit 4 &lt; 7" in seen["html"]
    assert (run.tmp / ".campaign" / "review-3.html").read_text() == seen["html"]
    assert calls[0] == "serve" and calls[1] == "pre" and calls[2][0] == "send"
    approved, skipped = store.advances
    assert approved == {
        "id": 5, "status": "approved", "at": NOW, "source": "page",
        "changes": {"body": "Edited body"},
    }  # fmt: skip
    assert skipped["status"] == "skipped" and skipped["changes"] is None
    assert "sent 1, remaining 0" in out


def test_send_without_a_postal_address_sends_nothing(monkeypatch, run):
    monkeypatch.setenv("CAMPAIGN_POSTAL_ADDRESS", "")
    monkeypatch.setattr(cli.campaign_send, "restage_stale", lambda c, *, now, stale_after_days: 0)
    records = {
        "drolet.ai": ["v=spf1 include:spf.protection.outlook.com -all"],
        "_dmarc.drolet.ai": ["v=DMARC1; p=quarantine"],
        "selector1._domainkey.drolet.ai": ["v=DKIM1; k=rsa; p=x"],
    }
    monkeypatch.setattr(cli.campaign_send, "_txt", lambda r, name: records.get(name, []))

    def send_batch(c, **kw):
        raise AssertionError("must not send")

    monkeypatch.setattr(cli.campaign_send, "send_batch", send_batch)
    code, _, err = run("send")
    assert code == 1 and "postal address" in err


def test_send_prints_the_halt_and_the_needs_check_warning(monkeypatch, run):
    calls = _fake_send(
        monkeypatch, SendResult(sent=2, remaining=5, halted=True, needs_check=[8, 9])
    )
    code, out, _ = run("send")
    assert code == 0
    assert "sent 2, remaining 5" in out
    assert "halted: more than two bounces; run campaign resume" in out
    assert "8, 9" in out and "Sent Items/Drafts" in out
    kw = calls[-1][1]
    assert kw["force"] is False and kw["from_address"] == "ben@drolet.ai"
    assert kw["postal_address"] == "1 Main St, Springfield" and kw["targets"] == TARGETS


def test_resume_forces_the_send(monkeypatch, run):
    calls = _fake_send(monkeypatch)
    assert run("resume")[0] == 0
    assert calls[-1][1]["force"] is True


def test_a_token_minter_failure_fails_preflight_and_sends_nothing(monkeypatch, run):
    import subprocess

    records = {
        "drolet.ai": ["v=spf1 -all"],
        "_dmarc.drolet.ai": ["v=DMARC1; p=quarantine"],
        "selector1._domainkey.drolet.ai": ["v=DKIM1; p=x"],
    }
    monkeypatch.setattr(cli.campaign_send, "_txt", lambda r, name: records.get(name, []))
    monkeypatch.setattr(cli.campaign_send, "restage_stale", lambda c, *, now, stale_after_days: 0)

    def mint(base):
        raise subprocess.CalledProcessError(1, ["gcloud"])

    monkeypatch.setattr(cli.gcp_auth, "id_token_for", mint)

    def send_batch(c, **kw):
        raise AssertionError("must not send")

    monkeypatch.setattr(cli.campaign_send, "send_batch", send_batch)
    code, _, err = run("send")
    assert code == 1 and "inbox-api is not reachable" in err


def test_usage_error_exits_1(run, capsys):
    code, _, _ = run("contact", "draft")
    assert code == 1
    assert "usage:" in capsys.readouterr().err


def test_inbox_reachable_counts_any_response(monkeypatch):
    monkeypatch.setenv("INBOX_API_URL", "https://inbox.example")
    monkeypatch.setattr(cli.gcp_auth, "id_token_for", lambda base: "tok")
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kw: cli.httpx.Response(404))
    assert cli.inbox_reachable() is True

    def down(url, **kw):
        raise cli.httpx.ConnectError("refused")

    monkeypatch.setattr(cli.httpx, "get", down)
    assert cli.inbox_reachable() is False


# --- follow-ups, hand moves, report, weekly -------------------------------------------------


def test_followups_prints_failed_and_the_report_hint(monkeypatch, run):
    monkeypatch.setattr(cli.campaign_send, "preflight", lambda domain, **kw: None)
    monkeypatch.setattr(
        cli.campaign_followups,
        "run",
        lambda c, **kw: FollowUpResult(sent=3, fallback=1, skipped=0, failed=2),
    )
    code, out, _ = run("followups")
    assert code == 0
    assert "sent 3 (fallback 1, skipped 0, failed 2)" in out and "campaign report" in out


def test_followups_without_failures_has_no_hint(monkeypatch, run):
    monkeypatch.setattr(cli.campaign_send, "preflight", lambda domain, **kw: None)
    monkeypatch.setattr(
        cli.campaign_followups, "run", lambda c, **kw: FollowUpResult(sent=1, fallback=0, skipped=0)
    )
    _, out, _ = run("followups")
    assert out.strip() == "sent 1 (fallback 0, skipped 0, failed 0)"


def test_booked_by_email_advances_from_the_cli(run, store):
    store.contacts[5]["status"] = "replied"
    code, out, _ = run("booked", "jane@acme.com")
    assert code == 0 and "booked 5" in out
    assert store.advances[0]["status"] == "booked" and store.advances[0]["source"] == "cli"


def test_bounced_on_a_late_contact_is_a_user_error(monkeypatch, run, store):
    def advance(c, cid, status, **kw):
        raise cli.campaign.InvalidTransition("drafted", "bounced")

    monkeypatch.setattr(cli.campaign, "advance", advance)
    code, _, err = run("bounced", "5")
    assert code == 1 and "cannot move to bounced" in err and "rollback" in run.conn.log


def test_unknown_contact_is_a_user_error(run):
    code, _, err = run("booked", "nobody@x.com")
    assert code == 1 and "nobody@x.com" in err


def test_optout_stops_the_company(monkeypatch, run):
    seen = {}

    def opt_out_company(c, company_id, *, at, source, note):
        seen.update(company_id=company_id, source=source, note=note)
        return 2

    monkeypatch.setattr(cli.campaign, "opt_out_company", opt_out_company)
    code, out, _ = run("optout", "jane@acme.com", "--note", "asked by phone")
    assert code == 0 and "opted_out" in out
    assert seen == {"company_id": 1, "source": "cli", "note": "asked by phone"}


def test_report_passes_the_window(monkeypatch, run):
    seen = {}

    def report(c, *, since, until):
        seen.update(since=since, until=until)
        return "R"

    monkeypatch.setattr(cli.campaign_report, "report", report)
    monkeypatch.setattr(cli.campaign_report, "format_report", lambda r: f"formatted {r}")
    code, out, _ = run("report", "--since", "2026-10-01")
    assert code == 0 and "formatted R" in out
    assert seen == {"since": date(2026, 10, 1), "until": None}


def test_weekly_runs_followups_before_review(monkeypatch, run, store):
    order = []
    monkeypatch.setattr(
        cli.campaign_followups,
        "run",
        lambda c, **kw: order.append("followups") or FollowUpResult(sent=0, fallback=0, skipped=0),
    )
    store.contacts[5]["status"] = "drafted"
    _fake_send(monkeypatch)
    monkeypatch.setattr(
        cli.campaign_review, "serve", lambda **kw: order.append("review") or kw["on_submit"]([])
    )
    assert run("weekly")[0] == 0
    assert order == ["followups", "review"]


# --- the sendable gate in the CLI -----------------------------------------------------------


def _verify_finds(monkeypatch, email="jane@acme.com", verification="valid"):
    monkeypatch.setattr(
        cli.campaign_verify,
        "resolve_email",
        lambda contact, company, *, apollo, hunter: (email, verification, "apollo"),
    )


def test_verify_parks_a_contact_whose_found_email_is_warm(monkeypatch, run, store):
    store.contacts[5].update(email=None, verification=None)
    store.people["jane@acme.com"] = {"id": 7, "message_count": 4, "my_response_count": 2}
    _verify_finds(monkeypatch)
    code, out, _ = run("contact", "verify", "5")
    assert code == 0 and "skipped_warm 5 (warm)" in out
    assert store.contacts[5]["status"] == "skipped_warm"
    assert store.advances[-1]["note"] == "warm"
    assert store.companies[1]["status"] == "warm"
    assert "commit" in run.conn.log


def test_verify_parks_a_bounced_address_without_warming_the_company(monkeypatch, run, store):
    store.contacts[5].update(email=None, verification=None)
    store.bounced_at["jane@acme.com"] = NOW
    _verify_finds(monkeypatch, verification="accept_all")
    code, out, _ = run("contact", "verify", "5")
    assert code == 0 and "skipped_warm 5 (bounced address)" in out
    assert store.contacts[5]["status"] == "skipped_warm"
    assert store.companies[1]["status"] == "active"


def test_verify_of_a_cold_contact_is_unchanged(monkeypatch, run, store):
    _verify_finds(monkeypatch)
    code, out, _ = run("contact", "verify", "5")
    assert code == 0 and "skipped_warm" not in out and store.contacts[5]["status"] == "candidate"


def test_a_contact_at_a_warm_company_cannot_be_drafted(monkeypatch, run, store):
    store.companies[1]["status"] = "warm"
    monkeypatch.setattr(cli.campaign_factcheck, "check", lambda o, e: FactCheck(True, [], "{}"))
    code, _, err = run("contact", "draft", "5", "--opener", "Congrats on the Series A.")
    assert code == 1 and "company warm" in err
    assert store.advances == []


def test_a_contact_whose_address_bounced_cannot_be_drafted(monkeypatch, run, store):
    store.bounced_at["jane@acme.com"] = NOW
    code, _, err = run("contact", "draft", "5", "--generic")
    assert code == 1 and "bounced address" in err
    assert store.advances == []


def test_a_warm_contact_cannot_be_drafted(monkeypatch, run, store):
    store.people["jane@acme.com"] = {"id": 7, "message_count": 4, "my_response_count": 2}
    code, _, err = run("contact", "draft", "5", "--generic")
    assert code == 1 and "warm" in err


def test_hand_bounced_stamps_the_people_row(run, store):
    store.contacts[5]["status"] = "sent"
    code, out, _ = run("bounced", "5")
    assert code == 0 and "bounced 5" in out
    assert store.people_bounced == [("jane@acme.com", NOW)]


def test_hand_booked_does_not_stamp_a_bounce(run, store):
    store.contacts[5]["status"] = "replied"
    assert run("booked", "5")[0] == 0
    assert store.people_bounced == []


def test_send_prints_the_contacts_the_gate_skipped(monkeypatch, run):
    _fake_send(
        monkeypatch,
        SendResult(sent=1, remaining=1, halted=False, skipped=[(6, "company opted_out")]),
    )
    code, out, _ = run("send")
    assert code == 0 and "not sent: 6 (company opted_out)" in out


# --- follow-ups: preflight and the shared daily cap -----------------------------------------


def test_followups_run_preflight_first(monkeypatch, run):
    calls = []
    monkeypatch.setattr(
        cli.campaign_send, "preflight", lambda domain, **kw: calls.append(("pre", domain, kw))
    )
    monkeypatch.setattr(
        cli.campaign_followups,
        "run",
        lambda c, **kw: calls.append("run") or FollowUpResult(sent=0, fallback=0, skipped=0),
    )
    assert run("followups")[0] == 0
    assert calls[0][0] == "pre" and calls[0][1] == "drolet.ai" and calls[1] == "run"
    kw = calls[0][2]
    assert kw["postal_address"] == "1 Main St, Springfield"
    assert kw["inbox_reachable"] is cli.inbox_reachable


def test_a_failed_preflight_sends_no_follow_ups(monkeypatch, run):
    def fail(domain, **kw):
        raise cli.campaign_send.PreflightError("dmarc: no v=DMARC1 TXT record")

    def never(c, **kw):
        raise AssertionError("must not follow up")

    monkeypatch.setattr(cli.campaign_send, "preflight", fail)
    monkeypatch.setattr(cli.campaign_followups, "run", never)
    code, _, err = run("followups")
    assert code == 1 and "dmarc" in err


def test_followups_get_what_is_left_of_the_daily_cap(monkeypatch, run, store):
    store.sent_today = 5
    seen = {}
    monkeypatch.setattr(cli.campaign_send, "preflight", lambda domain, **kw: None)

    def fu(c, **kw):
        seen.update(kw)
        return FollowUpResult(sent=15, fallback=0, skipped=0, remaining=2)

    monkeypatch.setattr(cli.campaign_followups, "run", fu)
    code, out, _ = run("followups")
    assert code == 0 and seen["budget"] == 15
    assert "remaining 2" in out
