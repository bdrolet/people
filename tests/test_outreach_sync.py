from datetime import UTC, datetime, timedelta

import httpx
import pytest

from services import outreach_sync as sync

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def ev(**kw):
    base = {
        "id": 7,
        "pipeline": "referrer",
        "stage": "briefed",
        "at": NOW,
        "channel": "email",
        "asana_task_gid": None,
        "asana_completed_at": None,
        "buyer_label": None,
        "person_name": "Alice Example",
    }
    return base | kw


@pytest.mark.parametrize(
    "event,name,tags",
    [
        (ev(), "Brief: Alice Example", ["outreach", "serves:consulting"]),
        (
            ev(pipeline="buyer", stage="intro", buyer_label="Bo Buyer, Co"),
            "Intro: Bo Buyer, Co via Alice Example",
            ["intro", "serves:consulting"],
        ),
        (
            ev(pipeline="buyer", stage="conversation", buyer_label="Bo, Co"),
            "Conversation: Bo, Co",
            ["conversation", "serves:consulting"],
        ),
        (
            ev(pipeline="buyer", stage="proposal", buyer_label="Bo, Co"),
            "Proposal: Bo, Co",
            ["proposal", "serves:consulting"],
        ),
        (
            ev(pipeline="buyer", stage="signed", buyer_label="Bo, Co"),
            "Signed: Bo, Co",
            ["signed-client", "serves:consulting"],
        ),
    ],
)
def test_payload_per_event_type(event, name, tags):
    p = sync.task_payload(event)
    assert p["name"] == name and p["tags"] == tags
    assert "2026-10-20" in p["context"] and "outreach event 7" in p["context"]


def test_only_the_spec_events_produce_tasks():
    assert sync.produces_task("referrer", "briefed")
    for pipeline, stage in [
        ("referrer", "replied"),
        ("referrer", "declined"),
        ("buyer", "lost"),
        ("referrer", "candidate"),
    ]:
        assert not sync.produces_task(pipeline, stage)


@pytest.mark.parametrize(
    "age,bucket",
    [
        (timedelta(hours=3), "0d"),
        (timedelta(hours=30), "1d"),
        (timedelta(days=5), "2-7d"),
        (timedelta(days=9), "8d+"),
    ],
)
def test_late_buckets(age, bucket):
    assert sync.late_bucket(NOW - age, NOW) == bucket


class FakeTasks:
    def __init__(self, fail_create=False, fail_complete=False):
        self.fail_create, self.fail_complete = fail_create, fail_complete
        self.created, self.completed = [], []

    def configured(self):
        return True

    def create_task(self, name, tags, context):
        if self.fail_create:
            raise httpx.ConnectError("down")
        self.created.append(name)
        return f"gid{len(self.created)}"

    def complete_task(self, gid):
        if self.fail_complete:
            raise httpx.ConnectError("down")
        self.completed.append(gid)


class FakeRepo:
    def __init__(self, rows):
        self.rows = {r["id"]: r for r in rows}

    def sync_rows(self, conn, keys, *, event_id=None, limit=200):
        rows = [
            r
            for r in self.rows.values()
            if f"{r['pipeline']}:{r['stage']}" in keys
            and (r["asana_task_gid"] is None or r["asana_completed_at"] is None)
        ]
        return [r for r in rows if event_id is None or r["id"] == event_id][:limit]

    def set_asana_gid(self, conn, event_id, gid):
        self.rows[event_id]["asana_task_gid"] = gid

    def set_asana_completed(self, conn, event_id):
        self.rows[event_id]["asana_completed_at"] = NOW


class Conn:
    commits = 0

    def commit(self):
        self.commits += 1


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, n, attrs=None):
        self.calls.append(attrs)


@pytest.fixture
def wired(monkeypatch):
    def _wire(rows, **tasks_kw):
        tasks, repo = FakeTasks(**tasks_kw), FakeRepo(rows)
        monkeypatch.setattr(sync, "tasks_api", tasks)
        monkeypatch.setattr(sync, "outreach_repo", repo)
        monkeypatch.setattr(sync.otel, "outreach_sync", Recorder())
        monkeypatch.setattr(sync.otel, "external_errors", Recorder())
        return tasks, repo

    return _wire


def test_sync_creates_then_completes_and_records_both(wired):
    tasks, repo = wired([ev()])
    res = sync.sync_events(Conn(), [7], now=NOW)
    assert res.synced == 1 and res.failed == 0 and res.late == {"0d": 1}
    assert tasks.created == ["Brief: Alice Example"] and tasks.completed == ["gid1"]
    assert repo.rows[7]["asana_task_gid"] == "gid1" and repo.rows[7]["asana_completed_at"] == NOW
    assert sync.otel.outreach_sync.calls == [{"target": "asana", "outcome": "ok", "late": "0d"}]


def test_a_failed_post_leaves_the_gid_null_and_a_retry_succeeds(wired):
    tasks, repo = wired([ev()], fail_create=True)
    assert sync.sync_events(Conn(), [7], now=NOW).failed == 1
    assert repo.rows[7]["asana_task_gid"] is None
    tasks.fail_create = False
    assert sync.sync_pending(Conn(), now=NOW).synced == 1
    assert tasks.created == ["Brief: Alice Example"]


def test_a_failed_patch_keeps_the_gid_and_retries_only_completion(wired):
    tasks, repo = wired([ev()], fail_complete=True)
    sync.sync_events(Conn(), [7], now=NOW)
    assert repo.rows[7]["asana_task_gid"] == "gid1" and repo.rows[7]["asana_completed_at"] is None
    tasks.fail_complete = False
    sync.sync_pending(Conn(), now=NOW)
    assert tasks.created == ["Brief: Alice Example"]  # no second task
    assert tasks.completed == ["gid1"]


def test_the_gid_is_committed_before_the_completion_call(wired):
    tasks, _ = wired([ev()], fail_complete=True)
    conn = Conn()
    sync.sync_events(conn, [7], now=NOW)
    assert conn.commits == 1  # the gid survives a crash between the two calls


def test_events_without_a_task_are_skipped(wired):
    tasks, _ = wired([ev(stage="replied")])
    assert sync.sync_events(Conn(), [7], now=NOW).synced == 0 and tasks.created == []


def test_nothing_to_do_means_no_commit_and_no_call(wired):
    tasks, _ = wired([])
    conn = Conn()
    assert sync.sync_events(conn, [], now=NOW).synced == 0 and conn.commits == 0


def test_unconfigured_is_skipped_not_failed(monkeypatch, wired):
    tasks, _ = wired([ev()])
    monkeypatch.setattr(tasks, "configured", lambda: False)
    res = sync.sync_events(Conn(), [7], now=NOW)
    assert res.skipped and res.failed == 0 and tasks.created == []
