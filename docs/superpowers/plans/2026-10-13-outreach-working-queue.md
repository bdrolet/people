# Outreach Working Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make follow-ups recorded facts with a fixed cadence, add brief targets and an `outreach today` screen with a pace verdict, a saved queue order, skip/decline notes, and a `working-outreach-queue` skill that works the pipeline through the CLI.

**Architecture:** Phase 1's stage machine stays the single writer. A `nudge` becomes an event kind on a `briefed` referrer, not a stage: `services/outreach.py::nudge` appends it and moves `follow_up_on` along a fixed +7/+14/+21 schedule counted from the brief. The `email_sent` handler briefs a `to_contact` recipient and nudges a `briefed` one. Two new pure modules, `services/outreach_targets.py` (TOML loader) and `services/outreach_today.py` (verdicts and rendering), turn DB counts into the daily screen. The skill holds judgment only; every count comes from tested code.

**Tech Stack:** Python 3.13, Postgres (pg8000 on Cloud SQL / psycopg3 locally), tomllib, OpenTelemetry, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md`. Read §2–§8a before starting. It builds on `docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md` (phase 1, "phase-1 §N").

## Global Constraints

- **Repo is public.** No real names, addresses, targets, floors, actuals or message text in code, tests, fixtures, commit messages or logs. Fixtures use `example.com` addresses and `+1555010…` numbers. Target and floor numbers live only in `~/src/consulting/docs/` (private repo); test fixtures may use the spec's illustrative numbers (5/day, 25/week, 150 total).
- **One writer.** Only `services/outreach.py` writes `outreach_contacts` and `outreach_events` (the Asana columns excepted, as in phase 1). The CLI and the handler call it; nothing calls `repo/outreach.py` writes directly.
- **Durable first.** Stage changes and nudges are committed before any tasks-api, inbox-api or Google call.
- **Layer rules (CLAUDE.md).** `clients/` I/O only; `repo/` takes an open connection, never commits, decides nothing; `services/` business logic; `handlers/` orchestrate; `models/` pure types; scripts call services.
- **Enums are exact strings.** Referrer stages `candidate, to_contact, briefed, replied, introduced, declined, skipped`; activity kinds `nudge`; channels `email, imessage, whatsapp, linkedin, call, in_person`; sources `event, cli, hubspot, seed`. Follow-up kinds `nudge 1`, `nudge 2`, `close`. Verdicts `ON TRACK`, `ON PACE`, `BEHIND`; line statuses `met`, `on pace`, `behind`, `n/a`.
- **Time zone.** Every date ("today", the week, the brief's day, due dates) is an `America/New_York` date. Weeks run Monday–Sunday.
- **Nudge cadence (spec §3.1, D3).** Due dates count from `briefed_at`'s New York date: `+7 * (min(nudge_count, 2) + 1)` days. A nudge needs `at >= briefed_at + 1 day`.
- **pg8000 gotcha.** `clients/db.py::_adapt_params` rewrites any all-numeric Python list parameter into a string. Never pass a list of ints as a query parameter.
- **Metric names** keep the `people.` prefix: the new counter is `people.outreach_nudges` with attributes `channel`, `source`, `n` (`1`, `2`, `3+`). Attribute values come only from closed enums.
- **The skill never sends.** Email is left as an Outlook draft; other channels are Ben's to send.
- **Branch.** Implementation goes on `outreach-working-queue` (it already holds the spec and this plan, pushed to origin), so spec, plan and code ship in one PR. Open the PR with the `/pr-open` skill (Task 10), never by hand.
- **Commit trailer.** End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01VXreHMh6ffcAPXJvhLmUG4
  ```
- **Format first.** Code blocks here are not pre-formatted. Run `.venv/bin/ruff check --fix . && .venv/bin/ruff format .` before the checks.
- **Checks before every commit:**
  ```bash
  .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
  ```
- **Real-Postgres tests.** `tests/test_outreach_db.py` is skipped unless `TEST_DATABASE_URL` is set. CI sets it (`.github/workflows/ci.yml`). Locally, use a throwaway container before every commit that touches SQL:
  ```bash
  docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
  export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
  ```
  If Docker is unavailable, say so in the task report and rely on CI; do not claim the SQL was verified locally.

## Decisions this plan makes where the spec is silent or loose

Each is called out so a reviewer can reject it on its own.

1. **Replay check covers every referrer event, not only nudges.** `nudge` returns `None` when *any* referrer event for that person already carries the `graph_message_id`. Inbox's sweep can republish a brief days after the contact reached `briefed`; checking only `nudge` rows would count that republished brief as nudge 1.
2. **`nudge` also takes `from_address`.** The CLI's `--from` (spec §3.3, "reuse `mark`'s `--from` check") needs somewhere to go; it lands on the event's existing `from_address` column, as `mark --from` does.
3. **The handler's dispatch lives in the service.** `services/outreach.py::advance_from_sent` locks the row and picks brief vs nudge by stage, returning `("briefed" | "nudge", event_id)`. The handler stays a loop.
4. **Week and plan marks are clipped to the plan window.** The plan starts on a Tuesday (2026-10-13); Monday of that week is not a missed workday. Before `start`, after `end`, and on non-workdays there is no daily target.
5. **Nudges due/overdue and proposed closes come from the `follow_ups` list**, classified in Python, rather than three more COUNT queries. Same rows, one query, no drift between the list and its counts.
6. **`outreach report` without floors still works.** Spec §4.2 says resolution ends in an error; for `report` that would remove the floors-free report phase 1 ships and tests (`test_report_without_floors`). `report` falls back to `OUTREACH_FLOORS`, then to no floors. `today` errors when it finds no targets, naming `--targets` and `OUTREACH_TARGETS`.
7. **`rank --file` refuses an empty file and a repeated person**, alongside lines that fail to resolve or are not at `to_contact`. Nothing changes on refusal.
8. **`--note` is accepted only where the spec allows it**: `decide --skip`, `mark ... declined`, `nudge`. Elsewhere it is a user error, not silently dropped.
9. **`queue --json` writes warnings to stderr**, so stdout is pure JSON for the skill.
10. **The spec's §5.2 example is corrected** (Task 9): 2026-10-14 is a Wednesday, and with an overdue nudge the verdict is `BEHIND`, not `ON PACE`; nudges due counts nudge items only (closes are listed separately).

## Review Focus

1. **Inbox republishes the brief email days later, after the contact is at `briefed`.** Expected: no nudge recorded. Pinned in Task 2 (`test_a_replayed_brief_is_not_a_nudge`).
2. **The plan's first week starts mid-week.** Expected: on Tue 2026-10-13 morning with nothing sent, the week is `on pace` (Monday is not owed), not `behind`. Pinned in Task 7 (`test_first_morning_of_a_plan_that_starts_mid_week_is_on_pace`).
3. **`.env` sets `OUTREACH_FLOORS`/`OUTREACH_TARGETS` and leaks into the CLI tests** (`scripts/outreach.py` calls `load_dotenv()` at import). Expected: tests are hermetic. Pinned in Task 8 (the `run` fixture deletes both vars; `test_today_without_targets_names_both_options`).
4. **A rank file with one bad line** (unknown person, someone already briefed, a repeat). Expected: every bad line listed, nothing written, existing ranks kept. Pinned in Task 8 (`test_rank_file_refuses_the_whole_file`).
5. **Ben sends a thank-you from the consulting address to someone at `replied` or `declined`.** Expected: no nudge, no stage change. Pinned in Task 3 (`test_advance_from_sent_ignores_other_stages`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repo/schema.sql` | Modify | New columns `nudge_count`, `last_nudged_at`, `rank`, `note`; event stage CHECK admits `nudge`. |
| `models/outreach.py` | Modify | `ACTIVITY_KINDS`, `TodayCounts`. |
| `clients/otel.py` | Modify | `people.outreach_nudges` counter. |
| `repo/outreach.py` | Modify | New columns in reads/updates; `note` on events; `referrer_event_with_message`; `clear_ranks`; rank-ordered `queue`; enriched `follow_ups`; `first_briefs_between`, `nudges_between`, `stage_count`. |
| `services/outreach.py` | Modify | `nudge`, `nudge_due_date`, `advance_from_sent`, `note` on `advance`, rank clearing, `set_rank`/`set_ranks`/`clear_ranks`, `follow_up_item`, `today_counts`, `rank` in `queue_item`. |
| `handlers/email_sent.py` | Modify | `_advance_briefed` → `_record_outreach`: brief or nudge per To recipient. |
| `services/outreach_targets.py` | Create | Pure: `Targets`, `TargetsError`, `load_targets`. |
| `services/outreach_today.py` | Create | Pure: `Today`, `workdays`, `build`, `render`, `to_json`. |
| `scripts/outreach.py` | Modify | `nudge`, `rank`, `today`; `queue --json`; `--note`; `followups` classification; `report` env fallback. |
| `scripts/fetch-env.sh` | Modify | Write `OUTREACH_TARGETS`/`OUTREACH_FLOORS` when the files exist. |
| `scripts/link-skills.sh` | Modify | Link `working-outreach-queue`. |
| `.claude/skills/working-outreach-queue/SKILL.md` | Create | The skill (spec §7). |
| `CLAUDE.md`, `.claude/skills/people-architecture/SKILL.md` | Modify | Document the working queue. |
| `docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md` | Modify | Correct the §5.2 example; mark implemented. |
| `tests/test_outreach_db.py`, `tests/test_repo_outreach.py`, `tests/test_outreach.py`, `tests/test_handlers.py`, `tests/test_outreach_cli.py` | Modify | As each task says. |
| `tests/test_outreach_targets.py`, `tests/test_outreach_today.py` | Create | Pure-module tests. |

---

### Task 1: Schema and model constants

**Files:**
- Modify: `repo/schema.sql` (outreach block, lines ~404–474)
- Modify: `models/outreach.py`
- Test: `tests/test_outreach_db.py`

**Interfaces:**
- Produces: columns `outreach_contacts.nudge_count SMALLINT NOT NULL DEFAULT 0`, `outreach_contacts.last_nudged_at TIMESTAMPTZ`, `outreach_contacts.rank INTEGER`, `outreach_events.note TEXT`; event stage `nudge` accepted; `models.outreach.ACTIVITY_KINDS = ("nudge",)`.

- [ ] **Step 1: Write the failing DB tests**

Append to `tests/test_outreach_db.py`:

```python
def test_nudge_is_an_event_kind_but_not_a_contact_stage(conn):
    pid = _person(conn)
    conn.execute(
        "INSERT INTO outreach_events (pipeline, person_id, stage, at, source) "
        "VALUES ('referrer', %s, 'nudge', %s, 'cli')",
        (pid, TS),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'nudge')", (pid,))


def test_working_queue_columns_have_their_defaults(conn):
    pid = _person(conn)
    row = conn.execute(
        "INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'to_contact') "
        "RETURNING nudge_count, last_nudged_at, rank",
        (pid,),
    ).fetchone()
    assert row == {"nudge_count": 0, "last_nudged_at": None, "rank": None}


def test_the_stage_check_upgrade_runs_on_a_phase1_database(conn):
    # A phase-1 database has the old inline CHECK, which lacks 'nudge'.
    conn.execute("ALTER TABLE outreach_events DROP CONSTRAINT outreach_events_stage_check")
    conn.execute(
        "ALTER TABLE outreach_events ADD CONSTRAINT outreach_events_stage_check "
        "CHECK (stage IN ('candidate','briefed'))"
    )
    conn.execute(SCHEMA.read_text())
    conn.execute(SCHEMA.read_text())
    pid = _person(conn)
    conn.execute(
        "INSERT INTO outreach_events (pipeline, person_id, stage, at, source) "
        "VALUES ('referrer', %s, 'nudge', %s, 'cli')",
        (pid, TS),
    )
    names = conn.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'outreach_events'::regclass AND conname = 'outreach_events_stage_check'"
    ).fetchall()
    assert len(names) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_db.py -q -k "nudge or working_queue or stage_check"` (with `TEST_DATABASE_URL` set)
Expected: FAIL — `CheckViolation` on the `nudge` event insert, `column "nudge_count" does not exist`.

- [ ] **Step 3: Change the schema**

In `repo/schema.sql`, in the `CREATE TABLE IF NOT EXISTS outreach_events` statement, add `'nudge'` to the stage list so a fresh database gets it directly:

```sql
    stage               TEXT NOT NULL CHECK (stage IN
                          ('candidate','to_contact','briefed','replied','introduced','declined','skipped',
                           'intro','conversation','proposal','signed','lost','nudge')),
```

Then append at the very end of the file:

```sql

-- Outreach working queue (docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md
-- §3.2, §6). A `nudge` is an event on a briefed referrer, not a stage: the contact stays at
-- `briefed`, and outreach_contacts.stage never takes the value.
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS nudge_count    SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS last_nudged_at TIMESTAMPTZ;
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS rank           INTEGER;  -- queue order, to_contact only
ALTER TABLE outreach_events   ADD COLUMN IF NOT EXISTS note           TEXT;     -- free text, never sent anywhere

-- A phase-1 database has the inline stage CHECK without 'nudge' (Postgres names it
-- outreach_events_stage_check). Replace it once; a database that already admits 'nudge'
-- is left alone, so the file stays safe to re-run. position(), not LIKE '%…%': this file
-- holds no percent sign, so no driver's paramstyle can misread it.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'outreach_events'::regclass
          AND conname = 'outreach_events_stage_check'
          AND position('nudge' in pg_get_constraintdef(oid)) > 0
    ) THEN
        ALTER TABLE outreach_events DROP CONSTRAINT IF EXISTS outreach_events_stage_check;
        ALTER TABLE outreach_events ADD CONSTRAINT outreach_events_stage_check CHECK (stage IN
            ('candidate','to_contact','briefed','replied','introduced','declined','skipped',
             'intro','conversation','proposal','signed','lost','nudge'));
    END IF;
END $$;
```

- [ ] **Step 4: Add the model constant**

In `models/outreach.py`, after `REFERRER_STAGES`:

```python
# Event kinds that are not stages (working-queue spec §3.2): appended to outreach_events,
# never stored in outreach_contacts.stage, never a CLI stage choice, never an Asana task.
ACTIVITY_KINDS = ("nudge",)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_db.py -q`
Expected: PASS, including the existing `test_schema_is_still_idempotent`.

- [ ] **Step 6: Commit**

```bash
git add repo/schema.sql models/outreach.py tests/test_outreach_db.py
git commit -m "outreach: schema for nudges, queue rank and event notes"
```

---

### Task 2: Record a nudge (repo, service, metric)

**Files:**
- Modify: `repo/outreach.py`
- Modify: `services/outreach.py`
- Modify: `clients/otel.py`
- Test: `tests/test_outreach.py`, `tests/test_repo_outreach.py`, `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: Task 1's columns.
- Produces:
  - `repo/outreach.py`: `insert_event(..., note: str | None = None) -> int`; `referrer_event_with_message(conn, person_id: int, graph_message_id: str) -> bool`; `_CONTACT_COLUMNS` and `_CONTACT_UPDATABLE` include `nudge_count`, `last_nudged_at`, `rank`.
  - `services/outreach.py`: `nudge_due_date(briefed_at: datetime, nudge_count: int) -> date`; `nudge(conn, person_id: int, *, at: datetime, channel: str, source: str, graph_message_id: str | None = None, from_address: str | None = None, note: str | None = None) -> int | None`; `advance(..., note: str | None = None)` passes `note` to the event.
  - `clients/otel.py`: `outreach_nudges: metrics.Counter`.

- [ ] **Step 1: Extend the test fakes**

In `tests/test_outreach.py`, `FakeRepo.insert_contact_if_missing`, add three keys to the new row dict:

```python
            "nudge_count": 0,
            "last_nudged_at": None,
            "rank": None,
```

Add these methods to `FakeRepo`:

```python
    def referrer_event_with_message(self, conn, person_id, graph_message_id):
        return any(
            e.get("pipeline") == "referrer"
            and e.get("person_id") == person_id
            and e.get("graph_message_id") == graph_message_id
            for e in self.events
        )

    def clear_ranks(self, conn):
        n = 0
        for row in self.contacts.values():
            if row.get("rank") is not None:
                row["rank"] = None
                n += 1
        return n
```

In the `repo` fixture, record the new counter too:

```python
    for name in ("outreach_stage_events", "outreach_lost", "outreach_nudges"):
        monkeypatch.setattr(svc.otel, name, Recorder())
```

`advance` will now always pass `note`, so in `test_briefed_sets_cohort_first_brief_and_follow_up` add `"note": None,` to the expected `repo.events[0]` dict (after `"from_address": None,`).

- [ ] **Step 2: Write the failing service tests**

Append to `tests/test_outreach.py`:

```python
BRIEF = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)  # Tue 2026-10-06, 11:00 New York


def _briefed(repo, pid=1, at=BRIEF, channel="email"):
    _at(repo, pid, "briefed")
    repo.contacts[pid].update(briefed_at=at, channel=channel, follow_up_on=date(2026, 10, 13))


def test_nudge_due_date_counts_from_the_brief_in_new_york():
    # 02:00 UTC on 10-07 is still 10-06 in New York.
    brief = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    assert svc.nudge_due_date(brief, 1) == date(2026, 10, 20)
    assert svc.nudge_due_date(brief, 2) == date(2026, 10, 27)
    assert svc.nudge_due_date(brief, 5) == date(2026, 10, 27)


def test_nudge_records_the_event_and_moves_the_follow_up(repo):
    _briefed(repo)
    at = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
    eid = svc.nudge(None, 1, at=at, channel="email", source="event", graph_message_id="g2")
    assert eid == 1
    row = repo.contacts[1]
    assert row["stage"] == "briefed"
    assert row["nudge_count"] == 1 and row["last_nudged_at"] == at
    assert row["follow_up_on"] == date(2026, 10, 20)  # brief + 14
    assert repo.events[0] == {
        "pipeline": "referrer",
        "person_id": 1,
        "stage": "nudge",
        "at": at,
        "channel": "email",
        "source": "event",
        "graph_message_id": "g2",
        "from_address": None,
        "note": None,
    }
    assert svc.otel.outreach_nudges.calls == [
        (1, {"channel": "email", "source": "event", "n": "1"})
    ]


@pytest.mark.parametrize(
    "n,due,label",
    [(1, date(2026, 10, 20), "1"), (2, date(2026, 10, 27), "2"), (3, date(2026, 10, 27), "3+")],
)
def test_nudge_due_dates_stop_at_twenty_one_days(repo, n, due, label):
    _briefed(repo)
    for i in range(n):
        svc.nudge(None, 1, at=BRIEF + timedelta(days=8 + i), channel="imessage", source="cli")
    assert repo.contacts[1]["nudge_count"] == n
    assert repo.contacts[1]["follow_up_on"] == due
    assert svc.otel.outreach_nudges.calls[-1][1]["n"] == label


def test_a_late_nudge_does_not_push_the_schedule_back(repo):
    _briefed(repo)
    svc.nudge(None, 1, at=BRIEF + timedelta(days=12), channel="email", source="cli")
    assert repo.contacts[1]["follow_up_on"] == date(2026, 10, 20)


def test_a_nudge_within_a_day_of_the_brief_is_ignored(repo):
    _briefed(repo)
    at = BRIEF + timedelta(hours=23)
    assert svc.nudge(None, 1, at=at, channel="email", source="event") is None
    assert repo.events == [] and repo.contacts[1]["nudge_count"] == 0


def test_a_nudge_exactly_a_day_after_the_brief_counts(repo):
    _briefed(repo)
    assert svc.nudge(None, 1, at=BRIEF + timedelta(days=1), channel="call", source="cli") == 1


@pytest.mark.parametrize("stage", ["candidate", "to_contact", "replied", "introduced", "declined"])
def test_a_nudge_outside_briefed_is_ignored(repo, stage):
    _briefed(repo)
    repo.contacts[1]["stage"] = stage
    assert svc.nudge(None, 1, at=BRIEF + timedelta(days=8), channel="email", source="cli") is None
    assert repo.events == []


def test_a_nudge_without_a_pipeline_row_is_ignored(repo):
    assert svc.nudge(None, 9, at=TS, channel="email", source="cli") is None


def test_a_replayed_nudge_email_is_not_a_second_nudge(repo):
    _briefed(repo)
    at = BRIEF + timedelta(days=7)
    assert svc.nudge(None, 1, at=at, channel="email", source="event", graph_message_id="g2") == 1
    assert svc.nudge(None, 1, at=at, channel="email", source="event", graph_message_id="g2") is None
    assert repo.contacts[1]["nudge_count"] == 1


def test_a_replayed_brief_is_not_a_nudge(repo):
    # Inbox's sweep republishes the brief email days later, after the contact is briefed.
    _briefed(repo)
    repo.events.append(
        {"pipeline": "referrer", "person_id": 1, "stage": "briefed", "graph_message_id": "g1"}
    )
    at = BRIEF + timedelta(days=9)
    assert svc.nudge(None, 1, at=at, channel="email", source="event", graph_message_id="g1") is None
    assert repo.contacts[1]["nudge_count"] == 0


def test_nudge_keeps_from_address_and_note(repo):
    _briefed(repo)
    svc.nudge(
        None,
        1,
        at=BRIEF + timedelta(days=8),
        channel="email",
        source="cli",
        from_address="alias@example.com",
        note="sent from phone",
    )
    assert repo.events[0]["from_address"] == "alias@example.com"
    assert repo.events[0]["note"] == "sent from phone"


def test_nudge_rejects_an_unknown_channel(repo):
    _briefed(repo)
    with pytest.raises(ValueError):
        svc.nudge(None, 1, at=BRIEF + timedelta(days=8), channel="pigeon", source="cli")


def test_advance_stores_a_note_on_the_event(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "skipped", at=TS, source="cli", note="moved abroad")
    assert repo.events[-1]["note"] == "moved abroad"
```

- [ ] **Step 3: Write the failing repo tests**

Append to `tests/test_repo_outreach.py`:

```python
def test_insert_event_passes_the_note():
    conn = FakeConn([[{"id": 7}]])
    outreach.insert_event(
        conn, pipeline="referrer", person_id=1, stage="skipped", at=TS, source="cli", note="why"
    )
    sql, params = conn.calls[0]
    assert "note" in sql and params[-1] == "why"


def test_referrer_event_with_message():
    conn = FakeConn([[{"found": True}]])
    assert outreach.referrer_event_with_message(conn, 1, "g1") is True
    sql, params = conn.calls[0]
    assert "pipeline = 'referrer'" in sql and params == (1, "g1")


def test_update_contact_accepts_the_working_queue_columns():
    conn = FakeConn([[{"person_id": 1}]])
    outreach.update_contact(conn, 1, {"nudge_count": 1, "last_nudged_at": TS, "rank": None})
    assert conn.calls[0][1] == (1, TS, None, 1)
```

Append to `tests/test_outreach_db.py`:

```python
def test_nudge_columns_and_message_lookup_round_trip(conn):
    pid = _person(conn)
    outreach_repo.insert_contact_if_missing(conn, pid)
    row = outreach_repo.update_contact(
        conn, pid, {"stage": "briefed", "nudge_count": 1, "last_nudged_at": TS, "rank": None}
    )
    assert row["nudge_count"] == 1 and row["last_nudged_at"] == TS and row["rank"] is None
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="nudge",
        at=TS,
        source="event",
        channel="email",
        graph_message_id="g9",
        note="hi",
    )
    assert outreach_repo.referrer_event_with_message(conn, pid, "g9")
    assert not outreach_repo.referrer_event_with_message(conn, pid, "g10")
    assert conn.execute("SELECT note FROM outreach_events").fetchone() == {"note": "hi"}
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_repo_outreach.py -q`
Expected: FAIL — `AttributeError: module 'services.outreach' has no attribute 'nudge'`, `insert_event() got an unexpected keyword argument 'note'`, `not updatable: ['last_nudged_at', 'nudge_count', 'rank']`, and `svc.otel` has no `outreach_nudges`.

- [ ] **Step 5: Add the metric**

In `clients/otel.py`, next to the other outreach counter declarations (line ~43):

```python
outreach_nudges: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

Add `outreach_nudges` to the `global` statement in `setup_telemetry` (the line `global outreach_stage_events, outreach_lost, outreach_sync, outreach_report_runs`), and after `outreach_report_runs = meter.create_counter(...)`:

```python
    outreach_nudges = meter.create_counter(
        "people.outreach_nudges",
        description="Outreach follow-up nudges by channel/source/n (1, 2, 3+)",
    )
```

- [ ] **Step 6: Change the repo**

In `repo/outreach.py`:

```python
_CONTACT_COLUMNS = """
    person_id, stage, cohort_week, briefed_at, replied_at, channel, has_lead,
    follow_up_on, notes, nudge_count, last_nudged_at, rank, hubspot_synced_at,
    created_at, updated_at
"""
```

Add `"nudge_count"`, `"last_nudged_at"` and `"rank"` to `_CONTACT_UPDATABLE`.

Replace `insert_event` with:

```python
def insert_event(
    conn: Any,
    *,
    pipeline: str,
    stage: str,
    at: datetime,
    source: str,
    person_id: int | None = None,
    opportunity_id: int | None = None,
    channel: str | None = None,
    graph_message_id: str | None = None,
    from_address: str | None = None,
    note: str | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO outreach_events
            (pipeline, person_id, opportunity_id, stage, at, channel, source,
             graph_message_id, from_address, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            pipeline,
            person_id,
            opportunity_id,
            stage,
            at,
            channel,
            source,
            graph_message_id,
            from_address,
            note,
        ),
    ).fetchone()
    return int(row["id"])


def referrer_event_with_message(conn: Any, person_id: int, graph_message_id: str) -> bool:
    """Has this email already been recorded for this person, as a brief or a nudge? Inbox's
    sweep can republish a brief days later, after the contact has reached `briefed`."""
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM outreach_events
            WHERE pipeline = 'referrer' AND person_id = %s AND graph_message_id = %s
        ) AS found
        """,
        (person_id, graph_message_id),
    ).fetchone()
    return bool(row["found"])
```

- [ ] **Step 7: Change the service**

In `services/outreach.py`, import `CHANNELS` from `models.outreach` alongside the existing names. Give `advance` a `note: str | None = None` keyword (after `force`) and pass `note=note` to its `outreach_repo.insert_event` call. Then add, after `advance_from_event`:

```python
NUDGE_LABELS = ("1", "2", "3+")


def nudge_due_date(briefed_at: datetime, nudge_count: int) -> date:
    """Working-queue spec §3.1: due dates count from the brief, never from the last nudge,
    so a late nudge does not push the schedule back. +14 after nudge 1, +21 after 2 or more."""
    return briefed_at.astimezone(TZ).date() + timedelta(
        days=FOLLOW_UP_DAYS * (min(nudge_count, 2) + 1)
    )


def nudge(
    conn: Any,
    person_id: int,
    *,
    at: datetime,
    channel: str,
    source: str,
    graph_message_id: str | None = None,
    from_address: str | None = None,
    note: str | None = None,
) -> int | None:
    """Record a follow-up on a briefed referrer (working-queue spec §3.3). A nudge is an
    event, not a stage: the contact stays at `briefed`. Returns the event id, or None when
    the person is not at `briefed`, `at` is less than a day after the brief (a same-day
    correction is not a follow-up), or this email was already recorded for them."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; one of {', '.join(CHANNELS)}")
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None or row["stage"] != "briefed" or row["briefed_at"] is None:
        return None
    if at < row["briefed_at"] + timedelta(days=1):
        return None
    if graph_message_id and outreach_repo.referrer_event_with_message(
        conn, person_id, graph_message_id
    ):
        return None
    n = int(row["nudge_count"] or 0) + 1
    last = row["last_nudged_at"]
    outreach_repo.update_contact(
        conn,
        person_id,
        {
            "nudge_count": n,
            "last_nudged_at": max(at, last) if last else at,
            "follow_up_on": nudge_due_date(row["briefed_at"], n),
        },
    )
    event_id = outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=person_id,
        stage="nudge",
        at=at,
        channel=channel,
        source=source,
        graph_message_id=graph_message_id,
        from_address=from_address,
        note=note,
    )
    otel.outreach_nudges.add(
        1, {"channel": channel, "source": source, "n": NUDGE_LABELS[min(n, 3) - 1]}
    )
    return event_id
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_repo_outreach.py tests/test_outreach_db.py -q`
Expected: PASS.

- [ ] **Step 9: Run the full checks and commit**

```bash
git add repo/outreach.py services/outreach.py clients/otel.py tests/test_outreach.py tests/test_repo_outreach.py tests/test_outreach_db.py
git commit -m "outreach: record nudges on briefed referrers, with event notes"
```

---

### Task 3: Email path — brief or nudge each To recipient

**Files:**
- Modify: `services/outreach.py`
- Modify: `handlers/email_sent.py`
- Test: `tests/test_outreach.py`, `tests/test_handlers.py`

**Interfaces:**
- Consumes: `nudge`, `advance_from_event` (Task 2 / phase 1).
- Produces: `services/outreach.py::advance_from_sent(conn, person_id: int, *, at: datetime, graph_message_id: str | None) -> tuple[str, int] | None` — `("briefed", event_id)` or `("nudge", event_id)`.

- [ ] **Step 1: Write the failing service tests**

Append to `tests/test_outreach.py`:

```python
def test_advance_from_sent_briefs_a_to_contact_row(repo):
    _at(repo, 1, "to_contact")
    assert svc.advance_from_sent(None, 1, at=TS, graph_message_id="g1") == ("briefed", 1)
    assert repo.contacts[1]["stage"] == "briefed"


def test_advance_from_sent_nudges_a_briefed_row(repo):
    _briefed(repo)
    at = BRIEF + timedelta(days=7)
    assert svc.advance_from_sent(None, 1, at=at, graph_message_id="g2") == ("nudge", 1)
    assert repo.events[0]["channel"] == "email" and repo.events[0]["source"] == "event"


@pytest.mark.parametrize("stage", ["candidate", "replied", "introduced", "declined", "skipped"])
def test_advance_from_sent_ignores_other_stages(repo, stage):
    # e.g. a thank-you from the consulting address to someone who already replied
    _briefed(repo)
    repo.contacts[1]["stage"] = stage
    at = BRIEF + timedelta(days=8)
    assert svc.advance_from_sent(None, 1, at=at, graph_message_id="g3") is None
    assert repo.events == [] and repo.contacts[1]["stage"] == stage


def test_advance_from_sent_without_a_row(repo):
    assert svc.advance_from_sent(None, 9, at=TS, graph_message_id="g1") is None


def test_advance_from_sent_replaying_the_brief_records_nothing(repo):
    _at(repo, 1, "to_contact")
    assert svc.advance_from_sent(None, 1, at=TS, graph_message_id="g1") == ("briefed", 1)
    assert svc.advance_from_sent(None, 1, at=TS, graph_message_id="g1") is None
    assert len(repo.events) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py -q -k advance_from_sent`
Expected: FAIL — `no attribute 'advance_from_sent'`.

- [ ] **Step 3: Implement `advance_from_sent`**

In `services/outreach.py`, after `nudge`:

```python
def advance_from_sent(
    conn: Any, person_id: int, *, at: datetime, graph_message_id: str | None
) -> tuple[str, int] | None:
    """Mail from an outreach address to a pipeline contact (working-queue spec §3.3): it
    briefs a `to_contact` row and nudges a `briefed` one. Any other stage, or no row, is
    ignored. Returns the kind and the new event id, if one was written."""
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        return None
    if row["stage"] == "to_contact":
        event_id = advance_from_event(
            conn, person_id, "briefed", at=at, graph_message_id=graph_message_id
        )
        return ("briefed", event_id) if event_id is not None else None
    if row["stage"] == "briefed":
        event_id = nudge(
            conn,
            person_id,
            at=at,
            channel="email",
            source="event",
            graph_message_id=graph_message_id,
        )
        return ("nudge", event_id) if event_id is not None else None
    return None
```

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py -q`
Expected: PASS.

- [ ] **Step 5: Update the handler tests**

In `tests/test_handlers.py`, in the `calls` fixture, after the `advance_from_event` patch, add:

```python
    monkeypatch.setattr(outreach, "advance_from_sent", lambda c, pid, **kw: None)
```

Replace `test_sent_advances_only_to_recipients` and `test_sent_from_another_identity_does_not_brief` with:

```python
def test_sent_records_outreach_only_for_to_recipients(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)
    seen = []

    def adv(c, pid, *, at, graph_message_id):
        seen.append((pid, at, graph_message_id))
        log.append("advance")
        return ("briefed", 11)

    monkeypatch.setattr(outreach, "advance_from_sent", adv)
    email_sent.handle(_sent())
    assert seen == [(1, datetime(2026, 10, 13, 15, 0, tzinfo=UTC), "g1")]  # carol is Cc
    # Durable first: the write is inside the first commit; the Asana sync comes after it.
    assert log.index("advance") < log.index("commit") < log.index(("sync", [11]))


def test_one_email_briefs_one_recipient_and_nudges_another(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    rows = [
        IngestResult(row={"id": 1, "email": "bob@x.com", "eligible": False}, newly_eligible=False),
        IngestResult(row={"id": 3, "email": "dan@x.com", "eligible": False}, newly_eligible=False),
    ]
    monkeypatch.setattr(ingest, "record_outbound", lambda conn, **kw: rows)
    outcome = {1: ("briefed", 11), 3: ("nudge", 12)}
    monkeypatch.setattr(outreach, "advance_from_sent", lambda c, pid, **kw: outcome[pid])
    email_sent.handle(_sent() | {"to": ["bob@x.com", "dan@x.com"]})
    # Only briefs produce Asana tasks; the nudge is recorded but never synced.
    assert [e for e in log if isinstance(e, tuple) and e[0] == "sync"] == [("sync", [11])]


def test_sent_from_another_identity_records_no_outreach(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)
    seen = []
    monkeypatch.setattr(outreach, "advance_from_sent", lambda c, pid, **kw: seen.append(pid))
    email_sent.handle(_sent(frm="ben@alias.example.com"))
    assert seen == [] and not any(isinstance(e, tuple) and e[0] == "sync" for e in log)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_handlers.py -q -k sent`
Expected: FAIL — the handler still calls `advance_from_event`, so `seen` is empty.

- [ ] **Step 7: Change the handler**

In `handlers/email_sent.py`, replace the module docstring's last sentence and `_advance_briefed`:

```python
"""email_sent → my_response_count / last_contacted for each To+Cc recipient;
writing to someone makes them eligible. Spec §6.2. A note from the consulting
identity also briefs each To recipient at `to_contact` and nudges each one at
`briefed` (outreach spec §6.2, working-queue spec §3.3), inside the same durable
transaction. Only briefs get an Asana task, after the commit."""
```

```python
def _record_outreach(conn, event: dict, results: list, sent_at) -> tuple[list[int], int]:
    """Brief or nudge each To recipient in the pipeline. Returns the brief event ids
    (they go on to the Asana sync) and the number of nudges."""
    if not outreach.is_outreach_sender(event.get("from") or ""):
        return [], 0
    to = {eligibility.normalize(a) for a in event.get("to") or []}
    briefed: list[int] = []
    nudged = 0
    for res in results:
        if res.row.get("id") is None or res.row.get("email") not in to:
            continue  # Cc recipients are copied, not briefed or nudged
        done = outreach.advance_from_sent(
            conn, res.row["id"], at=sent_at, graph_message_id=event.get("graph_message_id")
        )
        if done is None:
            continue
        kind, event_id = done
        if kind == "briefed":
            briefed.append(event_id)
        else:
            nudged += 1
    return briefed, nudged
```

In `handle`, replace `briefed = _advance_briefed(conn, event, results, sent_at)` with `briefed, nudged = _record_outreach(conn, event, results, sent_at)`, and the log line with:

```python
    logger.info(
        "email_sent %s → %d recipients, %d briefed, %d nudged",
        event.get("graph_message_id"),
        len(results),
        len(briefed),
        nudged,
    )
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_handlers.py tests/test_outreach.py -q`
Expected: PASS.

- [ ] **Step 9: Run the full checks and commit**

```bash
git add services/outreach.py handlers/email_sent.py tests/test_outreach.py tests/test_handlers.py
git commit -m "outreach: consulting mail to a briefed contact records a nudge"
```

---

### Task 4: Queue rank

**Files:**
- Modify: `repo/outreach.py`
- Modify: `services/outreach.py`
- Test: `tests/test_outreach.py`, `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: the `rank` column (Task 1), `FakeRepo.clear_ranks` (Task 2).
- Produces:
  - `repo/outreach.py`: `clear_ranks(conn) -> int`; `queue` rows carry `rank` and sort `rank NULLS LAST` first; the shared SELECT `_pipeline_rows(where: str) -> str`, reused in Task 5.
  - `services/outreach.py`: `class NotQueued(ValueError)`; `set_rank(conn, person_id: int, rank: int) -> dict`; `set_ranks(conn, person_ids: list[int]) -> None`; `clear_ranks(conn) -> int`; `queue_item(r)` gains `"rank"`; `advance` clears `rank` when a row leaves `to_contact`.

- [ ] **Step 1: Write the failing service tests**

In `tests/test_outreach.py`, `test_queue_item_matches_the_spec_shape`: add `"rank": 4,` to the input `row` and `"rank": 4,` to the expected dict (after `"stage": "to_contact",`). Then append:

```python
def test_leaving_to_contact_clears_the_rank(repo):
    _at(repo, 1, "to_contact")
    repo.contacts[1]["rank"] = 3
    svc.advance(None, 1, "briefed", at=TS, source="cli", channel="imessage")
    assert repo.contacts[1]["rank"] is None


def test_skipping_clears_the_rank(repo):
    _at(repo, 1, "to_contact")
    repo.contacts[1]["rank"] = 3
    svc.advance(None, 1, "skipped", at=TS, source="cli")
    assert repo.contacts[1]["rank"] is None


def test_set_rank_needs_a_to_contact_row(repo):
    _at(repo, 1, "briefed")
    with pytest.raises(svc.NotQueued):
        svc.set_rank(None, 1, 1)
    with pytest.raises(svc.NotInPipeline):
        svc.set_rank(None, 9, 1)


def test_set_rank_rejects_a_rank_below_one(repo):
    _at(repo, 1, "to_contact")
    with pytest.raises(ValueError):
        svc.set_rank(None, 1, 0)


def test_set_ranks_assigns_in_order_and_clears_the_rest(repo):
    for pid in (1, 2, 3):
        _at(repo, pid, "to_contact")
    repo.contacts[3]["rank"] = 1
    svc.set_ranks(None, [2, 1])
    assert [repo.contacts[p]["rank"] for p in (1, 2, 3)] == [2, 1, None]


def test_set_ranks_refuses_a_repeat_before_writing(repo):
    for pid in (1, 2):
        _at(repo, pid, "to_contact")
    repo.contacts[1]["rank"] = 5
    with pytest.raises(ValueError):
        svc.set_ranks(None, [1, 2, 1])
    assert repo.contacts[1]["rank"] == 5
```

- [ ] **Step 2: Write the failing DB test**

Append to `tests/test_outreach_db.py`:

```python
def test_queue_puts_ranked_rows_first_in_rank_order(conn):
    a = _person(conn)
    b = _person(conn, email="b@example.com")
    c = _person(conn, email="c@example.com")
    for pid in (a, b, c):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.update_contact(conn, pid, {"stage": "to_contact"})
    outreach_repo.update_contact(conn, c, {"rank": 1})
    outreach_repo.update_contact(conn, b, {"rank": 2})
    rows = outreach_repo.queue(conn, stage="to_contact", limit=10)
    assert [r["id"] for r in rows] == [c, b, a]
    assert [r["rank"] for r in rows] == [1, 2, None]
    assert outreach_repo.clear_ranks(conn) == 2
    assert [r["rank"] for r in outreach_repo.queue(conn, stage="to_contact", limit=10)] == [
        None,
        None,
        None,
    ]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_outreach_db.py -q -k "rank"`
Expected: FAIL — `KeyError: 'rank'` in `queue_item`, `no attribute 'NotQueued'`, `no attribute 'clear_ranks'`.

- [ ] **Step 4: Change the repo**

In `repo/outreach.py`, replace `queue` with a shared row SELECT and the rank-ordered query:

```python
def _pipeline_rows(where: str) -> str:
    """One row per pipeline contact with what Ben needs on screen before writing:
    identity, the pipeline columns, each channel's last message and the person's
    labels. `where` filters on `c` (outreach_contacts) and `p` (people)."""
    return f"""
        SELECT p.id, p.email, p.display_name, p.company, p.job_title, p.notes,
               c.stage, c.follow_up_on, c.has_lead, c.notes AS outreach_notes, c.rank,
               c.briefed_at, c.channel, c.nudge_count, c.last_nudged_at,
               NULLIF(GREATEST(COALESCE(p.last_seen, 'epoch'::timestamptz),
                               COALESCE(p.last_contacted, 'epoch'::timestamptz)),
                      'epoch'::timestamptz) AS email_last,
               im.last_at AS imessage_last, (im.n IS NOT NULL) AS has_imessage,
               wa.last_at AS whatsapp_last, (wa.n IS NOT NULL) AS has_whatsapp,
               li.profile_url AS linkedin_url, li.last_message_at AS linkedin_last,
               {_LABELS} AS labels
        FROM outreach_contacts c
        JOIN people p ON p.id = c.person_id
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS n, MAX(last_message_at) AS last_at
            FROM imessage_handles WHERE person_id = p.id HAVING COUNT(*) > 0
        ) im ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS n, MAX(last_message_at) AS last_at
            FROM whatsapp_handles WHERE person_id = p.id HAVING COUNT(*) > 0
        ) wa ON TRUE
        LEFT JOIN LATERAL (
            SELECT profile_url, last_message_at FROM linkedin_connections
            WHERE person_id = p.id ORDER BY last_message_at DESC NULLS LAST LIMIT 1
        ) li ON TRUE
        WHERE {where}
    """


def queue(conn: Any, *, stage: str, limit: int) -> list[dict]:
    """Contacts at `stage` (spec §7.2): Ben's saved order first (working-queue spec §6.1),
    then least recently in touch on any channel; never-contacted rows lead that part."""
    return conn.execute(
        f"""
        SELECT q.*,
               GREATEST(q.email_last, q.imessage_last, q.whatsapp_last, q.linkedin_last)
                   AS last_interaction
        FROM ({_pipeline_rows("c.stage = %s")}) q
        ORDER BY q.rank ASC NULLS LAST, last_interaction ASC NULLS FIRST, q.id
        LIMIT %s
        """,
        (stage, limit),
    ).fetchall()


def clear_ranks(conn: Any) -> int:
    return conn.execute(
        "UPDATE outreach_contacts SET rank = NULL, updated_at = now() WHERE rank IS NOT NULL"
    ).rowcount
```

- [ ] **Step 5: Change the service**

In `services/outreach.py`, after `class OpportunityNotFound`:

```python
class NotQueued(ValueError):
    """Only a `to_contact` referrer has a queue position (working-queue spec §6)."""
```

In `advance`, right after `changes: dict[str, Any] = {"stage": stage}`:

```python
    if row["stage"] == "to_contact":
        changes["rank"] = None  # a queue position means nothing once they leave the queue
```

In `queue_item`, add `"rank": r["rank"],` after `"stage": r["stage"],`. Then add after `queue`:

```python
def set_rank(conn: Any, person_id: int, rank: int) -> dict:
    if rank < 1:
        raise ValueError("rank must be 1 or more")
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        raise NotInPipeline(str(person_id))
    if row["stage"] != "to_contact":
        raise NotQueued(f"person {person_id} is at {row['stage']}, not to_contact")
    return outreach_repo.update_contact(conn, person_id, {"rank": rank})


def set_ranks(conn: Any, person_ids: list[int]) -> None:
    """Rank 1..N in the given order and clear every other rank (working-queue spec §6.2).
    The caller commits, so a failure part-way leaves nothing behind."""
    if len(set(person_ids)) != len(person_ids):
        raise ValueError("a person appears more than once")
    outreach_repo.clear_ranks(conn)
    for n, person_id in enumerate(person_ids, start=1):
        set_rank(conn, person_id, n)


def clear_ranks(conn: Any) -> int:
    return outreach_repo.clear_ranks(conn)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_outreach_db.py -q`
Expected: PASS, including the existing `test_queue_orders_by_latest_contact_on_any_channel_and_fills_channels`.

- [ ] **Step 7: Run the full checks and commit**

```bash
git add repo/outreach.py services/outreach.py tests/test_outreach.py tests/test_outreach_db.py
git commit -m "outreach: saved queue order (rank), cleared when a contact leaves the queue"
```

---

### Task 5: Follow-ups list with nudge classification

**Files:**
- Modify: `repo/outreach.py`
- Modify: `services/outreach.py`
- Test: `tests/test_outreach.py`, `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: `_pipeline_rows` (Task 4).
- Produces:
  - `repo/outreach.py::follow_ups(conn, *, today: date) -> list[dict]` — every `_pipeline_rows` column plus `brief_at`, `brief_channel`, `brief_message_id` (the first `briefed` event) and `last_nudge_channel`.
  - `services/outreach.py::follow_up_item(r: dict, *, today: date) -> dict` with keys `person` (`id`, `email`, `display_name`), `kind` (`"nudge 1"`, `"nudge 2"`, `"close"`), `nudge_count`, `follow_up_on` (ISO date), `overdue` (bool), `brief` (`channel`, `at` ISO date, `graph_message_id`), `last_nudge_channel`, `last_nudged_at` (ISO date or None), `channels` (as `queue_item`), `suggested_channel` (str or None).
  - `services/outreach.py::follow_ups(conn, *, today: date) -> list[dict]` now returns `follow_up_item` dicts.
  - `services/outreach.py::_channels(r) -> dict`, shared with `queue_item`.

- [ ] **Step 1: Write the failing service tests**

Append to `tests/test_outreach.py`:

```python
def _fu(**over):
    row = {
        "id": 5,
        "email": "bob@example.com",
        "display_name": "Bob Example",
        "nudge_count": 0,
        "follow_up_on": date(2026, 10, 13),
        "last_nudged_at": None,
        "briefed_at": datetime(2026, 10, 6, 15, tzinfo=UTC),
        "brief_at": datetime(2026, 10, 6, 15, tzinfo=UTC),
        "brief_channel": "email",
        "brief_message_id": "g1",
        "last_nudge_channel": None,
        "has_imessage": True,
        "has_whatsapp": False,
        "linkedin_url": None,
    }
    return row | over


def test_first_nudge_stays_on_the_brief_channel_and_thread():
    item = svc.follow_up_item(_fu(), today=date(2026, 10, 14))
    assert item["kind"] == "nudge 1" and item["suggested_channel"] == "email"
    assert item["overdue"] is True and item["follow_up_on"] == "2026-10-13"
    assert item["brief"] == {"channel": "email", "at": "2026-10-06", "graph_message_id": "g1"}
    assert item["person"] == {"id": 5, "email": "bob@example.com", "display_name": "Bob Example"}


def test_second_nudge_switches_to_a_channel_not_used_yet():
    item = svc.follow_up_item(
        _fu(nudge_count=1, last_nudge_channel="email", follow_up_on=date(2026, 10, 20)),
        today=date(2026, 10, 20),
    )
    assert item["kind"] == "nudge 2" and item["suggested_channel"] == "imessage"
    assert item["overdue"] is False


def test_second_nudge_with_no_other_channel_suggests_none():
    item = svc.follow_up_item(_fu(nudge_count=1, has_imessage=False), today=date(2026, 10, 20))
    assert item["suggested_channel"] is None


def test_two_nudges_proposes_a_close():
    item = svc.follow_up_item(_fu(nudge_count=2), today=date(2026, 10, 27))
    assert item["kind"] == "close" and item["suggested_channel"] is None


def test_a_missing_brief_event_falls_back_to_briefed_at():
    item = svc.follow_up_item(
        _fu(brief_at=None, brief_channel=None, brief_message_id=None), today=date(2026, 10, 14)
    )
    assert item["brief"] == {"channel": None, "at": "2026-10-06", "graph_message_id": None}
```

- [ ] **Step 2: Write the failing DB test**

In `tests/test_outreach_db.py`, change the import to `from datetime import UTC, date, datetime, timedelta`, then append:

```python
def test_follow_ups_carry_the_brief_thread_and_the_last_nudge(conn):
    pid = _person(conn)
    outreach_repo.insert_contact_if_missing(conn, pid)
    outreach_repo.update_contact(
        conn,
        pid,
        {
            "stage": "briefed",
            "briefed_at": TS,
            "channel": "email",
            "nudge_count": 1,
            "follow_up_on": date(2026, 10, 27),
        },
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="briefed",
        at=TS,
        source="event",
        channel="email",
        graph_message_id="g-brief",
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=pid,
        stage="nudge",
        at=TS + timedelta(days=7),
        source="cli",
        channel="imessage",
    )
    [row] = outreach_repo.follow_ups(conn, today=date(2026, 10, 27))
    assert row["brief_message_id"] == "g-brief" and row["brief_channel"] == "email"
    assert row["brief_at"] == TS and row["last_nudge_channel"] == "imessage"
    assert row["nudge_count"] == 1 and row["email"] == "alice@example.com"
    assert row["has_imessage"] is False
    assert outreach_repo.follow_ups(conn, today=date(2026, 10, 26)) == []
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_outreach_db.py -q -k "nudge or close or brief_event or follow_ups"`
Expected: FAIL — `no attribute 'follow_up_item'`, `KeyError: 'brief_message_id'`.

- [ ] **Step 4: Change the repo**

Replace `follow_ups` in `repo/outreach.py`:

```python
def follow_ups(conn: Any, *, today: date) -> list[dict]:
    """Briefed contacts whose follow-up date has arrived, with the brief itself (its
    channel and email thread, so nudge 1 can reply in it) and the last nudge's channel
    (so nudge 2 can switch). Working-queue spec §3.4."""
    return conn.execute(
        f"""
        SELECT q.*, b.at AS brief_at, b.channel AS brief_channel,
               b.graph_message_id AS brief_message_id, n.channel AS last_nudge_channel
        FROM ({_pipeline_rows("c.stage = 'briefed' AND c.follow_up_on <= %s")}) q
        LEFT JOIN LATERAL (
            SELECT at, channel, graph_message_id FROM outreach_events
            WHERE pipeline = 'referrer' AND person_id = q.id AND stage = 'briefed'
            ORDER BY at, id LIMIT 1
        ) b ON TRUE
        LEFT JOIN LATERAL (
            SELECT channel FROM outreach_events
            WHERE pipeline = 'referrer' AND person_id = q.id AND stage = 'nudge'
            ORDER BY at DESC, id DESC LIMIT 1
        ) n ON TRUE
        ORDER BY q.follow_up_on, q.id
        """,
        (today,),
    ).fetchall()
```

- [ ] **Step 5: Change the service**

In `services/outreach.py`, factor the channel map out of `queue_item` and add the classifier:

```python
# The order nudge 2 tries channels in when switching away from the brief's.
SWITCH_ORDER = ("email", "imessage", "whatsapp", "linkedin")


def _channels(r: dict) -> dict:
    return {
        "email": r["email"] is not None,
        "imessage": bool(r["has_imessage"]),
        "linkedin": r["linkedin_url"] or False,
        "whatsapp": bool(r["has_whatsapp"]),
    }
```

In `queue_item`, replace the inline `"channels": {...}` dict with `"channels": _channels(r),`. Then replace `follow_ups` with:

```python
def follow_up_item(r: dict, *, today: date) -> dict:
    """Working-queue spec §3.4: nudge 1 stays on the brief's channel (and email thread);
    nudge 2 switches to a channel not used yet when one exists; after two, propose a close."""
    count = int(r["nudge_count"] or 0)
    channels = _channels(r)
    brief_channel = r["brief_channel"]
    if count == 0:
        kind, suggested = "nudge 1", brief_channel
    elif count == 1:
        used = {brief_channel, r["last_nudge_channel"]}
        kind = "nudge 2"
        suggested = next((c for c in SWITCH_ORDER if channels[c] and c not in used), None)
    else:
        kind, suggested = "close", None
    return {
        "person": {"id": r["id"], "email": r["email"], "display_name": r["display_name"]},
        "kind": kind,
        "nudge_count": count,
        "follow_up_on": r["follow_up_on"].isoformat(),
        "overdue": r["follow_up_on"] < today,
        "brief": {
            "channel": brief_channel,
            "at": _day(r["brief_at"] or r["briefed_at"]),
            "graph_message_id": r["brief_message_id"],
        },
        "last_nudge_channel": r["last_nudge_channel"],
        "last_nudged_at": _day(r["last_nudged_at"]),
        "channels": channels,
        "suggested_channel": suggested,
    }


def follow_ups(conn: Any, *, today: date) -> list[dict]:
    return [follow_up_item(r, today=today) for r in outreach_repo.follow_ups(conn, today=today)]
```

- [ ] **Step 6: Keep `outreach followups` working until Task 8 rewrites it**

`scripts/outreach.py::cmd_followups` reads the old row keys. Replace its table body so it prints the new items (Task 8 adds the test):

```python
def cmd_followups(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    items = outreach.follow_ups(conn, today=now.astimezone(outreach.TZ).date())
    if not items:
        print("no follow-ups due", file=out)
        return
    print(
        outreach_report.table(
            ["id", "name", "action", "briefed", "due", "channel"],
            [
                [
                    i["person"]["id"],
                    _label(i["person"]),
                    i["kind"],
                    i["brief"]["at"] or "",
                    i["follow_up_on"] + (" (overdue)" if i["overdue"] else ""),
                    _how(i),
                ]
                for i in items
            ],
        ),
        file=out,
    )


def _how(item: dict) -> str:
    brief = item["brief"]["channel"]
    if item["kind"] == "close":
        return f"outreach mark {item['person']['id']} declined"
    via = item["suggested_channel"] or brief or "any channel"
    return f"switch to {via}" if item["kind"] == "nudge 2" and via != brief else via
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS.

- [ ] **Step 8: Run the full checks and commit**

```bash
git add repo/outreach.py services/outreach.py scripts/outreach.py tests/test_outreach.py tests/test_outreach_db.py
git commit -m "outreach: follow-ups classified as nudge 1, nudge 2 or close, with the brief's thread"
```

---

### Task 6: Targets file loader

**Files:**
- Create: `services/outreach_targets.py`
- Test: `tests/test_outreach_targets.py`

**Interfaces:**
- Produces: `WEEKDAYS: tuple[str, ...]`; `class TargetsError(ValueError)`; `@dataclass(frozen=True) class Targets(per_day: int, per_week: int, start: date, end: date, total: int, low_water_days: int, workdays: frozenset[int])` — `workdays` holds `date.weekday()` numbers, Monday = 0; `load_targets(path: Path) -> Targets`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outreach_targets.py`:

```python
from datetime import date

import pytest

from services.outreach_targets import Targets, TargetsError, load_targets

GOOD = """
[briefs]
per_day  = 5
per_week = 25
start    = 2026-10-13
end      = 2026-11-21
total    = 150

[queue]
low_water_days = 5

[workdays]
days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
"""


def _write(tmp_path, text):
    path = tmp_path / "outreach_targets.toml"
    path.write_text(text)
    return path


def test_loads_the_spec_example(tmp_path):
    assert load_targets(_write(tmp_path, GOOD)) == Targets(
        per_day=5,
        per_week=25,
        start=date(2026, 10, 13),
        end=date(2026, 11, 21),
        total=150,
        low_water_days=5,
        workdays=frozenset({0, 1, 2, 3, 4}),
    )


def test_missing_file(tmp_path):
    with pytest.raises(TargetsError, match="not found"):
        load_targets(tmp_path / "nope.toml")


def test_bad_toml(tmp_path):
    with pytest.raises(TargetsError):
        load_targets(_write(tmp_path, "[briefs\nper_day = 5"))


@pytest.mark.parametrize("table", ["briefs", "queue", "workdays"])
def test_missing_table(tmp_path, table):
    text = GOOD.replace(f"[{table}]", "[other]")
    with pytest.raises(TargetsError, match=rf"\[{table}\]"):
        load_targets(_write(tmp_path, text))


@pytest.mark.parametrize("key", ["per_day", "per_week", "start", "end", "total"])
def test_missing_briefs_key(tmp_path, key):
    text = "\n".join(line for line in GOOD.splitlines() if not line.startswith(key))
    with pytest.raises(TargetsError, match=key):
        load_targets(_write(tmp_path, text))


@pytest.mark.parametrize(
    "line,replacement",
    [
        ("per_day  = 5", "per_day  = 0"),
        ("per_week = 25", "per_week = -1"),
        ("total    = 150", "total    = 1.5"),
        ("low_water_days = 5", "low_water_days = true"),
    ],
)
def test_non_positive_or_non_integer_numbers(tmp_path, line, replacement):
    with pytest.raises(TargetsError, match="positive whole number"):
        load_targets(_write(tmp_path, GOOD.replace(line, replacement)))


def test_start_after_end(tmp_path):
    text = GOOD.replace("start    = 2026-10-13", "start    = 2026-12-01")
    with pytest.raises(TargetsError, match="after"):
        load_targets(_write(tmp_path, text))


def test_a_date_must_be_a_bare_date(tmp_path):
    text = GOOD.replace("start    = 2026-10-13", 'start    = "2026-10-13"')
    with pytest.raises(TargetsError, match="date"):
        load_targets(_write(tmp_path, text))


def test_unknown_weekday(tmp_path):
    text = GOOD.replace('"Fri"]', '"Friday"]')
    with pytest.raises(TargetsError, match="Friday"):
        load_targets(_write(tmp_path, text))


def test_empty_weekday_list(tmp_path):
    text = GOOD.replace('days = ["Mon", "Tue", "Wed", "Thu", "Fri"]', "days = []")
    with pytest.raises(TargetsError, match="days"):
        load_targets(_write(tmp_path, text))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_targets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.outreach_targets'`.

- [ ] **Step 3: Implement the loader**

Create `services/outreach_targets.py`:

```python
"""`outreach today`'s brief targets (working-queue spec §4). Pure. The numbers are not
in this public repo: they come from a TOML file in the private consulting repo, passed
with --targets or OUTREACH_TARGETS."""

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class TargetsError(ValueError):
    pass


@dataclass(frozen=True)
class Targets:
    per_day: int
    per_week: int
    start: date
    end: date
    total: int
    low_water_days: int
    workdays: frozenset[int]  # date.weekday() numbers, Monday = 0


def _table(data: dict, name: str, path: Path) -> dict:
    t = data.get(name)
    if not isinstance(t, dict):
        raise TargetsError(f"{path}: missing [{name}]")
    return t


def _value(t: dict, table: str, key: str, path: Path) -> object:
    if key not in t:
        raise TargetsError(f"{path}: [{table}] lacks {key}")
    return t[key]


def _positive(t: dict, table: str, key: str, path: Path) -> int:
    v = _value(t, table, key, path)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise TargetsError(f"{path}: [{table}] {key} must be a positive whole number, got {v!r}")
    return v


def _date(t: dict, table: str, key: str, path: Path) -> date:
    v = _value(t, table, key, path)
    if isinstance(v, datetime) or not isinstance(v, date):
        raise TargetsError(f"{path}: [{table}] {key} must be a date like 2026-10-13, got {v!r}")
    return v


def load_targets(path: Path) -> Targets:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise TargetsError(f"targets file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise TargetsError(f"{path}: {e}") from e
    briefs = _table(data, "briefs", path)
    queue = _table(data, "queue", path)
    work = _table(data, "workdays", path)
    start = _date(briefs, "briefs", "start", path)
    end = _date(briefs, "briefs", "end", path)
    if start > end:
        raise TargetsError(f"{path}: [briefs] start {start} is after end {end}")
    days = work.get("days")
    if not isinstance(days, list) or not days:
        raise TargetsError(f"{path}: [workdays] days must be a non-empty list like [\"Mon\"]")
    unknown = [d for d in days if d not in WEEKDAYS]
    if unknown:
        raise TargetsError(
            f"{path}: [workdays] unknown day(s) {unknown}; use {', '.join(WEEKDAYS)}"
        )
    return Targets(
        per_day=_positive(briefs, "briefs", "per_day", path),
        per_week=_positive(briefs, "briefs", "per_week", path),
        start=start,
        end=end,
        total=_positive(briefs, "briefs", "total", path),
        low_water_days=_positive(queue, "queue", "low_water_days", path),
        workdays=frozenset(WEEKDAYS.index(d) for d in days),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_targets.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full checks and commit**

```bash
git add services/outreach_targets.py tests/test_outreach_targets.py
git commit -m "outreach: load brief targets from a private TOML file"
```

---

### Task 7: `today` counts, verdicts and rendering

**Files:**
- Modify: `models/outreach.py`
- Modify: `repo/outreach.py`
- Modify: `services/outreach.py`
- Create: `services/outreach_today.py`
- Test: `tests/test_outreach_today.py`, `tests/test_outreach.py`, `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: `Targets` (Task 6); `follow_ups` and `follow_up_item` dicts (Task 5); `queue`/`queue_item` with `rank` (Task 4).
- Produces:
  - `models/outreach.py`: `@dataclass class TodayCounts(briefs_today: int, briefs_week: int, briefs_plan: int, nudges_week: int, queue_depth: int, follow_ups: list[dict], next_up: list[dict])`.
  - `repo/outreach.py`: `first_briefs_between(conn, *, since: date, until: date) -> int`; `nudges_between(conn, *, since: date, until: date) -> int`; `stage_count(conn, stage: str) -> int`. All bounds are inclusive New York dates.
  - `services/outreach.py::today_counts(conn, *, today: date, per_day: int, plan_start: date, plan_end: date) -> TodayCounts`.
  - `services/outreach_today.py`: `Today` dataclass (fields below), `workdays(start: date, end: date, days: frozenset[int]) -> int`, `build(counts: TodayCounts, targets: Targets, *, now: datetime) -> Today`, `render(t: Today) -> str`, `to_json(t: Today) -> str`.

- [ ] **Step 1: Write the failing DB test**

Append to `tests/test_outreach_db.py`:

```python
def test_today_counts_use_new_york_dates(conn):
    early = _person(conn)
    late = _person(conn, email="b@example.com")
    # October is EDT (UTC-4): 03:30 UTC on 10-14 is 23:30 on 10-13 in New York;
    # 04:30 UTC is 00:30 on 10-14.
    for pid, at in (
        (early, datetime(2026, 10, 14, 3, 30, tzinfo=UTC)),
        (late, datetime(2026, 10, 14, 4, 30, tzinfo=UTC)),
    ):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.insert_event(
            conn, pipeline="referrer", person_id=pid, stage="briefed", at=at, source="event"
        )
    # A re-brief after a reopen is not a first brief.
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=early,
        stage="briefed",
        at=datetime(2026, 10, 15, 15, tzinfo=UTC),
        source="cli",
    )
    outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=late,
        stage="nudge",
        at=datetime(2026, 10, 14, 3, 59, tzinfo=UTC),
        source="cli",
    )
    d13, d14, d15 = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
    assert outreach_repo.first_briefs_between(conn, since=d13, until=d13) == 1
    assert outreach_repo.first_briefs_between(conn, since=d14, until=d15) == 1
    assert outreach_repo.first_briefs_between(conn, since=d13, until=d15) == 2
    assert outreach_repo.nudges_between(conn, since=d13, until=d13) == 1
    assert outreach_repo.nudges_between(conn, since=d14, until=d15) == 0
    assert outreach_repo.stage_count(conn, "candidate") == 2
    assert outreach_repo.stage_count(conn, "to_contact") == 0
```

- [ ] **Step 2: Write the failing `today_counts` test**

Append to `tests/test_outreach.py`:

```python
def test_today_counts_windows(monkeypatch):
    calls = []

    class Repo:
        def first_briefs_between(self, conn, *, since, until):
            calls.append(("briefs", since, until))
            return len(calls)

        def nudges_between(self, conn, *, since, until):
            calls.append(("nudges", since, until))
            return 4

        def stage_count(self, conn, stage):
            return 38 if stage == "to_contact" else 0

        def follow_ups(self, conn, *, today):
            return []

        def queue(self, conn, *, stage, limit):
            calls.append(("queue", stage, limit))
            return []

    monkeypatch.setattr(svc, "outreach_repo", Repo())
    wed = date(2026, 10, 14)
    counts = svc.today_counts(
        None, today=wed, per_day=5, plan_start=date(2026, 10, 13), plan_end=date(2026, 11, 21)
    )
    assert calls == [
        ("briefs", wed, wed),
        ("briefs", date(2026, 10, 12), wed),
        ("briefs", date(2026, 10, 13), wed),
        ("nudges", date(2026, 10, 12), wed),
        ("queue", "to_contact", 5),
    ]
    assert (counts.briefs_today, counts.briefs_week, counts.briefs_plan) == (1, 2, 3)
    assert counts.nudges_week == 4 and counts.queue_depth == 38


def test_today_counts_before_the_plan_counts_no_plan_briefs(monkeypatch):
    class Repo:
        def first_briefs_between(self, conn, *, since, until):
            assert since <= until
            return 1

        nudges_between = first_briefs_between

        def stage_count(self, conn, stage):
            return 0

        def follow_ups(self, conn, *, today):
            return []

        def queue(self, conn, *, stage, limit):
            return []

    monkeypatch.setattr(svc, "outreach_repo", Repo())
    counts = svc.today_counts(
        None,
        today=date(2026, 10, 12),
        per_day=5,
        plan_start=date(2026, 10, 13),
        plan_end=date(2026, 11, 21),
    )
    assert counts.briefs_plan == 0
```

- [ ] **Step 3: Write the failing pure tests**

Create `tests/test_outreach_today.py`:

```python
import json
from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from models.outreach import TodayCounts
from services.outreach_targets import Targets
from services.outreach_today import build, render, to_json, workdays

TZ = ZoneInfo("America/New_York")
WEEKDAYS = frozenset(range(5))
TARGETS = Targets(
    per_day=5,
    per_week=25,
    start=date(2026, 10, 13),  # a Tuesday
    end=date(2026, 11, 21),
    total=150,
    low_water_days=5,
    workdays=WEEKDAYS,
)


def _counts(**kw) -> TodayCounts:
    base = dict(
        briefs_today=0,
        briefs_week=0,
        briefs_plan=0,
        nudges_week=0,
        queue_depth=100,
        follow_ups=[],
        next_up=[],
    )
    return TodayCounts(**(base | kw))


def _at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=TZ)


def _item(kind, overdue, *, pid=41, suggested=None, n=0):
    return {
        "person": {"id": pid, "email": f"p{pid}@example.com", "display_name": f"Person {pid}"},
        "kind": kind,
        "nudge_count": n,
        "follow_up_on": "2026-10-13",
        "overdue": overdue,
        "brief": {"channel": "email", "at": "2026-10-06", "graph_message_id": "g1"},
        "last_nudge_channel": None,
        "last_nudged_at": None,
        "channels": {"email": True, "imessage": True, "linkedin": False, "whatsapp": False},
        "suggested_channel": suggested,
    }


def _queued(pid=7, rank=1):
    return {
        "person": {"id": pid, "email": "ana@example.com", "display_name": "Ana Example"},
        "rank": rank,
        "last_interaction": "2026-09-30",
        "channels": {"email": True, "imessage": True, "linkedin": False, "whatsapp": False},
    }


def test_workdays_counts_inclusive_and_skips_weekends():
    assert workdays(date(2026, 10, 12), date(2026, 10, 18), WEEKDAYS) == 5
    assert workdays(date(2026, 10, 13), date(2026, 10, 13), WEEKDAYS) == 1
    assert workdays(date(2026, 10, 14), date(2026, 10, 13), WEEKDAYS) == 0


def test_first_morning_of_a_plan_that_starts_mid_week_is_on_pace():
    # Tue 2026-10-13: Monday is before the plan, so nothing is owed by yesterday.
    t = build(_counts(), TARGETS, now=_at(date(2026, 10, 13)))
    assert (t.week_by_yesterday, t.week_by_tonight) == (0, 5)
    assert (t.plan_by_yesterday, t.plan_by_tonight) == (0, 5)
    assert t.statuses == {"today": "on pace", "week": "on pace", "plan": "on pace", "nudges": "met"}
    assert t.verdict == "ON PACE" and t.need_today == 5


def test_monday_morning_after_a_full_first_week():
    t = build(_counts(briefs_plan=20), TARGETS, now=_at(date(2026, 10, 19)))
    assert (t.plan_by_yesterday, t.plan_by_tonight) == (20, 25)
    assert (t.week_by_yesterday, t.week_by_tonight) == (0, 5)
    assert t.verdict == "ON PACE"


def test_mid_week_behind():
    # Thu 10-22: Mon–Wed owed 15; 9 done.
    t = build(
        _counts(briefs_week=9, briefs_plan=29), TARGETS, now=_at(date(2026, 10, 22), 15)
    )
    assert (t.week_by_yesterday, t.week_by_tonight) == (15, 20)
    assert t.statuses["week"] == "behind" and t.statuses["plan"] == "behind"
    assert t.verdict == "BEHIND" and t.need_today == 11


def test_every_line_met_is_on_track():
    t = build(
        _counts(briefs_today=5, briefs_week=10, briefs_plan=10),
        TARGETS,
        now=_at(date(2026, 10, 14), 18),
    )
    assert t.verdict == "ON TRACK" and t.need_today is None


def test_weekend_sets_no_target_and_keeps_fridays_marks():
    t = build(_counts(briefs_week=20, briefs_plan=20), TARGETS, now=_at(date(2026, 10, 17)))
    assert t.today_target is None and t.no_target_reason == "weekend"
    assert (t.week_by_yesterday, t.week_by_tonight) == (20, 20)
    assert t.statuses["today"] == "n/a" and t.verdict == "ON TRACK"
    assert "no target (weekend)" in render(t)


def test_before_start_the_plan_takes_no_part():
    t = build(_counts(), TARGETS, now=_at(date(2026, 10, 12)))
    assert t.plan_phase == "before start" and t.statuses["plan"] == "n/a"
    assert t.today_target is None and t.no_target_reason == "outside plan"
    assert t.verdict == "ON TRACK"
    assert "plan starts 2026-10-13" in render(t)


def test_after_end_the_plan_takes_no_part():
    t = build(_counts(briefs_plan=150), TARGETS, now=_at(date(2026, 11, 23)))
    assert t.plan_phase == "ended" and t.statuses["plan"] == "n/a"
    assert "plan ended 2026-11-21" in render(t)


def test_marks_are_capped_at_per_week_and_total():
    t = build(
        _counts(briefs_today=6, briefs_week=25, briefs_plan=54),
        replace(TARGETS, per_day=6),
        now=_at(date(2026, 10, 23), 18),
    )
    assert t.week_by_tonight == 25
    t = build(_counts(), replace(TARGETS, total=12), now=_at(date(2026, 10, 16)))
    assert t.plan_by_tonight == 12


def test_nudge_statuses():
    wed = _at(date(2026, 10, 14))
    assert build(_counts(follow_ups=[_item("nudge 1", False)]), TARGETS, now=wed).statuses[
        "nudges"
    ] == "on pace"
    behind = build(
        _counts(follow_ups=[_item("nudge 1", False), _item("nudge 2", True)]), TARGETS, now=wed
    )
    assert behind.statuses["nudges"] == "behind" and behind.verdict == "BEHIND"
    assert (behind.nudges_due, behind.nudges_overdue) == (2, 1)
    closes = build(_counts(follow_ups=[_item("close", True, n=2)]), TARGETS, now=wed)
    assert closes.statuses["nudges"] == "met" and closes.nudges_due == 0
    assert len(closes.closes) == 1


def test_low_queue_warning():
    t = build(_counts(queue_depth=24), TARGETS, now=_at(date(2026, 10, 14)))
    assert t.low_queue
    assert "Queue low: 24 at to_contact (4.8 workdays). Reassess or refill." in render(t)
    assert not build(_counts(queue_depth=25), TARGETS, now=_at(date(2026, 10, 14))).low_queue


def test_render_layout():
    t = build(
        _counts(
            briefs_today=2,
            briefs_week=7,
            briefs_plan=7,
            nudges_week=4,
            queue_depth=38,
            next_up=[_queued()],
            follow_ups=[
                _item("nudge 1", True, pid=11),
                _item("nudge 2", False, pid=12, suggested="imessage", n=1),
                _item("close", False, pid=41, n=2),
            ],
        ),
        TARGETS,
        now=_at(date(2026, 10, 14), 10),
    )
    lines = render(t).splitlines()
    assert lines[0].startswith("Outreach: Wed 2026-10-14") and lines[0].endswith("BEHIND")
    assert lines[2] == (
        "Briefs   today 2/5 · week 7/10 expected (25) · need 3 today · "
        "plan 7/10 by today (150 by Nov 21)"
    )
    assert lines[3] == "Nudges   due 2 (1 overdue) · sent this week 4"
    assert lines[4] == "Queue    38 at to_contact (7.6 workdays)"
    text = "\n".join(lines)
    assert "Brief next" in text and "Ana Example" in text and "#rank 1" in text
    assert "nudge 1 · briefed 10-06 · due 10-13 (overdue) · email" in text
    assert "nudge 2 · briefed 10-06 · due 10-13 · switch to imessage" in text
    assert "2 nudges, no reply → outreach mark 41 declined" in text


def test_to_json_is_plain_json():
    t = build(_counts(next_up=[_queued()]), TARGETS, now=_at(date(2026, 10, 13)))
    data = json.loads(to_json(t))
    assert data["day"] == "2026-10-13" and data["verdict"] == "ON PACE"
    assert data["next_up"][0]["person"]["id"] == 7
```

- [ ] **Step 4: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_today.py tests/test_outreach.py -q`
Expected: FAIL — `ImportError: cannot import name 'TodayCounts'`, `No module named 'services.outreach_today'`.

- [ ] **Step 5: Add `TodayCounts`**

Append to `models/outreach.py`:

```python
@dataclass
class TodayCounts:
    """What `outreach today` reads from the DB (working-queue spec §5). The verdicts are
    computed from these by services/outreach_today.py, which does no I/O."""

    briefs_today: int
    briefs_week: int
    briefs_plan: int
    nudges_week: int
    queue_depth: int
    follow_ups: list[dict]  # services.outreach.follow_up_item dicts
    next_up: list[dict]  # services.outreach.queue_item dicts, rank order
```

- [ ] **Step 6: Add the repo counts**

Append to `repo/outreach.py`:

```python
def first_briefs_between(conn: Any, *, since: date, until: date) -> int:
    """People whose first brief fell between two New York dates, inclusive. A re-brief
    after a reopen does not count again (working-queue spec §5)."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT MIN(at) AS first_at FROM outreach_events
            WHERE pipeline = 'referrer' AND stage = 'briefed'
            GROUP BY person_id
        ) f
        WHERE (f.first_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
        """,
        (since, until),
    ).fetchone()
    return int(row["n"])


def nudges_between(conn: Any, *, since: date, until: date) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM outreach_events
        WHERE pipeline = 'referrer' AND stage = 'nudge'
          AND (at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
        """,
        (since, until),
    ).fetchone()
    return int(row["n"])


def stage_count(conn: Any, stage: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outreach_contacts WHERE stage = %s", (stage,)
    ).fetchone()
    return int(row["n"])
```

- [ ] **Step 7: Add `today_counts`**

In `services/outreach.py`, import `TodayCounts` from `models.outreach`, then add after `follow_ups`:

```python
def today_counts(
    conn: Any, *, today: date, per_day: int, plan_start: date, plan_end: date
) -> TodayCounts:
    """The numbers `outreach today` needs (working-queue spec §5), in New York dates. The
    week runs Monday to today; the plan from its start to today, capped at its end."""
    week_start = today - timedelta(days=today.weekday())
    return TodayCounts(
        briefs_today=outreach_repo.first_briefs_between(conn, since=today, until=today),
        briefs_week=outreach_repo.first_briefs_between(conn, since=week_start, until=today),
        briefs_plan=(
            outreach_repo.first_briefs_between(
                conn, since=plan_start, until=min(today, plan_end)
            )
            if today >= plan_start
            else 0
        ),
        nudges_week=outreach_repo.nudges_between(conn, since=week_start, until=today),
        queue_depth=outreach_repo.stage_count(conn, "to_contact"),
        follow_ups=follow_ups(conn, today=today),
        next_up=queue(conn, limit=per_day),
    )
```

- [ ] **Step 8: Implement `services/outreach_today.py`**

```python
"""`outreach today` (working-queue spec §5): what to do today and whether Ben is on pace.
Pure: the counts come from services/outreach.py::today_counts and the targets from
services/outreach_targets.py.

Each brief line has two marks: what was owed by the end of yesterday and what is owed
by tonight. Below the first is behind; between them is on pace (the day still has
time); at or above the second is met. Marks count only workdays inside the plan window,
so the days before a mid-week start are never owed."""

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models.outreach import TodayCounts
from services.outreach_targets import Targets

TZ = ZoneInfo("America/New_York")
MET, ON_PACE, BEHIND, NA = "met", "on pace", "behind", "n/a"


@dataclass
class Today:
    day: date
    verdict: str  # ON TRACK | ON PACE | BEHIND
    statuses: dict[str, str]  # today, week, plan, nudges → met | on pace | behind | n/a
    briefs_today: int
    today_target: int | None  # None: no target today
    no_target_reason: str | None  # "weekend" | "outside plan"
    briefs_week: int
    week_by_yesterday: int
    week_by_tonight: int
    per_week: int
    need_today: int | None  # set when the week line is not met
    plan_phase: str  # "before start" | "active" | "ended"
    briefs_plan: int
    plan_by_yesterday: int
    plan_by_tonight: int
    total: int
    start: date
    end: date
    nudges_due: int
    nudges_overdue: int
    nudges_week: int
    queue_depth: int
    queue_workdays: float
    low_queue: bool
    next_up: list[dict]
    nudges: list[dict]
    closes: list[dict]


def workdays(start: date, end: date, days: frozenset[int]) -> int:
    """Workdays from start to end inclusive; 0 when end is before start."""
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() in days)


def _status(actual: int, by_yesterday: int, by_tonight: int) -> str:
    if actual >= by_tonight:
        return MET
    if actual < by_yesterday:
        return BEHIND
    return ON_PACE


def build(counts: TodayCounts, targets: Targets, *, now: datetime) -> Today:
    day = now.astimezone(TZ).date()
    yesterday = day - timedelta(days=1)
    in_plan = targets.start <= day <= targets.end
    is_workday = day.weekday() in targets.workdays

    # Week marks: workdays this week inside the plan window.
    lo = max(day - timedelta(days=day.weekday()), targets.start)
    week_by_yesterday = min(
        targets.per_day * workdays(lo, min(yesterday, targets.end), targets.workdays),
        targets.per_week,
    )
    week_by_tonight = min(
        targets.per_day * workdays(lo, min(day, targets.end), targets.workdays),
        targets.per_week,
    )
    plan_by_yesterday = min(
        targets.per_day * workdays(targets.start, min(yesterday, targets.end), targets.workdays),
        targets.total,
    )
    plan_by_tonight = min(
        targets.per_day * workdays(targets.start, min(day, targets.end), targets.workdays),
        targets.total,
    )
    plan_phase = "before start" if day < targets.start else "ended" if day > targets.end else "active"

    today_target = targets.per_day if in_plan and is_workday else None
    no_target_reason = None if today_target else ("weekend" if not is_workday else "outside plan")

    nudges = [f for f in counts.follow_ups if f["kind"] != "close"]
    closes = [f for f in counts.follow_ups if f["kind"] == "close"]
    overdue = sum(1 for f in nudges if f["overdue"])

    week_status = _status(counts.briefs_week, week_by_yesterday, week_by_tonight)
    statuses = {
        "today": NA if today_target is None else MET if counts.briefs_today >= today_target else ON_PACE,
        "week": week_status,
        "plan": _status(counts.briefs_plan, plan_by_yesterday, plan_by_tonight)
        if plan_phase == "active"
        else NA,
        "nudges": MET if not nudges else BEHIND if overdue else ON_PACE,
    }
    live = [s for s in statuses.values() if s != NA]
    verdict = "BEHIND" if BEHIND in live else "ON TRACK" if all(s == MET for s in live) else "ON PACE"

    return Today(
        day=day,
        verdict=verdict,
        statuses=statuses,
        briefs_today=counts.briefs_today,
        today_target=today_target,
        no_target_reason=no_target_reason,
        briefs_week=counts.briefs_week,
        week_by_yesterday=week_by_yesterday,
        week_by_tonight=week_by_tonight,
        per_week=targets.per_week,
        need_today=None if week_status == MET else week_by_tonight - counts.briefs_week,
        plan_phase=plan_phase,
        briefs_plan=counts.briefs_plan,
        plan_by_yesterday=plan_by_yesterday,
        plan_by_tonight=plan_by_tonight,
        total=targets.total,
        start=targets.start,
        end=targets.end,
        nudges_due=len(nudges),
        nudges_overdue=overdue,
        nudges_week=counts.nudges_week,
        queue_depth=counts.queue_depth,
        queue_workdays=counts.queue_depth / targets.per_day,
        low_queue=counts.queue_depth < targets.low_water_days * targets.per_day,
        next_up=counts.next_up,
        nudges=nudges,
        closes=closes,
    )


def _name(person: dict) -> str:
    return person.get("display_name") or person.get("email") or f"person {person['id']}"


def _md(iso: str | None) -> str:
    return iso[5:] if iso else "?"


def _brief_line(n: int, item: dict) -> str:
    channels = " · ".join(k for k, v in item["channels"].items() if v)
    rank = f"   #rank {item['rank']}" if item.get("rank") else ""
    last = item["last_interaction"] or "never"
    return f"  {n}. {_name(item['person']):<18}{channels:<24}last {last}{rank}"


def _nudge_line(item: dict) -> str:
    brief = item["brief"]["channel"]
    via = item["suggested_channel"] or brief or "any channel"
    how = f"switch to {via}" if item["kind"] == "nudge 2" and via != brief else via
    due = f"due {_md(item['follow_up_on'])}" + (" (overdue)" if item["overdue"] else "")
    return (
        f"  {_name(item['person']):<18}{item['kind']} · briefed {_md(item['brief']['at'])}"
        f" · {due} · {how}"
    )


def _close_line(item: dict) -> str:
    return (
        f"  {_name(item['person']):<18}briefed {_md(item['brief']['at'])} · "
        f"{item['nudge_count']} nudges, no reply → outreach mark {item['person']['id']} declined"
    )


def render(t: Today) -> str:
    head = f"Outreach: {t.day:%a %Y-%m-%d}"
    lines = [f"{head:<60}{t.verdict}", ""]
    if t.today_target is not None:
        parts = [f"today {t.briefs_today}/{t.today_target}"]
    else:
        parts = [f"today {t.briefs_today}, no target ({t.no_target_reason})"]
    parts.append(f"week {t.briefs_week}/{t.week_by_tonight} expected ({t.per_week})")
    if t.need_today:
        parts.append(f"need {t.need_today} today")
    if t.plan_phase == "active":
        parts.append(
            f"plan {t.briefs_plan}/{t.plan_by_tonight} by today "
            f"({t.total} by {t.end:%b} {t.end.day})"
        )
    elif t.plan_phase == "before start":
        parts.append(f"plan starts {t.start.isoformat()}")
    else:
        parts.append(f"plan ended {t.end.isoformat()} at {t.briefs_plan}/{t.total}")
    lines.append("Briefs   " + " · ".join(parts))
    overdue = f" ({t.nudges_overdue} overdue)" if t.nudges_overdue else ""
    lines.append(f"Nudges   due {t.nudges_due}{overdue} · sent this week {t.nudges_week}")
    lines.append(f"Queue    {t.queue_depth} at to_contact ({t.queue_workdays:.1f} workdays)")
    if t.low_queue:
        lines.append(
            f"Queue low: {t.queue_depth} at to_contact ({t.queue_workdays:.1f} workdays). "
            "Reassess or refill."
        )
    if t.next_up:
        lines += ["", "Brief next"] + [_brief_line(n, i) for n, i in enumerate(t.next_up, 1)]
    if t.nudges:
        lines += ["Nudge"] + [_nudge_line(i) for i in t.nudges]
    if t.closes:
        lines += ["Propose close"] + [_close_line(i) for i in t.closes]
    return "\n".join(lines)


def to_json(t: Today) -> str:
    return json.dumps(asdict(t), indent=2, default=lambda v: v.isoformat())
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_today.py tests/test_outreach.py tests/test_outreach_db.py -q`
Expected: PASS.

- [ ] **Step 10: Run the full checks and commit**

```bash
git add models/outreach.py repo/outreach.py services/outreach.py services/outreach_today.py tests/test_outreach_today.py tests/test_outreach.py tests/test_outreach_db.py
git commit -m "outreach: today's counts, pace verdicts and the daily screen"
```

---

### Task 8: CLI — `nudge`, `rank`, `today`, `queue --json`, notes, env fallback

**Files:**
- Modify: `scripts/outreach.py`
- Test: `tests/test_outreach_cli.py`

**Interfaces:**
- Consumes: `outreach.nudge`, `get_contact`, `set_rank`, `set_ranks`, `clear_ranks`, `NotQueued`, `today_counts`, `follow_ups`, `queue` (Tasks 2–7); `outreach_targets.load_targets`, `TargetsError` (Task 6); `outreach_today.build`, `render`, `to_json` (Task 7).
- Produces the commands of spec §8a:
  - `outreach nudge <person> --channel C [--at T] [--from ADDR] [--note TEXT]`
  - `outreach rank <person> <n>` | `outreach rank --file PATH` | `outreach rank --clear`
  - `outreach today [--targets PATH] [--json]`
  - `outreach queue [--stage S] [--limit N] [--json]`
  - `outreach decide <person> [--skip [--note TEXT]]`
  - `outreach mark <person> declined [--note TEXT]` (note on `declined` only)
  - `outreach report [--floors PATH]`, falling back to `OUTREACH_FLOORS`

- [ ] **Step 1: Make the CLI tests hermetic and update the changed expectations**

In `tests/test_outreach_cli.py`, change the datetime import to `from datetime import UTC, date, datetime, timedelta`, add `import json`, change the models import to `from models.outreach import Advance, CohortCounts, FunnelCounts, TodayCounts`, and add these at the top of the `run` fixture body:

```python
    # scripts/outreach.py loads .env at import; never let the real files leak into tests.
    monkeypatch.delenv("OUTREACH_TARGETS", raising=False)
    monkeypatch.delenv("OUTREACH_FLOORS", raising=False)
```

`advance` now receives `note`: in `test_mark_briefed_commits_then_syncs`, add `"note": None,` to the expected `seen` dict.

- [ ] **Step 2: Write the failing CLI tests**

Append to `tests/test_outreach_cli.py`:

```python
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
def people(monkeypatch):
    def resolve(c, ident):
        if ident not in PEOPLE:
            raise cli.person_lookup.NotFound(ident)
        return dict(PEOPLE[ident])

    monkeypatch.setattr(cli.person_lookup, "resolve", resolve)
    monkeypatch.setattr(
        cli.outreach, "get_contact", lambda c, pid: {"stage": STAGES[pid]} if pid in STAGES else None
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
    code, _, err = run(
        "mark", "alice@example.com", "replied", "--channel", "email", "--note", "x"
    )
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
```

(`test_report_without_floors` stays as it is: with neither `--floors` nor `OUTREACH_FLOORS`, `report` still prints counts without verdicts.)

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_cli.py -q`
Expected: FAIL — `invalid choice: 'nudge'`, `'rank'`, `'today'`; `unrecognized arguments: --json`, `--note`.

- [ ] **Step 4: Implement the CLI changes**

In `scripts/outreach.py`:

Update the module docstring's usage block to:

```python
  python scripts/outreach.py today [--targets PATH] [--json]
  python scripts/outreach.py queue [--stage S] [--limit N] [--json]
  python scripts/outreach.py decide <person> [--skip [--note TEXT]]
  python scripts/outreach.py rank <person> <n> | --file PATH | --clear
  python scripts/outreach.py draft <person> [--subject S]
  python scripts/outreach.py mark <person> briefed|replied|declined|to_contact
                                  [--channel C] [--at T] [--from ADDR] [--force] [--note TEXT]
  python scripts/outreach.py nudge <person> --channel C [--at T] [--from ADDR] [--note TEXT]
  python scripts/outreach.py lead <person> "<name, company>"
  python scripts/outreach.py intro <referrer> "<buyer name, company>" [--buyer <person>] [--at T]
  python scripts/outreach.py conversation|proposal|signed <opportunity-id> [--at T]
  python scripts/outreach.py lost <opportunity-id> --reason R [--at T]
  python scripts/outreach.py followups
  python scripts/outreach.py report [--floors PATH] [--since D] [--until D]
  python scripts/outreach.py sync-asana
```

and add to its prose: "`today` reads its targets from --targets or OUTREACH_TARGETS; `report` reads floors from --floors or OUTREACH_FLOORS (scripts/fetch-env.sh writes both). `--note` goes on `decide --skip`, `mark ... declined` and `nudge`, and is never sent anywhere."

Imports: add `import json` and `import os`; extend the services import to `from services import outreach, outreach_report, outreach_sync, outreach_targets, outreach_today, person_edit, person_lookup`. Add to `USER_ERRORS`: `outreach.NotQueued`, `outreach_targets.TargetsError`.

Add helpers and commands:

```python
def _config_path(flag: str | None, env: str) -> Path | None:
    value = flag or os.environ.get(env)
    return Path(value).expanduser() if value else None


def cmd_today(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    path = _config_path(args.targets, "OUTREACH_TARGETS")
    if path is None:
        raise CliError("no targets file: pass --targets PATH or set OUTREACH_TARGETS")
    targets = outreach_targets.load_targets(path)
    counts = outreach.today_counts(
        conn,
        today=now.astimezone(outreach.TZ).date(),
        per_day=targets.per_day,
        plan_start=targets.start,
        plan_end=targets.end,
    )
    today = outreach_today.build(counts, targets, now=now)
    print(outreach_today.to_json(today) if args.json else outreach_today.render(today), file=out)


def cmd_nudge(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if args.from_address and args.channel != "email":
        raise CliError("--from applies only with --channel email")
    person = _resolve(conn, args.person)
    contact = outreach.get_contact(conn, person["id"])
    if contact is None:
        raise CliError(f"{_label(person)} is not in the pipeline; run `outreach decide` first")
    if contact["stage"] != "briefed":
        raise CliError(f"{_label(person)} is at {contact['stage']}; only a briefed contact is nudged")
    event_id = outreach.nudge(
        conn,
        person["id"],
        at=parse_at(args.at, now),
        channel=args.channel,
        source="cli",
        from_address=args.from_address,
        note=args.note,
    )
    if event_id is None:
        raise CliError(
            f"{_label(person)}: a nudge must come at least a day after the brief "
            f"({contact['briefed_at'].astimezone(outreach.TZ).date()})"
        )
    conn.commit()
    row = outreach.get_contact(conn, person["id"]) or {}
    print(
        f"{_label(person)}: nudge {row.get('nudge_count')} recorded; "
        f"next follow-up {row.get('follow_up_on')}",
        file=out,
    )


def _rank_file(conn: Any, path: Path) -> list[int]:
    try:
        text = path.read_text()
    except FileNotFoundError as e:
        raise CliError(f"rank file not found: {path}") from e
    ids: list[int] = []
    seen: dict[int, int] = {}
    bad: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            person = _resolve(conn, line)
        except CliError as e:
            bad.append(f"  line {lineno}: {e}")
            continue
        contact = outreach.get_contact(conn, person["id"])
        stage = contact["stage"] if contact else "not in the pipeline"
        if stage != "to_contact":
            bad.append(f"  line {lineno}: {line} is {stage}, not to_contact")
        elif person["id"] in seen:
            bad.append(f"  line {lineno}: {line} repeats line {seen[person['id']]}")
        else:
            seen[person["id"]] = lineno
            ids.append(person["id"])
    if bad:
        raise CliError("rank file refused; nothing changed:\n" + "\n".join(bad))
    if not ids:
        raise CliError("rank file lists nobody; use `outreach rank --clear` to clear ranks")
    return ids


def cmd_rank(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    forms = sum([args.clear, args.file is not None, args.person is not None])
    if forms != 1:
        raise CliError("give exactly one of: outreach rank <person> <n>, --file PATH, --clear")
    if args.clear:
        n = outreach.clear_ranks(conn)
        conn.commit()
        print(f"cleared {n} ranks", file=out)
        return
    if args.file is not None:
        ids = _rank_file(conn, Path(args.file).expanduser())
        outreach.set_ranks(conn, ids)
        conn.commit()
        print(f"ranked {len(ids)}; every other to_contact rank cleared", file=out)
        return
    if args.n is None:
        raise CliError("give the rank: outreach rank <person> <n>")
    person = _resolve(conn, args.person)
    outreach.set_rank(conn, person["id"], args.n)
    conn.commit()
    print(f"{_label(person)}: rank {args.n}", file=out)
```

Replace `cmd_queue` with the version below. With `--json`, stdout carries only the JSON and the shared-brief warnings go to stderr:

```python
def cmd_queue(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    items = outreach.queue(conn, stage=args.stage, limit=args.limit)
    if args.json:
        print(json.dumps(items, indent=2), file=out)
    elif items:
        rows = [
            [
                i["person"]["id"],
                i["rank"] or "",
                _label(i["person"]),
                i["person"]["company"] or "",
                i["last_interaction"] or "never",
                " ".join(k for k, v in i["channels"].items() if v),
                i["follow_up_on"] or "",
                "lead" if i["has_lead"] else "",
            ]
            for i in items
        ]
        print(
            outreach_report.table(
                ["id", "rank", "name", "company", "last", "channels", "follow-up", ""], rows
            ),
            file=out,
        )
    else:
        print(f"no contacts at {args.stage}", file=out)
    warn_to = sys.stderr if args.json else out
    for s in outreach.shared_briefs(conn):
        print(
            f"warning: one email ({s['graph_message_id']}) briefed {s['n']} contacts; "
            "a group note is not a brief",
            file=warn_to,
        )
```

Commands receive `out` but not `err`, so `main_with` routes `sys.stderr` to its `err` argument while a command runs (this is what lets `test_queue_json_is_pure_json` see the warning on `err`). Add `import contextlib`, and in `main_with` wrap the command call:

```python
            try:
                with contextlib.redirect_stderr(err):
                    COMMANDS[args.command](conn, args, out, now)
```

In `cmd_decide`, before resolving:

```python
    if args.note and not args.skip:
        raise CliError("--note goes with --skip (a reason to skip); put context in the notes")
```

and pass `note=args.note` to `outreach.advance`.

In `cmd_mark`, add to the checks at the top:

```python
    if args.note and args.stage != "declined":
        raise CliError("--note applies only to declined")
```

and pass `note=args.note` to `outreach.advance`.

In `cmd_report`, replace the `floors = ...` line with:

```python
    path = _config_path(args.floors, "OUTREACH_FLOORS")
    floors = outreach_report.load_floors(path) if path else None
```

Register the commands in `COMMANDS`: `"today": cmd_today`, `"nudge": cmd_nudge`, `"rank": cmd_rank`. In `build_parser`:

```python
    t = sub.add_parser("today")
    t.add_argument("--targets")
    t.add_argument("--json", action="store_true")
```

On `queue`: `q.add_argument("--json", action="store_true")`. On `decide`: `d.add_argument("--note")`. On `mark`: `m.add_argument("--note")`. Then:

```python
    n = sub.add_parser("nudge")
    n.add_argument("person")
    n.add_argument("--channel", required=True, choices=CHANNELS)
    n.add_argument("--at")
    n.add_argument("--from", dest="from_address")
    n.add_argument("--note")
    rk = sub.add_parser("rank")
    rk.add_argument("person", nargs="?")
    rk.add_argument("n", nargs="?", type=int)
    rk.add_argument("--file")
    rk.add_argument("--clear", action="store_true")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_cli.py -q`
Expected: PASS. If `test_today_renders_the_screen` fails on the weekday, check `NOW` is `2026-10-13 15:00 UTC` (a Tuesday in New York).

- [ ] **Step 6: Smoke the CLI against the throwaway DB**

With the Postgres container from Global Constraints running:

`clients/db.py` connects directly when `CLOUD_SQL_CONNECTION_NAME` is empty, using `POSTGRES_HOST/PORT/USER/PASSWORD/DB`. Set them on the command line; `load_dotenv()` never overrides a variable that is already set, so `.env` cannot point these runs at production:

```bash
export CLOUD_SQL_CONNECTION_NAME= POSTGRES_HOST=localhost POSTGRES_PORT=55432 \
       POSTGRES_USER=postgres POSTGRES_PASSWORD=test POSTGRES_DB=postgres
.venv/bin/python scripts/migrate_db.py
printf '[briefs]\nper_day=5\nper_week=25\nstart=2026-10-13\nend=2026-11-21\ntotal=150\n[queue]\nlow_water_days=5\n[workdays]\ndays=["Mon","Tue","Wed","Thu","Fri"]\n' > "$TMPDIR/targets.toml"
.venv/bin/python scripts/outreach.py today --targets "$TMPDIR/targets.toml"
.venv/bin/python scripts/outreach.py queue --json
unset CLOUD_SQL_CONNECTION_NAME POSTGRES_HOST POSTGRES_PORT POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB
```

Expected: `migrate_db.py` prints `Migration complete` (this also proves the schema runs through the production driver path); the screen renders with zero counts and `Queue low: 0 at to_contact`; `queue --json` prints `[]`. If the container is not running, say so in the task report.

- [ ] **Step 7: Run the full checks and commit**

```bash
git add scripts/outreach.py tests/test_outreach_cli.py
git commit -m "outreach CLI: today, nudge, rank, queue --json, notes, env-located floors"
```

---

### Task 9: Skill, environment, linking and docs

**Files:**
- Create: `.claude/skills/working-outreach-queue/SKILL.md`
- Modify: `scripts/link-skills.sh`
- Modify: `scripts/fetch-env.sh`
- Modify: `CLAUDE.md`
- Modify: `.claude/skills/people-architecture/SKILL.md` (the `## Referral outreach` section)
- Modify: `docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md`

**Interfaces:**
- Consumes: every command from Task 8, their exact flags and the `today --json` keys from Task 7 (`verdict`, `next_up`, `nudges`, `closes`, `need_today`, `low_queue`; item keys `kind`, `brief.graph_message_id`, `suggested_channel`).

- [ ] **Step 1: Write the skill**

Create `.claude/skills/working-outreach-queue/SKILL.md`:

````markdown
---
name: working-outreach-queue
description: >
  Use when working Ben's referral outreach pipeline — "work my outreach",
  "what's my outreach today", "am I on track with outreach", "brief the next
  person", "who do I need to nudge", "reassess the outreach queue", "set my
  outreach targets", "add X to my outreach list", "X could refer me",
  "record that X replied to my outreach", "X introduced me to Y". Runs the
  outreach CLI in ~/src/people and supplies the judgment: who to brief, what
  to write, which channel. Not for job-search activity (logging-job-search)
  or the general task list (prioritizing-tasks).
metadata:
  depends-on: "sending-inbox-email, searching-people, fetching-person, creating-person"
---

# Working the outreach queue

The referral pipeline lives in the `people` DB. Every count comes from the
CLI; this skill runs commands and supplies judgment. Design:
`~/src/people/docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md`.

```bash
cd ~/src/people
O=".venv/bin/python scripts/outreach.py"
```

`<person>` is an email, an E.164 phone (`+1…`) or a people id.
`OUTREACH_TARGETS` and `OUTREACH_FLOORS` come from `.env`
(`scripts/fetch-env.sh`). If `today` says no targets file, run that script.

## Rules

- Briefs and email nudges go from **`ben@drolet.ai`**. Mail from the default
  sender, `ben@drolet.cloud`, never counts.
- **Never send.** Email is left as an Outlook draft; other channels are
  Ben's to send.
- Record a non-email brief, nudge or reply **only after Ben confirms it
  happened**. Never record by hand anything email proves (briefs, email
  nudges, email replies): the `email_sent` / `email_classified` events do it.
- **Closing is always Ben's call.** Propose; never run `mark … declined`
  without a yes.

## Today

```bash
$O today --json
```

Lead with `verdict` (`ON TRACK` / `ON PACE` / `BEHIND`) and the one next
action: briefs still owed (`need_today`), then overdue nudges, then closes.
If `low_queue` is true, say so and offer a reassessment.

## Brief

1. Take the first item in `next_up` (rank order).
2. Read its context: `channels`, `last_messages`, `notes`, `outreach_notes`,
   `labels`; use `fetching-person` when that's not enough.
3. **Email is the best channel:** dispatch the `email-builder` agent with
   the brief, the recipient's address and the explicit identity
   `ben@drolet.ai`. It leaves a draft; Ben sends it.
4. **Another channel:** write the text for Ben to send from his phone.
   After he confirms it went:
   `$O mark <person> briefed --channel imessage|whatsapp|linkedin|call|in_person`.
5. **Verify an email brief:** once Ben says it's sent, re-run
   `$O today --json`. The week's briefs should rise and the person should
   leave `next_up`. If not, it most likely went from `ben@drolet.cloud` and
   does not count. Say so; record it by hand only if Ben confirms it went
   from `drolet.ai`: `$O mark <person> briefed --channel email --from ben@drolet.ai`.

## Nudge

Work the `nudges` items in `today --json`.

- **`nudge 1`:** short and light, same channel. For an email brief,
  dispatch `email-builder` as a **reply to `brief.graph_message_id`** from
  `ben@drolet.ai`, so it lands in the same thread.
- **`nudge 2`:** use `suggested_channel` when it is set (otherwise the same
  channel). Word it as a graceful exit: no pressure, door open.
- A non-email nudge, after Ben confirms it went:
  `$O nudge <person> --channel <c> [--note "..."]`. Email nudges record
  themselves; verify them against `nudges_week` as for briefs.

## Close

For each `closes` item, show the brief date, the nudges and the silence, and
ask. On yes:

```bash
$O mark <person> declined --note "no reply after 2 nudges"
```

The note is free text. A late reply later is `$O mark <person> replied --channel <c> --force`.

## Add a person

1. Find them with `searching-people`. If they're missing, create them with
   `creating-person` (needs an email or an E.164 phone).
2. `$O decide <person>` — moves them to `to_contact` and adds the
   `consulting-outreach` label.
3. If Ben gives a position ("near the top"): `$O rank <person> <n>`.
4. Many people at once: point Ben to `scripts/import_contacts.py`, then
   `scripts/outreach_seed.py --dry-run`. He runs those.

## Record outcomes

- Email replies record themselves. Other replies:
  `$O mark <person> replied --channel <c>`.
- A lead with no introduction yet: `$O lead <person> "<name, company>"`.
- An introduction: `$O intro <referrer> "<buyer name, company>" [--buyer <person>]`.
  It prints the opportunity id.
- Buyer stages: `$O conversation|proposal|signed <opportunity-id>`.
- Lost: `$O lost <opportunity-id> --reason no_budget|no_fit|price|scope|timing|no_response|other`.

## Reassess the queue

On request, or when `today` warns the queue is low.

1. `$O queue --json --limit 500` and `$O queue --stage candidate --json`.
2. Propose an order with a one-line reason each, showing the current
   `rank`. Rank on how likely they are to know buyers (role, company,
   industry), warmth (recency, two-way threads) and channel strength.
3. Propose skips, each with a reason.
4. If the queue is still below the low-water mark, propose candidates to
   promote.
5. Ask Ben to confirm, then apply:
   - skips: `$O decide <person> --skip --note "<reason>"`;
   - promotions: `$O decide <person>`;
   - order: write one identifier per line, best first, to a temp file and run
     `$O rank --file <path>`. A bad line refuses the whole file and lists
     every problem; fix and re-run.

## Targets

`~/src/consulting/docs/outreach_targets.toml` holds the brief targets
(`[briefs] per_day, per_week, start, end, total`, `[queue] low_water_days`,
`[workdays] days`). On a request like "4 briefs a day from next week":

1. Edit the file and show the diff.
2. Commit it in `~/src/consulting` with a one-line message. Don't push.
3. A change of plan (dates or total) needs `client_funnel.md` updated first,
   per that file's header. Say so; don't edit the funnel document.

## Report

```bash
$O report
```

Floors come from `OUTREACH_FLOORS`.
````

- [ ] **Step 2: Link it**

In `scripts/link-skills.sh`, change the loop list to:

```bash
for skill in searching-people fetching-person editing-person creating-person working-outreach-queue; do
```

and the header comment's first line to "Symlink the people consumer skills (and the outreach working skill) into ~/.claude/skills/".

- [ ] **Step 3: Locate the private files from `.env`**

In `scripts/fetch-env.sh`, after the `grep -q '^TASKS_API_URL=.'` line and before `echo ".env written"`:

```bash
# Outreach targets and floors live in the private consulting repo (the repo is public).
CONSULTING_DOCS="$HOME/src/consulting/docs"
[ -f "$CONSULTING_DOCS/outreach_targets.toml" ] && echo "OUTREACH_TARGETS=$CONSULTING_DOCS/outreach_targets.toml" >> .env
[ -f "$CONSULTING_DOCS/client_funnel_floors.toml" ] && echo "OUTREACH_FLOORS=$CONSULTING_DOCS/client_funnel_floors.toml" >> .env
```

Run `bash -n scripts/fetch-env.sh`. Expected: no output.

- [ ] **Step 4: Update `CLAUDE.md`**

- In the Stack table's **Referral outreach** row, append: "follow-ups are `nudge` events on a `briefed` contact (+7/+14 days from the brief, propose close at +21); `outreach today` checks pace against `OUTREACH_TARGETS` (private TOML); skill `working-outreach-queue`".
- In the `outreach_contacts`/`outreach_events` mention in the Database row, no change is needed (table list is unchanged).
- In **Code layout**, under `services/`, add:
  ```
  outreach_targets.py       pure: targets TOML → Targets (per_day, per_week, plan window, workdays)
  outreach_today.py         pure: TodayCounts + Targets → pace verdicts and the daily screen
  ```
  Change the `outreach.py` (services) line to "the only stage writer: transition / advance / advance_from_event / advance_from_sent, nudge, ranks, opportunities, merge_person, queue, follow_ups, today_counts, counts, is_outreach_sender". Change the `scripts/outreach.py` line to "referral outreach CLI: today, queue, decide, rank, mark, nudge, intro, conversation, lost, lead, draft, followups, report, sync-asana".
- In the `.claude/skills/` list, add `working-outreach-queue`; in the `link-skills.sh` line, add it to the linked list.
- In **Local dev → Referral outreach**, replace the code block with:
  ```bash
  .venv/bin/python scripts/outreach_seed.py --dry-run          # then without --dry-run
  .venv/bin/python scripts/outreach.py today                    # pace verdict + what to do (OUTREACH_TARGETS)
  .venv/bin/python scripts/outreach.py queue                    # to_contact in rank order
  .venv/bin/python scripts/outreach.py report                   # floors from OUTREACH_FLOORS
  ```
  and add a sentence: "`OUTREACH_TARGETS` and `OUTREACH_FLOORS` point at files in the private `~/src/consulting/docs/`; `scripts/fetch-env.sh` writes them when the files exist."

- [ ] **Step 5: Update `people-architecture`**

In `.claude/skills/people-architecture/SKILL.md`'s `## Referral outreach` section, append:

```markdown
- **Follow-ups (working-queue spec).** A `nudge` is an `outreach_events` row on
  a `briefed` contact, never a stage and never an Asana task.
  `services/outreach.py::nudge` sets `follow_up_on` from the brief: +14 after
  nudge 1, +21 after nudge 2 (propose close). Mail from an outreach address to a
  `briefed` contact records one (`advance_from_sent`); a replayed email never
  does (any referrer event with the same `graph_message_id` blocks it).
- **Queue order.** `outreach_contacts.rank` (to_contact only, cleared on leaving)
  sorts `queue` first; `outreach rank --file` rewrites it in one transaction.
- **`outreach today`.** `services/outreach_today.py` turns `today_counts` and the
  private targets TOML into `ON TRACK` / `ON PACE` / `BEHIND`. Marks count only
  workdays inside the plan window.
```

- [ ] **Step 6: Correct the spec example and mark it implemented**

In `docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md` §5.2, change the example's first lines to:

```
Outreach: Wed 2026-10-14                                    BEHIND

Briefs   today 2/5 · week 7/10 expected (25) · need 3 today · plan 7/10 by today (150 by Nov 21)
Nudges   due 2 (1 overdue) · sent this week 4
```

and add under it: "2026-10-14 is a Wednesday; Monday 10-12 is before the plan's start, so the week's marks count from Tuesday. An overdue nudge makes the verdict `BEHIND`. Nudges due counts nudge items; proposed closes are listed separately." Change the **Status** line to begin "**Status:** implemented (see `docs/superpowers/plans/2026-10-13-outreach-working-queue.md`)." and in §12 replace "No implementation plan exists yet." with "The plan is `docs/superpowers/plans/2026-10-13-outreach-working-queue.md`."

- [ ] **Step 7: Check and commit**

Run the full checks (Global Constraints). Then:

```bash
git add .claude/skills/working-outreach-queue/SKILL.md scripts/link-skills.sh scripts/fetch-env.sh CLAUDE.md .claude/skills/people-architecture/SKILL.md docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md
git commit -m "outreach: working-outreach-queue skill, env-located targets, docs"
```

---

### Task 10: Whole-branch verification and the PR

**Files:** none new.

- [ ] **Step 1: Full checks with the real-Postgres tests**

```bash
docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
docker stop people-test-pg
```

Expected: all pass, with `tests/test_outreach_db.py` and `tests/test_schema.py` **not** skipped (check the summary's skip count).

- [ ] **Step 2: Read the skill once against the CLI**

For every command in `.claude/skills/working-outreach-queue/SKILL.md`, run `.venv/bin/python scripts/outreach.py <command> --help` and confirm the flags exist exactly as written. Fix the skill, not the CLI, on any mismatch.

- [ ] **Step 3: Open the PR**

Use the `/pr-open` skill on branch `outreach-working-queue` (base `main`). The description must state:
- **Run `scripts/migrate_db.py` against production before merging**: the handler writes `nudge` rows as soon as it deploys, and the old CHECK rejects them.
- The rollout steps of Task 11.
- Decisions 1–10 above, briefly.
- Whether the real-Postgres tests ran locally (Docker) or only in CI.

---

### Task 11: Rollout (Ben runs these; production-touching)

Not for an agent to run unattended. Listed so the PR carries them.

1. **Migrate production before merging** (needs `.env` from `scripts/fetch-env.sh`):
   ```bash
   .venv/bin/python scripts/migrate_db.py
   ```
2. **Create the targets file** in the consulting repo and commit it there:
   ```bash
   cat > ~/src/consulting/docs/outreach_targets.toml <<'EOF'
   # Targets for `outreach today` (people repo). Derived from client_funnel.md
   # "Cadence and checkpoints"; change that document first, then this file.
   [briefs]
   per_day  = 5
   per_week = 25
   start    = 2026-10-13
   end      = 2026-11-21
   total    = 150

   [queue]
   low_water_days = 5        # warn when to_contact holds fewer than low_water_days * per_day

   [workdays]
   days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
   EOF
   git -C ~/src/consulting add docs/outreach_targets.toml
   git -C ~/src/consulting commit -m "Outreach targets for the people repo's outreach today"
   ```
3. **Retire the old skill** (unversioned, local only), then re-link:
   ```bash
   rm -r ~/.claude/skills/adding-referral-contact
   scripts/fetch-env.sh && scripts/link-skills.sh
   ```
4. **Merge the PR**, then check production: `.venv/bin/python scripts/outreach.py today`.
