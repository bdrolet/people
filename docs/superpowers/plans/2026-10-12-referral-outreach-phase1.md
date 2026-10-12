# Referral Outreach Phase 1 (the Counting Layer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every warm contact's referral stage and every introduction's buyer stage in the `people` DB, advance `briefed`/`replied` automatically from the email events people already receives, write one completed Asana task per countable stage event, and give Ben a CLI (`scripts/outreach.py`) and a seed script to work the pipeline and report actuals against the funnel floors.

**Architecture:** Three new tables (`outreach_contacts`, `outreach_opportunities`, `outreach_events`) are the record; the events table is an append-only log every count derives from. `services/outreach.py` owns the stage machine and is the only writer. The two existing event handlers call it inside their durable transaction; the Asana sync (`services/outreach_sync.py` over a new `clients/tasks_api.py`) runs after commit, best-effort, and the nightly `people-sync` retries what it missed. HubSpot, people-api routes and enrichment are phases 2 and 3 and are **not** in this plan.

**Tech Stack:** Python 3.13, Postgres (pg8000 on Cloud SQL / psycopg3 locally), httpx, Google ID tokens (Cloud Run IAM), OpenTelemetry, pytest, ruff, mypy, Terraform.

**Spec:** `docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md` (phase 1 is §13's first paragraph; read §4–§9 and §12 before starting).

## Global Constraints

- **Repo is public.** No real names, addresses, floors, actuals or message text in code, tests, fixtures, commit messages or logs. Fixtures use `example.com` addresses and `+1555010…` numbers. `OUTREACH_FROM_ADDRESSES` lives only in env / uncommitted `terraform.tfvars` / a GitHub variable.
- **Durable first.** A stage change is written and committed before any tasks-api, inbox-api, Google or HubSpot call. A side-effect failure never loses or rolls back a count (spec §3, §8).
- **One writer.** Only `services/outreach.py` writes `outreach_contacts`, `outreach_opportunities` and `outreach_events` stage data (the Asana gid columns are written by `services/outreach_sync.py`).
- **Layer rules (CLAUDE.md).** `clients/` I/O only; `repo/` takes an open connection, never opens one, and decides nothing; `services/` business logic; `handlers/` orchestrate; scripts call services.
- **Enums are exact strings.** Referrer stages `candidate, to_contact, briefed, replied, introduced, declined, skipped`; buyer stages `intro, conversation, proposal, signed, lost`; channels `email, imessage, whatsapp, linkedin, call, in_person`; sources `event, cli, hubspot, seed`; lost reasons `no_budget, no_fit, price, scope, timing, no_response, other`.
- **Time zone.** Cohort weeks, follow-up dates and "today" are computed in `America/New_York` (`services.outreach.TZ`). Cohort week = the Monday of the brief's local date.
- **pg8000 gotcha.** `clients/db.py::_adapt_params` rewrites any all-numeric Python list into the string `"[1,2,3]"` (a pgvector hack). Never pass a list of ints as a query parameter; pass one id at a time or a list of strings with an explicit `::text[]` cast.
- **Metric names** keep the repo's `people.` prefix (CLAUDE.md: "metrics prefixed `people_`"), so the spec's `outreach.stage_events` ships as `people.outreach_stage_events`, and so on for every §12.2 instrument. Attribute values come only from the closed enums above; never a name, address or id.
- **No drafting.** Nothing here writes message text. `outreach draft` creates a draft with an empty body and, unless `--subject` is given, an empty subject.
- **Branch.** Work on `referral-outreach-phase1`, created from `referral-outreach-spec` so the spec and this plan ship in the same PR. Open the PR with the `/pr-open` skill at the end (Task 13), never by hand.
- **Commit trailer.** End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Nw4SktphsyvVx4yfx1TJSk
  ```
- **Format first.** The plan's code blocks are not pre-formatted, and some steps add a second import from a module already imported. Run `.venv/bin/ruff check --fix . && .venv/bin/ruff format .` before the checks below; it merges and sorts imports and wraps long lines.
- **Checks before every commit:**
  ```bash
  .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
  ```
- **Real-Postgres tests.** `tests/test_outreach_db.py` (created in Task 1) is skipped unless `TEST_DATABASE_URL` is set. CI does not set it, so run it locally before each commit that touches SQL. There is no local Postgres on this machine; use a throwaway container:
  ```bash
  docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
  export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
  ```
  If Docker is unavailable, say so in the task report and in the PR. Do not claim the SQL was verified.

## Decisions this plan makes where the spec is silent or loose

Each is small; each is called out so a reviewer can reject it on its own.

1. **Two extra `outreach_events` columns.** `from_address` records `mark --from` (spec §7.4 names the flag but no column). `asana_completed_at` lets the sync retry only the completion after a successful create (spec §11 requires that behaviour; one gid column cannot express it).
2. **CHECK constraints on enums** for `channel` and event `stage`, plus "`lost_reason` is set exactly when stage is `lost`".
3. **`advance` returns `Advance | None`** (row, changed flag, event id) and raises `InvalidTransition` for a disallowed move. Handlers call `advance_from_event`, which swallows `InvalidTransition`, so the CLI rejects and the handlers ignore, as §4.3 requires.
4. **`intro` on a `briefed` referrer first appends `replied`.** An introduction proves a reply, and the replied count must never be below the intro count. Every other disallowed stage still rejects.
5. **`decide` creates the pipeline row when missing** (at `candidate`, source `cli`), so a person the seed never proposed can still enter the pipeline.
6. **`follow_up_on` is cleared on every stage except `briefed`**, not only on `replied`, so `followups` never lists a declined or skipped contact.
7. **`--channel` is required** on `mark briefed` and `mark replied`; `--from` is accepted only with `--channel email`.
8. **Report "Actual" counts only lag-ripe cohorts**, the same cohorts `Floor@n` is computed from, so a young cohort's early replies cannot mask a miss. The verdict is `at floor` when the actual equals `Floor@n` rounded half-up, else `above floor` or `below floor`.
9. **Queue `last_interaction`** is the latest of the email, iMessage, WhatsApp and LinkedIn last-message dates, and the queue sorts on it ascending with never-contacted first.
10. **Merging duplicate contacts keeps pipeline history.** `scripts/merge_duplicate_contacts.py` deletes the loser `people` row, and the outreach tables cascade. The spec does not mention the merge script; Task 4 re-points the outreach rows first.
11. **`outreach draft` is included** (spec §7.3–§7.4) even though §13's phase-1 list omits it. `INBOX_API_URL` is set only in the local `.env`, because only the CLI uses it.
12. **`outreach lead`** appends `YYYY-MM-DD lead: <text>` to the row's `notes` and sets `has_lead`. It appends no event, because a lead is not a stage.

## Review Focus

1. **Pub/Sub redelivers the same `email_sent` or `email_classified`.** Expected: no second event, no second Asana task. Pinned in Task 3 (`test_advance_twice_appends_one_event`) and Task 7 (`test_sent_no_event_means_no_sync`).
2. **A brief with Ben's own address in To, or the contact in Cc.** Expected: only To recipients who are not Ben advance. Pinned in Task 7 (`test_sent_advances_only_to_recipients`).
3. **The From address arrives in a different case or with whitespace.** Expected: still recognised as the consulting identity. Pinned in Task 7 (`test_outreach_sender_is_case_and_space_insensitive`).
4. **A contact writes before Ben's brief is sent, or after they were declined.** Expected: ignored by the handler, no exception, no event. Pinned in Task 3 (`test_advance_from_event_ignores_disallowed_moves`).
5. **A brief sent late Sunday evening in New York, which is already Monday in UTC.** Expected: the cohort is the earlier Monday. Pinned in Task 2 (`test_cohort_week_uses_new_york_time`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repo/schema.sql` | Modify | Append the three `outreach_*` tables and indexes. |
| `models/outreach.py` | Create | Stage/channel/source/lost-reason tuples, `Advance`, `CohortCounts`, `FunnelCounts`. Pure types. |
| `repo/outreach.py` | Create | Every SQL statement on the `outreach_*` tables, including the read queries for queue, follow-ups, report counts, population, Asana sync and seed sources. |
| `services/outreach.py` | Create | Stage machine (`transition`), `advance`, `advance_from_event`, `ensure_contact`, `open_opportunity`, `advance_opportunity`, `set_lead`, `merge_person`, `queue`, `follow_ups`, `shared_briefs`, `counts`, `record_population`, `is_outreach_sender`. |
| `services/outreach_sync.py` | Create | Asana task payloads, `sync_events`, `sync_pending`, lateness buckets. |
| `services/outreach_report.py` | Create | Floors TOML loading, `stage_verdicts`, `render`, `table`. Pure. |
| `services/outreach_seed.py` | Create | Warm-pool bars over raw rows, producing a `SeedPlan`. Pure. |
| `services/person_lookup.py` | Create | Resolve an email / phone / id to one `people` row for scripts. |
| `clients/gcp_auth.py` | Create | Google ID tokens for Cloud Run callers (ported from the tasks repo). |
| `clients/tasks_api.py` | Create | `create_task`, `complete_task`, `configured`. |
| `clients/inbox_api.py` | Create | `create_draft` (no `from` block). |
| `clients/otel.py` | Modify | Outreach counters and observable gauges. |
| `handlers/email_sent.py` | Modify | Advance To recipients to `briefed`; sync after commit. |
| `handlers/email_classified.py` | Modify | Advance the sender to `replied`. |
| `handlers/sync.py` | Modify | Retry pending Asana syncs; record the population gauges. |
| `scripts/outreach.py` | Create | The CLI. |
| `scripts/outreach_seed.py` | Create | The seed script. |
| `scripts/merge_duplicate_contacts.py` | Modify | Re-point outreach rows before deleting a loser row. |
| `requirements.txt` | Modify | Add `httpx`. |
| `terraform/variables.tf`, `terraform/cloud_functions.tf`, `terraform/terraform.tfvars.example`, `.github/workflows/deploy.yml`, `scripts/fetch-env.sh` | Modify | `OUTREACH_FROM_ADDRESSES`, `TASKS_API_URL`, local `INBOX_API_URL`. |
| `CLAUDE.md`, `.claude/skills/people-architecture/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`, spec status line | Modify | Document the pipeline. |
| Tests | Create/Modify | `tests/test_outreach_db.py`, `tests/test_outreach.py`, `tests/test_repo_outreach.py`, `tests/test_outreach_sync.py`, `tests/test_tasks_api.py`, `tests/test_inbox_api.py`, `tests/test_gcp_auth.py`, `tests/test_outreach_report.py`, `tests/test_outreach_seed.py`, `tests/test_outreach_cli.py`, `tests/test_person_lookup.py`; `tests/test_handlers.py`, `tests/test_main.py`, `tests/test_merge_duplicate_contacts.py`. |
| **tasks repo** `terraform/variables.tf`, `terraform/api.tf` | Modify (Task 14) | Invoker grant for the two people service accounts. |
| **inbox repo** `.claude/skills/sending-inbox-email/SKILL.md`, `.claude/agents/email-builder.md` | Modify (Task 15) | "Consulting briefs go from the primary address." |

---

### Task 1: Schema

**Files:**
- Modify: `repo/schema.sql` (append at the very end; the tables reference `people(id)`, which only becomes a primary key in the person-identity migration earlier in the file)
- Create: `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `outreach_contacts`, `outreach_opportunities`, `outreach_events` with exactly the columns below; `tests/test_outreach_db.py` with the `conn` fixture and `_person` helper later tasks append tests to.

- [ ] **Step 1: Create the branch**

```bash
cd /Users/ben/src/people && git checkout referral-outreach-spec && git checkout -b referral-outreach-phase1
```

- [ ] **Step 2: Write the failing schema tests**

Create `tests/test_outreach_db.py`:

```python
"""Referral outreach SQL against a real Postgres (outreach spec §5). Skipped unless
TEST_DATABASE_URL is set; see the plan's Global Constraints for a throwaway container."""

import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = Path(__file__).resolve().parent.parent / "repo" / "schema.sql"
TS = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True, row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        yield c


def _person(conn, email="alice@example.com", **cols) -> int:
    cols = {"email": email, "first_seen": TS, **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    return conn.execute(
        f"INSERT INTO people ({names}) VALUES ({marks}) RETURNING id", tuple(cols.values())
    ).fetchone()["id"]


def test_schema_is_still_idempotent(conn):
    conn.execute(SCHEMA.read_text())


def test_contact_rejects_an_unknown_stage(conn):
    pid = _person(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'pinged')", (pid,)
        )


def test_pipeline_rows_cascade_with_their_person(conn):
    pid = _person(conn)
    conn.execute("INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'briefed')", (pid,))
    oid = conn.execute(
        "INSERT INTO outreach_opportunities (referrer_id, buyer_label, stage, intro_at) "
        "VALUES (%s, 'Buyer, Co', 'intro', %s) RETURNING id",
        (pid, TS),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO outreach_events (pipeline, opportunity_id, stage, at, source) "
        "VALUES ('buyer', %s, 'intro', %s, 'cli')",
        (oid, TS),
    )
    conn.execute("DELETE FROM people WHERE id = %s", (pid,))
    for table in ("outreach_contacts", "outreach_opportunities", "outreach_events"):
        assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


def test_deleting_the_buyer_keeps_the_opportunity(conn):
    referrer = _person(conn)
    buyer = _person(conn, email="buyer@example.com")
    conn.execute(
        "INSERT INTO outreach_opportunities (referrer_id, buyer_id, buyer_label, stage, intro_at) "
        "VALUES (%s, %s, 'Buyer, Co', 'intro', %s)",
        (referrer, buyer, TS),
    )
    conn.execute("DELETE FROM people WHERE id = %s", (buyer,))
    row = conn.execute("SELECT buyer_id, buyer_label FROM outreach_opportunities").fetchone()
    assert row == {"buyer_id": None, "buyer_label": "Buyer, Co"}


def test_a_referrer_event_needs_a_person_and_no_opportunity(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_events (pipeline, stage, at, source) "
            "VALUES ('referrer', 'briefed', %s, 'cli')",
            (TS,),
        )


def test_lost_needs_a_reason_and_only_lost_has_one(conn):
    pid = _person(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_opportunities (referrer_id, buyer_label, stage, intro_at) "
            "VALUES (%s, 'B', 'lost', %s)",
            (pid, TS),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO outreach_opportunities (referrer_id, buyer_label, stage, intro_at, lost_reason) "
            "VALUES (%s, 'B', 'intro', %s, 'price')",
            (pid, TS),
        )
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `TEST_DATABASE_URL=$TEST_DATABASE_URL .venv/bin/pytest tests/test_outreach_db.py -v`
Expected: FAIL with `UndefinedTable` for `outreach_contacts`.

- [ ] **Step 4: Append the DDL to the end of `repo/schema.sql`**

```sql
-- Referral outreach pipeline (docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §5).
-- Appended last: every table references people(id), which only becomes a primary key in
-- the person-identity migration above. Pipeline rows CASCADE with their person, so:
-- export outreach_* before any DELETE FROM people (rebuild runbook, CLAUDE.md), and
-- scripts/merge_duplicate_contacts.py re-points them before it deletes a loser row.
-- services/outreach.py is the only writer of stage data; outreach_events is append-only
-- and every count in `outreach report` derives from it.

CREATE TABLE IF NOT EXISTS outreach_contacts (
    person_id          BIGINT PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    stage              TEXT NOT NULL CHECK (stage IN
                         ('candidate','to_contact','briefed','replied','introduced','declined','skipped')),
    cohort_week        DATE,                -- Monday (America/New_York) of the first brief
    briefed_at         TIMESTAMPTZ,         -- first brief
    replied_at         TIMESTAMPTZ,         -- first reply
    channel            TEXT CHECK (channel IN ('email','imessage','whatsapp','linkedin','call','in_person')),
    has_lead           BOOLEAN NOT NULL DEFAULT FALSE,
    follow_up_on       DATE,                -- set only while stage = 'briefed'
    notes              TEXT,
    hubspot_synced_at  TIMESTAMPTZ,         -- phase 2
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outreach_contacts_stage_idx ON outreach_contacts (stage, follow_up_on);

CREATE TABLE IF NOT EXISTS outreach_opportunities (
    id               BIGSERIAL PRIMARY KEY,
    referrer_id      BIGINT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    buyer_id         BIGINT REFERENCES people(id) ON DELETE SET NULL,
    buyer_label      TEXT NOT NULL,
    stage            TEXT NOT NULL CHECK (stage IN ('intro','conversation','proposal','signed','lost')),
    lost_reason      TEXT CHECK (lost_reason IN
                       ('no_budget','no_fit','price','scope','timing','no_response','other')),
    intro_at         TIMESTAMPTZ NOT NULL,
    conversation_at  TIMESTAMPTZ,
    proposal_at      TIMESTAMPTZ,
    signed_at        TIMESTAMPTZ,
    lost_at          TIMESTAMPTZ,
    hubspot_deal_id  TEXT UNIQUE,           -- phase 2
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((stage = 'lost') = (lost_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS outreach_opportunities_referrer_idx ON outreach_opportunities (referrer_id);
CREATE INDEX IF NOT EXISTS outreach_opportunities_buyer_idx ON outreach_opportunities (buyer_id);

CREATE TABLE IF NOT EXISTS outreach_events (
    id                  BIGSERIAL PRIMARY KEY,
    pipeline            TEXT NOT NULL CHECK (pipeline IN ('referrer','buyer')),
    person_id           BIGINT REFERENCES people(id) ON DELETE CASCADE,
    opportunity_id      BIGINT REFERENCES outreach_opportunities(id) ON DELETE CASCADE,
    stage               TEXT NOT NULL CHECK (stage IN
                          ('candidate','to_contact','briefed','replied','introduced','declined','skipped',
                           'intro','conversation','proposal','signed','lost')),
    at                  TIMESTAMPTZ NOT NULL,
    channel             TEXT CHECK (channel IN ('email','imessage','whatsapp','linkedin','call','in_person')),
    source              TEXT NOT NULL CHECK (source IN ('event','cli','hubspot','seed')),
    graph_message_id    TEXT,               -- the email that proved it, when source = 'event'
    from_address        TEXT,               -- `outreach mark --from`: a hand-sent email brief's identity
    asana_task_gid      TEXT,               -- set once the strategy task exists (§8)
    asana_completed_at  TIMESTAMPTZ,        -- set once that task is marked complete
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((pipeline = 'referrer') = (person_id IS NOT NULL)),
    CHECK ((pipeline = 'buyer') = (opportunity_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS outreach_events_stage_at_idx ON outreach_events (pipeline, stage, at);
CREATE INDEX IF NOT EXISTS outreach_events_person_idx ON outreach_events (person_id);
CREATE INDEX IF NOT EXISTS outreach_events_opportunity_idx ON outreach_events (opportunity_id);
CREATE INDEX IF NOT EXISTS outreach_events_unsynced_idx ON outreach_events (id)
    WHERE asana_task_gid IS NULL OR asana_completed_at IS NULL;
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_db.py tests/test_schema.py -v`
Expected: PASS (both modules; `test_schema.py` proves the append did not break the existing migration).

- [ ] **Step 6: Commit**

```bash
git add repo/schema.sql tests/test_outreach_db.py
git commit -m "feat(schema): outreach_contacts, outreach_opportunities, outreach_events"
```

---

### Task 2: Stage model and pure stage rules

**Files:**
- Create: `models/outreach.py`
- Create: `services/outreach.py` (pure part only in this task)
- Test: `tests/test_outreach.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `models.outreach`: `REFERRER_STAGES`, `BUYER_STAGES`, `CHANNELS`, `SOURCES`, `LOST_REASONS`, `REPORT_STAGES` (tuples of str); `Advance(row: dict, changed: bool, event_id: int | None)`; `CohortCounts(cohort_week: date | None, briefed=0, replied=0, intro=0, conversation=0, proposal=0, signed=0)`; `FunnelCounts(cohorts: list[CohortCounts], lost_reasons: dict[str, int], follow_ups_due: int)`.
  - `services.outreach`: `TZ`, `FOLLOW_UP_DAYS = 7`, `transition(pipeline: str, current: str, target: str, *, force: bool = False) -> Literal["advance", "noop", "invalid"]`, `cohort_week(at: datetime) -> date`, `follow_up_date(at: datetime) -> date`, `iso_week(d: date | None) -> str`, `REFERRER_RANK: dict[str, int]`, exceptions `InvalidTransition(pipeline, current, target)` (attrs of the same names), `NotInPipeline(LookupError)`, `OpportunityNotFound(LookupError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outreach.py`:

```python
from datetime import UTC, date, datetime

import pytest

from services import outreach as svc


@pytest.mark.parametrize(
    "current,target",
    [
        ("candidate", "to_contact"),
        ("candidate", "skipped"),
        ("to_contact", "briefed"),
        ("to_contact", "skipped"),
        ("briefed", "replied"),
        ("briefed", "declined"),
        ("replied", "introduced"),
        ("replied", "declined"),
    ],
)
def test_referrer_transitions_in_the_table_advance(current, target):
    assert svc.transition("referrer", current, target) == "advance"


@pytest.mark.parametrize(
    "current,target",
    [
        ("briefed", "briefed"),
        ("replied", "briefed"),
        ("introduced", "replied"),
        ("introduced", "introduced"),
        ("declined", "briefed"),
        ("skipped", "candidate"),
    ],
)
def test_reaching_a_stage_already_reached_is_a_noop(current, target):
    assert svc.transition("referrer", current, target) == "noop"


@pytest.mark.parametrize(
    "current,target",
    [
        ("candidate", "briefed"),  # deciding is Ben's act; the email only proves it
        ("to_contact", "replied"),
        ("declined", "replied"),
        ("skipped", "briefed"),
        ("skipped", "to_contact"),
        ("declined", "to_contact"),
    ],
)
def test_moves_outside_the_table_are_invalid(current, target):
    assert svc.transition("referrer", current, target) == "invalid"


@pytest.mark.parametrize("current", ["declined", "skipped"])
def test_force_reopens_a_terminal_referrer(current):
    assert svc.transition("referrer", current, "to_contact", force=True) == "advance"


def test_force_does_not_bypass_the_table_for_an_open_row():
    assert svc.transition("referrer", "to_contact", "replied", force=True) == "invalid"


@pytest.mark.parametrize(
    "current,target,expected",
    [
        ("intro", "conversation", "advance"),
        ("intro", "lost", "advance"),
        ("conversation", "proposal", "advance"),
        ("proposal", "signed", "advance"),
        ("proposal", "conversation", "noop"),
        ("intro", "proposal", "invalid"),
        ("signed", "lost", "invalid"),
        ("lost", "conversation", "invalid"),
        ("lost", "intro", "noop"),
    ],
)
def test_buyer_transitions(current, target, expected):
    assert svc.transition("buyer", current, target) == expected


def test_unknown_stage_is_a_programming_error():
    with pytest.raises(ValueError):
        svc.transition("referrer", "briefed", "ghosted")


def test_cohort_week_is_the_monday_of_the_brief():
    assert svc.cohort_week(datetime(2026, 10, 15, 16, 0, tzinfo=UTC)) == date(2026, 10, 12)


def test_cohort_week_uses_new_york_time():
    # Sunday 2026-10-18 23:30 in New York is Monday 03:30 UTC.
    assert svc.cohort_week(datetime(2026, 10, 19, 3, 30, tzinfo=UTC)) == date(2026, 10, 12)


def test_follow_up_is_seven_local_days_later():
    assert svc.follow_up_date(datetime(2026, 10, 14, 2, 0, tzinfo=UTC)) == date(2026, 10, 20)


def test_iso_week_labels():
    assert svc.iso_week(date(2026, 10, 12)) == "2026-W42"
    assert svc.iso_week(None) == "none"


def test_invalid_transition_message_names_the_current_stage_and_the_reopen_hint():
    e = svc.InvalidTransition("referrer", "declined", "briefed")
    assert "declined" in str(e) and "--force" in str(e)
    assert "--force" not in str(svc.InvalidTransition("referrer", "to_contact", "replied"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py -v`
Expected: FAIL with `ImportError: cannot import name 'outreach' from 'services'`.

- [ ] **Step 3: Create `models/outreach.py`**

```python
"""Referral outreach pipeline types (outreach spec §4). Pure: no imports from other layers."""

from dataclasses import dataclass, field
from datetime import date

REFERRER_STAGES = (
    "candidate",
    "to_contact",
    "briefed",
    "replied",
    "introduced",
    "declined",
    "skipped",
)
BUYER_STAGES = ("intro", "conversation", "proposal", "signed", "lost")
CHANNELS = ("email", "imessage", "whatsapp", "linkedin", "call", "in_person")
SOURCES = ("event", "cli", "hubspot", "seed")
LOST_REASONS = ("no_budget", "no_fit", "price", "scope", "timing", "no_response", "other")
# The stages `outreach report` measures against the floors (spec §9), in funnel order.
REPORT_STAGES = ("replied", "intro", "conversation", "proposal", "signed")


@dataclass
class Advance:
    row: dict  # the outreach_contacts or outreach_opportunities row after the call
    changed: bool  # False: the row was already at or past the requested stage
    event_id: int | None  # the appended outreach_events id, when changed


@dataclass
class CohortCounts:
    cohort_week: date | None
    briefed: int = 0
    replied: int = 0
    intro: int = 0
    conversation: int = 0
    proposal: int = 0
    signed: int = 0


@dataclass
class FunnelCounts:
    cohorts: list[CohortCounts]
    lost_reasons: dict[str, int] = field(default_factory=dict)
    follow_ups_due: int = 0
```

- [ ] **Step 4: Create `services/outreach.py` with the pure stage rules**

```python
"""The referral outreach stage machine (outreach spec §4, §6.1). The only writer of
stage data on the outreach_* tables; handlers and scripts call it, never repo/outreach.py
directly. Every write function takes an open connection and leaves committing to the
caller, so the event handlers keep it inside their durable transaction (spec §6)."""

import os
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from models.outreach import BUYER_STAGES, REFERRER_STAGES

TZ = ZoneInfo("America/New_York")
FOLLOW_UP_DAYS = 7

Transition = Literal["advance", "noop", "invalid"]

# Spec §4.3, verbatim. Anything not listed is rejected (CLI) or ignored (handlers).
_ALLOWED: dict[str, dict[str, set[str]]] = {
    "referrer": {
        "candidate": {"to_contact", "skipped"},
        "to_contact": {"briefed", "skipped"},
        "briefed": {"replied", "declined"},
        "replied": {"introduced", "declined"},
        "introduced": set(),
        "declined": set(),
        "skipped": set(),
    },
    "buyer": {
        "intro": {"conversation", "lost"},
        "conversation": {"proposal", "lost"},
        "proposal": {"signed", "lost"},
        "signed": set(),
        "lost": set(),
    },
}

# Every stage a row at the key stage has necessarily passed through. Requesting one of
# these is a no-op returning the current row: that is what makes replaying an event safe.
_REACHED: dict[str, dict[str, set[str]]] = {
    "referrer": {
        "candidate": {"candidate"},
        "to_contact": {"candidate", "to_contact"},
        "briefed": {"candidate", "to_contact", "briefed"},
        "replied": {"candidate", "to_contact", "briefed", "replied"},
        "introduced": {"candidate", "to_contact", "briefed", "replied", "introduced"},
        "declined": {"candidate", "to_contact", "briefed", "declined"},
        "skipped": {"candidate", "skipped"},
    },
    "buyer": {
        "intro": {"intro"},
        "conversation": {"intro", "conversation"},
        "proposal": {"intro", "conversation", "proposal"},
        "signed": {"intro", "conversation", "proposal", "signed"},
        "lost": {"intro", "lost"},
    },
}

_REOPENABLE = {"declined", "skipped"}

# How far along a referrer row is; merge_person keeps the further row.
REFERRER_RANK = {
    "candidate": 0,
    "to_contact": 1,
    "skipped": 1,
    "briefed": 2,
    "declined": 3,
    "replied": 3,
    "introduced": 4,
}


class InvalidTransition(Exception):
    def __init__(self, pipeline: str, current: str, target: str) -> None:
        self.pipeline, self.current, self.target = pipeline, current, target
        hint = "; `outreach mark ... --force` reopens it" if current in _REOPENABLE else ""
        super().__init__(f"{pipeline} is at {current}; cannot move to {target}{hint}")


class NotInPipeline(LookupError):
    """The person has no outreach_contacts row (run `outreach decide` first)."""


class OpportunityNotFound(LookupError):
    pass


def transition(pipeline: str, current: str, target: str, *, force: bool = False) -> Transition:
    stages = REFERRER_STAGES if pipeline == "referrer" else BUYER_STAGES
    if target not in stages or current not in stages:
        raise ValueError(f"unknown {pipeline} stage: {current!r} -> {target!r}")
    if (
        force
        and pipeline == "referrer"
        and current in _REOPENABLE
        and target not in (current, "candidate")
    ):
        return "advance"
    if target in _REACHED[pipeline][current]:
        return "noop"
    if target in _ALLOWED[pipeline][current]:
        return "advance"
    return "invalid"


def cohort_week(at: datetime) -> date:
    local = at.astimezone(TZ).date()
    return local - timedelta(days=local.weekday())


def follow_up_date(at: datetime) -> date:
    return at.astimezone(TZ).date() + timedelta(days=FOLLOW_UP_DAYS)


def iso_week(d: date | None) -> str:
    if d is None:
        return "none"
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def is_outreach_sender(address: str) -> bool:
    """Spec §6.2: only these identities' sent mail counts as a brief. The list is env
    (OUTREACH_FROM_ADDRESSES, comma-separated) so the public repo holds no address."""
    wanted = {
        a.strip().lower()
        for a in os.environ.get("OUTREACH_FROM_ADDRESSES", "").split(",")
        if a.strip()
    }
    return (address or "").strip().lower() in wanted
```

`is_outreach_sender` is pure env logic and belongs with the rules; Task 7 adds its tests.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add models/outreach.py services/outreach.py tests/test_outreach.py
git commit -m "feat(outreach): stage model and transition rules"
```

---

### Task 3: Pipeline writes: repo, `advance`, opportunities, leads, and the outreach metrics

**Files:**
- Create: `repo/outreach.py`
- Modify: `services/outreach.py` (append the write functions)
- Modify: `clients/otel.py` (all outreach instruments, including the gauges later tasks feed)
- Test: `tests/test_outreach.py` (append), `tests/test_repo_outreach.py` (create), `tests/test_outreach_db.py` (append)

**Interfaces:**
- Consumes: Task 2's rules, exceptions and `models.outreach` types.
- Produces:
  - `repo.outreach`: `insert_contact_if_missing(conn, person_id) -> dict | None`, `get_contact(conn, person_id) -> dict | None`, `lock_contact(conn, person_id) -> dict | None`, `update_contact(conn, person_id, changes: dict) -> dict`, `insert_opportunity(conn, *, referrer_id, buyer_id, buyer_label, at) -> dict`, `lock_opportunity(conn, opportunity_id) -> dict | None`, `update_opportunity(conn, opportunity_id, changes: dict) -> dict`, `insert_event(conn, *, pipeline, stage, at, source, person_id=None, opportunity_id=None, channel=None, graph_message_id=None, from_address=None) -> int`.
  - `services.outreach`: `get_contact(conn, person_id) -> dict | None`, `ensure_contact(conn, person_id, *, at, source) -> int | None` (event id when it inserted), `advance(conn, person_id, stage, *, at, source, channel=None, graph_message_id=None, from_address=None, force=False) -> Advance | None`, `advance_from_event(conn, person_id, stage, *, at, graph_message_id) -> int | None`, `open_opportunity(conn, referrer_id, buyer_label, *, at, buyer_id=None, channel=None, source="cli") -> Advance`, `advance_opportunity(conn, opportunity_id, stage, *, at, lost_reason=None, source="cli") -> Advance`, `set_lead(conn, person_id, lead, *, today) -> dict`.
  - `clients.otel`: counters `outreach_stage_events`, `outreach_lost`, `outreach_sync`, `outreach_report_runs`; `set_outreach_population(rows: list[tuple[int, dict[str, str]]], follow_ups_due: int) -> None`; observable gauges `people.outreach_stage_population` and `people.outreach_follow_ups_due` that read what `set_outreach_population` stored.

- [ ] **Step 1: Write the failing service tests**

Add `from models.outreach import Advance` to the imports at the top of `tests/test_outreach.py` (ruff rejects imports below code), then append:

```python
TS = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


class FakeRepo:
    """In-memory stand-in for repo/outreach.py: same function names and shapes."""

    def __init__(self):
        self.contacts: dict[int, dict] = {}
        self.opps: dict[int, dict] = {}
        self.events: list[dict] = []

    def insert_contact_if_missing(self, conn, person_id):
        if person_id in self.contacts:
            return None
        self.contacts[person_id] = {
            "person_id": person_id,
            "stage": "candidate",
            "cohort_week": None,
            "briefed_at": None,
            "replied_at": None,
            "channel": None,
            "has_lead": False,
            "follow_up_on": None,
            "notes": None,
        }
        return dict(self.contacts[person_id])

    def get_contact(self, conn, person_id):
        row = self.contacts.get(person_id)
        return dict(row) if row else None

    lock_contact = get_contact

    def update_contact(self, conn, person_id, changes):
        self.contacts[person_id].update(changes)
        return dict(self.contacts[person_id])

    def insert_opportunity(self, conn, *, referrer_id, buyer_id, buyer_label, at):
        oid = len(self.opps) + 1
        self.opps[oid] = {
            "id": oid,
            "referrer_id": referrer_id,
            "buyer_id": buyer_id,
            "buyer_label": buyer_label,
            "stage": "intro",
            "lost_reason": None,
            "intro_at": at,
            "conversation_at": None,
            "proposal_at": None,
            "signed_at": None,
            "lost_at": None,
        }
        return dict(self.opps[oid])

    def lock_opportunity(self, conn, opportunity_id):
        row = self.opps.get(opportunity_id)
        return dict(row) if row else None

    def update_opportunity(self, conn, opportunity_id, changes):
        self.opps[opportunity_id].update(changes)
        return dict(self.opps[opportunity_id])

    def insert_event(self, conn, **kw):
        self.events.append(kw)
        return len(self.events)


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, n, attrs=None):
        self.calls.append((n, attrs))


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(svc, "outreach_repo", fake)
    for name in ("outreach_stage_events", "outreach_lost"):
        monkeypatch.setattr(svc.otel, name, Recorder())
    return fake


def _at(repo, person_id, stage):
    repo.insert_contact_if_missing(None, person_id)
    repo.contacts[person_id]["stage"] = stage


def test_advance_without_a_pipeline_row_returns_none(repo):
    assert svc.advance(None, 1, "briefed", at=TS, source="event", channel="email") is None
    assert repo.events == []


def test_briefed_sets_cohort_first_brief_and_follow_up(repo):
    _at(repo, 1, "to_contact")
    res = svc.advance(None, 1, "briefed", at=TS, source="event", channel="email", graph_message_id="g1")
    assert isinstance(res, Advance) and res.changed and res.event_id == 1
    row = repo.contacts[1]
    assert row["stage"] == "briefed"
    assert row["cohort_week"] == date(2026, 10, 12)
    assert row["briefed_at"] == TS and row["channel"] == "email"
    assert row["follow_up_on"] == date(2026, 10, 20)
    assert repo.events[0] == {
        "pipeline": "referrer",
        "person_id": 1,
        "stage": "briefed",
        "at": TS,
        "channel": "email",
        "source": "event",
        "graph_message_id": "g1",
        "from_address": None,
    }
    assert svc.otel.outreach_stage_events.calls == [
        (1, {"pipeline": "referrer", "stage": "briefed", "channel": "email", "source": "event"})
    ]


def test_advance_twice_appends_one_event(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "briefed", at=TS, source="event", channel="email")
    again = svc.advance(None, 1, "briefed", at=TS, source="event", channel="email")
    assert again is not None and not again.changed and again.event_id is None
    assert len(repo.events) == 1


def test_replied_clears_follow_up_and_records_first_reply(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "briefed", at=TS, source="event", channel="email")
    svc.advance(None, 1, "replied", at=TS, source="event", channel="email")
    assert repo.contacts[1]["follow_up_on"] is None
    assert repo.contacts[1]["replied_at"] == TS


def test_a_rebrief_after_reopen_keeps_the_first_cohort(repo):
    _at(repo, 1, "to_contact")
    svc.advance(None, 1, "briefed", at=TS, source="cli", channel="imessage")
    svc.advance(None, 1, "declined", at=TS, source="cli")
    later = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    svc.advance(None, 1, "to_contact", at=later, source="cli", force=True)
    svc.advance(None, 1, "briefed", at=later, source="cli", channel="email")
    row = repo.contacts[1]
    assert row["cohort_week"] == date(2026, 10, 12) and row["channel"] == "imessage"
    assert row["follow_up_on"] == date(2026, 11, 9)


def test_invalid_move_raises_and_writes_nothing(repo):
    _at(repo, 1, "candidate")
    with pytest.raises(svc.InvalidTransition):
        svc.advance(None, 1, "briefed", at=TS, source="cli", channel="email")
    assert repo.events == [] and repo.contacts[1]["stage"] == "candidate"


@pytest.mark.parametrize("stage", ["candidate", "to_contact", "declined", "skipped"])
def test_advance_from_event_ignores_disallowed_moves(repo, stage):
    _at(repo, 1, stage)
    assert svc.advance_from_event(None, 1, "replied", at=TS, graph_message_id="m") is None
    assert repo.events == []


def test_advance_from_event_returns_the_event_id(repo):
    _at(repo, 1, "briefed")
    assert svc.advance_from_event(None, 1, "replied", at=TS, graph_message_id="m") == 1
    assert repo.events[0]["source"] == "event" and repo.events[0]["channel"] == "email"


def test_ensure_contact_inserts_once_with_a_candidate_event(repo):
    assert svc.ensure_contact(None, 5, at=TS, source="seed") == 1
    assert svc.ensure_contact(None, 5, at=TS, source="seed") is None
    assert [e["stage"] for e in repo.events] == ["candidate"]


def test_open_opportunity_from_replied(repo):
    _at(repo, 1, "replied")
    res = svc.open_opportunity(None, 1, "  Buyer Example, Co  ", at=TS, buyer_id=9)
    assert res.row["buyer_label"] == "Buyer Example, Co" and res.row["buyer_id"] == 9
    assert repo.contacts[1]["stage"] == "introduced"
    assert [(e["pipeline"], e["stage"]) for e in repo.events] == [
        ("referrer", "introduced"),
        ("buyer", "intro"),
    ]
    assert res.event_id == 2


def test_open_opportunity_from_briefed_records_the_implied_reply(repo):
    _at(repo, 1, "briefed")
    svc.open_opportunity(None, 1, "Buyer, Co", at=TS)
    assert [e["stage"] for e in repo.events] == ["replied", "introduced", "intro"]


def test_a_second_intro_adds_an_opportunity_and_keeps_the_stage(repo):
    _at(repo, 1, "introduced")
    svc.open_opportunity(None, 1, "Second, Co", at=TS)
    assert len(repo.opps) == 1 and [e["stage"] for e in repo.events] == ["intro"]


@pytest.mark.parametrize("stage", ["candidate", "to_contact", "declined", "skipped"])
def test_open_opportunity_rejects_a_referrer_who_has_not_replied(repo, stage):
    _at(repo, 1, stage)
    with pytest.raises(svc.InvalidTransition):
        svc.open_opportunity(None, 1, "Buyer, Co", at=TS)
    assert repo.opps == {}


def test_open_opportunity_needs_a_pipeline_row_and_a_label(repo):
    with pytest.raises(svc.NotInPipeline):
        svc.open_opportunity(None, 1, "Buyer, Co", at=TS)
    _at(repo, 1, "replied")
    with pytest.raises(ValueError):
        svc.open_opportunity(None, 1, "   ", at=TS)


def test_advance_opportunity_sets_the_stage_timestamp(repo):
    _at(repo, 1, "replied")
    opp = svc.open_opportunity(None, 1, "Buyer, Co", at=TS).row
    res = svc.advance_opportunity(None, opp["id"], "conversation", at=TS)
    assert res.changed and res.row["conversation_at"] == TS
    assert repo.events[-1]["opportunity_id"] == opp["id"] and repo.events[-1]["person_id"] is None


def test_lost_needs_a_known_reason_and_is_counted(repo):
    _at(repo, 1, "replied")
    oid = svc.open_opportunity(None, 1, "Buyer, Co", at=TS).row["id"]
    with pytest.raises(ValueError):
        svc.advance_opportunity(None, oid, "lost", at=TS)
    with pytest.raises(ValueError):
        svc.advance_opportunity(None, oid, "lost", at=TS, lost_reason="vibes")
    with pytest.raises(ValueError):
        svc.advance_opportunity(None, oid, "conversation", at=TS, lost_reason="price")
    res = svc.advance_opportunity(None, oid, "lost", at=TS, lost_reason="timing")
    assert res.row["lost_reason"] == "timing" and res.row["lost_at"] == TS
    assert svc.otel.outreach_lost.calls == [(1, {"lost_reason": "timing"})]


def test_advance_opportunity_unknown_id(repo):
    with pytest.raises(svc.OpportunityNotFound):
        svc.advance_opportunity(None, 99, "conversation", at=TS)


def test_set_lead_appends_to_notes(repo):
    _at(repo, 1, "replied")
    svc.set_lead(None, 1, "Pat Example, Co", today=date(2026, 10, 14))
    svc.set_lead(None, 1, "Sam Example, Co", today=date(2026, 10, 15))
    row = repo.contacts[1]
    assert row["has_lead"] is True
    assert row["notes"] == "2026-10-14 lead: Pat Example, Co\n2026-10-15 lead: Sam Example, Co"
    with pytest.raises(svc.NotInPipeline):
        svc.set_lead(None, 2, "x", today=date(2026, 10, 14))
```

- [ ] **Step 2: Write the failing repo tests**

Create `tests/test_repo_outreach.py`:

```python
from datetime import UTC, datetime

import pytest

from repo import outreach


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple | None]] = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return FakeCursor(self._results.pop(0) if self._results else [])


TS = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


def test_lock_contact_locks_the_row():
    conn = FakeConn([[{"person_id": 1, "stage": "briefed"}]])
    assert outreach.lock_contact(conn, 1)["stage"] == "briefed"
    sql, params = conn.calls[0]
    assert sql.endswith("FOR UPDATE") and params == (1,)


def test_update_contact_only_touches_allowlisted_columns():
    conn = FakeConn([[{"person_id": 1}]])
    outreach.update_contact(conn, 1, {"stage": "briefed", "follow_up_on": None})
    sql, params = conn.calls[0]
    assert "SET stage = %s, follow_up_on = %s, updated_at = now()" in sql
    assert params == ("briefed", None, 1)
    with pytest.raises(ValueError):
        outreach.update_contact(conn, 1, {"person_id": 2})


def test_insert_event_returns_the_new_id():
    conn = FakeConn([[{"id": 42}]])
    eid = outreach.insert_event(
        conn, pipeline="referrer", person_id=1, stage="briefed", at=TS, source="cli", channel="email"
    )
    assert eid == 42
    sql, params = conn.calls[0]
    assert sql.startswith("INSERT INTO outreach_events") and "RETURNING id" in sql
    assert params == ("referrer", 1, None, "briefed", TS, "email", "cli", None, None)
```

- [ ] **Step 3: Write the failing real-DB test**

Add `from repo import outreach as outreach_repo` to the imports at the top of `tests/test_outreach_db.py`, then append:

```python
def test_repo_round_trip(conn):
    pid = _person(conn)
    assert outreach_repo.insert_contact_if_missing(conn, pid)["stage"] == "candidate"
    assert outreach_repo.insert_contact_if_missing(conn, pid) is None
    row = outreach_repo.update_contact(conn, pid, {"stage": "to_contact"})
    assert row["stage"] == "to_contact" and outreach_repo.lock_contact(conn, pid)["stage"] == "to_contact"
    opp = outreach_repo.insert_opportunity(conn, referrer_id=pid, buyer_id=None, buyer_label="B, Co", at=TS)
    assert opp["stage"] == "intro" and opp["intro_at"] == TS
    eid = outreach_repo.insert_event(
        conn, pipeline="buyer", opportunity_id=opp["id"], stage="intro", at=TS, source="cli"
    )
    assert isinstance(eid, int)
    lost = outreach_repo.update_opportunity(
        conn, opp["id"], {"stage": "lost", "lost_reason": "price", "lost_at": TS}
    )
    assert lost["lost_reason"] == "price"
```

- [ ] **Step 4: Run all three to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_repo_outreach.py tests/test_outreach_db.py -v`
Expected: FAIL (`AttributeError: module 'services.outreach' has no attribute 'outreach_repo'`, `ImportError` for `repo.outreach`).

- [ ] **Step 5: Create `repo/outreach.py`**

```python
"""Every SQL statement on the outreach_* tables (outreach spec §5). Takes an open
connection; never opens one, never commits, and decides nothing: whether a stage
change is allowed is services/outreach.py's call."""

from datetime import datetime
from typing import Any

_CONTACT_COLUMNS = """
    person_id, stage, cohort_week, briefed_at, replied_at, channel, has_lead,
    follow_up_on, notes, hubspot_synced_at, created_at, updated_at
"""
_OPPORTUNITY_COLUMNS = """
    id, referrer_id, buyer_id, buyer_label, stage, lost_reason, intro_at,
    conversation_at, proposal_at, signed_at, lost_at, hubspot_deal_id, notes,
    created_at, updated_at
"""
_CONTACT_UPDATABLE = frozenset(
    {"stage", "cohort_week", "briefed_at", "replied_at", "channel", "has_lead", "follow_up_on", "notes"}
)
_OPPORTUNITY_UPDATABLE = frozenset(
    {"stage", "lost_reason", "conversation_at", "proposal_at", "signed_at", "lost_at", "buyer_id", "notes"}
)


def _set_clause(changes: dict, allowed: frozenset[str]) -> str:
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ValueError(f"not updatable: {sorted(unknown) or 'nothing to update'}")
    return ", ".join(f"{column} = %s" for column in changes) + ", updated_at = now()"


def insert_contact_if_missing(conn: Any, person_id: int) -> dict | None:
    """The new row at `candidate`, or None when the person already has one (never downgrades)."""
    return conn.execute(
        f"""
        INSERT INTO outreach_contacts (person_id, stage) VALUES (%s, 'candidate')
        ON CONFLICT (person_id) DO NOTHING
        RETURNING {_CONTACT_COLUMNS}
        """,
        (person_id,),
    ).fetchone()


def get_contact(conn: Any, person_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM outreach_contacts WHERE person_id = %s", (person_id,)
    ).fetchone()


def lock_contact(conn: Any, person_id: int) -> dict | None:
    """Row-locked read: two deliveries of one event cannot both see the old stage."""
    return conn.execute(
        f"SELECT {_CONTACT_COLUMNS} FROM outreach_contacts WHERE person_id = %s FOR UPDATE",
        (person_id,),
    ).fetchone()


def update_contact(conn: Any, person_id: int, changes: dict) -> dict:
    return conn.execute(
        f"""
        UPDATE outreach_contacts SET {_set_clause(changes, _CONTACT_UPDATABLE)}
        WHERE person_id = %s RETURNING {_CONTACT_COLUMNS}
        """,
        (*changes.values(), person_id),
    ).fetchone()


def insert_opportunity(
    conn: Any, *, referrer_id: int, buyer_id: int | None, buyer_label: str, at: datetime
) -> dict:
    return conn.execute(
        f"""
        INSERT INTO outreach_opportunities (referrer_id, buyer_id, buyer_label, stage, intro_at)
        VALUES (%s, %s, %s, 'intro', %s)
        RETURNING {_OPPORTUNITY_COLUMNS}
        """,
        (referrer_id, buyer_id, buyer_label, at),
    ).fetchone()


def lock_opportunity(conn: Any, opportunity_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {_OPPORTUNITY_COLUMNS} FROM outreach_opportunities WHERE id = %s FOR UPDATE",
        (opportunity_id,),
    ).fetchone()


def update_opportunity(conn: Any, opportunity_id: int, changes: dict) -> dict:
    return conn.execute(
        f"""
        UPDATE outreach_opportunities SET {_set_clause(changes, _OPPORTUNITY_UPDATABLE)}
        WHERE id = %s RETURNING {_OPPORTUNITY_COLUMNS}
        """,
        (*changes.values(), opportunity_id),
    ).fetchone()


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
) -> int:
    row = conn.execute(
        """
        INSERT INTO outreach_events
            (pipeline, person_id, opportunity_id, stage, at, channel, source,
             graph_message_id, from_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (pipeline, person_id, opportunity_id, stage, at, channel, source, graph_message_id, from_address),
    ).fetchone()
    return int(row["id"])
```

- [ ] **Step 6: Add the outreach instruments to `clients/otel.py`**

At the top, beside the other imports, change the metrics import to also bring in the callback types:

```python
from opentelemetry.metrics import CallbackOptions, Observation
```

Beside the existing no-op instruments, add:

```python
outreach_stage_events: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_lost: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_sync: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_report_runs: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")

# Observable gauges read these at export time (each flush). services/outreach.py's
# record_population() refreshes them from the DB on every people-sync run and every
# outreach CLI invocation (outreach spec §12.2).
_outreach_population: list[tuple[int, dict[str, str]]] = []
_outreach_follow_ups_due: int | None = None


def set_outreach_population(rows: list[tuple[int, dict[str, str]]], follow_ups_due: int) -> None:
    global _outreach_population, _outreach_follow_ups_due
    _outreach_population = list(rows)
    _outreach_follow_ups_due = follow_ups_due


def _observe_population(options: CallbackOptions) -> list[Observation]:
    return [Observation(value, attrs) for value, attrs in _outreach_population]


def _observe_follow_ups_due(options: CallbackOptions) -> list[Observation]:
    return [] if _outreach_follow_ups_due is None else [Observation(_outreach_follow_ups_due)]
```

In `setup_telemetry`, add the four counters to the `global` statement and, after the WhatsApp instruments, create them:

```python
    outreach_stage_events = meter.create_counter(
        "people.outreach_stage_events",
        description="Outreach stage events by pipeline/stage/channel/source",
    )
    outreach_lost = meter.create_counter(
        "people.outreach_lost", description="Buyer opportunities lost, by lost_reason"
    )
    outreach_sync = meter.create_counter(
        "people.outreach_sync", description="Outreach sync attempts by target/outcome/late"
    )
    outreach_report_runs = meter.create_counter(
        "people.outreach_report_runs", description="outreach report runs by stages below floor"
    )
    meter.create_observable_gauge(
        "people.outreach_stage_population",
        callbacks=[_observe_population],
        description="Outreach rows per pipeline/stage/cohort_week",
    )
    meter.create_observable_gauge(
        "people.outreach_follow_ups_due",
        callbacks=[_observe_follow_ups_due],
        description="Briefed contacts whose follow-up date has arrived",
    )
```

- [ ] **Step 7: Append the write functions to `services/outreach.py`**

Add these imports at the top of the file (keep the existing ones):

```python
from typing import Any

import clients.otel as otel
from models.outreach import LOST_REASONS, Advance
from repo import outreach as outreach_repo
```

Then append:

```python
def _count_event(pipeline: str, stage: str, channel: str | None, source: str) -> None:
    otel.outreach_stage_events.add(
        1, {"pipeline": pipeline, "stage": stage, "channel": channel or "none", "source": source}
    )


def get_contact(conn: Any, person_id: int) -> dict | None:
    return outreach_repo.get_contact(conn, person_id)


def ensure_contact(conn: Any, person_id: int, *, at: datetime, source: str) -> int | None:
    """Put a person in the pipeline at `candidate` (the seed, or `decide` for someone the
    seed never proposed). Returns the candidate event's id, or None if they were already in."""
    if outreach_repo.insert_contact_if_missing(conn, person_id) is None:
        return None
    _count_event("referrer", "candidate", None, source)
    return outreach_repo.insert_event(
        conn, pipeline="referrer", person_id=person_id, stage="candidate", at=at, source=source
    )


def advance(
    conn: Any,
    person_id: int,
    stage: str,
    *,
    at: datetime,
    source: str,
    channel: str | None = None,
    graph_message_id: str | None = None,
    from_address: str | None = None,
    force: bool = False,
) -> Advance | None:
    """Move a referrer to `stage` (spec §6.1). None when the person has no pipeline row;
    a no-op Advance when they are already at or past it; InvalidTransition otherwise."""
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        return None
    kind = transition("referrer", row["stage"], stage, force=force)
    if kind == "invalid":
        raise InvalidTransition("referrer", row["stage"], stage)
    if kind == "noop":
        return Advance(row=row, changed=False, event_id=None)

    changes: dict[str, Any] = {"stage": stage}
    if stage == "briefed":
        changes["follow_up_on"] = follow_up_date(at)
        if row["briefed_at"] is None:
            changes.update(briefed_at=at, cohort_week=cohort_week(at), channel=channel)
    else:
        changes["follow_up_on"] = None
    if stage == "replied" and row["replied_at"] is None:
        changes["replied_at"] = at

    updated = outreach_repo.update_contact(conn, person_id, changes)
    event_id = outreach_repo.insert_event(
        conn,
        pipeline="referrer",
        person_id=person_id,
        stage=stage,
        at=at,
        channel=channel,
        source=source,
        graph_message_id=graph_message_id,
        from_address=from_address,
    )
    _count_event("referrer", stage, channel, source)
    return Advance(row=updated, changed=True, event_id=event_id)


def advance_from_event(
    conn: Any, person_id: int, stage: str, *, at: datetime, graph_message_id: str | None
) -> int | None:
    """The event handlers' entry (spec §6.2, §6.3): a move outside the table and a person
    with no pipeline row are both ignored silently. Returns the new event's id, if any."""
    try:
        res = advance(
            conn,
            person_id,
            stage,
            at=at,
            channel="email",
            source="event",
            graph_message_id=graph_message_id,
        )
    except InvalidTransition:
        return None
    return res.event_id if res else None


def open_opportunity(
    conn: Any,
    referrer_id: int,
    buyer_label: str,
    *,
    at: datetime,
    buyer_id: int | None = None,
    channel: str | None = None,
    source: str = "cli",
) -> Advance:
    """An introduction with the buyer on the thread (spec §4.2). The referrer moves to
    `introduced`; from `briefed`, the implied `replied` is appended first. Advance.row is
    the new opportunity and Advance.event_id its `intro` event."""
    label = (buyer_label or "").strip()
    if not label:
        raise ValueError("a buyer label (\"Name, Company\") is required")
    contact = outreach_repo.lock_contact(conn, referrer_id)
    if contact is None:
        raise NotInPipeline(str(referrer_id))
    if contact["stage"] == "briefed":
        advance(conn, referrer_id, "replied", at=at, channel=channel, source=source)
    advance(conn, referrer_id, "introduced", at=at, channel=channel, source=source)
    opp = outreach_repo.insert_opportunity(
        conn, referrer_id=referrer_id, buyer_id=buyer_id, buyer_label=label, at=at
    )
    event_id = outreach_repo.insert_event(
        conn,
        pipeline="buyer",
        opportunity_id=opp["id"],
        stage="intro",
        at=at,
        channel=channel,
        source=source,
    )
    _count_event("buyer", "intro", channel, source)
    return Advance(row=opp, changed=True, event_id=event_id)


def advance_opportunity(
    conn: Any,
    opportunity_id: int,
    stage: str,
    *,
    at: datetime,
    lost_reason: str | None = None,
    source: str = "cli",
) -> Advance:
    if stage == "lost":
        if lost_reason not in LOST_REASONS:
            raise ValueError(f"lost needs a reason, one of: {', '.join(LOST_REASONS)}")
    elif lost_reason is not None:
        raise ValueError("a lost reason applies only to the lost stage")
    opp = outreach_repo.lock_opportunity(conn, opportunity_id)
    if opp is None:
        raise OpportunityNotFound(str(opportunity_id))
    kind = transition("buyer", opp["stage"], stage)
    if kind == "invalid":
        raise InvalidTransition("buyer", opp["stage"], stage)
    if kind == "noop":
        return Advance(row=opp, changed=False, event_id=None)
    changes: dict[str, Any] = {"stage": stage, f"{stage}_at": at}
    if stage == "lost":
        changes["lost_reason"] = lost_reason
    updated = outreach_repo.update_opportunity(conn, opportunity_id, changes)
    event_id = outreach_repo.insert_event(
        conn, pipeline="buyer", opportunity_id=opportunity_id, stage=stage, at=at, source=source
    )
    _count_event("buyer", stage, None, source)
    if stage == "lost":
        otel.outreach_lost.add(1, {"lost_reason": str(lost_reason)})
    return Advance(row=updated, changed=True, event_id=event_id)


def set_lead(conn: Any, person_id: int, lead: str, *, today: date) -> dict:
    """A name Ben must chase (spec §4.2): a flag and a note, never an opportunity."""
    row = outreach_repo.lock_contact(conn, person_id)
    if row is None:
        raise NotInPipeline(str(person_id))
    line = f"{today.isoformat()} lead: {lead.strip()}"
    notes = f"{row['notes']}\n{line}" if row.get("notes") else line
    return outreach_repo.update_contact(conn, person_id, {"has_lead": True, "notes": notes})
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_repo_outreach.py tests/test_outreach_db.py -v`
Expected: PASS.

- [ ] **Step 9: Run the full checks, then commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
git add repo/outreach.py services/outreach.py clients/otel.py tests/test_outreach.py tests/test_repo_outreach.py tests/test_outreach_db.py
git commit -m "feat(outreach): stage writes, opportunities, leads, outreach metrics"
```

---

### Task 4: Keep pipeline history when duplicate contacts are merged

`scripts/merge_duplicate_contacts.py::_collapse_rows` deletes the loser `people` row after re-pointing its iMessage, LinkedIn and WhatsApp links. The outreach tables `ON DELETE CASCADE`, so without this task a merge silently erases the loser's stage, events and opportunities.

**Files:**
- Modify: `repo/outreach.py`, `services/outreach.py`, `scripts/merge_duplicate_contacts.py`
- Test: `tests/test_outreach.py`, `tests/test_outreach_db.py`, `tests/test_merge_duplicate_contacts.py`

**Interfaces:**
- Consumes: `REFERRER_RANK`, `outreach_repo.lock_contact` (Tasks 2–3).
- Produces: `repo.outreach.repoint_events(conn, from_id, to_id) -> int`, `repoint_opportunities(conn, from_id, to_id) -> int`, `move_contact(conn, from_id, to_id) -> None`, `delete_contact(conn, person_id) -> None`; `services.outreach.merge_person(conn, from_id, to_id) -> int` (rows moved or resolved).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_outreach.py` (the `FakeRepo` from Task 3 needs four more methods; add them to the class):

```python
    # --- add inside class FakeRepo ---
    def repoint_events(self, conn, from_id, to_id):
        moved = [e for e in self.events if e.get("person_id") == from_id]
        for e in moved:
            e["person_id"] = to_id
        return len(moved)

    def repoint_opportunities(self, conn, from_id, to_id):
        n = 0
        for o in self.opps.values():
            for col in ("referrer_id", "buyer_id"):
                if o[col] == from_id:
                    o[col] = to_id
                    n += 1
        return n

    def move_contact(self, conn, from_id, to_id):
        row = self.contacts.pop(from_id)
        row["person_id"] = to_id
        self.contacts[to_id] = row

    def delete_contact(self, conn, person_id):
        self.contacts.pop(person_id, None)
```

```python
def test_merge_moves_a_loser_row_the_keeper_lacks(repo):
    _at(repo, 2, "briefed")
    repo.insert_event(None, pipeline="referrer", person_id=2, stage="briefed")
    assert svc.merge_person(None, 2, 1) == 2
    assert 2 not in repo.contacts and repo.contacts[1]["stage"] == "briefed"
    assert repo.events[0]["person_id"] == 1


def test_merge_keeps_the_further_row(repo):
    _at(repo, 1, "candidate")
    _at(repo, 2, "replied")
    svc.merge_person(None, 2, 1)
    assert repo.contacts[1]["stage"] == "replied" and 2 not in repo.contacts


def test_merge_keeps_the_keeper_on_a_tie_or_when_it_is_further(repo):
    _at(repo, 1, "introduced")
    _at(repo, 2, "briefed")
    svc.merge_person(None, 2, 1)
    assert repo.contacts[1]["stage"] == "introduced" and 2 not in repo.contacts


def test_merge_moves_opportunities_on_both_sides(repo):
    _at(repo, 2, "replied")
    svc.open_opportunity(None, 2, "B, Co", at=TS, buyer_id=2)
    svc.merge_person(None, 2, 1)
    assert repo.opps[1]["referrer_id"] == 1 and repo.opps[1]["buyer_id"] == 1


def test_merge_with_no_pipeline_rows_is_a_noop(repo):
    assert svc.merge_person(None, 2, 1) == 0
```

Append to `tests/test_outreach_db.py`:

```python
def test_repoint_then_delete_keeps_history(conn):
    keeper, loser = _person(conn), _person(conn, email="dup@example.com")
    outreach_repo.insert_contact_if_missing(conn, loser)
    outreach_repo.insert_event(conn, pipeline="referrer", person_id=loser, stage="candidate", at=TS, source="seed")
    outreach_repo.insert_opportunity(conn, referrer_id=loser, buyer_id=loser, buyer_label="B", at=TS)
    assert outreach_repo.repoint_events(conn, loser, keeper) == 1
    assert outreach_repo.repoint_opportunities(conn, loser, keeper) == 2
    outreach_repo.move_contact(conn, loser, keeper)
    conn.execute("DELETE FROM people WHERE id = %s", (loser,))
    assert outreach_repo.get_contact(conn, keeper) is not None
    assert conn.execute("SELECT COUNT(*) AS n FROM outreach_events WHERE person_id = %s", (keeper,)).fetchone()["n"] == 1
```

In `tests/test_merge_duplicate_contacts.py`, inside the `wired` fixture, after the `repoint_wa` stub is installed, add:

```python
    def merge_outreach(c, frm, to):
        state["order"].append("repoint_outreach")
        return 0

    monkeypatch.setattr(mdc.outreach, "merge_person", merge_outreach)
```

and change the assertion in `test_links_are_repointed_before_the_row_is_deleted` to:

```python
    assert wired["state"]["order"] == ["repoint", "repoint", "repoint_outreach", "delete_row"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_merge_duplicate_contacts.py tests/test_outreach_db.py -v`
Expected: FAIL (`merge_person` missing; `mdc` has no attribute `outreach`).

- [ ] **Step 3: Append to `repo/outreach.py`**

```python
def repoint_events(conn: Any, from_id: int, to_id: int) -> int:
    return conn.execute(
        "UPDATE outreach_events SET person_id = %s WHERE person_id = %s", (to_id, from_id)
    ).rowcount


def repoint_opportunities(conn: Any, from_id: int, to_id: int) -> int:
    referrer = conn.execute(
        "UPDATE outreach_opportunities SET referrer_id = %s, updated_at = now() WHERE referrer_id = %s",
        (to_id, from_id),
    ).rowcount
    buyer = conn.execute(
        "UPDATE outreach_opportunities SET buyer_id = %s, updated_at = now() WHERE buyer_id = %s",
        (to_id, from_id),
    ).rowcount
    return referrer + buyer


def move_contact(conn: Any, from_id: int, to_id: int) -> None:
    conn.execute(
        "UPDATE outreach_contacts SET person_id = %s, updated_at = now() WHERE person_id = %s",
        (to_id, from_id),
    )


def delete_contact(conn: Any, person_id: int) -> None:
    conn.execute("DELETE FROM outreach_contacts WHERE person_id = %s", (person_id,))
```

- [ ] **Step 4: Append to `services/outreach.py`**

```python
def merge_person(conn: Any, from_id: int, to_id: int) -> int:
    """Fold one person's pipeline into another's before the `from_id` people row is
    deleted (the outreach tables cascade). Events and opportunities all move. When both
    have a contact row, the further-along one survives; a tie keeps the keeper's."""
    moved = outreach_repo.repoint_events(conn, from_id, to_id)
    moved += outreach_repo.repoint_opportunities(conn, from_id, to_id)
    loser = outreach_repo.lock_contact(conn, from_id)
    if loser is None:
        return moved
    keeper = outreach_repo.lock_contact(conn, to_id)
    if keeper is None:
        outreach_repo.move_contact(conn, from_id, to_id)
    elif REFERRER_RANK[loser["stage"]] > REFERRER_RANK[keeper["stage"]]:
        outreach_repo.delete_contact(conn, to_id)
        outreach_repo.move_contact(conn, from_id, to_id)
    else:
        outreach_repo.delete_contact(conn, from_id)
    return moved + 1
```

- [ ] **Step 5: Call it from `scripts/merge_duplicate_contacts.py`**

Add `from services import outreach` beside the existing `services` imports. In `_collapse_rows`, after the WhatsApp re-point and before `people_repo.delete(conn, row["id"])`, add:

```python
        # outreach_* rows are ON DELETE CASCADE, not SET NULL: deleting first would
        # erase the loser's referral stage, events and opportunities outright.
        counts["links_repointed"] += outreach.merge_person(conn, row["id"], keeper["id"])
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py tests/test_merge_duplicate_contacts.py tests/test_outreach_db.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full checks, then commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
git add repo/outreach.py services/outreach.py scripts/merge_duplicate_contacts.py tests/test_outreach.py tests/test_outreach_db.py tests/test_merge_duplicate_contacts.py
git commit -m "fix(merge): carry referral pipeline rows to the surviving person"
```

---

### Task 5: Clients for tasks-api and inbox-api

**Files:**
- Create: `clients/gcp_auth.py`, `clients/tasks_api.py`, `clients/inbox_api.py`
- Modify: `requirements.txt` (add `httpx>=0.27`; it is already in `requirements-dev.txt`)
- Test: `tests/test_gcp_auth.py`, `tests/test_tasks_api.py`, `tests/test_inbox_api.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `clients.gcp_auth.id_token_for(audience: str) -> str`, `reset_cache() -> None`.
  - `clients.tasks_api.configured() -> bool`, `create_task(name: str, tags: list[str], context: str) -> str` (task gid), `complete_task(gid: str) -> None`, `TasksApiUnavailable(RuntimeError)`. Both calls raise on any non-2xx (`httpx.HTTPStatusError`).
  - `clients.inbox_api.create_draft(to: list[str], subject: str, body: str = "") -> dict` (`{"id", "web_link"}`), `InboxApiUnavailable(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_gcp_auth.py`:

```python
import base64
import json
import time

from clients import gcp_auth


def _token(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_tokens_are_cached_per_audience_until_near_expiry(monkeypatch):
    gcp_auth.reset_cache()
    minted = []

    def mint(aud):
        minted.append(aud)
        return _token(time.time() + 3600)

    monkeypatch.setattr(gcp_auth, "_mint", mint)
    gcp_auth.id_token_for("https://a")
    gcp_auth.id_token_for("https://a")
    gcp_auth.id_token_for("https://b")
    assert minted == ["https://a", "https://b"]


def test_a_token_inside_the_refresh_margin_is_reminted(monkeypatch):
    gcp_auth.reset_cache()
    tokens = iter([_token(time.time() + 60), _token(time.time() + 3600)])
    monkeypatch.setattr(gcp_auth, "_mint", lambda aud: next(tokens))
    first = gcp_auth.id_token_for("https://a")
    assert gcp_auth.id_token_for("https://a") != first
```

`tests/test_tasks_api.py`:

```python
import httpx
import pytest

from clients import tasks_api


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TASKS_API_URL", "https://tasks.example.test/")
    monkeypatch.setattr(tasks_api.gcp_auth, "id_token_for", lambda aud: f"tok-for-{aud}")


def test_create_task_posts_name_tags_context_and_nothing_else(monkeypatch, env):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return Resp(201, {"task_gid": "123", "permalink_url": "x"})

    monkeypatch.setattr(tasks_api.httpx, "post", post)
    gid = tasks_api.create_task("Brief: A", ["outreach", "serves:consulting"], "ctx")
    assert gid == "123"
    assert seen["url"] == "https://tasks.example.test/tasks"
    assert seen["json"] == {"name": "Brief: A", "tags": ["outreach", "serves:consulting"], "context": "ctx"}
    # The audience is the base URL without the trailing slash: tasks-api's custom audience.
    assert seen["headers"] == {"Authorization": "Bearer tok-for-https://tasks.example.test"}


def test_complete_task_patches_completed_true(monkeypatch, env):
    seen = {}
    monkeypatch.setattr(
        tasks_api.httpx, "patch", lambda url, json, headers, timeout: seen.update(url=url, json=json) or Resp()
    )
    tasks_api.complete_task("123")
    assert seen == {"url": "https://tasks.example.test/tasks/123", "json": {"completed": True}}


def test_errors_raise(monkeypatch, env):
    monkeypatch.setattr(tasks_api.httpx, "post", lambda *a, **k: Resp(502))
    with pytest.raises(httpx.HTTPStatusError):
        tasks_api.create_task("n", [], "c")


def test_unconfigured(monkeypatch):
    monkeypatch.delenv("TASKS_API_URL", raising=False)
    assert tasks_api.configured() is False
    with pytest.raises(tasks_api.TasksApiUnavailable):
        tasks_api.create_task("n", [], "c")
```

`tests/test_inbox_api.py`:

```python
from clients import inbox_api


def test_create_draft_never_sends_a_from_block(monkeypatch):
    monkeypatch.setenv("INBOX_API_URL", "https://inbox.example.test")
    monkeypatch.setattr(inbox_api.gcp_auth, "id_token_for", lambda aud: "tok")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "d1", "web_link": "https://outlook.example.test/d1"}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return Resp()

    monkeypatch.setattr(inbox_api.httpx, "post", post)
    out = inbox_api.create_draft(["alice@example.com"], subject="")
    assert out["id"] == "d1"
    assert seen["url"] == "https://inbox.example.test/emails/drafts"
    assert seen["json"] == {"to": ["alice@example.com"], "subject": "", "body": ""}
    assert "from" not in seen["json"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_gcp_auth.py tests/test_tasks_api.py tests/test_inbox_api.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Create `clients/gcp_auth.py`** (ported from the tasks repo's `clients/gcp_auth.py`, unchanged in behaviour)

```python
"""Google-signed ID tokens for calling other Cloud Run APIs (tasks-api, inbox-api).

On Cloud Run / Cloud Functions (K_SERVICE set) the metadata server mints a token for
this workload's own service account. Off it, gcloud's user credential is used, so the
local CLI hits the real APIs as Ben. Tokens are cached per audience and refreshed five
minutes before `exp`; ID tokens last an hour and the metadata server rate-limits."""

import base64
import json
import os
import subprocess
import time

REFRESH_MARGIN_S = 300
_cache: dict[str, tuple[str, float]] = {}


def _exp(token: str) -> float:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])


def _mint(audience: str) -> str:
    if os.environ.get("K_SERVICE"):
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(request, audience)
    return subprocess.check_output(["gcloud", "auth", "print-identity-token"], text=True).strip()


def id_token_for(audience: str) -> str:
    """A bearer token accepted by the Cloud Run service whose URL is `audience`."""
    cached = _cache.get(audience)
    if cached and cached[1] - time.time() > REFRESH_MARGIN_S:
        return cached[0]
    token = _mint(audience)
    _cache[audience] = (token, _exp(token))
    return token


def reset_cache() -> None:
    _cache.clear()
```

`google.auth.transport.requests` needs the `requests` package at runtime in the Cloud Function. Confirm it is installed there before relying on it: `requirements.txt` lists `requests>=2.31`, so it is. Keep that line.

- [ ] **Step 4: Create `clients/tasks_api.py`**

```python
"""tasks-api (Cloud Run, IAM-authenticated): one completed Asana task per countable
outreach stage event (outreach spec §8). Every call raises on failure;
services/outreach_sync.py owns the retry policy. The URL is read per call so tests and
the CLI can set it late; it is also the ID-token audience (tasks-api's custom audience)."""

import os

import httpx

from clients import gcp_auth

TIMEOUT_S = 20


class TasksApiUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("TASKS_API_URL", "").strip())


def _base() -> str:
    url = os.environ.get("TASKS_API_URL", "").strip().rstrip("/")
    if not url:
        raise TasksApiUnavailable("TASKS_API_URL unset")
    return url


def _headers(base: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"}


def create_task(name: str, tags: list[str], context: str) -> str:
    """POST /tasks into tasks-api's default project; no priority, no assignee."""
    base = _base()
    resp = httpx.post(
        f"{base}/tasks",
        json={"name": name, "tags": tags, "context": context},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return str(resp.json()["task_gid"])


def complete_task(gid: str) -> None:
    """A second call because POST /tasks has no `completed` field (spec §8)."""
    base = _base()
    resp = httpx.patch(
        f"{base}/tasks/{gid}",
        json={"completed": True},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
```

- [ ] **Step 5: Create `clients/inbox_api.py`**

```python
"""inbox-api (Cloud Run, IAM-authenticated), used only by `outreach draft` (outreach
spec §7.3). People never talks to Microsoft Graph from its Cloud Functions; inbox-api is
the mailbox gateway. Drafts carry no `from` block, so they come from the primary
mailbox, which is the consulting address, the only identity whose sent mail counts as
a brief."""

import os

import httpx

from clients import gcp_auth

TIMEOUT_S = 20


class InboxApiUnavailable(RuntimeError):
    pass


def _base() -> str:
    url = os.environ.get("INBOX_API_URL", "").strip().rstrip("/")
    if not url:
        raise InboxApiUnavailable("INBOX_API_URL unset")
    return url


def create_draft(to: list[str], subject: str, body: str = "") -> dict:
    base = _base()
    resp = httpx.post(
        f"{base}/emails/drafts",
        json={"to": to, "subject": subject, "body": body},
        headers={"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()
```

- [ ] **Step 6: Add `httpx>=0.27` to `requirements.txt`** under a new comment line `# Cloud Run callers (tasks-api, inbox-api)`.

- [ ] **Step 7: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_gcp_auth.py tests/test_tasks_api.py tests/test_inbox_api.py -v`
Expected: PASS. Then run the full checks from Global Constraints.

- [ ] **Step 8: Commit**

```bash
git add clients/gcp_auth.py clients/tasks_api.py clients/inbox_api.py requirements.txt tests/test_gcp_auth.py tests/test_tasks_api.py tests/test_inbox_api.py
git commit -m "feat(clients): tasks-api and inbox-api over Cloud Run IAM"
```

---

### Task 6: Asana sync, one completed task per countable stage event

**Files:**
- Modify: `repo/outreach.py` (sync queries)
- Create: `services/outreach_sync.py`
- Test: `tests/test_outreach_sync.py`, `tests/test_outreach_db.py` (append)

**Interfaces:**
- Consumes: `clients.tasks_api` (Task 5), `clients.otel.outreach_sync` and `external_errors`, `services.outreach.TZ`.
- Produces:
  - `repo.outreach.sync_rows(conn, keys: list[str], *, event_id: int | None = None, limit: int = 200) -> list[dict]` (rows with `id, pipeline, stage, at, channel, asana_task_gid, asana_completed_at, buyer_label, person_name`), `set_asana_gid(conn, event_id, gid)`, `set_asana_completed(conn, event_id)`.
  - `services.outreach_sync`: `TASKS: dict[tuple[str, str], tuple[str, str]]`, `GOAL_TAG = "serves:consulting"`, `produces_task(pipeline, stage) -> bool`, `task_payload(ev: dict) -> dict`, `late_bucket(at, now) -> str`, `SyncResult(synced=0, failed=0, skipped=False, late: dict[str, int])`, `sync_events(conn, event_ids: list[int], *, now=None) -> SyncResult`, `sync_pending(conn, *, now=None, limit=200) -> SyncResult`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outreach_sync.py`:

```python
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
        (ev(pipeline="buyer", stage="conversation", buyer_label="Bo, Co"), "Conversation: Bo, Co", ["conversation", "serves:consulting"]),
        (ev(pipeline="buyer", stage="proposal", buyer_label="Bo, Co"), "Proposal: Bo, Co", ["proposal", "serves:consulting"]),
        (ev(pipeline="buyer", stage="signed", buyer_label="Bo, Co"), "Signed: Bo, Co", ["signed-client", "serves:consulting"]),
    ],
)
def test_payload_per_event_type(event, name, tags):
    p = sync.task_payload(event)
    assert p["name"] == name and p["tags"] == tags
    assert "2026-10-20" in p["context"] and "outreach event 7" in p["context"]


def test_only_the_spec_events_produce_tasks():
    assert sync.produces_task("referrer", "briefed")
    for pipeline, stage in [("referrer", "replied"), ("referrer", "declined"), ("buyer", "lost"), ("referrer", "candidate")]:
        assert not sync.produces_task(pipeline, stage)


@pytest.mark.parametrize(
    "age,bucket", [(timedelta(hours=3), "0d"), (timedelta(hours=30), "1d"), (timedelta(days=5), "2-7d"), (timedelta(days=9), "8d+")]
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
            r for r in self.rows.values()
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
```

Append to `tests/test_outreach_db.py`:

```python
def test_sync_rows_finds_only_unsynced_task_events(conn):
    pid = _person(conn, display_name="Alice Example")
    outreach_repo.insert_contact_if_missing(conn, pid)
    briefed = outreach_repo.insert_event(conn, pipeline="referrer", person_id=pid, stage="briefed", at=TS, source="cli", channel="email")
    outreach_repo.insert_event(conn, pipeline="referrer", person_id=pid, stage="replied", at=TS, source="cli")
    opp = outreach_repo.insert_opportunity(conn, referrer_id=pid, buyer_id=None, buyer_label="Bo, Co", at=TS)
    intro = outreach_repo.insert_event(conn, pipeline="buyer", opportunity_id=opp["id"], stage="intro", at=TS, source="cli")
    keys = ["referrer:briefed", "buyer:intro"]
    rows = outreach_repo.sync_rows(conn, keys)
    assert [r["id"] for r in rows] == [briefed, intro]
    assert rows[1]["person_name"] == "Alice Example" and rows[1]["buyer_label"] == "Bo, Co"
    outreach_repo.set_asana_gid(conn, briefed, "g1")
    outreach_repo.set_asana_completed(conn, briefed)
    assert [r["id"] for r in outreach_repo.sync_rows(conn, keys)] == [intro]
    assert [r["id"] for r in outreach_repo.sync_rows(conn, keys, event_id=intro)] == [intro]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_sync.py tests/test_outreach_db.py -v`
Expected: FAIL with `ImportError` / `AttributeError: sync_rows`.

- [ ] **Step 3: Append the sync queries to `repo/outreach.py`**

```python
def sync_rows(
    conn: Any, keys: list[str], *, event_id: int | None = None, limit: int = 200
) -> list[dict]:
    """Events of the kinds in `keys` ("pipeline:stage") whose Asana task is missing or
    not yet completed, oldest first, with what the task name needs. Pass one event_id at
    a time: an all-int list param is mangled by clients/db.py's pgvector shim."""
    by_id = "AND e.id = %s" if event_id is not None else ""
    params: tuple = (keys, event_id, limit) if event_id is not None else (keys, limit)
    return conn.execute(
        f"""
        SELECT e.id, e.pipeline, e.stage, e.at, e.channel, e.asana_task_gid,
               e.asana_completed_at, o.buyer_label,
               COALESCE(p.display_name, p.email, 'person ' || p.id::text) AS person_name
        FROM outreach_events e
        LEFT JOIN outreach_opportunities o ON o.id = e.opportunity_id
        LEFT JOIN people p ON p.id = COALESCE(e.person_id, o.referrer_id)
        WHERE (e.asana_task_gid IS NULL OR e.asana_completed_at IS NULL)
          AND (e.pipeline || ':' || e.stage) = ANY(%s::text[])
          {by_id}
        ORDER BY e.id
        LIMIT %s
        """,
        params,
    ).fetchall()


def set_asana_gid(conn: Any, event_id: int, gid: str) -> None:
    conn.execute("UPDATE outreach_events SET asana_task_gid = %s WHERE id = %s", (gid, event_id))


def set_asana_completed(conn: Any, event_id: int) -> None:
    conn.execute("UPDATE outreach_events SET asana_completed_at = now() WHERE id = %s", (event_id,))
```

- [ ] **Step 4: Create `services/outreach_sync.py`**

```python
"""Strategy sync: one completed Asana task per countable stage event (outreach spec §8).
Best-effort and never on the durable path: the event row is already committed, a failure
leaves it pending, and `outreach sync-asana` and the nightly people-sync retry it. Like
hubspot_mirror.reconcile, this commits its own progress after each external write, so a
crash between the create and the complete never makes a second task."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

import clients.otel as otel
from clients import tasks_api
from repo import outreach as outreach_repo
from services.outreach import TZ

logger = logging.getLogger(__name__)

GOAL_TAG = "serves:consulting"
# (pipeline, stage) -> (lead/tripwire tag, task-name prefix). Spec §8's table, verbatim.
TASKS: dict[tuple[str, str], tuple[str, str]] = {
    ("referrer", "briefed"): ("outreach", "Brief"),
    ("buyer", "intro"): ("intro", "Intro"),
    ("buyer", "conversation"): ("conversation", "Conversation"),
    ("buyer", "proposal"): ("proposal", "Proposal"),
    ("buyer", "signed"): ("signed-client", "Signed"),
}
_KEYS = [f"{pipeline}:{stage}" for pipeline, stage in TASKS]


@dataclass
class SyncResult:
    synced: int = 0
    failed: int = 0
    skipped: bool = False  # TASKS_API_URL unset: nothing attempted
    late: dict[str, int] = field(default_factory=dict)  # synced events by lateness bucket

    def as_dict(self) -> dict[str, Any]:
        return {"synced": self.synced, "failed": self.failed, "skipped": self.skipped, "late": self.late}


def produces_task(pipeline: str, stage: str) -> bool:
    return (pipeline, stage) in TASKS


def task_payload(ev: dict) -> dict:
    tag, prefix = TASKS[(ev["pipeline"], ev["stage"])]
    if ev["pipeline"] == "referrer":
        subject = ev["person_name"]
    elif ev["stage"] == "intro":
        subject = f"{ev['buyer_label']} via {ev['person_name']}"
    else:
        subject = ev["buyer_label"]
    when = ev["at"].astimezone(TZ).date().isoformat()
    via = f" via {ev['channel']}" if ev.get("channel") else ""
    return {
        "name": f"{prefix}: {subject}",
        "tags": [tag, GOAL_TAG],
        "context": f"Referral outreach: {ev['pipeline']} {ev['stage']} on {when}{via} "
        f"(outreach event {ev['id']}).",
    }


def late_bucket(at: datetime, now: datetime) -> str:
    """How long after the stage event its task reached Asana. The strategy layer counts
    by completed_at, so a late sync can move a count into the wrong week (spec §8)."""
    age = now - at
    if age.days < 1:
        return "0d"
    if age.days < 2:
        return "1d"
    if age.days <= 7:
        return "2-7d"
    return "8d+"


def _outcome(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        if 400 <= exc.response.status_code < 500:
            return "failed"  # tasks-api rejected it; retrying may not help
    return "retry"


def _sync_one(conn: Any, ev: dict, now: datetime, result: SyncResult) -> None:
    bucket = late_bucket(ev["at"], now)
    try:
        gid = ev["asana_task_gid"]
        if gid is None:
            gid = tasks_api.create_task(**task_payload(ev))
            outreach_repo.set_asana_gid(conn, ev["id"], gid)
            conn.commit()
        tasks_api.complete_task(gid)
        outreach_repo.set_asana_completed(conn, ev["id"])
        conn.commit()
    except Exception as exc:
        result.failed += 1
        otel.external_errors.add(1, {"system": "tasks"})
        otel.outreach_sync.add(1, {"target": "asana", "outcome": _outcome(exc), "late": bucket})
        logger.warning("Asana sync failed for outreach event %s", ev["id"], exc_info=True)
        return
    result.synced += 1
    result.late[bucket] = result.late.get(bucket, 0) + 1
    otel.outreach_sync.add(1, {"target": "asana", "outcome": "ok", "late": bucket})


def sync_events(conn: Any, event_ids: list[int], *, now: datetime | None = None) -> SyncResult:
    """Right after a commit, for the events that commit created (handlers and the CLI)."""
    result = SyncResult()
    if not event_ids:
        return result
    if not tasks_api.configured():
        result.skipped = True
        return result
    now = now or datetime.now(UTC)
    for event_id in event_ids:
        for ev in outreach_repo.sync_rows(conn, _KEYS, event_id=event_id):
            _sync_one(conn, ev, now, result)
    return result


def sync_pending(conn: Any, *, now: datetime | None = None, limit: int = 200) -> SyncResult:
    """The backstop: every countable event still missing its completed task."""
    result = SyncResult()
    if not tasks_api.configured():
        result.skipped = True
        return result
    now = now or datetime.now(UTC)
    for ev in outreach_repo.sync_rows(conn, _KEYS, limit=limit):
        _sync_one(conn, ev, now, result)
    return result
```

- [ ] **Step 5: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_outreach_sync.py tests/test_outreach_db.py -v`
Expected: PASS. Then the full checks.

- [ ] **Step 6: Commit**

```bash
git add repo/outreach.py services/outreach_sync.py tests/test_outreach_sync.py tests/test_outreach_db.py
git commit -m "feat(outreach): sync stage events to completed Asana tasks"
```

---

### Task 7: Advance stages from email events; retry the Asana sync nightly

**Files:**
- Modify: `handlers/email_sent.py`, `handlers/email_classified.py`, `handlers/sync.py`
- Test: `tests/test_handlers.py`, `tests/test_outreach.py`

**Interfaces:**
- Consumes: `services.outreach.advance_from_event`, `is_outreach_sender` (Tasks 2–3); `services.outreach_sync.sync_events`, `sync_pending`, `SyncResult` (Task 6).
- Produces: `handlers.sync.run()` now returns `{"google": ..., "hubspot": ..., "asana": SyncResult.as_dict()}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_outreach.py`:

```python
def test_outreach_sender_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", " Ben@Consulting.Example.com , other@example.com")
    assert svc.is_outreach_sender("ben@consulting.example.com ")
    assert svc.is_outreach_sender("BEN@consulting.example.com")
    assert not svc.is_outreach_sender("alias@consulting.example.com")
    assert not svc.is_outreach_sender("")
    monkeypatch.delenv("OUTREACH_FROM_ADDRESSES")
    assert not svc.is_outreach_sender("ben@consulting.example.com")
```

In `tests/test_handlers.py`, add imports at the top:

```python
import services.outreach as outreach
import services.outreach_sync as outreach_sync
from services.outreach_sync import SyncResult
```

Extend the `calls` fixture so existing tests never reach the real outreach code (their rows carry no `id`), by adding before `return log, conn`:

```python
    monkeypatch.setattr(outreach, "advance_from_event", lambda c, pid, stage, **kw: None)
    monkeypatch.setattr(
        outreach_sync, "sync_events", lambda c, ids, **kw: log.append(("sync", list(ids)))
    )
```

Then append:

```python
def _sent(frm="Ben@Consulting.Example.com "):
    return {
        "event": "email_sent",
        "graph_message_id": "g1",
        "conversation_id": None,
        "sent_at": "2026-10-13T15:00:00Z",
        "from": frm,
        "to": ["bob@x.com", "me@x.com"],
        "cc": ["carol@x.com"],
        "subject": "Hello",
    }


def _outbound_rows(monkeypatch):
    # record_outbound already drops Ben's own addresses (OWN_ADDRESSES); To and Cc both come back.
    rows = [
        IngestResult(row={"id": 1, "email": "bob@x.com", "eligible": True}, newly_eligible=False),
        IngestResult(row={"id": 2, "email": "carol@x.com", "eligible": True}, newly_eligible=False),
    ]
    monkeypatch.setattr(ingest, "record_outbound", lambda conn, **kw: rows)


def test_sent_advances_only_to_recipients(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)
    advanced = []

    def adv(c, pid, stage, *, at, graph_message_id):
        advanced.append((pid, stage, at, graph_message_id))
        log.append("advance")
        return 11

    monkeypatch.setattr(outreach, "advance_from_event", adv)
    email_sent.handle(_sent())
    assert advanced == [(1, "briefed", datetime(2026, 10, 13, 15, 0, tzinfo=UTC), "g1")]
    # Durable first: the advance is inside the first commit; the Asana sync comes after it.
    assert log.index("advance") < log.index("commit") < log.index(("sync", [11]))


def test_sent_from_another_identity_does_not_brief(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)
    advanced = []
    monkeypatch.setattr(outreach, "advance_from_event", lambda c, pid, stage, **kw: advanced.append(pid))
    email_sent.handle(_sent(frm="ben@alias.example.com"))
    assert advanced == [] and not any(isinstance(e, tuple) and e[0] == "sync" for e in log)


def test_sent_no_event_means_no_sync(monkeypatch, calls):
    log, _ = calls
    monkeypatch.setenv("OUTREACH_FROM_ADDRESSES", "ben@consulting.example.com")
    _outbound_rows(monkeypatch)  # advance_from_event returns None: a replay, or no pipeline row
    email_sent.handle(_sent())
    assert not any(isinstance(e, tuple) and e[0] == "sync" for e in log)


@pytest.mark.parametrize(
    "category,automated,expected",
    [("review", False, [(5, "replied")]), ("ignore", False, []), ("respond", True, [])],
)
def test_classified_advances_a_reply(monkeypatch, calls, category, automated, expected):
    log, _ = calls
    row = {"id": 5, "email": "alice@x.com", "eligible": True, "automated": automated}
    monkeypatch.setattr(
        ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=False)
    )
    advanced = []

    def adv(c, pid, stage, *, at, graph_message_id):
        advanced.append((pid, stage))
        log.append("advance")

    monkeypatch.setattr(outreach, "advance_from_event", adv)
    email_classified.handle(_event(category))
    assert advanced == expected
    if expected:
        assert log.index("advance") < log.index("commit")
```

Replace `test_sync_handler_commits_twice_on_success` with:

```python
def test_sync_handler_runs_google_hubspot_then_the_asana_backstop(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(gsync, "run_sync", lambda c: {"updated": 1})
    monkeypatch.setattr(mirror, "reconcile", lambda c: {"adopted": 1})
    monkeypatch.setattr(outreach_sync, "sync_pending", lambda c: SyncResult(synced=2))
    result = sync.run()
    assert conn.commits == 2
    assert result == {
        "google": {"updated": 1},
        "hubspot": {"adopted": 1},
        "asana": {"synced": 2, "failed": 0, "skipped": False, "late": {}},
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_handlers.py tests/test_outreach.py -v`
Expected: the new handler tests FAIL (no advance recorded, no `asana` key); `test_outreach_sender_is_case_and_space_insensitive` passes already (Task 2 wrote the function) and stays as a guard.

- [ ] **Step 3: Update `handlers/email_sent.py`**

```python
"""email_sent → my_response_count / last_contacted for each To+Cc recipient;
writing to someone makes them eligible. Spec §6.2. A note from the consulting
identity also proves `briefed` for each To recipient at `to_contact` (outreach
spec §6.2), inside the same durable transaction; its Asana task follows the commit."""

import logging

import clients.otel as otel
from clients import db
from services import (
    eligibility,
    google_contacts_sync,
    hubspot_mirror,
    ingest,
    outreach,
    outreach_sync,
)

logger = logging.getLogger(__name__)


def _advance_briefed(conn, event: dict, results: list, sent_at) -> list[int]:
    if not outreach.is_outreach_sender(event.get("from") or ""):
        return []
    to = {eligibility.normalize(a) for a in event.get("to") or []}
    event_ids = []
    for res in results:
        if res.row.get("id") is None or res.row.get("email") not in to:
            continue  # Cc recipients are copied, not briefed
        event_id = outreach.advance_from_event(
            conn, res.row["id"], "briefed", at=sent_at, graph_message_id=event.get("graph_message_id")
        )
        if event_id is not None:
            event_ids.append(event_id)
    return event_ids


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_sent"})
    recipients = list(event.get("to") or []) + list(event.get("cc") or [])
    sent_at = ingest.parse_ts(event["sent_at"])
    with db.get_conn() as conn:
        results = ingest.record_outbound(
            conn,
            recipients=recipients,
            display_by_email=None,
            sent_at=sent_at,
        )
        briefed = _advance_briefed(conn, event, results, sent_at)
        conn.commit()
        if briefed:
            outreach_sync.sync_events(conn, briefed)
        for res in results:
            if res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
        conn.commit()
    logger.info(
        "email_sent %s → %d recipients, %d briefed",
        event.get("graph_message_id"),
        len(results),
        len(briefed),
    )
```

- [ ] **Step 4: Update `handlers/email_classified.py`**

Add `outreach` to the `from services import ...` line. Replace the body of `handle` from `res = ingest.record_inbound(` through the first `conn.commit()` with:

```python
        received_at = ingest.parse_ts(event["received_at"])
        res = ingest.record_inbound(
            conn,
            sender=event["sender"],
            display=event.get("sender_display"),
            received_at=received_at,
            category=event.get("category", ""),
        )
        row = res.row
        # Outreach spec §6.3: any non-ignore mail from a briefed contact proves `replied`.
        # An out-of-office is filed `ignore` by inbox and does not count.
        if (
            row.get("id") is not None
            and event.get("category") != "ignore"
            and not row.get("automated")
        ):
            outreach.advance_from_event(
                conn,
                row["id"],
                "replied",
                at=received_at,
                graph_message_id=event.get("graph_message_id") or event.get("message_id"),
            )
        conn.commit()
```

and delete the now-duplicated `row = res.row` line that followed the old commit. `replied` produces no Asana task (spec §8), so this handler never syncs. Update the module docstring's last sentence to mention the outreach advance.

- [ ] **Step 5: Update `handlers/sync.py`**

```python
"""Nightly: Google Contacts incremental sync, HubSpot reconcile, then the outreach
Asana backstop (outreach spec §8): every countable stage event still missing its
completed task is retried."""

import logging

from clients import db
from services import google_contacts_sync, hubspot_mirror, outreach_sync

logger = logging.getLogger(__name__)


def run() -> dict:
    with db.get_conn() as conn:
        google = google_contacts_sync.run_sync(conn)
        conn.commit()
        hubspot = hubspot_mirror.reconcile(conn)
        conn.commit()
        asana = outreach_sync.sync_pending(conn).as_dict()  # commits its own progress
    logger.info("sync complete google=%s hubspot=%s asana=%s", google, hubspot, asana)
    return {"google": google, "hubspot": hubspot, "asana": asana}
```

- [ ] **Step 6: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_handlers.py tests/test_main.py tests/test_outreach.py -v`
Expected: PASS. Then the full checks.

- [ ] **Step 7: Commit**

```bash
git add handlers/ tests/test_handlers.py tests/test_outreach.py
git commit -m "feat(handlers): email_sent proves briefed, email_classified proves replied"
```

---

### Task 8: Read side: person lookup, queue, follow-ups, shared-brief warning, population gauges

**Files:**
- Create: `services/person_lookup.py`
- Modify: `repo/outreach.py`, `services/outreach.py`, `handlers/sync.py`
- Test: `tests/test_person_lookup.py`, `tests/test_outreach.py`, `tests/test_outreach_db.py`, `tests/test_handlers.py`

**Interfaces:**
- Consumes: `repo.people.get`, `get_by_id`, `get_by_phone`; `services.identity.classify`; `clients.otel.set_outreach_population` (Task 3).
- Produces:
  - `services.person_lookup.resolve(conn, ident: str) -> dict` raising `NotFound(LookupError)` or `Ambiguous(LookupError)` with `.candidates: list[int]`.
  - `repo.outreach.queue(conn, *, stage, limit) -> list[dict]`, `follow_ups(conn, *, today) -> list[dict]`, `follow_ups_due_count(conn, today) -> int`, `shared_briefs(conn) -> list[dict]`, `population(conn) -> list[dict]`.
  - `services.outreach.queue(conn, *, stage="to_contact", limit=50) -> list[dict]` (spec §7.2 shape plus `outreach_notes` and `has_lead`), `queue_item(row: dict) -> dict`, `follow_ups(conn, *, today) -> list[dict]`, `shared_briefs(conn) -> list[dict]`, `record_population(conn, *, today) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_person_lookup.py`:

```python
import pytest

from services import person_lookup


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setattr(person_lookup.people, "get", lambda c, e: {"id": 1} if e == "alice@example.com" else None)
    monkeypatch.setattr(person_lookup.people, "get_by_id", lambda c, i: {"id": i} if i == 7 else None)
    monkeypatch.setattr(
        person_lookup.people,
        "get_by_phone",
        lambda c, p: [{"id": 3}, {"id": 4}] if p == "+15550100009" else [{"id": 2}] if p == "+15550100001" else [],
    )


@pytest.mark.parametrize("ident,expected", [("Alice@Example.com", 1), ("7", 7), ("+1 555 010 0001", 2)])
def test_resolves_email_id_and_phone(people, ident, expected):
    assert person_lookup.resolve(None, ident)["id"] == expected


def test_a_shared_phone_is_ambiguous(people):
    with pytest.raises(person_lookup.Ambiguous) as e:
        person_lookup.resolve(None, "+15550100009")
    assert e.value.candidates == [3, 4]


@pytest.mark.parametrize("ident", ["nobody@example.com", "8", "", "not a person"])
def test_no_match(people, ident):
    with pytest.raises(person_lookup.NotFound):
        person_lookup.resolve(None, ident)
```

Append to `tests/test_outreach.py`:

```python
def test_queue_item_matches_the_spec_shape():
    row = {
        "id": 42,
        "email": "alice@example.com",
        "display_name": "Alice Example",
        "company": "Co",
        "job_title": "CTO",
        "notes": "met at a conference",
        "stage": "to_contact",
        "follow_up_on": None,
        "has_lead": False,
        "email_last": datetime(2025, 3, 2, 12, tzinfo=UTC),
        "imessage_last": datetime(2026, 7, 19, 12, tzinfo=UTC),
        "has_imessage": True,
        "whatsapp_last": None,
        "has_whatsapp": False,
        "linkedin_url": "https://www.linkedin.com/in/alice-example",
        "linkedin_last": datetime(2024, 11, 30, 12, tzinfo=UTC),
        "last_interaction": datetime(2026, 7, 19, 12, tzinfo=UTC),
        "labels": ["former-colleague"],
        "outreach_notes": None,
    }
    assert svc.queue_item(row) == {
        "person": {"id": 42, "email": "alice@example.com", "display_name": "Alice Example", "company": "Co", "job_title": "CTO"},
        "stage": "to_contact",
        "last_interaction": "2026-07-19",
        "channels": {"email": True, "imessage": True, "linkedin": "https://www.linkedin.com/in/alice-example", "whatsapp": False},
        "last_messages": {"email": "2025-03-02", "imessage": "2026-07-19", "linkedin": "2024-11-30"},
        "labels": ["former-colleague"],
        "notes": "met at a conference",
        "outreach_notes": None,
        "has_lead": False,
        "follow_up_on": None,
    }


def test_queue_rejects_an_unknown_stage():
    with pytest.raises(ValueError):
        svc.queue(None, stage="pinged")


def test_record_population_labels_cohorts_by_iso_week(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        svc.outreach_repo,
        "population",
        lambda c: [
            {"pipeline": "referrer", "stage": "briefed", "cohort_week": date(2026, 10, 12), "n": 3},
            {"pipeline": "referrer", "stage": "candidate", "cohort_week": None, "n": 40},
        ],
    )
    monkeypatch.setattr(svc.outreach_repo, "follow_ups_due_count", lambda c, today: 2)
    monkeypatch.setattr(svc.otel, "set_outreach_population", lambda rows, due: seen.update(rows=rows, due=due))
    svc.record_population(None, today=date(2026, 10, 20))
    assert seen == {
        "rows": [
            (3, {"pipeline": "referrer", "stage": "briefed", "cohort_week": "2026-W42"}),
            (40, {"pipeline": "referrer", "stage": "candidate", "cohort_week": "none"}),
        ],
        "due": 2,
    }
```

Append to `tests/test_outreach_db.py`:

```python
def test_queue_orders_by_latest_contact_on_any_channel_and_fills_channels(conn):
    stale = _person(conn, email="stale@example.com", last_seen=datetime(2024, 1, 1, tzinfo=UTC))
    texted = _person(conn, email="texted@example.com", last_seen=datetime(2024, 1, 1, tzinfo=UTC))
    never = _person(conn, email=None, phone_numbers=["+15550100003"])
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_id, message_count, my_message_count, last_message_at) "
        "VALUES ('+15550100002', %s, 20, 10, %s)",
        (texted, datetime(2026, 7, 19, tzinfo=UTC)),
    )
    for pid in (stale, texted, never):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.update_contact(conn, pid, {"stage": "to_contact"})
    rows = outreach_repo.queue(conn, stage="to_contact", limit=10)
    assert [r["id"] for r in rows] == [never, stale, texted]  # never-contacted first
    by_id = {r["id"]: r for r in rows}
    assert by_id[texted]["has_imessage"] and not by_id[stale]["has_imessage"]
    assert by_id[texted]["last_interaction"] == datetime(2026, 7, 19, tzinfo=UTC)
    assert by_id[never]["email_last"] is None and by_id[never]["labels"] == []
    assert outreach_repo.queue(conn, stage="briefed", limit=10) == []


def test_follow_ups_and_shared_briefs(conn):
    a, b = _person(conn), _person(conn, email="b@example.com")
    for pid in (a, b):
        outreach_repo.insert_contact_if_missing(conn, pid)
        outreach_repo.update_contact(conn, pid, {"stage": "briefed", "follow_up_on": datetime(2026, 10, 20).date()})
        outreach_repo.insert_event(conn, pipeline="referrer", person_id=pid, stage="briefed", at=TS, source="event", channel="email", graph_message_id="g-shared")
    outreach_repo.update_contact(conn, b, {"follow_up_on": datetime(2026, 10, 25).date()})
    today = datetime(2026, 10, 21).date()
    assert [r["id"] for r in outreach_repo.follow_ups(conn, today=today)] == [a]
    assert outreach_repo.follow_ups_due_count(conn, today) == 1
    assert outreach_repo.shared_briefs(conn) == [{"graph_message_id": "g-shared", "n": 2}]
    pop = {(r["pipeline"], r["stage"]): r["n"] for r in outreach_repo.population(conn)}
    assert pop == {("referrer", "briefed"): 2}
```

Append to `tests/test_handlers.py`:

```python
def test_sync_handler_records_the_population_and_survives_its_failure(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(gsync, "run_sync", lambda c: {})
    monkeypatch.setattr(mirror, "reconcile", lambda c: {})
    monkeypatch.setattr(outreach_sync, "sync_pending", lambda c: SyncResult())

    def boom(c, today):
        raise RuntimeError("gauge query failed")

    monkeypatch.setattr(outreach, "record_population", boom)
    assert sync.run()["asana"]["synced"] == 0  # a gauge failure never fails the nightly sync
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_person_lookup.py tests/test_outreach.py tests/test_outreach_db.py tests/test_handlers.py -v`
Expected: FAIL (`ImportError: person_lookup`; `queue_item`, `record_population` missing).

- [ ] **Step 3: Create `services/person_lookup.py`**

```python
"""Resolve an email, E.164 phone, or numeric id to one people row, for scripts
(outreach spec §7.4: `<person>` resolves the way people-api does). The rule is
services/identity.py's; ambiguity is an error with the candidate ids, never a guess."""

from typing import Any

from repo import people
from services import identity


class NotFound(LookupError):
    pass


class Ambiguous(LookupError):
    def __init__(self, ident: str, candidates: list[int]) -> None:
        self.candidates = candidates
        super().__init__(f"{ident} matches several people: {candidates}")


def resolve(conn: Any, ident: str) -> dict:
    classified = identity.classify(ident)
    if classified is None:
        raise NotFound(ident)
    kind, value = classified
    if kind == "email":
        row = people.get(conn, str(value))
    elif kind == "id":
        row = people.get_by_id(conn, int(value))
    else:
        rows = people.get_by_phone(conn, str(value))
        if len(rows) > 1:
            raise Ambiguous(ident, [r["id"] for r in rows])
        row = rows[0] if rows else None
    if row is None:
        raise NotFound(ident)
    return row
```

- [ ] **Step 4: Append the read queries to `repo/outreach.py`**

```python
_LABELS = """
    ARRAY(
        SELECT g.name FROM people_labels pl
        JOIN contact_groups g ON g.resource_name = pl.group_resource_name
        WHERE pl.person_id = p.id
        ORDER BY lower(g.name), g.name
    )
"""


def queue(conn: Any, *, stage: str, limit: int) -> list[dict]:
    """Contacts at `stage` with what Ben needs on screen before writing (spec §7.2),
    least recently in touch on any channel first; never-contacted rows lead."""
    return conn.execute(
        f"""
        SELECT q.*,
               GREATEST(q.email_last, q.imessage_last, q.whatsapp_last, q.linkedin_last)
                   AS last_interaction
        FROM (
            SELECT p.id, p.email, p.display_name, p.company, p.job_title, p.notes,
                   c.stage, c.follow_up_on, c.has_lead, c.notes AS outreach_notes,
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
            WHERE c.stage = %s
        ) q
        ORDER BY last_interaction ASC NULLS FIRST, q.id
        LIMIT %s
        """,
        (stage, limit),
    ).fetchall()


def follow_ups(conn: Any, *, today: date) -> list[dict]:
    return conn.execute(
        """
        SELECT p.id, p.email, p.display_name, c.briefed_at, c.channel, c.follow_up_on
        FROM outreach_contacts c JOIN people p ON p.id = c.person_id
        WHERE c.stage = 'briefed' AND c.follow_up_on <= %s
        ORDER BY c.follow_up_on, p.id
        """,
        (today,),
    ).fetchall()


def follow_ups_due_count(conn: Any, today: date) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outreach_contacts WHERE stage = 'briefed' AND follow_up_on <= %s",
        (today,),
    ).fetchone()
    return int(row["n"])


def shared_briefs(conn: Any) -> list[dict]:
    """Emails that briefed more than one contact: a group note, not a brief (spec §6.2)."""
    return conn.execute(
        """
        SELECT graph_message_id, COUNT(DISTINCT person_id) AS n
        FROM outreach_events
        WHERE pipeline = 'referrer' AND stage = 'briefed' AND graph_message_id IS NOT NULL
        GROUP BY graph_message_id
        HAVING COUNT(DISTINCT person_id) > 1
        ORDER BY graph_message_id
        """
    ).fetchall()


def population(conn: Any) -> list[dict]:
    """Rows per pipeline, stage and cohort week, for the stage-population gauge (§12.2).
    A buyer row takes its referrer's cohort."""
    return conn.execute(
        """
        SELECT 'referrer' AS pipeline, stage, cohort_week, COUNT(*) AS n
        FROM outreach_contacts GROUP BY stage, cohort_week
        UNION ALL
        SELECT 'buyer' AS pipeline, o.stage, c.cohort_week, COUNT(*) AS n
        FROM outreach_opportunities o
        LEFT JOIN outreach_contacts c ON c.person_id = o.referrer_id
        GROUP BY o.stage, c.cohort_week
        """
    ).fetchall()
```

Add `date` to the `from datetime import` line at the top of `repo/outreach.py`.

- [ ] **Step 5: Append the read functions to `services/outreach.py`**

```python
def _day(ts: datetime | None) -> str | None:
    return ts.astimezone(TZ).date().isoformat() if ts else None


def queue_item(r: dict) -> dict:
    last = {
        "email": _day(r["email_last"]),
        "imessage": _day(r["imessage_last"]),
        "linkedin": _day(r["linkedin_last"]),
        "whatsapp": _day(r["whatsapp_last"]),
    }
    return {
        "person": {
            "id": r["id"],
            "email": r["email"],
            "display_name": r["display_name"],
            "company": r["company"],
            "job_title": r["job_title"],
        },
        "stage": r["stage"],
        "last_interaction": _day(r["last_interaction"]),
        "channels": {
            "email": r["email"] is not None,
            "imessage": bool(r["has_imessage"]),
            "linkedin": r["linkedin_url"] or False,
            "whatsapp": bool(r["has_whatsapp"]),
        },
        "last_messages": {k: v for k, v in last.items() if v},
        "labels": list(r["labels"] or []),
        "notes": r["notes"],
        "outreach_notes": r["outreach_notes"],
        "has_lead": bool(r["has_lead"]),
        "follow_up_on": r["follow_up_on"].isoformat() if r["follow_up_on"] else None,
    }


def queue(conn: Any, *, stage: str = "to_contact", limit: int = 50) -> list[dict]:
    if stage not in REFERRER_STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(REFERRER_STAGES)}")
    return [queue_item(r) for r in outreach_repo.queue(conn, stage=stage, limit=limit)]


def follow_ups(conn: Any, *, today: date) -> list[dict]:
    return outreach_repo.follow_ups(conn, today=today)


def shared_briefs(conn: Any) -> list[dict]:
    return outreach_repo.shared_briefs(conn)


def record_population(conn: Any, *, today: date) -> None:
    """Refresh the funnel-shape gauges (spec §12.2). The cohort label is an ISO week,
    the only per-cohort attribute, so cardinality stays one series per stage per week."""
    rows = [
        (
            int(r["n"]),
            {"pipeline": r["pipeline"], "stage": r["stage"], "cohort_week": iso_week(r["cohort_week"])},
        )
        for r in outreach_repo.population(conn)
    ]
    otel.set_outreach_population(rows, outreach_repo.follow_ups_due_count(conn, today))
```

- [ ] **Step 6: Record the population in the nightly sync**

In `handlers/sync.py`, change the services import to `from services import google_contacts_sync, hubspot_mirror, outreach, outreach_sync`, add `from datetime import datetime`, and after the `asana = ...` line add:

```python
        try:
            outreach.record_population(conn, today=datetime.now(outreach.TZ).date())
        except Exception:
            # A gauge is telemetry: its failure must not fail the nightly sync.
            logger.warning("outreach population gauge failed", exc_info=True)
```

Update `test_sync_handler_runs_google_hubspot_then_the_asana_backstop` (Task 7) to also stub it: `monkeypatch.setattr(outreach, "record_population", lambda c, today: None)`.

- [ ] **Step 7: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_person_lookup.py tests/test_outreach.py tests/test_outreach_db.py tests/test_handlers.py -v`
Expected: PASS. Then the full checks.

- [ ] **Step 8: Commit**

```bash
git add services/person_lookup.py repo/outreach.py services/outreach.py handlers/sync.py tests/
git commit -m "feat(outreach): queue, follow-ups, shared-brief warning, population gauges"
```

---

### Task 9: Report: counts by cohort, floors, verdicts

**Files:**
- Modify: `repo/outreach.py`, `services/outreach.py`
- Create: `services/outreach_report.py`
- Test: `tests/test_outreach_report.py`, `tests/test_outreach.py`, `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: `models.outreach.CohortCounts`, `FunnelCounts`, `REPORT_STAGES`; `repo.outreach.follow_ups_due_count` (Task 8).
- Produces:
  - `repo.outreach.referrer_cohort_counts(conn, *, since, until) -> list[dict]` (`cohort_week, briefed, replied`), `buyer_cohort_counts(conn, *, since, until) -> list[dict]` (`cohort_week, intro, conversation, proposal, signed`), `lost_reasons(conn, *, since, until) -> list[dict]` (`lost_reason, n`).
  - `services.outreach.counts(conn, *, today: date, since: date | None = None, until: date | None = None) -> FunnelCounts`.
  - `services.outreach_report`: `Floors(rates, readable_after, lag_weeks)`, `FloorsError(ValueError)`, `load_floors(path: Path) -> Floors`, `StageVerdict(stage, actual, n, floor_at_n, readable, threshold, verdict)`, `stage_verdicts(counts, floors, *, today) -> list[StageVerdict]`, `render(counts, verdicts=None) -> str`, `table(headers: list[str], rows: list[list]) -> str`, `pct(num, den) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outreach_report.py`:

```python
from datetime import date
from pathlib import Path

import pytest

from models.outreach import CohortCounts, FunnelCounts
from services import outreach_report as rpt

FLOORS_TOML = """
[floors]
replied = 0.40
intro = 0.08
conversation = 0.048
proposal = 0.016
signed = 0.0053

[readable_after]
replied = 6
intro = 36
conversation = 61
proposal = 186
signed = 564

[lag_weeks]
replied = 1
intro = 3
conversation = 5
proposal = 7
signed = 11
"""
TODAY = date(2026, 10, 20)


@pytest.fixture
def floors(tmp_path) -> rpt.Floors:
    path = tmp_path / "floors.toml"
    path.write_text(FLOORS_TOML)
    return rpt.load_floors(path)


@pytest.fixture
def counts() -> FunnelCounts:
    return FunnelCounts(
        cohorts=[
            CohortCounts(date(2026, 9, 7), briefed=20, replied=9, intro=2, conversation=1),
            CohortCounts(date(2026, 10, 12), briefed=10, replied=2),
        ],
        lost_reasons={"timing": 1, "no_budget": 1},
        follow_ups_due=9,
    )


def test_verdicts_use_only_lag_ripe_cohorts(counts, floors):
    v = {s.stage: s for s in rpt.stage_verdicts(counts, floors, today=TODAY)}
    # replied: both cohorts are a week old; floor 0.40 * 30 = 12; 11 < 12 and n >= 6
    assert (v["replied"].n, v["replied"].actual, v["replied"].floor_at_n) == (30, 11, pytest.approx(12.0))
    assert v["replied"].readable and v["replied"].verdict == "below floor"
    # intro: only the September cohort is three weeks old; n=20 < 36, so no verdict
    assert (v["intro"].n, v["intro"].actual) == (20, 2)
    assert not v["intro"].readable and v["intro"].verdict == "--"
    # proposal: no cohort is seven weeks old yet
    assert (v["proposal"].n, v["proposal"].floor_at_n) == (0, 0)


@pytest.mark.parametrize("actual,verdict", [(12, "at floor"), (13, "above floor"), (11, "below floor")])
def test_verdict_rounds_the_floor_half_up(floors, actual, verdict):
    c = FunnelCounts([CohortCounts(date(2026, 9, 7), briefed=30, replied=actual)])
    assert rpt.stage_verdicts(c, floors, today=TODAY)[0].verdict == verdict


def test_render_without_floors_prints_counts_rates_lost_and_follow_ups(counts):
    text = rpt.render(counts)
    lines = text.splitlines()
    assert lines[0].split() == ["Cohort", "Briefed", "Replied", "Intro", "Conv", "Prop", "Signed", "Reply%", "Intro%", "Conv%"]
    assert lines[1].split() == ["2026-09-07", "20", "9", "2", "1", "0", "0", "45%", "10%", "5.0%"]
    assert lines[3].split() == ["All", "30", "11", "2", "1", "0", "0", "37%", "6.7%", "3.3%"]
    assert "Floor@n" not in text
    assert "Lost reasons: no_budget 1, timing 1" in text and "Follow-ups due: 9" in text


def test_render_with_floors_adds_the_stage_table(counts, floors):
    text = rpt.render(counts, rpt.stage_verdicts(counts, floors, today=TODAY))
    assert "Readable?" in text
    assert "no (n<36)" in text
    replied = next(line for line in text.splitlines() if line.startswith("Replied"))
    assert replied.split() == ["Replied", "11", "12", "yes", "below", "floor"]


def test_render_with_nothing_yet():
    text = rpt.render(FunnelCounts([]))
    assert "All" in text and "Lost reasons: none" in text


@pytest.mark.parametrize("num,den,out", [(0, 0, "--"), (0, 5, "0%"), (12, 30, "40%"), (1, 30, "3.3%")])
def test_pct(num, den, out):
    assert rpt.pct(num, den) == out


def test_floors_file_errors_are_specific(tmp_path):
    with pytest.raises(rpt.FloorsError, match="not found"):
        rpt.load_floors(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text(FLOORS_TOML.replace("signed = 564\n", ""))
    with pytest.raises(rpt.FloorsError, match=r"\[readable_after\] lacks signed"):
        rpt.load_floors(bad)
    bad.write_text("[floors]\nreplied = 0.4\n")
    with pytest.raises(rpt.FloorsError):
        rpt.load_floors(bad)
```

Append to `tests/test_outreach.py`:

```python
def test_counts_merges_referrer_and_buyer_rows_by_cohort(monkeypatch):
    w1, w2 = date(2026, 10, 12), date(2026, 10, 19)
    monkeypatch.setattr(
        svc.outreach_repo,
        "referrer_cohort_counts",
        lambda c, since, until: [
            {"cohort_week": w2, "briefed": 1, "replied": 0},
            {"cohort_week": w1, "briefed": 2, "replied": 1},
        ],
    )
    monkeypatch.setattr(
        svc.outreach_repo,
        "buyer_cohort_counts",
        lambda c, since, until: [{"cohort_week": w1, "intro": 2, "conversation": 1, "proposal": 0, "signed": 0}],
    )
    monkeypatch.setattr(svc.outreach_repo, "lost_reasons", lambda c, since, until: [{"lost_reason": "timing", "n": 1}])
    monkeypatch.setattr(svc.outreach_repo, "follow_ups_due_count", lambda c, today: 4)
    out = svc.counts(None, today=date(2026, 10, 21))
    assert [c.cohort_week for c in out.cohorts] == [w1, w2]
    assert (out.cohorts[0].briefed, out.cohorts[0].replied, out.cohorts[0].intro) == (2, 1, 2)
    assert out.lost_reasons == {"timing": 1} and out.follow_ups_due == 4
```

Append to `tests/test_outreach_db.py`:

```python
def _brief(conn, pid, week, *, replied=False):
    outreach_repo.insert_contact_if_missing(conn, pid)
    outreach_repo.update_contact(conn, pid, {"stage": "replied" if replied else "briefed", "cohort_week": week})
    outreach_repo.insert_event(conn, pipeline="referrer", person_id=pid, stage="briefed", at=TS, source="cli", channel="email")
    if replied:
        outreach_repo.insert_event(conn, pipeline="referrer", person_id=pid, stage="replied", at=TS, source="cli", channel="email")


def _opp(conn, referrer, stages, lost_reason=None):
    opp = outreach_repo.insert_opportunity(conn, referrer_id=referrer, buyer_id=None, buyer_label="B", at=TS)
    for stage in ["intro", *stages]:
        outreach_repo.insert_event(conn, pipeline="buyer", opportunity_id=opp["id"], stage=stage, at=TS, source="cli")
    final = stages[-1] if stages else "intro"
    changes = {"stage": final} | ({"lost_reason": lost_reason, "lost_at": TS} if final == "lost" else {})
    if final != "intro":
        outreach_repo.update_opportunity(conn, opp["id"], changes)


def test_cohort_counts_derive_from_the_event_log(conn):
    w1, w2 = datetime(2026, 10, 12).date(), datetime(2026, 10, 19).date()
    a, b, c = _person(conn), _person(conn, email="b@example.com"), _person(conn, email="c@example.com")
    _brief(conn, a, w1, replied=True)
    _brief(conn, b, w1)
    _brief(conn, c, w2)
    _opp(conn, a, ["conversation"])
    _opp(conn, a, ["lost"], lost_reason="timing")
    ref = {r["cohort_week"]: (r["briefed"], r["replied"]) for r in outreach_repo.referrer_cohort_counts(conn, since=None, until=None)}
    assert ref == {w1: (2, 1), w2: (1, 0)}
    buyer = outreach_repo.buyer_cohort_counts(conn, since=None, until=None)
    assert [(r["cohort_week"], r["intro"], r["conversation"], r["proposal"]) for r in buyer] == [(w1, 2, 1, 0)]
    assert outreach_repo.lost_reasons(conn, since=None, until=None) == [{"lost_reason": "timing", "n": 1}]
    assert [r["cohort_week"] for r in outreach_repo.referrer_cohort_counts(conn, since=w2, until=None)] == [w2]
    assert outreach_repo.buyer_cohort_counts(conn, since=w2, until=None) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_report.py tests/test_outreach.py tests/test_outreach_db.py -v`
Expected: FAIL (`ImportError: outreach_report`; `counts` missing).

- [ ] **Step 3: Append the count queries to `repo/outreach.py`**

```python
# A cohort filter both bounds may leave open: NULL means "no bound". Each bound is passed
# twice because pg8000 takes positional parameters only.
_COHORT_FILTER = """
    (%s::date IS NULL OR c.cohort_week >= %s::date)
    AND (%s::date IS NULL OR c.cohort_week <= %s::date)
"""


def referrer_cohort_counts(conn: Any, *, since: date | None, until: date | None) -> list[dict]:
    """Briefed and replied people per cohort week. cohort_week is set in the same
    transaction as a contact's first `briefed` event, so it marks the briefed set;
    replied comes from the event log, so a later decline does not erase a reply."""
    return conn.execute(
        f"""
        SELECT c.cohort_week,
               COUNT(*) AS briefed,
               COUNT(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM outreach_events e
                   WHERE e.pipeline = 'referrer' AND e.person_id = c.person_id
                     AND e.stage = 'replied')) AS replied
        FROM outreach_contacts c
        WHERE c.cohort_week IS NOT NULL AND {_COHORT_FILTER}
        GROUP BY c.cohort_week
        ORDER BY c.cohort_week
        """,
        (since, since, until, until),
    ).fetchall()


def buyer_cohort_counts(conn: Any, *, since: date | None, until: date | None) -> list[dict]:
    """Opportunities per referrer cohort that ever reached each buyer stage."""

    def reached(stage: str) -> str:
        return (
            "COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM outreach_events e "
            f"WHERE e.opportunity_id = o.id AND e.stage = '{stage}')) AS {stage}"
        )

    return conn.execute(
        f"""
        SELECT c.cohort_week, COUNT(*) AS intro,
               {reached("conversation")}, {reached("proposal")}, {reached("signed")}
        FROM outreach_opportunities o
        LEFT JOIN outreach_contacts c ON c.person_id = o.referrer_id
        WHERE {_COHORT_FILTER}
        GROUP BY c.cohort_week
        ORDER BY c.cohort_week NULLS LAST
        """,
        (since, since, until, until),
    ).fetchall()


def lost_reasons(conn: Any, *, since: date | None, until: date | None) -> list[dict]:
    return conn.execute(
        f"""
        SELECT o.lost_reason, COUNT(*) AS n
        FROM outreach_opportunities o
        LEFT JOIN outreach_contacts c ON c.person_id = o.referrer_id
        WHERE o.stage = 'lost' AND {_COHORT_FILTER}
        GROUP BY o.lost_reason
        ORDER BY o.lost_reason
        """,
        (since, since, until, until),
    ).fetchall()
```

The stage names interpolated by `reached()` are literals from this function, never input.

- [ ] **Step 4: Append `counts` to `services/outreach.py`**

Add `CohortCounts, FunnelCounts` to the `from models.outreach import` line, then append:

```python
def counts(
    conn: Any, *, today: date, since: date | None = None, until: date | None = None
) -> FunnelCounts:
    """Spec §9's numbers, per cohort week, all derived from the DB (never from counters)."""
    by_week: dict[date | None, CohortCounts] = {}

    def cohort(week: date | None) -> CohortCounts:
        return by_week.setdefault(week, CohortCounts(cohort_week=week))

    for r in outreach_repo.referrer_cohort_counts(conn, since=since, until=until):
        c = cohort(r["cohort_week"])
        c.briefed, c.replied = int(r["briefed"]), int(r["replied"])
    for r in outreach_repo.buyer_cohort_counts(conn, since=since, until=until):
        c = cohort(r["cohort_week"])
        c.intro, c.conversation = int(r["intro"]), int(r["conversation"])
        c.proposal, c.signed = int(r["proposal"]), int(r["signed"])
    cohorts = sorted(by_week.values(), key=lambda c: (c.cohort_week is None, c.cohort_week or date.min))
    lost = {r["lost_reason"]: int(r["n"]) for r in outreach_repo.lost_reasons(conn, since=since, until=until)}
    return FunnelCounts(cohorts, lost, outreach_repo.follow_ups_due_count(conn, today))
```

- [ ] **Step 5: Create `services/outreach_report.py`**

```python
"""`outreach report`: actuals against the funnel floors (outreach spec §9). Pure. The
floors, thresholds and lags are not in this public repo; they come from a TOML file in
the private consulting repo, passed with --floors."""

import math
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from models.outreach import REPORT_STAGES, CohortCounts, FunnelCounts

STAGE_LABELS = {
    "replied": "Replied",
    "intro": "Intro",
    "conversation": "Conversation",
    "proposal": "Proposal",
    "signed": "Signed",
}


class FloorsError(ValueError):
    pass


@dataclass
class Floors:
    rates: dict[str, float]  # rate per briefed contact
    readable_after: dict[str, int]  # briefed count before a zero means anything
    lag_weeks: dict[str, int]  # weeks a cohort needs to reach the stage


@dataclass
class StageVerdict:
    stage: str
    actual: int
    n: int  # briefed contacts in cohorts old enough to have reached the stage
    floor_at_n: float
    readable: bool
    threshold: int
    verdict: str  # "above floor" | "at floor" | "below floor" | "--"


def load_floors(path: Path) -> Floors:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise FloorsError(f"floors file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise FloorsError(f"{path}: {e}") from e
    tables: dict[str, dict] = {}
    for name in ("floors", "readable_after", "lag_weeks"):
        t = data.get(name)
        if not isinstance(t, dict):
            raise FloorsError(f"{path}: missing [{name}]")
        missing = [s for s in REPORT_STAGES if s not in t]
        if missing:
            raise FloorsError(f"{path}: [{name}] lacks {', '.join(missing)}")
        tables[name] = t
    return Floors(
        rates={s: float(tables["floors"][s]) for s in REPORT_STAGES},
        readable_after={s: int(tables["readable_after"][s]) for s in REPORT_STAGES},
        lag_weeks={s: int(tables["lag_weeks"][s]) for s in REPORT_STAGES},
    )


def stage_verdicts(counts: FunnelCounts, floors: Floors, *, today: date) -> list[StageVerdict]:
    """Actual and floor both come from the cohorts old enough to have reached the stage,
    so a cohort briefed yesterday neither drags a rate down nor props it up."""
    out = []
    for stage in REPORT_STAGES:
        lag = timedelta(weeks=floors.lag_weeks[stage])
        ripe = [c for c in counts.cohorts if c.cohort_week is not None and c.cohort_week + lag <= today]
        n = sum(c.briefed for c in ripe)
        actual = sum(getattr(c, stage) for c in ripe)
        floor_at_n = floors.rates[stage] * n
        threshold = floors.readable_after[stage]
        readable = n >= threshold
        target = math.floor(floor_at_n + 0.5)
        if not readable:
            verdict = "--"
        elif actual > target:
            verdict = "above floor"
        elif actual == target:
            verdict = "at floor"
        else:
            verdict = "below floor"
        out.append(StageVerdict(stage, actual, n, floor_at_n, readable, threshold, verdict))
    return out


def pct(num: int, den: int) -> str:
    if den == 0:
        return "--"
    value = 100 * num / den
    return f"{value:.0f}%" if value == 0 or value >= 10 else f"{value:.1f}%"


def _floor(x: float) -> str:
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(h) for h in headers]] + [[str(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in cells)


def _total(counts: FunnelCounts) -> CohortCounts:
    t = CohortCounts(cohort_week=None)
    for c in counts.cohorts:
        for stage in ("briefed", *REPORT_STAGES):
            setattr(t, stage, getattr(t, stage) + getattr(c, stage))
    return t


def _cohort_row(label: str, c: CohortCounts) -> list:
    return [
        label,
        c.briefed,
        c.replied,
        c.intro,
        c.conversation,
        c.proposal,
        c.signed,
        pct(c.replied, c.briefed),
        pct(c.intro, c.briefed),
        pct(c.conversation, c.briefed),
    ]


def render(counts: FunnelCounts, verdicts: list[StageVerdict] | None = None) -> str:
    rows = [_cohort_row(str(c.cohort_week or "none"), c) for c in counts.cohorts]
    rows.append(_cohort_row("All", _total(counts)))
    parts = [
        table(
            ["Cohort", "Briefed", "Replied", "Intro", "Conv", "Prop", "Signed", "Reply%", "Intro%", "Conv%"],
            rows,
        )
    ]
    if verdicts is not None:
        parts.append(
            table(
                ["Stage", "Actual", "Floor@n", "Readable?", "Verdict"],
                [
                    [
                        STAGE_LABELS[v.stage],
                        v.actual,
                        _floor(v.floor_at_n),
                        "yes" if v.readable else f"no (n<{v.threshold})",
                        v.verdict,
                    ]
                    for v in verdicts
                ],
            )
        )
    lost = ", ".join(f"{k} {n}" for k, n in sorted(counts.lost_reasons.items())) or "none"
    parts.append(f"Lost reasons: {lost}\nFollow-ups due: {counts.follow_ups_due}")
    return "\n\n".join(parts)
```

- [ ] **Step 6: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_outreach_report.py tests/test_outreach.py tests/test_outreach_db.py -v`
Expected: PASS. Then the full checks.

- [ ] **Step 7: Commit**

```bash
git add repo/outreach.py services/outreach.py services/outreach_report.py tests/
git commit -m "feat(outreach): funnel counts by cohort and the floors report"
```

---

### Task 10: The CLI, `scripts/outreach.py`

**Files:**
- Create: `scripts/outreach.py`
- Test: `tests/test_outreach_cli.py`

**Interfaces:**
- Consumes: everything in `services.outreach`, `services.outreach_sync`, `services.outreach_report`, `services.person_lookup`, `services.person_edit.update`, `clients.inbox_api.create_draft`, `clients.otel`.
- Produces: `scripts.outreach.main_with(argv: list[str], get_conn, *, out=sys.stdout, err=sys.stderr, now: datetime | None = None) -> int` (0 ok, 1 user error; argparse exits 2 on bad flags), `parse_at(value: str | None, now: datetime) -> datetime`, `LABEL = "consulting-outreach"`.

Subcommands (spec §7.4, minus phase 2's `sync-hubspot`): `queue [--stage S] [--limit N]`, `decide <person> [--skip]`, `draft <person> [--subject S]`, `mark <person> briefed|replied|declined|to_contact [--channel C] [--at T] [--from ADDR] [--force]`, `lead <person> "<name, company>"`, `intro <referrer> "<buyer name, company>" [--buyer <person>] [--at T]`, `conversation|proposal|signed <opportunity-id> [--at T]`, `lost <opportunity-id> --reason R [--at T]`, `followups`, `report [--floors PATH] [--since D] [--until D]`, `sync-asana`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outreach_cli.py`:

```python
import io
from datetime import UTC, date, datetime

import pytest

from models.outreach import Advance, CohortCounts, FunnelCounts
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
    code, _, err = run("mark", "alice@example.com", "briefed", "--channel", "imessage", "--from", "a@example.com")
    assert code == 1 and "--from" in err


def test_mark_briefed_commits_then_syncs(monkeypatch, run):
    seen = {}
    _advance_returns(monkeypatch, Advance({"stage": "briefed"}, True, 11), seen)
    code, out, _ = run("mark", "alice@example.com", "briefed", "--channel", "imessage")
    assert code == 0 and "briefed" in out and "asana: task created" in out
    assert run.conn.log[:2] == ["commit", ("sync", [11])]
    assert seen == {
        "pid": 7, "stage": "briefed", "at": NOW, "channel": "imessage", "source": "cli",
        "from_address": None, "force": False,
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
    run("mark", "7", "briefed", "--channel", "email", "--from", "alias@example.com", "--at", "2026-10-10", "--force")
    assert seen["from_address"] == "alias@example.com" and seen["force"] is True
    assert seen["at"] == datetime(2026, 10, 10, 12, 0, tzinfo=svc.TZ)


def test_decide_records_the_stage_before_the_label_and_survives_a_label_failure(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "ensure_contact", lambda c, pid, **kw: run.conn.log.append("ensure"))
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
        lambda c, today, since=None, until=None: FunnelCounts([CohortCounts(date(2026, 10, 12), briefed=3, replied=1)]),
    )
    monkeypatch.setattr(cli.otel, "outreach_report_runs", Recorder())
    code, out, _ = run("report")
    assert code == 0 and out.startswith("Cohort") and "Floor@n" not in out
    assert cli.otel.outreach_report_runs.calls == [{"verdict_below": "0"}]


def test_report_with_a_bad_floors_path(run):
    code, _, err = run("report", "--floors", "/nonexistent/floors.toml")
    assert code == 1 and "not found" in err


def test_draft_refuses_a_person_without_email(monkeypatch, run):
    monkeypatch.setattr(cli.person_lookup, "resolve", lambda c, ident: {"id": 8, "email": None, "display_name": "P"})
    code, _, err = run("draft", "8")
    assert code == 1 and "no email" in err


def test_draft_goes_from_the_primary_mailbox(monkeypatch, run):
    monkeypatch.setattr(cli.outreach, "get_contact", lambda c, pid: {"stage": "to_contact"})
    seen = {}
    monkeypatch.setattr(
        cli.inbox_api, "create_draft",
        lambda to, subject, body="": seen.update(to=to, subject=subject, body=body) or {"id": "d", "web_link": "https://o/d"},
    )
    code, out, _ = run("draft", "alice@example.com")
    assert code == 0 and "https://o/d" in out
    assert seen == {"to": ["alice@example.com"], "subject": "", "body": ""}


def test_parse_at():
    assert cli.parse_at(None, NOW) == NOW
    assert cli.parse_at("2026-10-10", NOW) == datetime(2026, 10, 10, 12, 0, tzinfo=svc.TZ)
    assert cli.parse_at("2026-10-10T09:30:00Z", NOW) == datetime(2026, 10, 10, 9, 30, tzinfo=UTC)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'outreach' from 'scripts'`.

- [ ] **Step 3: Create `scripts/outreach.py`**

```python
#!/usr/bin/env python3
# scripts/outreach.py
"""Work the referral outreach pipeline
(docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §7.4). Local only.

  python scripts/outreach.py queue [--stage S] [--limit N]
  python scripts/outreach.py decide <person> [--skip]
  python scripts/outreach.py draft <person> [--subject S]
  python scripts/outreach.py mark <person> briefed|replied|declined|to_contact
                                  [--channel C] [--at T] [--from ADDR] [--force]
  python scripts/outreach.py lead <person> "<name, company>"
  python scripts/outreach.py intro <referrer> "<buyer name, company>" [--buyer <person>] [--at T]
  python scripts/outreach.py conversation|proposal|signed <opportunity-id> [--at T]
  python scripts/outreach.py lost <opportunity-id> --reason R [--at T]
  python scripts/outreach.py followups
  python scripts/outreach.py report [--floors PATH] [--since D] [--until D]
  python scripts/outreach.py sync-asana

<person> is an email, an E.164 phone, or a people id. --at takes YYYY-MM-DD (noon, New
York time) or a full ISO timestamp; it defaults to now. Every stage change is committed
before any tasks-api, inbox-api or Google call, so a failed side effect never loses a
count: `sync-asana` retries the Asana half. Runs against whatever DB clients/db.py
resolves from env, like the import scripts.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from clients import inbox_api
from models.outreach import CHANNELS, LOST_REASONS, REFERRER_STAGES, Advance
from services import outreach, outreach_report, outreach_sync, person_edit, person_lookup
from services.ingest import parse_ts

LABEL = "consulting-outreach"  # Google Contacts label marking cohort membership (spec §7.1)
MARK_STAGES = ("briefed", "replied", "declined", "to_contact")


class CliError(Exception):
    pass


USER_ERRORS = (
    CliError,
    outreach.InvalidTransition,
    outreach.NotInPipeline,
    outreach.OpportunityNotFound,
    outreach_report.FloorsError,
    ValueError,
)


def parse_at(value: str | None, now: datetime) -> datetime:
    if not value:
        return now
    if len(value) == 10:  # a bare date: noon in New York, clear of any week boundary
        return datetime.combine(date.fromisoformat(value), time(12), tzinfo=outreach.TZ)
    return parse_ts(value)


def _label(person: dict) -> str:
    return person.get("display_name") or person.get("email") or f"person {person['id']}"


def _resolve(conn: Any, ident: str) -> dict:
    try:
        return person_lookup.resolve(conn, ident)
    except person_lookup.Ambiguous as e:
        raise CliError(f"{ident} matches several people ({e.candidates}); use an id") from e
    except person_lookup.NotFound as e:
        raise CliError(f"no person matches {ident}") from e


def _sync(conn: Any, res: Advance | None, pipeline: str, stage: str, out: TextIO) -> None:
    if res is None or res.event_id is None or not outreach_sync.produces_task(pipeline, stage):
        return
    result = outreach_sync.sync_events(conn, [res.event_id])
    if result.skipped:
        print("asana: TASKS_API_URL is unset; run `outreach sync-asana` once it is set", file=out)
    elif result.failed:
        print("asana: task not created yet; `outreach sync-asana` retries it", file=out)
    else:
        print("asana: task created and completed", file=out)


def _changed(res: Advance) -> str:
    return "" if res.changed else " (unchanged)"


def cmd_queue(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    items = outreach.queue(conn, stage=args.stage, limit=args.limit)
    rows = [
        [
            i["person"]["id"],
            _label(i["person"]),
            i["person"]["company"] or "",
            i["last_interaction"] or "never",
            " ".join(k for k, v in i["channels"].items() if v),
            i["follow_up_on"] or "",
            "lead" if i["has_lead"] else "",
        ]
        for i in items
    ]
    if rows:
        print(outreach_report.table(["id", "name", "company", "last", "channels", "follow-up", ""], rows), file=out)
    else:
        print(f"no contacts at {args.stage}", file=out)
    for s in outreach.shared_briefs(conn):
        print(
            f"warning: one email ({s['graph_message_id']}) briefed {s['n']} contacts; "
            "a group note is not a brief",
            file=out,
        )


def cmd_decide(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    person = _resolve(conn, args.person)
    target = "skipped" if args.skip else "to_contact"
    outreach.ensure_contact(conn, person["id"], at=now, source="cli")
    res = outreach.advance(conn, person["id"], target, at=now, source="cli")
    if res is None:  # ensure_contact just made the row; only a concurrent delete gets here
        raise CliError(f"{_label(person)} vanished from the pipeline")
    conn.commit()
    print(f"{_label(person)}: {res.row['stage']}{_changed(res)}", file=out)
    if target != "to_contact":
        return
    try:
        person_edit.update(conn, person["id"], labels={"add": [LABEL]})
        conn.commit()
    except Exception as e:  # the label is a convenience; the stage is already recorded
        conn.rollback()
        print(f"warning: stage recorded, but the {LABEL} label was not added: {e}", file=out)


def cmd_draft(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    person = _resolve(conn, args.person)
    if not person.get("email"):
        raise CliError(f"{_label(person)} has no email address; brief them by hand and `mark`")
    contact = outreach.get_contact(conn, person["id"])
    stage = contact["stage"] if contact else "not in the pipeline"
    if stage != "to_contact":
        print(f"warning: {_label(person)} is {stage}, not to_contact", file=out)
    draft = inbox_api.create_draft([person["email"]], subject=args.subject)
    print(f"draft created from the primary mailbox: {draft.get('web_link') or draft.get('id')}", file=out)


def cmd_mark(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    if args.stage in ("briefed", "replied") and not args.channel:
        raise CliError("--channel is required for briefed and replied")
    if args.from_address and args.channel != "email":
        raise CliError("--from applies only with --channel email")
    person = _resolve(conn, args.person)
    res = outreach.advance(
        conn,
        person["id"],
        args.stage,
        at=parse_at(args.at, now),
        channel=args.channel,
        source="cli",
        from_address=args.from_address,
        force=args.force,
    )
    if res is None:
        raise CliError(f"{_label(person)} is not in the pipeline; run `outreach decide` first")
    conn.commit()
    print(f"{_label(person)}: {res.row['stage']}{_changed(res)}", file=out)
    _sync(conn, res, "referrer", args.stage, out)


def cmd_lead(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    person = _resolve(conn, args.person)
    outreach.set_lead(conn, person["id"], args.lead, today=now.astimezone(outreach.TZ).date())
    conn.commit()
    print(f"{_label(person)}: lead noted ({args.lead})", file=out)


def cmd_intro(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    referrer = _resolve(conn, args.referrer)
    buyer_id = _resolve(conn, args.buyer)["id"] if args.buyer else None
    res = outreach.open_opportunity(
        conn, referrer["id"], args.buyer_label, at=parse_at(args.at, now), buyer_id=buyer_id, source="cli"
    )
    conn.commit()
    print(f"opportunity {res.row['id']}: {res.row['buyer_label']} via {_label(referrer)}", file=out)
    _sync(conn, res, "buyer", "intro", out)


def cmd_buyer(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    stage = args.command
    res = outreach.advance_opportunity(
        conn,
        args.opportunity,
        stage,
        at=parse_at(args.at, now),
        lost_reason=getattr(args, "reason", None),
        source="cli",
    )
    conn.commit()
    print(f"opportunity {res.row['id']} ({res.row['buyer_label']}): {res.row['stage']}{_changed(res)}", file=out)
    _sync(conn, res, "buyer", stage, out)


def cmd_followups(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    rows = outreach.follow_ups(conn, today=now.astimezone(outreach.TZ).date())
    if not rows:
        print("no follow-ups due", file=out)
        return
    print(
        outreach_report.table(
            ["id", "name", "briefed", "channel", "follow-up"],
            [
                [
                    r["id"],
                    _label(r),
                    r["briefed_at"].astimezone(outreach.TZ).date() if r["briefed_at"] else "",
                    r["channel"] or "",
                    r["follow_up_on"],
                ]
                for r in rows
            ],
        ),
        file=out,
    )


def cmd_report(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    today = now.astimezone(outreach.TZ).date()
    floors = outreach_report.load_floors(Path(args.floors).expanduser()) if args.floors else None
    counts = outreach.counts(
        conn,
        today=today,
        since=date.fromisoformat(args.since) if args.since else None,
        until=date.fromisoformat(args.until) if args.until else None,
    )
    verdicts = outreach_report.stage_verdicts(counts, floors, today=today) if floors else None
    print(outreach_report.render(counts, verdicts), file=out)
    below = sum(1 for v in verdicts or [] if v.verdict == "below floor")
    otel.outreach_report_runs.add(1, {"verdict_below": str(below)})


def cmd_sync_asana(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    res = outreach_sync.sync_pending(conn)
    if res.skipped:
        raise CliError("TASKS_API_URL is unset")
    late = ", ".join(f"{bucket} {n}" for bucket, n in sorted(res.late.items()) if bucket != "0d")
    print(f"asana: synced {res.synced}, failed {res.failed}" + (f"; late: {late}" if late else ""), file=out)


COMMANDS: dict[str, Callable[[Any, argparse.Namespace, TextIO, datetime], None]] = {
    "queue": cmd_queue,
    "decide": cmd_decide,
    "draft": cmd_draft,
    "mark": cmd_mark,
    "lead": cmd_lead,
    "intro": cmd_intro,
    "conversation": cmd_buyer,
    "proposal": cmd_buyer,
    "signed": cmd_buyer,
    "lost": cmd_buyer,
    "followups": cmd_followups,
    "report": cmd_report,
    "sync-asana": cmd_sync_asana,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="outreach", description="Work the referral outreach pipeline.")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--stage", default="to_contact", choices=REFERRER_STAGES)
    q.add_argument("--limit", type=int, default=50)
    d = sub.add_parser("decide")
    d.add_argument("person")
    d.add_argument("--skip", action="store_true")
    dr = sub.add_parser("draft")
    dr.add_argument("person")
    dr.add_argument("--subject", default="")
    m = sub.add_parser("mark")
    m.add_argument("person")
    m.add_argument("stage", choices=MARK_STAGES)
    m.add_argument("--channel", choices=CHANNELS)
    m.add_argument("--at")
    m.add_argument("--from", dest="from_address")
    m.add_argument("--force", action="store_true")
    le = sub.add_parser("lead")
    le.add_argument("person")
    le.add_argument("lead")
    i = sub.add_parser("intro")
    i.add_argument("referrer")
    i.add_argument("buyer_label")
    i.add_argument("--buyer")
    i.add_argument("--at")
    for stage in ("conversation", "proposal", "signed"):
        b = sub.add_parser(stage)
        b.add_argument("opportunity", type=int)
        b.add_argument("--at")
    lo = sub.add_parser("lost")
    lo.add_argument("opportunity", type=int)
    lo.add_argument("--reason", required=True, choices=LOST_REASONS)
    lo.add_argument("--at")
    sub.add_parser("followups")
    r = sub.add_parser("report")
    r.add_argument("--floors")
    r.add_argument("--since")
    r.add_argument("--until")
    sub.add_parser("sync-asana")
    return p


def main_with(
    argv: list[str],
    get_conn: Callable[[], Any],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: datetime | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    now = now or datetime.now(UTC)
    otel.setup_telemetry("people-outreach-local")
    try:
        with get_conn() as conn:
            try:
                COMMANDS[args.command](conn, args, out, now)
            except USER_ERRORS as e:
                conn.rollback()
                print(f"error: {e}", file=err)
                return 1
            try:
                outreach.record_population(conn, today=now.astimezone(outreach.TZ).date())
            except Exception:
                pass  # telemetry only; never fail a command over a gauge
        return 0
    finally:
        # A short-lived local process exports nothing unless it flushes (spec §12.2).
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_outreach_cli.py -v`
Expected: PASS. Then the full checks (mypy does not cover `scripts/`; ruff does).

- [ ] **Step 5: Commit**

```bash
git add scripts/outreach.py tests/test_outreach_cli.py
git commit -m "feat(outreach): the outreach CLI"
```

---

### Task 11: The seed script, `scripts/outreach_seed.py`

**Files:**
- Modify: `repo/outreach.py` (source queries)
- Create: `services/outreach_seed.py`, `scripts/outreach_seed.py`
- Test: `tests/test_outreach_seed.py`, `tests/test_outreach_db.py` (append)

**Interfaces:**
- Consumes: `services.outreach.ensure_contact` (Task 3).
- Produces:
  - `repo.outreach.email_seed_rows(conn)`, `linkedin_seed_rows(conn)`, `imessage_seed_rows(conn)`, `whatsapp_seed_rows(conn)` (each `-> list[dict]`), `pipeline_person_ids(conn) -> set[int]`.
  - `services.outreach_seed`: `RECRUITER_TERMS` (compiled regex), `Unlinked(source, name, detail)`, `SeedPlan(candidates: dict[int, set[str]], already_in: set[int], unlinked: list[Unlinked])` with `by_source() -> dict[str, int]`, `plan(*, email_rows, linkedin_rows, imessage_rows, whatsapp_rows, existing: set[int], now: datetime, window_days: int = 730, min_messages: int = 10) -> SeedPlan`.
  - `scripts.outreach_seed.run(get_conn, *, now, window_days, min_messages, dry_run) -> SeedPlan`, `summary(plan, *, dry_run) -> str`, `main_with(argv, get_conn) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outreach_seed.py`:

```python
from datetime import UTC, datetime, timedelta

from scripts import outreach_seed as script
from services import outreach_seed as seed

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(days=30)
OLD = NOW - timedelta(days=800)


def email(pid, **kw):
    return {"id": pid, "email": f"p{pid}@example.com", "display_name": f"P{pid}", "eligible": True,
            "automated": False, "message_count": 3, "my_response_count": 2, "last_interaction": RECENT} | kw


def linkedin(pid, **kw):
    return {"person_id": pid, "full_name": f"L{pid}", "company": "Co", "position": "Engineer",
            "profile_url": f"https://www.linkedin.com/in/l{pid}", "message_count": 4,
            "my_message_count": 2, "last_message_at": RECENT} | kw


def handle(pid, **kw):
    return {"person_id": pid, "handle": f"+1555010000{pid or 9}", "display_name": f"H{pid}",
            "message_count": 12, "my_message_count": 5, "last_message_at": RECENT} | kw


def _plan(**kw):
    rows = {"email_rows": [], "linkedin_rows": [], "imessage_rows": [], "whatsapp_rows": [], "existing": set()}
    return seed.plan(**(rows | kw), now=NOW, window_days=730, min_messages=10)


def test_email_bar():
    p = _plan(email_rows=[
        email(1),
        email(2, eligible=False),
        email(3, automated=True),
        email(4, my_response_count=0),  # Ben never wrote
        email(5, message_count=0),  # they never wrote
        email(6, last_interaction=OLD),
    ])
    assert p.candidates == {1: {"email"}}


def test_linkedin_bar_excludes_recruiters_and_one_sided_threads():
    p = _plan(linkedin_rows=[
        linkedin(1),
        linkedin(2, position="Senior Technical Recruiter"),
        linkedin(3, position="Head of Talent"),
        linkedin(4, my_message_count=4),  # all four messages were Ben's
        linkedin(5, last_message_at=OLD),
        linkedin(6, position=None),
    ])
    assert set(p.candidates) == {1, 6}


def test_text_bars_need_volume_and_a_reply_from_ben():
    p = _plan(
        imessage_rows=[handle(1), handle(2, message_count=9), handle(3, my_message_count=0)],
        whatsapp_rows=[handle(4), handle(5, last_message_at=None)],
    )
    assert p.candidates == {1: {"imessage"}, 4: {"whatsapp"}}


def test_sources_merge_per_person_and_by_source_counts():
    p = _plan(email_rows=[email(1)], imessage_rows=[handle(1)], linkedin_rows=[linkedin(2)])
    assert p.candidates == {1: {"email", "imessage"}, 2: {"linkedin"}}
    assert p.by_source() == {"email": 1, "imessage": 1, "linkedin": 1, "whatsapp": 0}


def test_rows_without_a_person_are_listed_not_inserted():
    p = _plan(linkedin_rows=[linkedin(None)], whatsapp_rows=[handle(None)])
    assert p.candidates == {}
    assert [(u.source, u.detail) for u in p.unlinked] == [
        ("linkedin", "https://www.linkedin.com/in/lNone"),
        ("whatsapp", "+15550100009"),
    ]


def test_people_already_in_the_pipeline_are_never_proposed_again():
    p = _plan(email_rows=[email(1), email(2)], existing={1})
    assert p.candidates == {2: {"email"}} and p.already_in == {1}


class Conn:
    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch):
    repo = script.outreach_repo
    monkeypatch.setattr(repo, "email_seed_rows", lambda c: [email(1), email(2)])
    monkeypatch.setattr(repo, "linkedin_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "imessage_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "whatsapp_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "pipeline_person_ids", lambda c: {2})
    inserted = []
    monkeypatch.setattr(script.outreach, "ensure_contact", lambda c, pid, *, at, source: inserted.append((pid, source)))
    return inserted


def test_dry_run_inserts_nothing(monkeypatch):
    inserted = _wire(monkeypatch)
    conn = Conn()
    p = script.run(lambda: conn, now=NOW, window_days=730, min_messages=10, dry_run=True)
    assert inserted == [] and conn.rolled_back and set(p.candidates) == {1}


def test_a_real_run_inserts_candidates_as_seed(monkeypatch):
    inserted = _wire(monkeypatch)
    script.run(lambda: Conn(), now=NOW, window_days=730, min_messages=10, dry_run=False)
    assert inserted == [(1, "seed")]


def test_summary_counts_and_lists_unlinked_people():
    p = seed.SeedPlan(candidates={1: {"email"}}, already_in={2}, unlinked=[seed.Unlinked("linkedin", "Lee Example", "https://www.linkedin.com/in/lee")])
    text = script.summary(p, dry_run=True)
    assert "DRY RUN" in text and "candidates 1" in text and "already in the pipeline 1" in text
    assert "Lee Example" in text and "creating-person" in text
```

Append to `tests/test_outreach_db.py`:

```python
def test_seed_source_queries_return_the_columns_the_bars_read(conn):
    pid = _person(conn, display_name="Alice", eligible=True, message_count=2, my_response_count=1, last_seen=TS)
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_id, message_count, my_message_count, last_message_at, snapshot_at) "
        "VALUES ('https://www.linkedin.com/in/a', 'Alice', %s, 4, 2, %s, %s)",
        (pid, TS, TS),
    )
    conn.execute(
        "INSERT INTO whatsapp_handles (handle, person_id, message_count, my_message_count, last_message_at) "
        "VALUES ('+15550100001', %s, 12, 5, %s)",
        (pid, TS),
    )
    e = outreach_repo.email_seed_rows(conn)[0]
    assert {k: e[k] for k in ("id", "eligible", "automated", "message_count", "my_response_count", "last_interaction")} == {
        "id": pid, "eligible": True, "automated": False, "message_count": 2, "my_response_count": 1, "last_interaction": TS,
    }
    assert outreach_repo.linkedin_seed_rows(conn)[0]["person_id"] == pid
    assert outreach_repo.whatsapp_seed_rows(conn)[0]["message_count"] == 12
    assert outreach_repo.imessage_seed_rows(conn) == []
    outreach_repo.insert_contact_if_missing(conn, pid)
    assert outreach_repo.pipeline_person_ids(conn) == {pid}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_seed.py tests/test_outreach_db.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Append the source queries to `repo/outreach.py`**

```python
def email_seed_rows(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT id, email, display_name, eligible, automated, message_count, my_response_count,
               GREATEST(COALESCE(last_seen, 'epoch'::timestamptz),
                        COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction
        FROM people WHERE email IS NOT NULL
        """
    ).fetchall()


def linkedin_seed_rows(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT person_id, full_name, company, position, profile_url,
               message_count, my_message_count, last_message_at
        FROM linkedin_connections
        """
    ).fetchall()


def imessage_seed_rows(conn: Any) -> list[dict]:
    """1:1 counters only: imessage_handles.message_count excludes group chats."""
    return conn.execute(
        """
        SELECT person_id, handle, display_name, message_count, my_message_count, last_message_at
        FROM imessage_handles
        """
    ).fetchall()


def whatsapp_seed_rows(conn: Any) -> list[dict]:
    """1:1 counters only: whatsapp_handles.message_count excludes group chats."""
    return conn.execute(
        """
        SELECT person_id, handle, display_name, message_count, my_message_count, last_message_at
        FROM whatsapp_handles
        """
    ).fetchall()


def pipeline_person_ids(conn: Any) -> set[int]:
    return {int(r["person_id"]) for r in conn.execute("SELECT person_id FROM outreach_contacts").fetchall()}
```

- [ ] **Step 4: Create `services/outreach_seed.py`**

```python
"""Propose referral candidates from the warm-pool bars measured 2026-10-12 (outreach
spec §7.1). Pure: the script fetches the rows, this decides. A row with no people
link is listed for Ben (creating the person is the creating-person path), never
inserted, and nobody already in the pipeline is proposed again."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SOURCES = ("email", "imessage", "linkedin", "whatsapp")
RECRUITER_TERMS = re.compile(r"recruit|talent|sourcer|sourcing|headhunt", re.IGNORECASE)


@dataclass
class Unlinked:
    source: str
    name: str | None
    detail: str  # LinkedIn profile URL, or the phone handle


@dataclass
class SeedPlan:
    candidates: dict[int, set[str]] = field(default_factory=dict)  # person id -> sources
    already_in: set[int] = field(default_factory=set)
    unlinked: list[Unlinked] = field(default_factory=list)

    def by_source(self) -> dict[str, int]:
        return {s: sum(1 for srcs in self.candidates.values() if s in srcs) for s in SOURCES}


def _recent(ts: datetime | None, cutoff: datetime) -> bool:
    return ts is not None and ts >= cutoff


def _two_way(row: dict) -> bool:
    """Both sides wrote: Ben sent some, and the total holds some of theirs."""
    return row["my_message_count"] > 0 and row["message_count"] - row["my_message_count"] > 0


def plan(
    *,
    email_rows: list[dict],
    linkedin_rows: list[dict],
    imessage_rows: list[dict],
    whatsapp_rows: list[dict],
    existing: set[int],
    now: datetime,
    window_days: int = 730,
    min_messages: int = 10,
) -> SeedPlan:
    cutoff = now - timedelta(days=window_days)
    result = SeedPlan()

    def add(person_id: int | None, source: str, name: str | None, detail: str) -> None:
        if person_id is None:
            result.unlinked.append(Unlinked(source, name, detail))
        elif person_id in existing:
            result.already_in.add(person_id)
        else:
            result.candidates.setdefault(person_id, set()).add(source)

    for r in email_rows:
        if (
            r["eligible"]
            and not r["automated"]
            and r["message_count"] > 0
            and r["my_response_count"] > 0
            and _recent(r["last_interaction"], cutoff)
        ):
            add(r["id"], "email", r["display_name"], r["email"])
    for r in linkedin_rows:
        if (
            _two_way(r)
            and not RECRUITER_TERMS.search(r["position"] or "")
            and _recent(r["last_message_at"], cutoff)
        ):
            add(r["person_id"], "linkedin", r["full_name"], r["profile_url"])
    for source, rows in (("imessage", imessage_rows), ("whatsapp", whatsapp_rows)):
        for r in rows:
            if (
                _two_way(r)
                and r["message_count"] >= min_messages
                and _recent(r["last_message_at"], cutoff)
            ):
                add(r["person_id"], source, r["display_name"], r["handle"])
    return result
```

- [ ] **Step 5: Create `scripts/outreach_seed.py`**

```python
#!/usr/bin/env python3
# scripts/outreach_seed.py
"""Propose referral candidates and insert them at `candidate`
(docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §7.1). Local only.

  python scripts/outreach_seed.py [--window-days 730] [--min-messages 10] [--dry-run]

Idempotent: a person already in the pipeline is never touched, so it never downgrades
anyone. Candidates are counted, not named (`outreach queue --stage candidate` lists
them). People found on LinkedIn, iMessage or WhatsApp with no people row are listed
by name for Ben to add through the creating-person skill; they print to this terminal
only.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from repo import outreach as outreach_repo
from services import outreach, outreach_seed


def run(
    get_conn: Callable[[], Any], *, now: datetime, window_days: int, min_messages: int, dry_run: bool
) -> outreach_seed.SeedPlan:
    with get_conn() as conn:
        p = outreach_seed.plan(
            email_rows=outreach_repo.email_seed_rows(conn),
            linkedin_rows=outreach_repo.linkedin_seed_rows(conn),
            imessage_rows=outreach_repo.imessage_seed_rows(conn),
            whatsapp_rows=outreach_repo.whatsapp_seed_rows(conn),
            existing=outreach_repo.pipeline_person_ids(conn),
            now=now,
            window_days=window_days,
            min_messages=min_messages,
        )
        if dry_run:
            conn.rollback()
        else:
            for person_id in sorted(p.candidates):
                outreach.ensure_contact(conn, person_id, at=now, source="seed")
    return p


def summary(p: outreach_seed.SeedPlan, *, dry_run: bool) -> str:
    lines = ["DRY RUN — nothing written"] if dry_run else []
    by_source = ", ".join(f"{s} {n}" for s, n in p.by_source().items())
    lines.append(f"candidates {len(p.candidates)} ({by_source}); already in the pipeline {len(p.already_in)}")
    if p.unlinked:
        lines.append(f"{len(p.unlinked)} warm contacts have no people row; add them with creating-person:")
        lines += [f"  {u.source:<9} {u.name or '(no name)'}  {u.detail}" for u in p.unlinked]
    return "\n".join(lines)


def main_with(argv: list[str], get_conn: Callable[[], Any]) -> None:
    ap = argparse.ArgumentParser(prog="outreach_seed")
    ap.add_argument("--window-days", type=int, default=730)
    ap.add_argument("--min-messages", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    otel.setup_telemetry("people-outreach-local")
    try:
        p = run(
            get_conn,
            now=datetime.now(UTC),
            window_days=args.window_days,
            min_messages=args.min_messages,
            dry_run=args.dry_run,
        )
        print(summary(p, dry_run=args.dry_run))
    finally:
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the tests to verify they pass, then the full checks**

Run: `.venv/bin/pytest tests/test_outreach_seed.py tests/test_outreach_db.py -v`
Expected: PASS. Then the full checks.

- [ ] **Step 7: Commit**

```bash
git add repo/outreach.py services/outreach_seed.py scripts/outreach_seed.py tests/test_outreach_seed.py tests/test_outreach_db.py
git commit -m "feat(outreach): seed candidates from the warm-pool bars"
```

---

### Task 12: Configuration: Terraform, deploy workflow, local env

**Files:**
- Modify: `terraform/variables.tf`, `terraform/cloud_functions.tf`, `terraform/terraform.tfvars.example`, `.github/workflows/deploy.yml`, `scripts/fetch-env.sh`, `main.py` (docstring env list)

**Interfaces:**
- Consumes: the env names read in Tasks 2, 5 and 6.
- Produces: `OUTREACH_FROM_ADDRESSES` and `TASKS_API_URL` on both Cloud Functions; `OUTREACH_FROM_ADDRESSES`, `TASKS_API_URL` and `INBOX_API_URL` in the local `.env`.

- [ ] **Step 1: Add the variables to `terraform/variables.tf`**

```hcl
variable "outreach_from_addresses" {
  description = "Comma-separated sending identities whose sent mail counts as a referral brief (outreach spec §6.2). Personal: tfvars / GitHub variable only. Empty means no email ever counts as a brief."
  type        = string
  default     = ""
}
variable "tasks_api_url" {
  description = "tasks-api base URL; also the ID-token audience (outreach spec §8)"
  type        = string
  default     = "https://tasks-api.drolet.cloud"
}
```

- [ ] **Step 2: Add them to `local.common_env` in `terraform/cloud_functions.tf`**

```hcl
    OUTREACH_FROM_ADDRESSES   = var.outreach_from_addresses
    TASKS_API_URL             = var.tasks_api_url
```

- [ ] **Step 3: Pass the variable in `.github/workflows/deploy.yml`**

Under the `Terraform apply` step's `env:` block, add:

```yaml
          TF_VAR_outreach_from_addresses: ${{ vars.OUTREACH_FROM_ADDRESSES }}
```

- [ ] **Step 4: Document it in `terraform/terraform.tfvars.example`**

```hcl
outreach_from_addresses  = "consulting@example.com"   # the primary mailbox; aliases never count as a brief
```

- [ ] **Step 5: Add the local values to `scripts/fetch-env.sh`**

Inside the heredoc, after `OWN_ADDRESSES=...`:

```bash
OUTREACH_FROM_ADDRESSES=$(tfvar outreach_from_addresses)
TASKS_API_URL=$(tfvar tasks_api_url || true)
INBOX_API_URL=https://inbox-api.drolet.cloud
```

and after the heredoc, before `echo ".env written"`, default the tasks URL when tfvars does not set it:

```bash
grep -q '^TASKS_API_URL=.' .env || sed -i '' 's|^TASKS_API_URL=.*|TASKS_API_URL=https://tasks-api.drolet.cloud|' .env
```

- [ ] **Step 6: Extend the env list in the `main.py` docstring**

```
     OUTREACH_FROM_ADDRESSES — identities whose sent mail counts as a referral brief
     TASKS_API_URL — tasks-api, for the completed Asana task per outreach stage event
```

- [ ] **Step 7: Validate**

Run:
```bash
cd terraform && terraform init -backend=false >/dev/null && terraform validate && cd ..
bash -n scripts/fetch-env.sh
```
Expected: `Success! The configuration is valid.` and no syntax error. Then the full checks.

- [ ] **Step 8: Commit**

```bash
git add terraform/ .github/workflows/deploy.yml scripts/fetch-env.sh main.py
git commit -m "chore(config): OUTREACH_FROM_ADDRESSES and TASKS_API_URL on the functions"
```

`terraform/terraform.tfvars` is gitignored; Ben adds the real `outreach_from_addresses` there in Task 16, never in a commit.

---

### Task 13: Documentation, end-to-end check on a throwaway DB, and the PR

**Files:**
- Modify: `CLAUDE.md`, `.claude/skills/people-architecture/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`, `docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md` (status line only)

- [ ] **Step 1: Update `CLAUDE.md`**

Make these edits, in the existing style, with no personal values:

1. **Stack table, Database row:** add `outreach_contacts`, `outreach_opportunities`, `outreach_events` to the table list.
2. **Stack table:** add a row `| **Referral outreach** | \`services/outreach.py\` stage machine over \`outreach_*\`; \`email_sent\` from \`OUTREACH_FROM_ADDRESSES\` proves *briefed*, \`email_classified\` proves *replied*; one completed Asana task per countable stage event via \`clients/tasks_api.py\` (Cloud Run IAM, \`TASKS_API_URL\`); CLI \`scripts/outreach.py\`, seed \`scripts/outreach_seed.py\` |`.
3. **Code layout:** add `models/outreach.py`, `repo/outreach.py`, `services/outreach.py`, `services/outreach_sync.py`, `services/outreach_report.py`, `services/outreach_seed.py`, `services/person_lookup.py`, `clients/gcp_auth.py`, `clients/tasks_api.py`, `clients/inbox_api.py`, `scripts/outreach.py`, `scripts/outreach_seed.py`, each with a one-line responsibility from this plan's File Structure table. Note against `clients/inbox_api.py` that it is CLI-only, so the Graph rule still holds.
4. **Source-of-truth table:** add a row `| \`outreach_*\` tables | DB (\`services/outreach.py\` is the only stage writer) | Never derived from Google; the Asana tasks are derived from \`outreach_events\` and retried nightly. Pipeline rows CASCADE with their person. |`.
5. **The "If the DB is lost" paragraph:** add "The `outreach_*` tables do **not** rebuild from anything. Export them before any `DELETE FROM people`: `pg_dump --data-only -t 'outreach_*'`."
6. **The merge-script paragraph:** add that `_collapse_rows` also calls `services/outreach.py::merge_person`.
7. **Observability row:** mention the `people.outreach_*` instruments.
8. **Local dev:** add a "Referral outreach" block:
   ```bash
   .venv/bin/python scripts/outreach_seed.py --dry-run          # then without --dry-run
   .venv/bin/python scripts/outreach.py queue                    # to_contact, warmest channel visible
   .venv/bin/python scripts/outreach.py report --floors ~/src/consulting/docs/client_funnel_floors.toml
   ```
9. **Env:** `OUTREACH_FROM_ADDRESSES` is personal (tfvars / GitHub variable), like `OWN_ADDRESSES`.

- [ ] **Step 2: Update the two skills**

In `.claude/skills/people-architecture/SKILL.md`, add a short "Referral outreach" section: the three tables, the stage machine's single-writer rule, durable-first then Asana, the nightly backstop. In `.claude/skills/querying-people-db/SKILL.md`, add these two queries under a "Referral outreach" heading:

```sql
-- Stage counts right now
SELECT stage, COUNT(*) FROM outreach_contacts GROUP BY stage ORDER BY stage;

-- Countable events still missing their completed Asana task
SELECT id, pipeline, stage, at, asana_task_gid
FROM outreach_events
WHERE (asana_task_gid IS NULL OR asana_completed_at IS NULL)
  AND (pipeline, stage) IN (('referrer','briefed'),('buyer','intro'),('buyer','conversation'),('buyer','proposal'),('buyer','signed'))
ORDER BY id;
```

- [ ] **Step 3: Update the spec's status line**

Change line 4 of the spec to: `**Status:** Reviewed 2026-10-12. Phase 1 implemented on branch \`referral-outreach-phase1\` (plan: \`docs/superpowers/plans/2026-10-12-referral-outreach-phase1.md\`).`

- [ ] **Step 4: Run the whole suite including the real-Postgres tests**

```bash
docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
sleep 3
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/ -q
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```
Expected: all pass, with `tests/test_outreach_db.py` and `tests/test_schema.py` **run**, not skipped (check the summary's skip count).

- [ ] **Step 5: Drive the CLI end to end against the same container**

The CLI reads `.env`, but `load_dotenv` never overrides variables already set, so these point it at the container. `TASKS_API_URL` is blank, so no real Asana task is created.

```bash
export CLOUD_SQL_CONNECTION_NAME= POSTGRES_HOST=localhost POSTGRES_PORT=55432 \
       POSTGRES_USER=postgres POSTGRES_PASSWORD=test POSTGRES_DB=postgres TASKS_API_URL= \
       GRAFANA_OTLP_ENDPOINT=
psql_() { docker exec -i people-test-pg psql -U postgres -At -c "$1"; }
psql_ "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker exec -i people-test-pg psql -U postgres -q < repo/schema.sql
psql_ "INSERT INTO people (email, display_name, first_seen, eligible, message_count, my_response_count, last_seen) VALUES ('alice@example.com','Alice Example',now(),true,3,2,now())"
.venv/bin/python scripts/outreach_seed.py --dry-run
.venv/bin/python scripts/outreach_seed.py
.venv/bin/python scripts/outreach.py queue --stage candidate
.venv/bin/python scripts/outreach.py decide alice@example.com        # label warning expected: no Google link
.venv/bin/python scripts/outreach.py mark alice@example.com briefed --channel imessage
.venv/bin/python scripts/outreach.py mark alice@example.com briefed --channel imessage   # (unchanged)
.venv/bin/python scripts/outreach.py mark alice@example.com replied --channel imessage
.venv/bin/python scripts/outreach.py intro alice@example.com "Bo Buyer, Co"
.venv/bin/python scripts/outreach.py conversation 1
.venv/bin/python scripts/outreach.py mark alice@example.com briefed --channel email; echo "exit $?"   # noop, exit 0
.venv/bin/python scripts/outreach.py lost 1 --reason timing
.venv/bin/python scripts/outreach.py report
psql_ "SELECT pipeline, stage, source FROM outreach_events ORDER BY id"
docker stop people-test-pg
```

Expected: the seed proposes 1 email candidate; `decide` prints `to_contact` plus a label warning; the second `mark briefed` prints `(unchanged)`; `report` shows one cohort with Briefed 1, Replied 1, Intro 1, Conv 1 and `Lost reasons: timing 1`; the event log reads `candidate, to_contact, briefed, replied, introduced, intro, conversation, lost` with sources `seed` then `cli`. Paste the report output into the PR description.

- [ ] **Step 6: Commit the docs**

```bash
git add CLAUDE.md .claude/skills/ docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md
git commit -m "docs: referral outreach phase 1 in CLAUDE.md and skills"
```

- [ ] **Step 7: Open the PR with `/pr-open`**

Invoke the `pr-open` skill. The description must say, prominently:

- **Migrate before merging.** Once `people-process` deploys, the handlers query `outreach_contacts` on every event. Without the tables every event fails and Pub/Sub retries it. Task 16's first steps run `scripts/migrate_db.py` against production before the merge.
- **Merge the tasks-repo IAM PR (Task 14) first.** Without it every Asana sync gets a 403. The counts are safe either way because they retry nightly, but the strategy layer's weekly counts would land late.
- Set the `OUTREACH_FROM_ADDRESSES` GitHub variable before merging. Without it no email counts as a brief.
- The decisions list from the top of this plan, so the reviewer sees each deviation from the spec.
- The Step 5 transcript, and whether the real-Postgres tests ran.

---

### Task 14: tasks repo: let the two people functions invoke tasks-api

**Files (in `/Users/ben/src/tasks`):**
- Modify: `terraform/variables.tf`, `terraform/api.tf`

Follow that repo's `CLAUDE.md` workflow (branch, `/pr-open` if it uses it). No application change (spec §12.1).

- [ ] **Step 1: Branch**

```bash
cd /Users/ben/src/tasks && git checkout main && git pull && git checkout -b people-outreach-invoker
```

- [ ] **Step 2: Add the variable to `terraform/variables.tf`**, after `api_invoker_users`:

```hcl
variable "api_invoker_service_accounts" {
  description = "Service accounts granted roles/run.invoker on tasks-api: people-process-cf and people-sync-cf post one completed task per referral outreach stage event (people repo, referral outreach spec §8)"
  type        = list(string)
  default = [
    "people-process-cf@bens-project-462804.iam.gserviceaccount.com",
    "people-sync-cf@bens-project-462804.iam.gserviceaccount.com",
  ]
}
```

- [ ] **Step 3: Add the grant to `terraform/api.tf`**, after `api_invoker_users`, and update that resource's comment ("Only the laptop and the deploy smoke test call it") to name the people functions too:

```hcl
resource "google_cloud_run_v2_service_iam_member" "api_invoker_service_accounts" {
  for_each = toset(var.api_invoker_service_accounts)
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}
```

- [ ] **Step 4: Validate and plan**

```bash
cd terraform && terraform init -backend=false >/dev/null && terraform validate && cd ..
```
Then use the `terraform-plan` skill in that repo. Expected plan: exactly two `google_cloud_run_v2_service_iam_member` additions, nothing else.

- [ ] **Step 5: Commit and open the PR** following that repo's workflow, with the commit trailer from Global Constraints. Merge and apply it before the people PR merges.

---

### Task 15: inbox repo: consulting briefs go from the primary address

**Files (in `/Users/ben/src/inbox`):**
- Modify: `.claude/skills/sending-inbox-email/SKILL.md`, `.claude/agents/email-builder.md`

Docs only (spec §12.1). Follow that repo's `CLAUDE.md` workflow.

- [ ] **Step 1: Branch** — `cd /Users/ben/src/inbox && git checkout main && git pull && git checkout -b consulting-briefs-primary`

- [ ] **Step 2: In `sending-inbox-email/SKILL.md`**, directly under the "Sending as a different identity" table, add:

```markdown
**Consulting and referral outreach go from the primary mailbox: omit `from`.** The
people service counts a sent note as a referral brief only when it comes from the
primary address (its `OUTREACH_FROM_ADDRESSES`); a brief sent from an alias stays
uncounted and shows up as unsent in `outreach queue`.
```

- [ ] **Step 3: In `.claude/agents/email-builder.md`**, in "## 3. Pick the identity", add after the first paragraph:

```markdown
Consulting correspondence, including referral outreach briefs, always goes from the
primary mailbox (omit `from`), even when the request names an alias. People counts a
brief only from that address. Say so in your report if you overrode a requested alias.
```

- [ ] **Step 4: Commit and open the PR** following that repo's workflow, with the commit trailer from Global Constraints.

---

### Task 16: Rollout (Ben runs these; production-touching)

Every step here writes to production or to GitHub settings, so each one needs Ben's go-ahead. An executing agent stops at this task and hands over the checklist.

- [ ] **1. Merge and apply the tasks-repo PR** (Task 14). Check that `gcloud run services get-iam-policy tasks-api --region us-central1 --project bens-project-462804` lists both `people-*-cf` service accounts.
- [ ] **2. Set the address.** Add `outreach_from_addresses = "<primary mailbox address>"` to `terraform/terraform.tfvars`, and create the GitHub variable: `gh variable set OUTREACH_FROM_ADDRESSES --body "<primary mailbox address>" --repo bdrolet/people`.
- [ ] **3. Migrate production before the merge.** `scripts/fetch-env.sh` then `.venv/bin/python scripts/migrate_db.py`. The change is additive and idempotent. Confirm with the `querying-people-db` skill: `SELECT COUNT(*) FROM outreach_contacts;` returns `0`.
- [ ] **4. Merge the people PR.** Watch `deploy.yml` and `deploy-api.yml` complete. Use `fetch-people-logs` on `people-process` for ten minutes: no new errors, and `email_sent ... briefed` log lines read `0 briefed` until someone is at `to_contact`.
- [ ] **5. Re-run `scripts/fetch-env.sh`** so the local `.env` has the three new values.
- [ ] **6. Seed.** `.venv/bin/python scripts/outreach_seed.py --dry-run`, review the counts and the unlinked list, then run it without `--dry-run`.
- [ ] **7. First real brief.** `outreach decide <someone>`, then `outreach draft <someone>`. Write and send the note from Outlook. Within a minute `outreach queue --stage briefed` lists them. A completed `Brief: <name>` task tagged `outreach` and `serves:consulting` exists in Asana. `outreach sync-asana` prints `synced 0, failed 0`.
- [ ] **8. Nightly check, the next morning.** The `people-sync` log shows `asana={'synced': 0, 'failed': 0, ...}`. The `querying-grafana-metrics` skill finds `people_outreach_stage_events_total` and `people_outreach_stage_population`.
- [ ] **9. Report.** `outreach report --floors ~/src/consulting/docs/client_funnel_floors.toml` prints both tables.

---

## Self-review notes (for the reviewer of this plan)

- **Spec coverage, phase 1 (§13).** Schema §5: Task 1. Service §6.1: Tasks 2, 3, 8, 9. Handlers §6.2–§6.3: Task 7. tasks-api client and Asana sync §8: Tasks 5, 6, plus the nightly retry in Task 7. CLI §7.4: Task 10, including `draft` (decision 11). Seed §7.1: Task 11. Report §9: Task 9. Telemetry §12.2: Task 3 instruments, Task 8 gauges, Task 10 report runs; `outreach.enrichment` is phase 3 and is not built. Env and IAM §12.1: Tasks 12 and 14. Inbox docs §12.1: Task 15. Rebuild runbook §12.1: Task 13. Testing §11: every listed test file exists, except the `test_hubspot_mirror.py` additions, which belong to phase 2.
- **Deliberately not in phase 1:** HubSpot property, deals and follow-up tasks (§10), `outreach sync-hubspot`, `GET /outreach/queue` and `GET /outreach/report`, the `working-outreach-queue` skill, snapshot-import stage advances, calendar matching, enrichment.
- **Spec gap closed:** the merge-duplicates cascade (Task 4).
