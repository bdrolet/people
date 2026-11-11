"""services/linkedin_outbox.py against an in-memory repo and a fake MCP sender. Nothing
here talks to LinkedIn."""

import contextlib
import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from models.linkedin_outbox import COUNTED_STATUSES, SendConfig
from services import linkedin_outbox as svc
from services.linkedin_pacing import InvalidMessage

# Tuesday 2026-11-03 10:00 PST: inside the default window.
NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
CFG = SendConfig()


class FakeRepo:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.state = {
            "id": 1,
            "paused_until": None,
            "paused_reason": None,
            "last_sent_at": None,
            "next_gap_seconds": None,
            "ramp_started_on": None,
            "consecutive_set_asides": 0,
            "last_error": None,
        }

    def insert(self, conn, **cols):
        rid = len(self.rows) + 1
        row = {
            "id": rid,
            "person_id": None,
            "profile_urn": None,
            "thread_id": None,
            "reason": None,
            "not_before": None,
            "attempted_at": None,
            "sent_at": None,
            "sent_thread_id": None,
            "thread_mismatch": False,
            "outreach_event_id": None,
            "session_hint": None,
            **cols,
        }
        self.rows[rid] = row
        return dict(row)

    def get(self, conn, rid):
        return dict(self.rows[rid]) if rid in self.rows else None

    lock = get

    def update(self, conn, rid, changes):
        self.rows[rid].update(changes)
        return dict(self.rows[rid])

    def claim_next(self, conn, now):
        ok = [
            r
            for r in self.rows.values()
            if r["status"] == "queued" and (r["not_before"] is None or r["not_before"] <= now)
        ]
        ok.sort(key=lambda r: (r["approved_at"], r["id"]))
        return dict(ok[0]) if ok else None

    def list_rows(self, conn, statuses):
        rows = [r for r in self.rows.values() if statuses is None or r["status"] in statuses]
        return [dict(r) for r in sorted(rows, key=lambda r: (r["approved_at"], r["id"]))]

    def count_counted_between(self, conn, since, until):
        return sum(
            1
            for r in self.rows.values()
            if r["status"] in COUNTED_STATUSES
            and since <= (r["sent_at"] or r["attempted_at"]) < until
        )

    def stale_sending(self, conn, older_than):
        return [
            dict(r)
            for r in self.rows.values()
            if r["status"] == "sending" and r["attempted_at"] < older_than
        ]

    def queue_stats(self, conn, *, mismatch_since):
        q = [r for r in self.rows.values() if r["status"] == "queued"]
        return {
            "queued": len(q),
            "oldest_approved_at": min((r["approved_at"] for r in q), default=None),
            "check_linkedin": sum(r["status"] == "check_linkedin" for r in self.rows.values()),
            "set_aside": sum(r["status"] == "set_aside" for r in self.rows.values()),
            "thread_mismatch": sum(
                bool(r["thread_mismatch"]) and r["sent_at"] >= mismatch_since
                for r in self.rows.values()
                if r["status"] == "sent"
            ),
        }

    def status_counts(self, conn):
        out: dict[str, int] = {}
        for r in self.rows.values():
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    def daily_counts(self, conn, *, since, tz):
        zone = ZoneInfo(tz)
        out: dict[tuple, int] = {}
        for r in self.rows.values():
            if r["approved_at"] >= since:
                key = ("approved", r["approved_at"].astimezone(zone).date(), r["kind"])
                out[key] = out.get(key, 0) + 1
            if r["status"] == "sent" and r["sent_at"] and r["sent_at"] >= since:
                key = ("sent", r["sent_at"].astimezone(zone).date(), r["kind"])
                out[key] = out.get(key, 0) + 1
        return [{"what": w, "day": d, "kind": k, "n": n} for (w, d, k), n in out.items()]

    def get_state(self, conn):
        return dict(self.state)

    lock_state = get_state

    def update_state(self, conn, changes):
        self.state.update(changes)
        return dict(self.state)


class Conn:
    def __init__(self):
        self.log: list = []

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")


class FakeSender:
    def __init__(self, conn, result=None, usable=True, raises=None):
        self.conn = conn
        self.result = result or {
            "status": "sent",
            "sent": True,
            "retry_safe": False,
            "thread_id": "t-new",
        }
        self.usable = usable
        self.raises = raises
        self.calls: list = []
        self.commits_before_send: int | None = None

    def session_problem(self):
        return None if self.usable is True else (self.usable or "login")

    def send(self, username, message, profile_urn):
        self.calls.append((username, message, profile_urn))
        self.commits_before_send = self.conn.log.count("commit")
        if self.raises:
            raise self.raises
        return self.result


class Recorder:
    def __init__(self):
        self.calls: list = []

    def add(self, n, attrs=None):
        self.calls.append(attrs or {})

    record = add


@pytest.fixture
def env(monkeypatch):
    repo = FakeRepo()
    for name in (
        "insert",
        "get",
        "lock",
        "update",
        "claim_next",
        "list_rows",
        "count_counted_between",
        "stale_sending",
        "queue_stats",
        "get_state",
        "lock_state",
        "update_state",
        "status_counts",
        "daily_counts",
    ):
        monkeypatch.setattr(svc.repo, name, getattr(repo, name))
    events: list = []

    def record_send(conn, person_id, *, at, channel, source):
        events.append((person_id, at, channel, source))
        return ("briefed", 100 + len(events))

    monkeypatch.setattr(svc.outreach, "record_send", record_send)
    metrics = {"sends": Recorder(), "queued": Recorder(), "latency": Recorder()}
    monkeypatch.setattr(svc.otel, "linkedin_sends", metrics["sends"])
    monkeypatch.setattr(svc.otel, "linkedin_queued", metrics["queued"])
    monkeypatch.setattr(svc.otel, "linkedin_approval_to_send", metrics["latency"])
    gauges: list = []
    monkeypatch.setattr(svc.otel, "set_linkedin_gauges", lambda v, r: gauges.append((v, r)))
    conn = Conn()
    monkeypatch.setattr(svc.otel, "set_rows", lambda key, rows: None)
    return {
        "repo": repo,
        "events": events,
        "metrics": metrics,
        "gauges": gauges,
        "conn": conn,
        "monkeypatch": monkeypatch,
    }


def _queue(env, *, person_id=7, message="Hi Alice", at=NOW, **kw):
    return svc.queue(
        env["conn"],
        person_id=person_id,
        username="alice",
        message=message,
        kind=kw.pop("kind", "brief"),
        send_now=kw.pop("send_now", False),
        now=at,
        cfg=CFG,
        **kw,
    )


def _run(env, sender=None, at=NOW, seed=1):
    sender = sender or FakeSender(env["conn"])
    word = svc.run_once(
        env["conn"],
        lambda: contextlib.nullcontext(sender),
        now=at,
        cfg=CFG,
        rng=random.Random(seed),
    )
    return word, sender


def _fill(env, n, status="sent", at=NOW):
    for _ in range(n):
        env["repo"].insert(
            None,
            linkedin_username="x",
            message="m",
            kind="other",
            mode="queued",
            status=status,
            approved_at=at,
            attempted_at=at,
            sent_at=at if status == "sent" else None,
        )


# --- queue ---------------------------------------------------------------


def test_queue_normalizes_and_starts_ramp(env):
    row = _queue(env, message="  Hi Alice,\r\nThanks \n")
    assert row["message"] == "Hi Alice,\nThanks"
    assert (row["status"], row["mode"], row["approved_at"]) == ("queued", "queued", NOW)
    assert env["repo"].state["ramp_started_on"] == date(2026, 11, 3)
    assert env["metrics"]["queued"].calls == [{"kind": "brief", "mode": "queued"}]


def test_queue_keeps_an_existing_ramp_start(env):
    env["repo"].state["ramp_started_on"] = date(2026, 10, 1)
    _queue(env)
    assert env["repo"].state["ramp_started_on"] == date(2026, 10, 1)


def test_queue_rejects_invalid_text_and_writes_nothing(env):
    with pytest.raises(InvalidMessage):
        _queue(env, message="tab\there")
    assert env["repo"].rows == {}


def test_queue_rejects_unknown_kind_and_empty_username(env):
    with pytest.raises(ValueError):
        _queue(env, kind="spam")
    with pytest.raises(ValueError):
        svc.queue(
            env["conn"],
            person_id=None,
            username=" ",
            message="Hi",
            kind="other",
            send_now=False,
            now=NOW,
            cfg=CFG,
        )


def test_send_now_writes_a_sending_row(env):
    row = _queue(env, send_now=True)
    assert (row["status"], row["mode"], row["attempted_at"]) == ("sending", "send_now", NOW)


def test_send_now_refused_at_ceiling_counts_inflight(env):
    # Day 0 of the ramp: the cap is 30. 29 sent plus one in flight is 30.
    env["repo"].state["ramp_started_on"] = date(2026, 11, 3)
    _fill(env, 29)
    _fill(env, 1, status="sending")
    with pytest.raises(svc.CeilingReached) as e:
        _queue(env, send_now=True)
    assert (e.value.sent_today, e.value.cap_today) == (30, 30)
    assert len(env["repo"].rows) == 30


def test_send_now_refused_at_weekly_ceiling(env):
    env["repo"].state["ramp_started_on"] = date(2026, 10, 1)
    _fill(env, 250, at=NOW - timedelta(days=1))
    with pytest.raises(svc.CeilingReached):
        _queue(env, send_now=True)


def test_queued_messages_are_not_refused_at_the_ceiling(env):
    env["repo"].state["ramp_started_on"] = date(2026, 11, 3)
    _fill(env, 30)
    assert _queue(env)["status"] == "queued"


# --- run_once ------------------------------------------------------------


def test_run_once_sends_one_and_records_brief(env):
    _queue(env, at=NOW - timedelta(hours=1))
    _queue(env, person_id=8, message="Second", at=NOW - timedelta(minutes=30))
    word, sender = _run(env)
    assert word == "sent"
    assert sender.calls == [("alice", "Hi Alice", None)]
    assert sender.commits_before_send and sender.commits_before_send >= 1
    row = env["repo"].rows[1]
    assert (row["status"], row["sent_at"], row["sent_thread_id"]) == ("sent", NOW, "t-new")
    assert row["outreach_event_id"] == 101
    assert env["events"] == [(7, NOW, "linkedin", "linkedin_outbox")]
    assert env["repo"].rows[2]["status"] == "queued"
    s = env["repo"].state
    assert s["last_sent_at"] == NOW and 180 <= s["next_gap_seconds"] <= 480
    assert env["metrics"]["sends"].calls[-1] == {
        "outcome": "sent",
        "mode": "queued",
        "kind": "brief",
    }
    assert env["metrics"]["latency"].calls  # approval → send recorded
    assert env["conn"].log[-1] == "commit"


def test_run_once_without_a_person_records_no_event(env):
    _queue(env, person_id=None)
    _run(env)
    assert env["events"] == [] and env["repo"].rows[1]["outreach_event_id"] is None


def test_run_once_outside_window_sends_nothing(env):
    _queue(env)
    saturday = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    word, sender = _run(env, at=saturday)
    assert word == "outside_window" and sender.calls == []


def test_run_once_respects_gap(env):
    _queue(env)
    env["repo"].state.update(last_sent_at=NOW - timedelta(seconds=200), next_gap_seconds=300)
    word, sender = _run(env)
    assert word == "gap" and sender.calls == []
    word, sender = _run(env, at=NOW + timedelta(seconds=101))
    assert word == "sent"


def test_run_once_respects_ceiling(env):
    env["repo"].state["ramp_started_on"] = date(2026, 11, 3)
    _fill(env, 30)
    _queue(env)
    word, sender = _run(env)
    assert word == "ceiling" and sender.calls == []


def test_run_once_empty_queue(env):
    word, sender = _run(env)
    assert word == "empty" and sender.calls == []


def test_run_once_honours_not_before(env):
    _queue(env, not_before=NOW + timedelta(hours=1))
    word, _ = _run(env)
    assert word == "empty"


def test_run_once_login_not_usable_holds_queue_and_keeps_row(env):
    _queue(env)
    sender = FakeSender(env["conn"], usable=False)
    word, _ = _run(env, sender)
    assert word == "login" and sender.calls == []
    assert env["repo"].rows[1]["status"] == "queued"
    assert env["repo"].state["paused_reason"] == "login"
    word, _ = _run(env)
    assert word == "paused"


def test_restricted_account_holds_with_its_reason(env):
    _queue(env)
    sender = FakeSender(env["conn"], usable="account_restricted")
    word, _ = _run(env, sender)
    assert word == "account_restricted" and sender.calls == []
    assert env["repo"].state["paused_reason"] == "account_restricted"


def test_mcp_that_will_not_start_requeues_without_pause(env):
    _queue(env)

    def broken():
        raise OSError("uvx missing")

    word = svc.run_once(env["conn"], broken, now=NOW, cfg=CFG, rng=random.Random(1))
    assert word == "mcp_unavailable"
    assert env["repo"].rows[1]["status"] == "queued"
    assert env["repo"].state["paused_reason"] is None


def test_browser_busy_requeues(env):
    _queue(env)
    sender = FakeSender(env["conn"], result={"error": {"code": "browser_busy"}})
    word, _ = _run(env, sender)
    assert word == "browser_busy"
    assert env["repo"].rows[1]["status"] == "queued"
    assert env["repo"].state["paused_reason"] is None and env["repo"].state["paused_until"] is None


def test_rate_limit_pause_clears_itself(env):
    _queue(env)
    sender = FakeSender(env["conn"], result={"error": {"code": "rate_limited", "retry_after": 60}})
    word, _ = _run(env, sender)
    assert word == "rate_limited"
    assert env["repo"].rows[1]["status"] == "check_linkedin"
    assert env["repo"].state["paused_until"] == NOW + timedelta(seconds=360)
    _queue(env, person_id=8, message="Next")
    assert _run(env, at=NOW + timedelta(seconds=300))[0] == "paused"
    assert _run(env, at=NOW + timedelta(seconds=900))[0] == "sent"
    assert env["repo"].state["paused_until"] is None


def test_hold_does_not_clear_itself(env):
    env["repo"].state["paused_reason"] = "login"
    _queue(env)
    assert _run(env, at=NOW + timedelta(days=2))[0] == "paused"


def test_three_set_asides_pause(env):
    for i in range(4):
        _queue(env, person_id=i, message=f"m{i}", at=NOW - timedelta(minutes=10 - i))
    bad = {"status": "message_unavailable", "sent": False, "retry_safe": True}
    for _ in range(3):
        assert _run(env, FakeSender(env["conn"], result=bad))[0] == "set_aside"
    assert env["repo"].state["paused_reason"] == "set_aside_streak"
    assert _run(env)[0] == "paused"


def test_a_send_resets_the_set_aside_streak(env):
    for i in range(3):
        _queue(env, person_id=i, message=f"m{i}", at=NOW - timedelta(minutes=10 - i))
    bad = {"status": "composer_occupied", "sent": False, "retry_safe": True}
    _run(env, FakeSender(env["conn"], result=bad))
    _run(env)
    assert env["repo"].state["consecutive_set_asides"] == 0


def test_sender_exception_is_check_linkedin(env):
    _queue(env)
    word, _ = _run(env, FakeSender(env["conn"], raises=RuntimeError("pipe closed")))
    assert word == "check_linkedin"
    assert env["repo"].rows[1]["status"] == "check_linkedin"
    assert env["repo"].rows[1]["reason"] == "outcome_unknown"


def test_unknown_status_pauses_and_keeps_row_queued(env):
    _queue(env)
    res = {"status": "enter_to_send_enabled", "sent": False, "retry_safe": True}
    word, _ = _run(env, FakeSender(env["conn"], result=res))
    assert word == "paused"
    assert env["repo"].rows[1]["status"] == "queued"
    assert env["repo"].state["paused_reason"] == "enter_to_send_enabled"


def test_stale_sending_becomes_check_linkedin(env):
    _queue(env, send_now=True, at=NOW - timedelta(minutes=16))
    _queue(env, send_now=True, message="fresh", at=NOW - timedelta(minutes=10))
    _run(env)
    assert env["repo"].rows[1]["status"] == "check_linkedin"
    assert env["repo"].rows[1]["reason"] == "stale_sending"
    assert env["repo"].rows[2]["status"] == "sending"


def test_stale_sweep_runs_outside_the_window(env):
    _queue(env, send_now=True, at=NOW - timedelta(days=4))
    saturday = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    _run(env, at=saturday)
    assert env["repo"].rows[1]["status"] == "check_linkedin"


def test_thread_mismatch_is_flagged(env):
    _queue(env, kind="reply", thread_id="t-old")
    _run(env)
    assert env["repo"].rows[1]["thread_mismatch"] is True


def test_gauges_every_run_without_identity(env):
    _queue(env)
    _run(env, at=datetime(2026, 11, 7, 18, 0, tzinfo=UTC))
    values, reason = env["gauges"][-1]
    assert values["queue_depth"] == 1 and values["heartbeat"] > 0
    assert set(values) == {
        "queue_depth",
        "oldest_age_seconds",
        "sends_today",
        "sends_week",
        "cap_today",
        "cap_week",
        "heartbeat",
        "paused",
    }
    assert reason is None


def test_metrics_carry_no_identity(env):
    _queue(env)
    _queue(env, person_id=8, message="b", send_now=True)
    _run(env)
    for rec in env["metrics"].values():
        for attrs in rec.calls:
            assert set(attrs) <= {"outcome", "mode", "kind"}


# --- apply_result / resolve / record / edit ---------------------------------


def test_apply_result_for_send_now(env):
    row = _queue(env, send_now=True)
    res = {"status": "sent", "sent": True, "retry_safe": False, "thread_id": "t"}
    out = svc.apply_result(env["conn"], row["id"], res, now=NOW, cfg=CFG, rng=random.Random(1))
    assert out.row_status == "sent"
    assert env["repo"].rows[1]["status"] == "sent"
    assert env["repo"].state["last_sent_at"] == NOW
    assert env["metrics"]["sends"].calls[-1]["mode"] == "send_now"


def test_apply_result_needs_a_sending_row(env):
    row = _queue(env)
    with pytest.raises(svc.NotResolvable):
        svc.apply_result(env["conn"], row["id"], {}, now=NOW, cfg=CFG, rng=random.Random(1))
    with pytest.raises(svc.OutboxNotFound):
        svc.apply_result(env["conn"], 99, {}, now=NOW, cfg=CFG, rng=random.Random(1))


def test_apply_sent_twice_writes_one_event(env):
    row = _queue(env, send_now=True)
    env["repo"].rows[row["id"]]["outreach_event_id"] = 55
    svc.resolve(env["conn"], row["id"], "sent", now=NOW, cfg=CFG, rng=random.Random(1))
    assert env["events"] == [] and env["repo"].rows[1]["outreach_event_id"] == 55


def test_resolve_check_linkedin_as_sent(env):
    row = _queue(env, send_now=True)
    env["repo"].rows[row["id"]]["status"] = "check_linkedin"
    svc.resolve(
        env["conn"], row["id"], "sent", now=NOW, cfg=CFG, rng=random.Random(1), thread_id="t9"
    )
    r = env["repo"].rows[1]
    assert (r["status"], r["sent_thread_id"], r["outreach_event_id"]) == ("sent", "t9", 101)


def test_resolve_set_aside_by_hand_does_not_feed_the_streak(env):
    row = _queue(env, send_now=True)
    svc.resolve(env["conn"], row["id"], "set_aside", now=NOW, cfg=CFG, rng=random.Random(1))
    assert env["repo"].state["consecutive_set_asides"] == 0


def test_resolve_refuses_a_queued_row_and_bad_status(env):
    row = _queue(env)
    with pytest.raises(svc.NotResolvable):
        svc.resolve(env["conn"], row["id"], "sent", now=NOW, cfg=CFG, rng=random.Random(1))
    row2 = _queue(env, send_now=True, message="x")
    with pytest.raises(ValueError):
        svc.resolve(env["conn"], row2["id"], "queued", now=NOW, cfg=CFG, rng=random.Random(1))


def test_record_manual_counts_and_records_event(env):
    at = NOW - timedelta(minutes=5)
    row = svc.record_manual(
        env["conn"],
        person_id=7,
        username="alice",
        message="Hand sent",
        kind="nudge",
        at=at,
        now=NOW,
        cfg=CFG,
        rng=random.Random(1),
    )
    assert (row["mode"], row["status"], row["sent_at"]) == ("manual", "sent", at)
    assert env["events"] == [(7, at, "linkedin", "linkedin_outbox")]
    assert env["repo"].state["last_sent_at"] == at
    assert svc.ceiling(env["conn"], now=NOW, cfg=CFG)["sent_today"] == 1


def test_record_manual_never_moves_last_sent_back(env):
    env["repo"].state["last_sent_at"] = NOW
    svc.record_manual(
        env["conn"],
        person_id=None,
        username="alice",
        message="old",
        kind="other",
        at=NOW - timedelta(hours=3),
        now=NOW,
        cfg=CFG,
        rng=random.Random(1),
    )
    assert env["repo"].state["last_sent_at"] == NOW


def test_edit_only_while_queued_resets_approval(env):
    row = _queue(env, at=NOW - timedelta(hours=1))
    edited = svc.edit(env["conn"], row["id"], " New text ", now=NOW)
    assert (edited["message"], edited["approved_at"]) == ("New text", NOW)
    env["repo"].rows[row["id"]]["status"] = "sent"
    with pytest.raises(svc.NotEditable):
        svc.edit(env["conn"], row["id"], "again", now=NOW)


def test_cancel(env):
    row = _queue(env)
    assert svc.cancel(env["conn"], row["id"])["status"] == "cancelled"
    with pytest.raises(svc.NotEditable):
        svc.cancel(env["conn"], row["id"])


def test_requeue_only_from_set_aside_or_check(env):
    row = _queue(env)
    with pytest.raises(svc.NotEditable):
        svc.requeue(env["conn"], row["id"])
    env["repo"].rows[row["id"]].update(status="check_linkedin", reason="send_unconfirmed")
    r = svc.requeue(env["conn"], row["id"])
    assert (r["status"], r["reason"], r["attempted_at"]) == ("queued", None, None)


def test_pause_and_resume(env):
    svc.pause(env["conn"], None)
    assert env["repo"].state["paused_reason"] == "manual"
    env["repo"].state.update(paused_until=NOW, consecutive_set_asides=2)
    svc.resume(env["conn"])
    s = env["repo"].state
    assert (s["paused_reason"], s["paused_until"], s["consecutive_set_asides"]) == (None, None, 0)


# --- status / estimates ----------------------------------------------------


def test_status_counts_against_cap(env):
    env["repo"].state["ramp_started_on"] = date(2026, 11, 3)
    _fill(env, 3)
    _queue(env)
    st = svc.status(env["conn"], now=NOW, cfg=CFG)
    assert (st["sent_today"], st["cap_today"], st["sent_week"], st["cap_week"]) == (3, 30, 3, 150)
    assert st["queued"] == 1 and st["next_send_at"] == NOW


def test_estimates_space_rows_and_skip_closed_hours(env):
    late = datetime(2026, 11, 4, 2, 55, tzinfo=UTC)  # Tue 18:55 PST
    _queue(env, at=late)
    _queue(env, person_id=8, message="b", at=late)
    _queue(env, person_id=9, message="c", at=late)
    est = svc.estimate_send_times(env["conn"], now=late, cfg=CFG)
    assert est[1] == late
    assert est[2] == late + timedelta(seconds=CFG.gap_min)
    # 18:58 + 3 min is past 19:00: Wednesday's opening.
    assert est[3] == datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def test_estimates_are_none_while_held(env):
    env["repo"].state["paused_reason"] = "login"
    _queue(env)
    assert svc.estimate_send_times(env["conn"], now=NOW, cfg=CFG) == {1: None}


def test_today_summary(env):
    _queue(env, at=NOW - timedelta(hours=5))
    env["repo"].state["paused_reason"] = "login"
    s = svc.today_summary(env["conn"], now=NOW, cfg=CFG)
    assert s["queued"] == 1 and s["oldest_age_hours"] == 5.0
    assert s["paused"] == "login" and s["check_linkedin"] == 0


def test_list_rows_filters_by_status(env):
    _queue(env)
    _queue(env, send_now=True, message="b")
    assert [r["id"] for r in svc.list_rows(env["conn"], ("queued",))] == [1]
    assert len(svc.list_rows(env["conn"], None)) == 2


def test_gauges_publish_outbox_rows_and_window_wait(env):
    _queue(env)  # approved Tue 10:00 Pacific
    _queue(env, person_id=8, message="b")
    env["repo"].rows[2].update(status="check_linkedin", attempted_at=NOW)
    published: dict = {}
    env["monkeypatch"].setattr(
        svc.otel, "set_rows", lambda key, rows: published.update({key: rows})
    )
    # Called directly: run_once suppresses gauge errors, which would hide a failure here.
    svc._emit_gauges(env["conn"], datetime(2026, 11, 3, 20, 0, tzinfo=UTC), CFG)  # 12:00 PT
    status = {a["status"]: v for v, a in published["linkedin_outbox"]}
    assert status["check_linkedin"] == 1
    assert published["linkedin_queue_oldest_window_seconds"] == [(7200.0, {})]
    daily = {(a["what"], a["day"], a["kind"]): v for v, a in published["linkedin_daily"]}
    assert daily[("approved", "2026-11-03", "brief")] == 2
