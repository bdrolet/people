import io
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from models.outreach import Advance, CohortCounts, FunnelCounts, TodayCounts
from scripts import outreach as cli
from services import outreach as svc
from services.outreach_sync import SyncResult

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
ALICE = {"id": 7, "email": "alice@example.com", "display_name": "Alice Example"}


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


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, n, attrs=None):
        self.calls.append(attrs)


@pytest.fixture
def run(monkeypatch):
    # scripts/outreach.py loads .env at import; never let the real files leak into tests.
    monkeypatch.delenv("OUTREACH_TARGETS", raising=False)
    monkeypatch.delenv("OUTREACH_FLOORS", raising=False)
    conn = Conn()
    monkeypatch.setattr(cli.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(cli.otel, "flush", lambda: None)
    monkeypatch.setattr(cli.outreach, "record_population", lambda c, today: None)
    monkeypatch.setattr(cli.person_lookup, "resolve", lambda c, ident: dict(ALICE))

    def sync_events(c, ids):
        conn.log.append(("sync", list(ids)))
        return SyncResult(synced=len(ids))

    monkeypatch.setattr(cli.outreach_sync, "sync_events", sync_events)

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(list(argv), lambda: conn, out=out, err=err, now=NOW)
        return code, out.getvalue(), err.getvalue()

    _run.conn = conn
    return _run


def _advance_returns(monkeypatch, result, seen=None):
    def advance(c, pid, stage, **kw):
        if seen is not None:
            seen.update(pid=pid, stage=stage, **kw)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli.outreach, "advance", advance)


def test_mark_briefed_needs_a_channel(run):
    code, _, err = run("mark", "alice@example.com", "briefed")
    assert code == 1 and "--channel is required" in err


def test_from_needs_the_email_channel(run):
    code, _, err = run(
        "mark", "alice@example.com", "briefed", "--channel", "imessage", "--from", "a@example.com"
    )
    assert code == 1 and "--from" in err


def test_mark_briefed_commits_then_syncs(monkeypatch, run):
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "briefed"}, True, 11), seen)
    code, out, _ = run("mark", "alice@example.com", "briefed", "--channel", "imessage")
    assert code == 0 and "briefed" in out and "asana: task created" in out
    assert run.conn.log[:2] == ["commit", ("sync", [11])]
    assert seen == {
        "pid": 7,
        "stage": "briefed",
        "at": NOW,
        "channel": "imessage",
        "source": "cli",
        "from_address": None,
        "force": False,
        "note": None,
    }


def test_mark_replied_creates_no_task(monkeypatch, run):
    _advance_returns(monkeypatch, Advance({"stage": "replied"}, True, 12))
    run("mark", "alice@example.com", "replied", "--channel", "whatsapp")
    assert not any(isinstance(e, tuple) for e in run.conn.log)


def test_an_invalid_move_names_the_current_stage(monkeypatch, run):
    _advance_returns(monkeypatch, svc.InvalidTransition("referrer", "declined", "briefed"))
    code, _, err = run("mark", "alice@example.com", "briefed", "--channel", "email")
    assert code == 1 and "declined" in err and "--force" in err
    assert "rollback" in run.conn.log


def test_mark_someone_not_in_the_pipeline(monkeypatch, run):
    _advance_returns(monkeypatch, None)
    code, _, err = run("mark", "alice@example.com", "replied", "--channel", "email")
    assert code == 1 and "outreach decide" in err


def test_mark_passes_at_from_and_force(monkeypatch, run):
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "briefed"}, True, None), seen)
    run(
        "mark",
        "7",
        "briefed",
        "--channel",
        "email",
        "--from",
        "alias@example.com",
        "--at",
        "2026-10-10",
        "--force",
    )
    assert seen["from_address"] == "alias@example.com" and seen["force"] is True
    assert seen["at"] == datetime(2026, 10, 10, 12, 0, tzinfo=svc.TZ)


def test_decide_records_the_stage_before_the_label_and_survives_a_label_failure(monkeypatch, run):
    monkeypatch.setattr(
        cli.outreach, "ensure_contact", lambda c, pid, **kw: run.conn.log.append("ensure")
    )
    _advance_returns(monkeypatch, Advance({"stage": "to_contact"}, True, 3))

    def update(c, pid, *, labels):
        run.conn.log.append(("label", labels))
        raise RuntimeError("google down")

    monkeypatch.setattr(cli.person_edit, "update", update)
    code, out, _ = run("decide", "alice@example.com")
    assert code == 0 and "to_contact" in out and "warning" in out
    assert run.conn.log[:3] == ["ensure", "commit", ("label", {"add": ["consulting-outreach"]})]


def test_decide_skip_adds_no_label(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "ensure_contact", lambda c, pid, **kw: None)
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "skipped"}, True, 4), seen)
    monkeypatch.setattr(cli.person_edit, "update", lambda *a, **k: pytest.fail("labelled a skip"))
    assert run("decide", "alice@example.com", "--skip")[0] == 0 and seen["stage"] == "skipped"


def test_intro_opens_an_opportunity_and_syncs(monkeypatch, run):
    seen = {}

    def open_opportunity(c, referrer_id, label, **kw):
        seen.update(referrer_id=referrer_id, label=label, **kw)
        return Advance({"id": 4, "buyer_label": label}, True, 20)

    monkeypatch.setattr(cli.outreach, "open_opportunity", open_opportunity)
    code, out, _ = run("intro", "alice@example.com", "Bo Buyer, Co", "--buyer", "bo@example.com")
    assert code == 0 and "opportunity 4" in out
    assert seen["label"] == "Bo Buyer, Co" and seen["buyer_id"] == 7 and seen["at"] == NOW
    assert ("sync", [20]) in run.conn.log


def test_lost_needs_a_known_reason(run):
    with pytest.raises(SystemExit):
        run("lost", "4", "--reason", "vibes")


def test_buyer_advance(monkeypatch, run):
    seen = {}

    def advance_opportunity(c, oid, stage, **kw):
        seen.update(oid=oid, stage=stage, **kw)
        return Advance({"id": oid, "stage": stage, "buyer_label": "Bo, Co"}, True, 30)

    monkeypatch.setattr(cli.outreach, "advance_opportunity", advance_opportunity)
    assert run("lost", "4", "--reason", "timing")[0] == 0
    assert seen == {"oid": 4, "stage": "lost", "at": NOW, "lost_reason": "timing", "source": "cli"}
    assert not any(isinstance(e, tuple) for e in run.conn.log)  # lost makes no task
    run("signed", "4")
    assert ("sync", [30]) in run.conn.log


def test_report_without_floors(monkeypatch, run):
    monkeypatch.setattr(
        cli.outreach,
        "counts",
        lambda c, today, since=None, until=None: FunnelCounts(
            [CohortCounts(date(2026, 10, 12), briefed=3, replied=1)]
        ),
    )
    monkeypatch.setattr(cli.otel, "outreach_report_runs", Recorder())
    code, out, _ = run("report")
    assert code == 0 and out.startswith("Cohort") and "Floor@n" not in out
    assert cli.otel.outreach_report_runs.calls == [{"verdict_below": "0"}]


def test_report_with_a_bad_floors_path(run):
    code, _, err = run("report", "--floors", "/nonexistent/floors.toml")
    assert code == 1 and "not found" in err


def test_draft_refuses_a_person_without_email(monkeypatch, run):
    monkeypatch.setattr(
        cli.person_lookup, "resolve", lambda c, ident: {"id": 8, "email": None, "display_name": "P"}
    )
    code, _, err = run("draft", "8")
    assert code == 1 and "no email" in err


def test_draft_goes_from_the_consulting_address(monkeypatch, run):
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com,other@example.com")
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})
    seen = {}
    monkeypatch.setattr(
        cli.inbox_api,
        "create_draft",
        lambda to, subject, body="", from_address=None: (
            seen.update(to=to, subject=subject, body=body, from_address=from_address)
            or {"id": "d", "web_link": "https://o/d"}
        ),
    )
    code, out, _ = run("draft", "alice@example.com")
    assert code == 0 and "https://o/d" in out and "ben@consulting.example.com" in out
    assert seen == {
        "to": ["alice@example.com"],
        "subject": "",
        "body": "",
        "from_address": "ben@consulting.example.com",
    }


def test_draft_without_outreach_addresses_is_a_user_error(monkeypatch, run):
    monkeypatch.delenv("OUTREACH_FROM_ADDRESSES", raising=False)
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})
    code, _, err = run("draft", "alice@example.com")
    assert code == 1 and "OUTREACH_FROM_ADDRESSES" in err


def test_parse_at():
    assert cli.parse_at(None, NOW) == NOW
    assert cli.parse_at("2026-10-10", NOW) == datetime(2026, 10, 10, 12, 0, tzinfo=svc.TZ)
    assert cli.parse_at("2026-10-10T09:30:00Z", NOW) == datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "exc", [cli.inbox_api.InboxApiUnavailable("inbox-api down"), httpx.ConnectError("down")]
)
def test_draft_inbox_failures_are_user_errors_not_tracebacks(monkeypatch, run, exc):
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli.inbox_api, "create_draft", boom)
    code, _, err = run("draft", "alice@example.com")
    assert code == 1 and err.startswith("error:")


def test_decide_not_linked_warning_says_so(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "ensure_contact", lambda c, pid, **kw: None)
    _advance_returns(monkeypatch, Advance({"stage": "to_contact"}, True, 3))

    def update(c, pid, *, labels):
        raise cli.person_edit.NotLinked(str(pid))

    monkeypatch.setattr(cli.person_edit, "update", update)
    code, out, _ = run("decide", "alice@example.com")
    assert code == 0 and "no Google contact is linked yet" in out


TARGETS_TOML = """
[briefs]
per_day = 5
per_week = 25
start = 2026-10-13
end = 2026-11-21
total = 150
[queue]
low_water_days = 5
[workdays]
days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
"""


def test_nudge_records_and_reports_the_next_follow_up(monkeypatch, run):
    contacts = iter(
        [
            {"stage": "briefed", "briefed_at": NOW - timedelta(days=8)},
            {"stage": "briefed", "nudge_count": 1, "follow_up_on": date(2026, 10, 19)},
        ]
    )
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: next(contacts))
    seen = {}

    def nudge(c, pid, **kw):
        seen.update(pid=pid, **kw)
        return 21

    monkeypatch.setattr(cli.outreach, "nudge", nudge)
    code, out, _ = run("nudge", "alice@example.com", "--channel", "imessage", "--note", "texted")
    assert code == 0 and "nudge 1 recorded" in out and "2026-10-19" in out
    assert seen == {
        "pid": 7,
        "at": NOW,
        "channel": "imessage",
        "source": "cli",
        "from_address": None,
        "note": "texted",
    }
    assert "commit" in run.conn.log and not any(isinstance(e, tuple) for e in run.conn.log)


def test_nudge_refuses_someone_not_briefed(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})
    code, _, err = run("nudge", "alice@example.com", "--channel", "email")
    assert code == 1 and "to_contact" in err and "briefed" in err


def test_nudge_too_soon_after_the_brief(monkeypatch, run):
    monkeypatch.setattr(
        cli.outreach, "get_contact", lambda c, pid: {"stage": "briefed", "briefed_at": NOW}
    )
    monkeypatch.setattr(cli.outreach, "nudge", lambda c, pid, **kw: None)
    code, _, err = run("nudge", "alice@example.com", "--channel", "email")
    assert code == 1 and "a day" in err


def test_nudge_from_needs_the_email_channel(run):
    code, _, err = run(
        "nudge", "alice@example.com", "--channel", "call", "--from", "alias@example.com"
    )
    assert code == 1 and "--from" in err


PEOPLE = {
    "a@example.com": {"id": 1, "email": "a@example.com", "display_name": "A"},
    "b@example.com": {"id": 2, "email": "b@example.com", "display_name": "B"},
    "c@example.com": {"id": 3, "email": "c@example.com", "display_name": "C"},
}
STAGES = {1: "to_contact", 2: "to_contact", 3: "briefed"}


@pytest.fixture
def people(monkeypatch, run):  # after `run`, so its resolve stub wins over run's default
    def resolve(c, ident):
        if ident not in PEOPLE:
            raise cli.person_lookup.NotFound(ident)
        return dict(PEOPLE[ident])

    monkeypatch.setattr(cli.person_lookup, "resolve", resolve)
    monkeypatch.setattr(
        cli.outreach,
        "get_contact",
        lambda c, pid: {"stage": STAGES[pid]} if pid in STAGES else None,
    )
    ranked = []
    monkeypatch.setattr(cli.outreach, "set_ranks", lambda c, ids: ranked.append(list(ids)))
    return ranked


def test_rank_file_assigns_in_file_order(people, run, tmp_path):
    path = tmp_path / "order.txt"
    path.write_text("# best first\nb@example.com\n\na@example.com\n")
    code, out, _ = run("rank", "--file", str(path))
    assert code == 0 and people == [[2, 1]] and "ranked 2" in out
    assert "commit" in run.conn.log


def test_rank_file_refuses_the_whole_file(people, run, tmp_path):
    path = tmp_path / "order.txt"
    path.write_text("a@example.com\nnobody@example.com\nc@example.com\na@example.com\n")
    code, _, err = run("rank", "--file", str(path))
    assert code == 1 and people == []
    assert "line 2" in err and "nobody@example.com" in err
    assert "line 3" in err and "briefed" in err
    assert "line 4" in err and "repeats line 1" in err


def test_rank_file_with_nobody_is_refused(people, run, tmp_path):
    path = tmp_path / "order.txt"
    path.write_text("# nothing yet\n")
    code, _, err = run("rank", "--file", str(path))
    assert code == 1 and people == [] and "nobody" in err


def test_rank_one_person(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(cli.outreach, "set_rank", lambda c, pid, n: seen.update(pid=pid, n=n))
    code, out, _ = run("rank", "alice@example.com", "3")
    assert code == 0 and seen == {"pid": 7, "n": 3} and "rank 3" in out


def test_rank_one_person_not_queued(monkeypatch, run):
    def set_rank(c, pid, n):
        raise svc.NotQueued("person 7 is at briefed, not to_contact")

    monkeypatch.setattr(cli.outreach, "set_rank", set_rank)
    code, _, err = run("rank", "alice@example.com", "3")
    assert code == 1 and "not to_contact" in err and "rollback" in run.conn.log


def test_rank_clear(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "clear_ranks", lambda c: 4)
    code, out, _ = run("rank", "--clear")
    assert code == 0 and "cleared 4" in out


@pytest.mark.parametrize(
    "argv", [("rank",), ("rank", "--clear", "--file", "x"), ("rank", "alice@example.com")]
)
def test_rank_needs_exactly_one_form(run, argv):
    code, _, err = run(*argv)
    assert code == 1 and "rank" in err


def _today_counts(monkeypatch, seen=None):
    def today_counts(c, **kw):
        if seen is not None:
            seen.update(kw)
        return TodayCounts(
            briefs_today=2,
            briefs_week=7,
            briefs_plan=7,
            nudges_week=4,
            queue_depth=38,
            follow_ups=[],
            next_up=[],
        )

    monkeypatch.setattr(cli.outreach, "today_counts", today_counts)


def test_today_renders_the_screen(monkeypatch, run, tmp_path):
    path = tmp_path / "targets.toml"
    path.write_text(TARGETS_TOML)
    seen = {}
    _today_counts(monkeypatch, seen)
    code, out, _ = run("today", "--targets", str(path))
    assert code == 0 and out.startswith("Outreach: Tue 2026-10-13")
    assert seen == {
        "today": date(2026, 10, 13),
        "per_day": 5,
        "plan_start": date(2026, 10, 13),
        "plan_end": date(2026, 11, 21),
    }


def test_today_json_from_the_env_var(monkeypatch, run, tmp_path):
    path = tmp_path / "targets.toml"
    path.write_text(TARGETS_TOML)
    monkeypatch.setenv("OUTREACH_TARGETS", str(path))
    _today_counts(monkeypatch)
    code, out, _ = run("today", "--json")
    data = json.loads(out)
    assert code == 0 and data["day"] == "2026-10-13" and data["briefs_week"] == 7


def test_today_without_targets_names_both_options(run):
    code, _, err = run("today")
    assert code == 1 and "--targets" in err and "OUTREACH_TARGETS" in err


def test_today_with_a_bad_targets_file(run, tmp_path):
    path = tmp_path / "targets.toml"
    path.write_text("[briefs]\n")
    code, _, err = run("today", "--targets", str(path))
    assert code == 1 and "missing [queue]" in err


def test_queue_json_is_pure_json(monkeypatch, run):
    item = {"person": {"id": 7}, "rank": 1, "stage": "to_contact"}
    seen = {}

    def queue(c, *, stage, limit):
        seen.update(stage=stage, limit=limit)
        return [item]

    monkeypatch.setattr(cli.outreach, "queue", queue)
    monkeypatch.setattr(
        cli.outreach, "shared_briefs", lambda c: [{"graph_message_id": "g", "n": 2}]
    )
    code, out, err = run("queue", "--stage", "candidate", "--limit", "500", "--json")
    assert code == 0 and json.loads(out) == [item] and "warning" in err
    assert seen == {"stage": "candidate", "limit": 500}


def test_decide_skip_note(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "ensure_contact", lambda c, pid, **kw: None)
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "skipped"}, True, 4), seen)
    assert run("decide", "alice@example.com", "--skip", "--note", "moved abroad")[0] == 0
    assert seen["stage"] == "skipped" and seen["note"] == "moved abroad"


def test_decide_note_needs_skip(run):
    code, _, err = run("decide", "alice@example.com", "--note", "why")
    assert code == 1 and "--skip" in err


def test_mark_declined_note(monkeypatch, run):
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "declined"}, True, 5), seen)
    run("mark", "alice@example.com", "declined", "--note", "no reply after 2 nudges")
    assert seen["note"] == "no reply after 2 nudges"


def test_mark_note_only_on_declined(run):
    code, _, err = run("mark", "alice@example.com", "replied", "--channel", "email", "--note", "x")
    assert code == 1 and "declined" in err


def test_report_reads_floors_from_the_env_var(monkeypatch, run):
    monkeypatch.setenv("OUTREACH_FLOORS", "/nonexistent/floors.toml")
    code, _, err = run("report")
    assert code == 1 and "not found" in err


def test_followups_prints_the_classification(monkeypatch, run):
    item = {
        "person": {"id": 12, "email": "tom@example.com", "display_name": "Tom Example"},
        "kind": "nudge 2",
        "nudge_count": 1,
        "follow_up_on": "2026-10-13",
        "overdue": False,
        "brief": {"channel": "email", "at": "2026-09-29", "graph_message_id": "g1"},
        "last_nudge_channel": "email",
        "last_nudged_at": "2026-10-06",
        "channels": {"email": True, "imessage": True, "linkedin": False, "whatsapp": False},
        "suggested_channel": "imessage",
    }
    monkeypatch.setattr(cli.outreach, "follow_ups", lambda c, today: [item])
    code, out, _ = run("followups")
    assert code == 0 and "nudge 2" in out and "switch to imessage" in out


DOC = "https://docs.google.com/document/d/doc7/edit"


def test_doc_records_the_link(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(
        cli.outreach, "set_doc", lambda c, pid, url: seen.update(pid=pid, url=url) or {}
    )
    code, out, _ = run("doc", "alice@example.com", DOC)
    assert code == 0 and seen == {"pid": 7, "url": DOC}
    assert f"doc recorded ({DOC})" in out and run.conn.log == ["commit"]


def test_doc_clear(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(cli.outreach, "set_doc", lambda c, pid, url: seen.update(url=url) or {})
    code, out, _ = run("doc", "alice@example.com", "--clear")
    assert code == 0 and seen == {"url": None} and "doc link cleared" in out


@pytest.mark.parametrize(
    "argv", [("doc", "alice@example.com"), ("doc", "alice@example.com", DOC, "--clear")]
)
def test_doc_needs_a_link_or_clear(run, argv):
    code, _, err = run(*argv)
    assert code == 1 and "--clear" in err


def test_doc_refuses_a_non_docs_link(monkeypatch, run):
    def set_doc(c, pid, url):
        raise svc.InvalidDocUrl(f"not a Google Docs link: {url}")

    monkeypatch.setattr(cli.outreach, "set_doc", set_doc)
    code, _, err = run("doc", "alice@example.com", "https://example.com/x")
    assert code == 1 and "not a Google Docs link" in err and run.conn.log == ["rollback"]


def test_queue_table_marks_people_with_a_doc(monkeypatch, run):
    def item(pid, name, doc_url):
        return {
            "person": {"id": pid, "email": None, "display_name": name, "company": None},
            "rank": None,
            "last_interaction": None,
            "channels": {"email": True},
            "follow_up_on": None,
            "has_lead": False,
            "doc_url": doc_url,
        }

    items = [item(7, "Alice Example", DOC), item(8, "Bob Example", None)]
    monkeypatch.setattr(cli.outreach, "queue", lambda c, *, stage, limit: items)
    monkeypatch.setattr(cli.outreach, "shared_briefs", lambda c: [])
    code, out, _ = run("queue")
    lines = out.splitlines()
    assert code == 0 and "doc" in lines[0]
    assert "✓" in next(line for line in lines if "Alice" in line)
    assert "✓" not in next(line for line in lines if "Bob" in line)
