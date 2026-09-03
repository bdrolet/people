# People Service Implementation Plan (Phase A + Phase C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the new public `bdrolet/people` service — a Cloud Function that turns inbox's `email_classified` / `email_sent` events into a per-person index (Postgres), canonical Google Contacts, and a capped HubSpot mirror, plus a `people-api` Cloud Run service — and deploy it alongside the unchanged inbox (Phase A). Phase C (switching HubSpot writes on) is the final task and runs only after the inbox-side plan has shipped.

**Architecture:** `people-process` subscribes to the inbox-owned `email-events` Pub/Sub topic. `services/ingest.py` updates counters and eligibility in the `people` DB first (durable), then best-effort side effects: `services/google_contacts_sync.py` creates/links a Google Contact for newly eligible people, `services/hubspot_mirror.py` keeps at most `HUBSPOT_MAX_CONTACTS` most-recent people in HubSpot. `people-sync` (HTTP, Cloud Scheduler nightly) pulls Google Contacts changes by sync token and reconciles HubSpot. `people-api` (FastAPI) reads the DB and writes edits through to Google.

**Tech Stack:** Python 3.13, functions-framework, FastAPI + uvicorn, psycopg3 (local) / pg8000 via Cloud SQL Python Connector (prod), `google-api-python-client` + `google-auth` (People API v1), `hubspot-api-client`, OpenTelemetry → Grafana Cloud, Terraform (GCS backend prefix `people`), GitHub Actions with WIF.

**Spec:** `docs/superpowers/specs/2026-09-03-people-service-extraction-design.md` (copied from inbox; inbox's copy is authoritative until this repo has its first commit, then they are identical).

**Companion plan:** the inbox-side changes (Phase B) are `~/src/inbox/docs/superpowers/plans/2026-09-03-people-extraction-inbox.md`. Tasks 1–17 here must be complete and Gate A passed before that plan starts. Task 18 here runs after that plan's Gate B.

## Global Constraints

- **Public repo.** Nothing personal in code or committed config: the HubSpot owner id, own email addresses, skip domains, tokens, DB passwords are env vars / Secret Manager / `terraform.tfvars` (gitignored). `HUBSPOT_OWNER_ID` has **no default**.
- **Layer rules** (spec §3): `clients/` I/O only; `repo/` takes an open conn, never opens one; `services/` one concern per file, no direct HTTP; `handlers/` orchestrate, called only from `main.py`; `models/` pure types; `main.py` transport only, `otel.flush()` in `finally`; `api/routers/` thin.
- **DB write first, external calls after, never fail the event on an external error** (spec §6.1). Counters are not idempotent under redelivery — accepted.
- **Eligibility is monotonic** (spec §5). **Never recreate a Google contact once `google_deleted_at` is set** (spec §4.3).
- **HubSpot cap is hard**: evict a managed contact before creating when `count >= HUBSPOT_MAX_CONTACTS` (default `1000`); never archive unmanaged contacts (spec §8.1). `HUBSPOT_WRITES_ENABLED` defaults to `false` until Task 18.
- **Google auth**: reuse schedule's OAuth client (`google-calendar-client-id`, `google-calendar-client-secret`, data sources); people owns `google-contacts-refresh-token`; scope `https://www.googleapis.com/auth/contacts`; build `Credentials` **without** scopes (spec §7.1).
- **Shared infra is referenced, never re-created**: `email-events` topic (inbox), Cloud SQL instance `inbox` (inbox), `grafana-otlp-endpoint`/`grafana-otlp-token` (platform state `~/src/infra`), the two Google OAuth client secrets (schedule). `hubspot-token` is **imported** into this state, never created.
- **Metrics prefix** `people_`; OTel service names `people-process`, `people-sync`, `people-api`.
- Local CI = `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && (cd terraform && terraform validate)`.
- Repo workflow: after Task 1 the repo has `main`; every later task commits to the feature branch `people-v1`; the PR at Task 17 goes through `/pr-open`. Terraform only via `/terraform-plan` and `/terraform-apply`.
- Python line length 100, ruff `E,F,I`, mypy `ignore_missing_imports`, `repo.*` and `clients.db` excluded from mypy (as tasks).

## Skills used by this plan

| Skill | Where |
|---|---|
| `setting-up-service-repo` | Task 1 (scaffold agents, CI/CD, skills checklist) |
| `creating-skills` / `superpowers:writing-skills` | Task 16 |
| `/terraform-plan`, `/terraform-apply` | Task 15, Task 17 |
| `/pr-open` | Task 17 |
| `using-1password-cli` | Task 17 (HubSpot token, if `.env` lacks it) |
| `refreshing-msal-token` (inbox) | **not** used — the import script uses its own cache file |

## File structure

```
people/
  main.py                       CF entry points: process (Pub/Sub) + sync (HTTP)
  models/
    events.py                   EmailClassifiedEvent, EmailSentEvent TypedDicts (mirror inbox payloads)
    types.py                    IngestResult dataclass
  clients/
    db.py                       copied from tasks (POSTGRES_DB default "people")
    otel.py                     copied from tasks, metrics renamed (people_*)
    hubspot.py                  HubSpot CRM I/O: find/create/update/archive/count/list contacts, log_email
    google_contacts.py          People API v1 I/O: credentials, search, create, biography, groups, connections sync
  repo/
    schema.sql                  people, sync_state
    people.py                   all reads/writes on people
    sync_state.py               get/set sync token
  services/
    eligibility.py              is_automated, is_own, compute eligibility
    ingest.py                   record_inbound / record_outbound (DB-only, returns IngestResult)
    google_contacts_sync.py     ensure_contact, run_sync, apply_person, relationship_label
    hubspot_mirror.py           ensure_contact, log_email, reconcile (cap logic)
    person_edit.py              PATCH write-through (Google first, DB second)
    sync_auth.py                bearer check for POST /sync
  handlers/
    email_classified.py         handle(event)
    email_sent.py               handle(event)
    sync.py                     run() → counts (Google sync + HubSpot reconcile)
  api/
    main.py, auth.py, errors.py, requirements.txt
    routers/people.py           GET/PATCH /people/{email}, GET /people?recent=, POST /people/{email}/sync
    routers/search.py           POST /search
  scripts/
    migrate_db.py               copied from tasks
    get_google_contacts_token.py
    import_contacts.py          local MSAL device-code scan of Inbox + Sent Items → services/ingest
    test-api-local.py           smoke test used by deploy-api.yml
  terraform/                    main, variables, secrets, cloudsql, cloud_functions, scheduler, api, iam, pubsub
  tests/                        one file per service/handler/router
  .github/workflows/            ci.yml, deploy.yml, deploy-api.yml
  .claude/skills/               Task 16
  docs/superpowers/specs|plans  this spec + plan
```

---

### Task 1: Scaffold the repo

**Files:**
- Create: everything the `setting-up-service-repo` skill's Phase 1 + 2 agents produce (`clients/db.py`, `clients/otel.py`, `pyproject.toml`, `requirements*.txt`, `.pre-commit-config.yaml`, `.gitignore`, `.github/workflows/{ci,deploy,deploy-api}.yml`, `Dockerfile`, `.dockerignore`, `api/{main,auth,errors}.py`, `scripts/fetch-env.sh`, `CLAUDE.md` skeleton)
- Create: `README.md` (one paragraph for now; Task 16 fills it)

**Interfaces:**
- Produces: `clients.db.get_conn()` (context manager yielding a conn with `.execute(sql, params) -> cursor` with `.fetchone()/.fetchall()`, `.commit()`), `clients.otel.setup_telemetry(service_name)`, `clients.otel.flush()`, `clients.otel.get_tracer()`, and the metric objects defined in Step 4.

- [ ] **Step 1: Create the GitHub repo and local git**

```bash
cd ~/src/people
git init -b main
gh repo create bdrolet/people --public --source=. --remote=origin --description "People service: Google Contacts + HubSpot mirror + people-api, fed by inbox email events"
```

Expected: `origin` set to `git@github.com:bdrolet/people.git` (or https), repo public.

- [ ] **Step 2: Run the scaffold skill**

Invoke `setting-up-service-repo` with: repo name `people`; components = Pub/Sub CF (`people-process`, entry `process`), HTTP CF (`people-sync`, entry `sync`), Cloud Run API (`people-api`), Cloud SQL (`people` DB on instance `inbox`); topic `email-events` (data source); external APIs = HubSpot, Google People API; GitHub remote already exists. Let its Phase 1 agents copy boilerplate from `~/src/tasks`. **Skip its Phase 2 (skills) for now** — Task 16 does that with the real routes known.

- [ ] **Step 3: Fix the DB defaults in `clients/db.py`**

Two edits in the copied file: `db=os.environ.get("POSTGRES_DB", "tasks")` → `"people"` (both the connector and the direct branches).

- [ ] **Step 4: Replace the metric block in `clients/otel.py`**

Delete the tasks metric globals and their `meter.create_*` lines; put these in their place (module globals first, then inside `setup_telemetry` after `meter = ...`):

```python
events_received: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
people_upserts: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
eligibility_changes: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
google_contacts_created: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
google_sync_changes: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
hubspot_contacts_created: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
hubspot_evictions: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
hubspot_engagements_logged: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
external_errors: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
api_requests: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
errors: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

```python
    global events_received, people_upserts, eligibility_changes, google_contacts_created
    global google_sync_changes, hubspot_contacts_created, hubspot_evictions
    global hubspot_engagements_logged, external_errors, api_requests, errors
    events_received = meter.create_counter("people.events_received", description="Events by kind")
    people_upserts = meter.create_counter("people.upserts", description="Row upserts by direction")
    eligibility_changes = meter.create_counter("people.eligibility_changes", description="Rows that became eligible")
    google_contacts_created = meter.create_counter("people.google_contacts_created")
    google_sync_changes = meter.create_counter("people.google_sync_changes", description="Sync changes by kind")
    hubspot_contacts_created = meter.create_counter("people.hubspot_contacts_created")
    hubspot_evictions = meter.create_counter("people.hubspot_evictions")
    hubspot_engagements_logged = meter.create_counter("people.hubspot_engagements_logged")
    external_errors = meter.create_counter("people.external_errors", description="Swallowed external failures by system")
    api_requests = meter.create_counter("people.api_requests", description="people-api requests by route/status")
    errors = meter.create_counter("people.errors", description="Handler errors by handler")
```

(Grafana renders `people.foo` as `people_foo`.)

- [ ] **Step 5: Requirements**

`requirements.txt`:

```
# HubSpot CRM
hubspot-api-client>=10.0

# Google People API
google-api-python-client>=2.130
google-auth>=2.30

# Cloud Functions
functions-framework>=3.0

# Database — connector uses pg8000 (does NOT support psycopg); psycopg3 for local direct
psycopg>=3.1
pg8000>=1.31
cloud-sql-python-connector>=1.7.0

# Observability
opentelemetry-sdk>=1.24
opentelemetry-exporter-otlp-proto-http>=1.24

# Environment (local dev / scripts)
python-dotenv>=1.0.0
```

`requirements-dev.txt`: tasks' file with `anthropic` removed and `hubspot-api-client>=10.0`, `google-api-python-client>=2.130`, `google-auth>=2.30`, `httpx>=0.27` (TestClient) added. `api/requirements.txt`: `fastapi>=0.111`, `uvicorn[standard]>=0.29`. Add to the top of `requirements-dev.txt` for the import script: `msal>=1.28`, `requests>=2.31`, `google-auth-oauthlib>=1.2`.

- [ ] **Step 6: Venv, install, verify the skeleton runs**

```bash
cd ~/src/people
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -c "import clients.db, clients.otel; print('ok')"
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Expected: `ok`, ruff clean (format anything it flags).

- [ ] **Step 7: Commit the scaffold on main, then branch**

```bash
git add -A
git commit -m "chore: scaffold people service from the tasks template"
git push -u origin main
git checkout -b people-v1
```

---

### Task 2: Event models and eligibility service

**Files:**
- Create: `models/events.py`, `models/types.py`, `services/eligibility.py`
- Test: `tests/test_eligibility.py`

**Interfaces:**
- Produces: `EmailClassifiedEvent`, `EmailSentEvent` TypedDicts; `IngestResult(row: dict, newly_eligible: bool)`; `eligibility.is_automated(email: str) -> bool`; `eligibility.is_own(email: str) -> bool`; `eligibility.normalize(email: str) -> str`; `eligibility.inbound_eligible(email, category: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eligibility.py
import pytest

from services import eligibility


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AUTOMATED_SENDER_PATTERN", raising=False)
    monkeypatch.setenv("AUTOMATED_SENDER_DOMAINS", "group.calendar.google.com,bcc.na2.hubspot.com")
    monkeypatch.setenv("OWN_ADDRESSES", "Me@Example.com, alias@example.com")


def test_normalize_lowercases_and_strips():
    assert eligibility.normalize("  Alice@Example.COM ") == "alice@example.com"


@pytest.mark.parametrize(
    "addr",
    [
        "no-reply@x.com",
        "NoReply@x.com",
        "do_not_reply@x.com",
        "mailer-daemon@x.com",
        "notifications@github.com",
        "alerts@x.com",
        "support@x.com",
        "newsletter@x.com",
        "cal@group.calendar.google.com",
        "me@example.com",
    ],
)
def test_automated_addresses(addr):
    assert eligibility.is_automated(addr) is True


@pytest.mark.parametrize("addr", ["alice@example.com", "bob.smith@corp.io"])
def test_human_addresses(addr):
    assert eligibility.is_automated(addr) is False


def test_empty_is_automated():
    assert eligibility.is_automated("") is True


def test_is_own_case_insensitive():
    assert eligibility.is_own("ME@example.com") is True
    assert eligibility.is_own("alias@example.com") is True
    assert eligibility.is_own("alice@example.com") is False


def test_pattern_override(monkeypatch):
    monkeypatch.setenv("AUTOMATED_SENDER_PATTERN", r"^bot@")
    assert eligibility.is_automated("bot@x.com") is True
    assert eligibility.is_automated("no-reply@x.com") is False


def test_inbound_eligible():
    assert eligibility.inbound_eligible("alice@example.com", "review") is True
    assert eligibility.inbound_eligible("alice@example.com", "ignore") is False
    assert eligibility.inbound_eligible("no-reply@x.com", "urgent") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_eligibility.py -q`
Expected: `ModuleNotFoundError: services.eligibility`

- [ ] **Step 3: Implement**

```python
# models/events.py
"""Typed payloads arriving on the email-events topic. Mirrors what inbox
publishes (inbox services/email_events.py). Domain events, not commands."""

from typing import Literal, NotRequired, TypedDict


class EmailClassifiedEvent(TypedDict):
    event: Literal["email_classified"]
    message_id: str
    category: str  # urgent | respond | review | reference | ignore
    importance: str
    confidence: float
    subject: str
    sender: str
    sender_display: str
    to: list[str]
    cc: list[str]
    received_at: str  # ISO-8601
    tags: list[str]
    reasoning: str
    body: str
    body_html: str | None
    web_link: str | None
    graph_message_id: NotRequired[str]
    has_attachments: NotRequired[bool]
    is_meeting_message: NotRequired[bool]


class EmailSentEvent(TypedDict):
    event: Literal["email_sent"]
    graph_message_id: str
    conversation_id: str | None
    sent_at: str  # ISO-8601
    sender: str  # "from" in JSON is a keyword — inbox publishes the key "from"; handlers read event["from"]
    to: list[str]
    cc: list[str]
    subject: str
```

(The `sender` field above is documentation only; the JSON key is `from`. Handlers use `event.get("from")`.)

```python
# models/types.py
from dataclasses import dataclass


@dataclass
class IngestResult:
    row: dict  # the people row after the write
    newly_eligible: bool  # False → eligible before this event, or still not eligible
```

```python
# services/eligibility.py
"""Who deserves a Google Contact (spec §5). Pure functions over env config so
nothing personal lives in code."""

import os
import re

DEFAULT_PATTERN = (
    r"(no.?reply|noreply|do.?not.?reply|mailer.?daemon|notifications?@|alerts?@|support@|newsletter@)"
)


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def _csv_env(name: str) -> set[str]:
    return {v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()}


def is_own(email: str) -> bool:
    return normalize(email) in _csv_env("OWN_ADDRESSES")


def is_automated(email: str) -> bool:
    addr = normalize(email)
    if not addr or "@" not in addr:
        return True
    if is_own(addr):
        return True
    if addr.rsplit("@", 1)[1] in _csv_env("AUTOMATED_SENDER_DOMAINS"):
        return True
    pattern = os.environ.get("AUTOMATED_SENDER_PATTERN") or DEFAULT_PATTERN
    return re.search(pattern, addr, re.IGNORECASE) is not None


def inbound_eligible(email: str, category: str) -> bool:
    """An inbound email makes its sender eligible unless the sender is
    automated or the email was filed as ignore."""
    return not is_automated(email) and category != "ignore"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_eligibility.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add models/ services/eligibility.py tests/test_eligibility.py
git commit -m "feat: event models and eligibility rules"
```

---

### Task 3: Schema and `repo/people.py`

**Files:**
- Create: `repo/schema.sql`, `repo/people.py`, `repo/sync_state.py`, `repo/__init__.py`, `scripts/migrate_db.py` (copy of `~/src/tasks/scripts/migrate_db.py` with `POSTGRES_USER=people POSTGRES_DB=people` in the docstring)
- Test: `tests/test_repo_people.py`

**Interfaces:**
- Produces (all take `conn` first; rows are dicts with the columns of §4.1 plus computed `last_interaction`):
  - `upsert_inbound(conn, email, display_name, received_at: datetime) -> dict`
  - `upsert_outbound(conn, email, display_name, sent_at: datetime) -> dict`
  - `set_flags(conn, email, *, automated: bool, eligible: bool) -> None`
  - `get(conn, email) -> dict | None`
  - `get_by_google_resource(conn, resource_name) -> dict | None`
  - `search(conn, q: str, limit: int) -> list[dict]`
  - `recent(conn, limit: int, eligible_only: bool = True) -> list[dict]`
  - `set_google(conn, email, *, resource_name, etag, display_name=None, notes=None, relationship_label=None) -> None`
  - `update_from_google(conn, resource_name, *, etag, display_name, notes, relationship_label) -> None`
  - `mark_google_deleted(conn, resource_name) -> None`
  - `create_from_google(conn, email, *, display_name, resource_name, etag, notes, relationship_label) -> dict`
  - `set_hubspot(conn, email, contact_id) -> None`, `clear_hubspot(conn, email) -> None`
  - `oldest_managed(conn) -> dict | None`
  - `managed(conn) -> list[dict]` (all rows with `hubspot_contact_id`)
  - `eligible_not_in_hubspot(conn, limit) -> list[dict]` (most recent first)
  - `sync_state.get_token(conn) -> str | None`, `sync_state.set_token(conn, token, status)`

- [ ] **Step 1: Schema**

```sql
-- repo/schema.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS people (
    email                 TEXT PRIMARY KEY,
    display_name          TEXT,
    first_seen            TIMESTAMPTZ NOT NULL,
    last_seen             TIMESTAMPTZ,
    last_contacted        TIMESTAMPTZ,
    message_count         INT  NOT NULL DEFAULT 0,
    my_response_count     INT  NOT NULL DEFAULT 0,
    relationship_label    TEXT,
    notes                 TEXT,
    eligible              BOOLEAN NOT NULL DEFAULT FALSE,
    automated             BOOLEAN NOT NULL DEFAULT FALSE,
    google_resource_name  TEXT UNIQUE,
    google_etag           TEXT,
    google_deleted_at     TIMESTAMPTZ,
    hubspot_contact_id    TEXT UNIQUE,
    hubspot_synced_at     TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS people_last_interaction_idx
    ON people (GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) DESC);
CREATE INDEX IF NOT EXISTS people_display_name_trgm_idx ON people USING gin (display_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    sync_token  TEXT,
    last_run_at TIMESTAMPTZ,
    last_status TEXT
);
```

- [ ] **Step 2: Write the failing repo tests (recording fake connection)**

```python
# tests/test_repo_people.py
from datetime import UTC, datetime

from repo import people


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records every (sql, params) and returns canned rows in order."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple | None]] = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        rows = self._results.pop(0) if self._results else []
        return FakeCursor(rows)


TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_upsert_inbound_lowercases_and_increments():
    conn = FakeConn(results=[[{"email": "alice@example.com", "message_count": 2}]])
    row = people.upsert_inbound(conn, "Alice@Example.com", "Alice", TS)
    sql, params = conn.calls[0]
    assert "INSERT INTO people" in sql and "ON CONFLICT (email)" in sql
    assert "message_count = people.message_count + 1" in sql
    assert "RETURNING" in sql
    assert params[0] == "alice@example.com"
    assert row["message_count"] == 2


def test_upsert_inbound_keeps_existing_display_name():
    conn = FakeConn(results=[[{"email": "a@b.c"}]])
    people.upsert_inbound(conn, "a@b.c", "New Name", TS)
    sql, _ = conn.calls[0]
    assert "display_name = COALESCE(people.display_name, EXCLUDED.display_name)" in sql


def test_upsert_outbound_increments_response_count():
    conn = FakeConn(results=[[{"email": "a@b.c"}]])
    people.upsert_outbound(conn, "a@b.c", None, TS)
    sql, _ = conn.calls[0]
    assert "my_response_count = people.my_response_count + 1" in sql
    assert "last_contacted = GREATEST" in sql


def test_set_flags_is_monotonic_on_eligible():
    conn = FakeConn()
    people.set_flags(conn, "a@b.c", automated=False, eligible=True)
    sql, params = conn.calls[0]
    assert "eligible = people.eligible OR %s" in sql
    assert params == (False, True, "a@b.c")


def test_search_uses_trigram_and_email_match():
    conn = FakeConn(results=[[]])
    people.search(conn, "ali", 10)
    sql, params = conn.calls[0]
    assert "email ILIKE %s" in sql and "display_name % %s" in sql
    assert params[0] == "%ali%"


def test_oldest_managed_orders_by_last_interaction_asc():
    conn = FakeConn(results=[[{"email": "old@b.c"}]])
    row = people.oldest_managed(conn)
    sql, _ = conn.calls[0]
    assert "hubspot_contact_id IS NOT NULL" in sql and "ASC LIMIT 1" in sql
    assert row["email"] == "old@b.c"


def test_mark_google_deleted_clears_resource_name():
    conn = FakeConn()
    people.mark_google_deleted(conn, "people/c1")
    sql, params = conn.calls[0]
    assert "google_deleted_at = now()" in sql and "google_resource_name = NULL" in sql
    assert params == ("people/c1",)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_repo_people.py -q`
Expected: `ModuleNotFoundError: repo.people`

- [ ] **Step 4: Implement `repo/people.py`**

```python
# repo/people.py
"""All reads and writes on the people table. Takes an open connection; never
opens one. Rows come back as dicts (both connection flavours in clients/db.py
return dict rows)."""

from datetime import datetime
from typing import Any

_COLUMNS = """
    email, display_name, first_seen, last_seen, last_contacted, message_count,
    my_response_count, relationship_label, notes, eligible, automated,
    google_resource_name, google_etag, google_deleted_at, hubspot_contact_id,
    hubspot_synced_at, updated_at,
    GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction
"""

_LAST_INTERACTION = (
    "GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz))"
)


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def upsert_inbound(conn: Any, email: str, display_name: str | None, received_at: datetime) -> dict:
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, last_seen, message_count)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT (email) DO UPDATE SET
            display_name  = COALESCE(people.display_name, EXCLUDED.display_name),
            last_seen     = GREATEST(COALESCE(people.last_seen, 'epoch'::timestamptz), EXCLUDED.last_seen),
            message_count = people.message_count + 1,
            updated_at    = now()
        RETURNING {_COLUMNS}
        """,
        (_norm(email), display_name or None, received_at, received_at),
    ).fetchone()


def upsert_outbound(conn: Any, email: str, display_name: str | None, sent_at: datetime) -> dict:
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, last_contacted, my_response_count)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT (email) DO UPDATE SET
            display_name      = COALESCE(people.display_name, EXCLUDED.display_name),
            last_contacted    = GREATEST(COALESCE(people.last_contacted, 'epoch'::timestamptz), EXCLUDED.last_contacted),
            my_response_count = people.my_response_count + 1,
            updated_at        = now()
        RETURNING {_COLUMNS}
        """,
        (_norm(email), display_name or None, sent_at, sent_at),
    ).fetchone()


def set_flags(conn: Any, email: str, *, automated: bool, eligible: bool) -> None:
    conn.execute(
        """
        UPDATE people SET automated = %s, eligible = people.eligible OR %s, updated_at = now()
        WHERE email = %s
        """,
        (automated, eligible, _norm(email)),
    )


def get(conn: Any, email: str) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE email = %s", (_norm(email),)
    ).fetchone()


def get_by_google_resource(conn: Any, resource_name: str) -> dict | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE google_resource_name = %s", (resource_name,)
    ).fetchone()


def search(conn: Any, q: str, limit: int) -> list[dict]:
    like = f"%{q.strip().lower()}%"
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE email ILIKE %s OR display_name % %s
        ORDER BY {_LAST_INTERACTION} DESC
        LIMIT %s
        """,
        (like, q.strip(), limit),
    ).fetchall()


def recent(conn: Any, limit: int, eligible_only: bool = True) -> list[dict]:
    where = "WHERE eligible" if eligible_only else ""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people {where} ORDER BY {_LAST_INTERACTION} DESC LIMIT %s",
        (limit,),
    ).fetchall()


def set_google(
    conn: Any,
    email: str,
    *,
    resource_name: str,
    etag: str | None,
    display_name: str | None = None,
    notes: str | None = None,
    relationship_label: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE people SET
            google_resource_name = %s,
            google_etag          = %s,
            display_name         = COALESCE(%s, display_name),
            notes                = COALESCE(%s, notes),
            relationship_label   = COALESCE(%s, relationship_label),
            updated_at           = now()
        WHERE email = %s
        """,
        (resource_name, etag, display_name, notes, relationship_label, _norm(email)),
    )


def update_from_google(
    conn: Any,
    resource_name: str,
    *,
    etag: str | None,
    display_name: str | None,
    notes: str | None,
    relationship_label: str | None,
) -> None:
    """Google is the truth for these fields: overwrite, including with NULL."""
    conn.execute(
        """
        UPDATE people SET google_etag = %s, display_name = COALESCE(%s, display_name),
            notes = %s, relationship_label = %s, updated_at = now()
        WHERE google_resource_name = %s
        """,
        (etag, display_name, notes, relationship_label, resource_name),
    )


def mark_google_deleted(conn: Any, resource_name: str) -> None:
    conn.execute(
        """
        UPDATE people SET google_deleted_at = now(), google_resource_name = NULL,
            google_etag = NULL, updated_at = now()
        WHERE google_resource_name = %s
        """,
        (resource_name,),
    )


def create_from_google(
    conn: Any,
    email: str,
    *,
    display_name: str | None,
    resource_name: str,
    etag: str | None,
    notes: str | None,
    relationship_label: str | None,
) -> dict:
    """A contact Ben made by hand that people has never seen mail from."""
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, eligible, automated,
                            google_resource_name, google_etag, notes, relationship_label)
        VALUES (%s, %s, now(), TRUE, FALSE, %s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE SET
            google_resource_name = EXCLUDED.google_resource_name,
            google_etag = EXCLUDED.google_etag,
            display_name = COALESCE(EXCLUDED.display_name, people.display_name),
            notes = EXCLUDED.notes, relationship_label = EXCLUDED.relationship_label,
            eligible = TRUE, updated_at = now()
        RETURNING {_COLUMNS}
        """,
        (_norm(email), display_name, resource_name, etag, notes, relationship_label),
    ).fetchone()


def set_hubspot(conn: Any, email: str, contact_id: str) -> None:
    conn.execute(
        "UPDATE people SET hubspot_contact_id = %s, hubspot_synced_at = now(), updated_at = now() WHERE email = %s",
        (contact_id, _norm(email)),
    )


def clear_hubspot(conn: Any, email: str) -> None:
    conn.execute(
        "UPDATE people SET hubspot_contact_id = NULL, hubspot_synced_at = NULL, updated_at = now() WHERE email = %s",
        (_norm(email),),
    )


def oldest_managed(conn: Any) -> dict | None:
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people WHERE hubspot_contact_id IS NOT NULL
        ORDER BY {_LAST_INTERACTION} ASC LIMIT 1
        """
    ).fetchone()


def managed(conn: Any) -> list[dict]:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM people WHERE hubspot_contact_id IS NOT NULL"
    ).fetchall()


def eligible_not_in_hubspot(conn: Any, limit: int) -> list[dict]:
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE eligible AND hubspot_contact_id IS NULL
        ORDER BY {_LAST_INTERACTION} DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()
```

```python
# repo/sync_state.py
from typing import Any

KEY = "google_contacts"


def get_token(conn: Any) -> str | None:
    row = conn.execute("SELECT sync_token FROM sync_state WHERE key = %s", (KEY,)).fetchone()
    return row["sync_token"] if row else None


def set_token(conn: Any, token: str | None, status: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (key, sync_token, last_run_at, last_status)
        VALUES (%s, %s, now(), %s)
        ON CONFLICT (key) DO UPDATE SET sync_token = EXCLUDED.sync_token,
            last_run_at = now(), last_status = EXCLUDED.last_status
        """,
        (KEY, token, status),
    )
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_repo_people.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add repo/ scripts/migrate_db.py tests/test_repo_people.py
git commit -m "feat: people schema and repo layer"
```

---

### Task 4: `services/ingest.py` (DB-only counters + eligibility)

**Files:**
- Create: `services/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `repo.people.upsert_inbound/upsert_outbound/set_flags/get`, `services.eligibility.*`, `models.types.IngestResult`.
- Produces: `ingest.record_inbound(conn, *, sender: str, display: str | None, received_at: datetime, category: str) -> IngestResult`; `ingest.record_outbound(conn, *, recipients: list[str], display_by_email: dict[str, str] | None, sent_at: datetime) -> list[IngestResult]`; `ingest.parse_ts(value: str) -> datetime`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_ingest.py
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
        row = self.rows.setdefault(email, {"email": email, "eligible": False, "my_response_count": 0})
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
    res = ingest.record_inbound(conn=None, sender="Alice@Example.com", display="Alice", received_at=TS, category="review")
    assert res.newly_eligible is True
    assert res.row["eligible"] is True
    assert repo.flags == [("alice@example.com", False, True)]


def test_inbound_ignore_does_not_make_eligible_but_counts(repo):
    res = ingest.record_inbound(conn=None, sender="alice@example.com", display=None, received_at=TS, category="ignore")
    assert res.newly_eligible is False
    assert res.row["eligible"] is False
    assert res.row["message_count"] == 1


def test_inbound_already_eligible_is_not_newly_eligible(repo):
    ingest.record_inbound(conn=None, sender="alice@example.com", display=None, received_at=TS, category="review")
    res = ingest.record_inbound(conn=None, sender="alice@example.com", display=None, received_at=TS, category="review")
    assert res.newly_eligible is False and res.row["eligible"] is True


def test_inbound_automated_never_eligible(repo):
    res = ingest.record_inbound(conn=None, sender="no-reply@x.com", display=None, received_at=TS, category="urgent")
    assert res.row["eligible"] is False and res.row["automated"] is True


def test_outbound_skips_own_address_and_marks_eligible(repo):
    results = ingest.record_outbound(
        conn=None, recipients=["me@example.com", "Bob@x.com", "bob@x.com"], display_by_email=None, sent_at=TS
    )
    assert [r.row["email"] for r in results] == ["bob@x.com", "bob@x.com"]
    assert results[0].newly_eligible is True
    assert results[1].newly_eligible is False
    assert repo.rows["bob@x.com"]["my_response_count"] == 2


def test_outbound_to_automated_address_counts_but_not_eligible(repo):
    results = ingest.record_outbound(conn=None, recipients=["support@x.com"], display_by_email=None, sent_at=TS)
    assert results[0].row["eligible"] is False


def test_parse_ts_accepts_iso_with_offset_and_z():
    assert ingest.parse_ts("2026-09-03T12:00:00+00:00") == TS
    assert ingest.parse_ts("2026-09-03T12:00:00Z") == TS
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ingest.py -q` — Expected: `ModuleNotFoundError: services.ingest`

- [ ] **Step 3: Implement**

```python
# services/ingest.py
"""Turn one email into people rows: counters, timestamps, eligibility. DB
only — callers commit, then do external side effects for newly eligible rows.
Shared by the event handlers and scripts/import_contacts.py so both compute
the same thing."""

import logging
from datetime import datetime, timezone
from typing import Any

import clients.otel as otel
from models.types import IngestResult
from repo import people
from services import eligibility

logger = logging.getLogger(__name__)


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _apply_flags(conn: Any, row: dict, *, automated: bool, eligible_now: bool) -> IngestResult:
    was_eligible = bool(row.get("eligible"))
    people.set_flags(conn, row["email"], automated=automated, eligible=eligible_now)
    updated = people.get(conn, row["email"]) or row
    newly = (not was_eligible) and bool(updated.get("eligible"))
    if newly:
        otel.eligibility_changes.add(1)
    return IngestResult(row=updated, newly_eligible=newly)


def record_inbound(
    conn: Any, *, sender: str, display: str | None, received_at: datetime, category: str
) -> IngestResult:
    email = eligibility.normalize(sender)
    row = people.upsert_inbound(conn, email, display, received_at)
    otel.people_upserts.add(1, {"direction": "inbound"})
    return _apply_flags(
        conn,
        row,
        automated=eligibility.is_automated(email),
        eligible_now=eligibility.inbound_eligible(email, category),
    )


def record_outbound(
    conn: Any,
    *,
    recipients: list[str],
    display_by_email: dict[str, str] | None,
    sent_at: datetime,
) -> list[IngestResult]:
    results: list[IngestResult] = []
    for raw in recipients:
        email = eligibility.normalize(raw)
        if not email or eligibility.is_own(email):
            continue
        display = (display_by_email or {}).get(email)
        row = people.upsert_outbound(conn, email, display, sent_at)
        otel.people_upserts.add(1, {"direction": "outbound"})
        automated = eligibility.is_automated(email)
        results.append(_apply_flags(conn, row, automated=automated, eligible_now=not automated))
    return results
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_ingest.py -q` — Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/ingest.py tests/test_ingest.py
git commit -m "feat: ingest service — counters and eligibility per email"
```

---

### Task 5: HubSpot client

**Files:**
- Create: `clients/hubspot.py`

**Interfaces:**
- Produces: `find_by_email(email) -> dict | None` (`{"id","email","last_email_date"}`), `create_contact(email, display_name, last_interaction: datetime | None) -> str`, `update_last_email_date(contact_id, dt)`, `archive_contact(contact_id) -> None`, `count_contacts() -> int`, `list_contacts() -> Iterator[dict]`, `log_email(contact_id, subject, sender_email, body, received_at, body_html=None) -> None`. All raise on failure (the service layer decides what to swallow). `HubSpotUnavailable` when `HUBSPOT_TOKEN` is unset.

- [ ] **Step 1: Write the client**

```python
# clients/hubspot.py
"""HubSpot CRM I/O (ported from inbox clients/hubspot.py). Every function
raises on failure; services/hubspot_mirror.py owns retry/swallow policy.
Owner id comes from HUBSPOT_OWNER_ID — no default (public repo)."""

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class HubSpotUnavailable(RuntimeError):
    pass


def _token() -> str:
    tok = os.environ.get("HUBSPOT_TOKEN", "")
    if not tok:
        raise HubSpotUnavailable("HUBSPOT_TOKEN unset")
    return tok


def _client():
    from hubspot import HubSpot

    return HubSpot(access_token=_token())


def _hs_date(dt: datetime) -> str:
    """HubSpot date property: midnight UTC in ms."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000))


def _split_name(display_name: str | None) -> tuple[str, str]:
    parts = (display_name or "").strip().split(" ", 1)
    return (parts[0] if parts and parts[0] else ""), (parts[1] if len(parts) > 1 else "")


def find_by_email(email: str) -> dict | None:
    from hubspot.crm.contacts import PublicObjectSearchRequest

    result = _client().crm.contacts.search_api.do_search(
        PublicObjectSearchRequest(
            filter_groups=[{"filters": [{"value": email, "propertyName": "email", "operator": "EQ"}]}],
            properties=["email", "last_email_date"],
            limit=1,
        )
    )
    if not result.results:
        return None
    hit = result.results[0]
    return {"id": hit.id, "email": hit.properties.get("email"), "last_email_date": hit.properties.get("last_email_date")}


def create_contact(email: str, display_name: str | None, last_interaction: datetime | None) -> str:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    first, last = _split_name(display_name)
    props = {
        "email": email,
        "lifecyclestage": "lead",
        "hs_lead_status": "NEW",
        "hubspot_owner_id": os.environ["HUBSPOT_OWNER_ID"],
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if last_interaction:
        props["last_email_date"] = _hs_date(last_interaction)
    created = _client().crm.contacts.basic_api.create(SimplePublicObjectInputForCreate(properties=props))
    return created.id


def update_last_email_date(contact_id: str, dt: datetime) -> None:
    from hubspot.crm.contacts import SimplePublicObjectInput

    _client().crm.contacts.basic_api.update(
        contact_id, SimplePublicObjectInput(properties={"last_email_date": _hs_date(dt)})
    )


def archive_contact(contact_id: str) -> None:
    """Soft delete — recoverable in HubSpot for 90 days."""
    _client().crm.contacts.basic_api.archive(contact_id)


def count_contacts() -> int:
    from hubspot.crm.contacts import PublicObjectSearchRequest

    result = _client().crm.contacts.search_api.do_search(PublicObjectSearchRequest(limit=1))
    return int(result.total)


def list_contacts() -> Iterator[dict]:
    """All contacts, paged. Yields {id, email, last_email_date}."""
    client = _client()
    after = None
    while True:
        page = client.crm.contacts.basic_api.get_page(
            limit=100, after=after, properties=["email", "last_email_date"]
        )
        for c in page.results:
            yield {
                "id": c.id,
                "email": (c.properties.get("email") or "").lower(),
                "last_email_date": c.properties.get("last_email_date"),
            }
        if not page.paging or not page.paging.next:
            return
        after = page.paging.next.after


def log_email(
    contact_id: str,
    subject: str,
    sender_email: str,
    body: str,
    received_at: datetime,
    body_html: str | None = None,
) -> None:
    from hubspot.crm import AssociationType
    from hubspot.crm.objects.emails import (
        AssociationSpec,
        PublicAssociationsForObject,
        PublicObjectId,
        SimplePublicObjectInputForCreate,
    )

    props = {
        "hs_timestamp": str(int(received_at.timestamp() * 1000)),
        "hs_email_subject": subject or "(no subject)",
        "hs_email_direction": "INCOMING_EMAIL",
        "hs_email_status": "SENT",
        "hs_email_headers": json.dumps({"from": {"email": sender_email}}),
    }
    if body_html:
        props["hs_email_html"] = body_html
    else:
        props["hs_email_text"] = body
    association = PublicAssociationsForObject(
        to=PublicObjectId(id=contact_id),
        types=[
            AssociationSpec(
                association_category="HUBSPOT_DEFINED",
                association_type_id=AssociationType.EMAIL_TO_CONTACT,
            )
        ],
    )
    _client().crm.objects.emails.basic_api.create(
        SimplePublicObjectInputForCreate(properties=props, associations=[association])
    )
```

- [ ] **Step 2: Verify import and lint**

```bash
.venv/bin/python -c "import clients.hubspot as h; print(h.count_contacts.__name__)"
.venv/bin/ruff check clients/hubspot.py && .venv/bin/mypy clients/hubspot.py
```

Expected: `count_contacts`, clean.

- [ ] **Step 3: Commit**

```bash
git add clients/hubspot.py
git commit -m "feat: HubSpot client (contacts CRUD, count, list, email engagement)"
```

---

### Task 6: `services/hubspot_mirror.py` — cap-bounded mirror

**Files:**
- Create: `services/hubspot_mirror.py`
- Test: `tests/test_hubspot_mirror.py`

**Interfaces:**
- Consumes: `clients.hubspot.*` (Task 5), `repo.people.set_hubspot/clear_hubspot/oldest_managed/managed/eligible_not_in_hubspot/get` (Task 3).
- Produces: `enabled() -> bool`, `cap() -> int`, `ensure_contact(conn, row: dict) -> dict` (row, possibly with `hubspot_contact_id` set), `log_email(row: dict, event: dict) -> None`, `reconcile(conn) -> dict[str, int]` with keys `adopted, healed, evicted, filled`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_hubspot_mirror.py
from datetime import UTC, datetime

import pytest

import clients.hubspot as hs
import repo.people as people_repo
from services import hubspot_mirror as mirror

TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class FakeHubSpot:
    def __init__(self, total=0, existing=None, contacts=None):
        self.total = total
        self.existing = existing or {}
        self.contacts = contacts or []
        self.created, self.archived, self.logged = [], [], []

    def count_contacts(self):
        return self.total

    def find_by_email(self, email):
        return self.existing.get(email)

    def create_contact(self, email, display, last):
        self.created.append(email)
        self.total += 1
        return f"hs-{email}"

    def archive_contact(self, cid):
        self.archived.append(cid)
        self.total -= 1

    def list_contacts(self):
        return iter(self.contacts)

    def log_email(self, *a, **k):
        self.logged.append(a)

    def update_last_email_date(self, *a):
        pass


class FakeRepo:
    def __init__(self, rows):
        self.rows = {r["email"]: dict(r) for r in rows}

    def set_hubspot(self, conn, email, cid):
        self.rows[email]["hubspot_contact_id"] = cid

    def clear_hubspot(self, conn, email):
        self.rows[email]["hubspot_contact_id"] = None

    def oldest_managed(self, conn):
        managed = [r for r in self.rows.values() if r.get("hubspot_contact_id")]
        return min(managed, key=lambda r: r["last_interaction"], default=None)

    def managed(self, conn):
        return [r for r in self.rows.values() if r.get("hubspot_contact_id")]

    def eligible_not_in_hubspot(self, conn, limit):
        rows = [r for r in self.rows.values() if r["eligible"] and not r.get("hubspot_contact_id")]
        return sorted(rows, key=lambda r: r["last_interaction"], reverse=True)[:limit]

    def get(self, conn, email):
        return self.rows.get(email)


def row(email, eligible=True, cid=None, when=TS):
    return {"email": email, "display_name": None, "eligible": eligible, "hubspot_contact_id": cid, "last_interaction": when}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HUBSPOT_WRITES_ENABLED", "true")
    monkeypatch.setenv("HUBSPOT_MAX_CONTACTS", "2")
    monkeypatch.setenv("HUBSPOT_TOKEN", "x")
    monkeypatch.setenv("HUBSPOT_OWNER_ID", "1")


def wire(monkeypatch, fake_hs, fake_repo):
    for n in ("count_contacts", "find_by_email", "create_contact", "archive_contact", "list_contacts", "log_email", "update_last_email_date"):
        monkeypatch.setattr(hs, n, getattr(fake_hs, n))
    for n in ("set_hubspot", "clear_hubspot", "oldest_managed", "managed", "eligible_not_in_hubspot", "get"):
        monkeypatch.setattr(people_repo, n, getattr(fake_repo, n))
    mirror._reset_count_cache()


def test_disabled_is_a_noop(monkeypatch, env):
    monkeypatch.setenv("HUBSPOT_WRITES_ENABLED", "false")
    fh, fr = FakeHubSpot(), FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert fh.created == [] and out.get("hubspot_contact_id") is None


def test_create_under_cap(monkeypatch, env):
    fh, fr = FakeHubSpot(total=1), FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert fh.created == ["a@x.com"] and out["hubspot_contact_id"] == "hs-a@x.com"


def test_adopts_existing_hubspot_contact_instead_of_creating(monkeypatch, env):
    fh = FakeHubSpot(total=1, existing={"a@x.com": {"id": "hs-old", "email": "a@x.com"}})
    fr = FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert fh.created == [] and out["hubspot_contact_id"] == "hs-old"


def test_at_cap_evicts_oldest_managed_then_creates(monkeypatch, env):
    old = datetime(2025, 1, 1, tzinfo=UTC)
    fh = FakeHubSpot(total=2)
    fr = FakeRepo([row("old@x.com", cid="hs-old", when=old), row("mid@x.com", cid="hs-mid"), row("new@x.com")])
    wire(monkeypatch, fh, fr)
    mirror.ensure_contact(None, fr.rows["new@x.com"])
    assert fh.archived == ["hs-old"]
    assert fr.rows["old@x.com"]["hubspot_contact_id"] is None
    assert fh.created == ["new@x.com"]


def test_at_cap_with_no_managed_contacts_skips(monkeypatch, env):
    fh, fr = FakeHubSpot(total=2), FakeRepo([row("new@x.com")])
    wire(monkeypatch, fh, fr)
    out = mirror.ensure_contact(None, fr.rows["new@x.com"])
    assert fh.created == [] and fh.archived == [] and out.get("hubspot_contact_id") is None


def test_hubspot_failure_is_swallowed(monkeypatch, env):
    fh, fr = FakeHubSpot(total=0), FakeRepo([row("a@x.com")])
    wire(monkeypatch, fh, fr)

    def boom(*a, **k):
        raise RuntimeError("hubspot down")

    monkeypatch.setattr(hs, "create_contact", boom)
    out = mirror.ensure_contact(None, fr.rows["a@x.com"])
    assert out.get("hubspot_contact_id") is None


def test_log_email_only_for_mirrored(monkeypatch, env):
    fh, fr = FakeHubSpot(), FakeRepo([row("a@x.com", cid="hs-a"), row("b@x.com")])
    wire(monkeypatch, fh, fr)
    event = {"subject": "s", "sender": "a@x.com", "body": "b", "body_html": None, "received_at": "2026-09-03T12:00:00Z"}
    mirror.log_email(fr.rows["a@x.com"], event)
    mirror.log_email(fr.rows["b@x.com"], event)
    assert len(fh.logged) == 1


def test_reconcile_adopt_heal_enforce_fill(monkeypatch, env):
    old = datetime(2025, 1, 1, tzinfo=UTC)
    fh = FakeHubSpot(
        total=3,
        contacts=[
            {"id": "hs-a", "email": "a@x.com"},      # managed already
            {"id": "hs-b", "email": "b@x.com"},      # in HubSpot, eligible row not linked → adopt
            {"id": "hs-u", "email": "u@x.com"},      # unmanaged, unknown to people → never evicted
        ],
    )
    fr = FakeRepo([
        row("a@x.com", cid="hs-a", when=old),
        row("b@x.com"),
        row("gone@x.com", cid="hs-gone"),           # id no longer in HubSpot → heal
        row("n@x.com"),                             # eligible, not in HubSpot
    ])
    wire(monkeypatch, fh, fr)
    counts = mirror.reconcile(None)
    assert counts["adopted"] == 1 and fr.rows["b@x.com"]["hubspot_contact_id"] == "hs-b"
    assert counts["healed"] == 1 and fr.rows["gone@x.com"]["hubspot_contact_id"] is None
    # cap=2, total=3 after adopt/heal → evict the oldest managed (a) once
    assert counts["evicted"] == 1 and fh.archived == ["hs-a"]
    # now total=2 == cap → nothing to fill
    assert counts["filled"] == 0 and fh.created == []


def test_reconcile_fills_when_under_cap(monkeypatch, env):
    monkeypatch.setenv("HUBSPOT_MAX_CONTACTS", "3")
    fh = FakeHubSpot(total=1, contacts=[{"id": "hs-a", "email": "a@x.com"}])
    fr = FakeRepo([row("a@x.com", cid="hs-a"), row("n1@x.com"), row("n2@x.com"), row("n3@x.com")])
    wire(monkeypatch, fh, fr)
    counts = mirror.reconcile(None)
    assert counts["filled"] == 2 and len(fh.created) == 2
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_hubspot_mirror.py -q` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# services/hubspot_mirror.py
"""HubSpot as a bounded mirror of eligible people (spec §8). At most cap()
contacts, most recently interacted-with first. Managed = has a
hubspot_contact_id in people; unmanaged contacts count toward the cap but are
never archived. Every HubSpot failure is swallowed and counted."""

import logging
import os
import time
from typing import Any

import clients.hubspot as hubspot
import clients.otel as otel
from repo import people
from services.ingest import parse_ts

logger = logging.getLogger(__name__)

_COUNT_TTL_S = 60
_count_cache: tuple[float, int] | None = None


def _reset_count_cache() -> None:
    global _count_cache
    _count_cache = None


def enabled() -> bool:
    return os.environ.get("HUBSPOT_WRITES_ENABLED", "false").lower() in ("1", "true", "yes")


def cap() -> int:
    return int(os.environ.get("HUBSPOT_MAX_CONTACTS", "1000"))


def _total() -> int:
    global _count_cache
    now = time.monotonic()
    if _count_cache and now - _count_cache[0] < _COUNT_TTL_S:
        return _count_cache[1]
    total = hubspot.count_contacts()
    _count_cache = (now, total)
    return total


def _bump_total(delta: int) -> None:
    global _count_cache
    if _count_cache:
        _count_cache = (_count_cache[0], _count_cache[1] + delta)


def _evict_oldest(conn: Any) -> bool:
    victim = people.oldest_managed(conn)
    if victim is None:
        logger.warning("HubSpot cap reached by unmanaged contacts — not creating")
        return False
    hubspot.archive_contact(victim["hubspot_contact_id"])
    people.clear_hubspot(conn, victim["email"])
    _bump_total(-1)
    otel.hubspot_evictions.add(1)
    logger.info("Evicted %s from HubSpot (last interaction %s)", victim["email"], victim["last_interaction"])
    return True


def _create(conn: Any, row: dict) -> str:
    existing = hubspot.find_by_email(row["email"])
    if existing:
        cid = existing["id"]
    else:
        cid = hubspot.create_contact(row["email"], row.get("display_name"), row.get("last_interaction"))
        _bump_total(1)
        otel.hubspot_contacts_created.add(1)
    people.set_hubspot(conn, row["email"], cid)
    return cid


def ensure_contact(conn: Any, row: dict) -> dict:
    if not enabled() or row.get("hubspot_contact_id") or not row.get("eligible"):
        return row
    try:
        if _total() >= cap() and not _evict_oldest(conn):
            return row
        _create(conn, row)
        return people.get(conn, row["email"]) or row
    except Exception:
        otel.external_errors.add(1, {"system": "hubspot"})
        logger.warning("HubSpot ensure_contact failed for %s", row["email"], exc_info=True)
        return row


def log_email(row: dict, event: dict) -> None:
    cid = row.get("hubspot_contact_id")
    if not enabled() or not cid:
        return
    try:
        received = parse_ts(event["received_at"])
        hubspot.update_last_email_date(cid, received)
        hubspot.log_email(
            cid, event.get("subject", ""), event.get("sender", ""), event.get("body") or "",
            received, body_html=event.get("body_html"),
        )
        otel.hubspot_engagements_logged.add(1)
    except Exception:
        otel.external_errors.add(1, {"system": "hubspot"})
        logger.warning("HubSpot log_email failed for %s", row.get("email"), exc_info=True)


def reconcile(conn: Any) -> dict[str, int]:
    """Nightly: adopt, heal, enforce, fill (spec §8.3). Raises on a HubSpot
    listing failure so the sync run reports it; per-contact writes are
    swallowed."""
    counts = {"adopted": 0, "healed": 0, "evicted": 0, "filled": 0}
    if not enabled():
        return counts
    _reset_count_cache()
    live = {c["id"]: c for c in hubspot.list_contacts()}
    total = len(live)

    # adopt
    managed_ids = {r["hubspot_contact_id"] for r in people.managed(conn)}
    for cid, c in live.items():
        if cid in managed_ids or not c["email"]:
            continue
        row = people.get(conn, c["email"])
        if row and row["eligible"] and not row.get("hubspot_contact_id"):
            people.set_hubspot(conn, row["email"], cid)
            counts["adopted"] += 1

    # heal
    for r in people.managed(conn):
        if r["hubspot_contact_id"] not in live:
            people.clear_hubspot(conn, r["email"])
            counts["healed"] += 1

    # enforce
    while total > cap():
        victim = people.oldest_managed(conn)
        if victim is None:
            logger.warning("HubSpot over cap by %d with no managed contacts to evict", total - cap())
            break
        try:
            hubspot.archive_contact(victim["hubspot_contact_id"])
        except Exception:
            otel.external_errors.add(1, {"system": "hubspot"})
            logger.warning("archive failed for %s", victim["email"], exc_info=True)
            break
        people.clear_hubspot(conn, victim["email"])
        total -= 1
        counts["evicted"] += 1
        otel.hubspot_evictions.add(1)

    # fill
    room = cap() - total
    if room > 0:
        for row in people.eligible_not_in_hubspot(conn, room):
            try:
                _create(conn, row)
                counts["filled"] += 1
            except Exception:
                otel.external_errors.add(1, {"system": "hubspot"})
                logger.warning("fill create failed for %s", row["email"], exc_info=True)
    return counts
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_hubspot_mirror.py -q` — Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/hubspot_mirror.py tests/test_hubspot_mirror.py
git commit -m "feat: HubSpot mirror service with hard cap and nightly reconcile"
```

---

### Task 7: Google Contacts client

**Files:**
- Create: `clients/google_contacts.py`, `scripts/get_google_contacts_token.py`

**Interfaces:**
- Produces: `SyncTokenExpired(Exception)`; `search_by_email(email) -> dict | None` (People API `person` dict); `create_contact(display_name, email, group_resource_name) -> dict`; `get_person(resource_name) -> dict`; `update_biography(resource_name, etag, text) -> dict`; `list_groups() -> dict[str, dict]` (name → `{"resourceName","groupType"}`); `ensure_group(name) -> str`; `modify_group_members(group_resource_name, add: list[str], remove: list[str]) -> None`; `list_connections(sync_token: str | None) -> tuple[list[dict], str]`. `PERSON_FIELDS = "names,emailAddresses,memberships,biographies,metadata"`.

- [ ] **Step 1: Token script**

```python
# scripts/get_google_contacts_token.py
"""One-off: run the OAuth flow for the People API and print a refresh token.
Reuses schedule's OAuth client (same Google account). Then:
  gcloud secrets versions add google-contacts-refresh-token --data-file=- <<< "$TOKEN"
Usage: GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/get_google_contacts_token.py
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

client_config = {
    "installed": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(
    client_config, scopes=["https://www.googleapis.com/auth/contacts"]
)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\nRefresh token (GOOGLE_REFRESH_TOKEN / google-contacts-refresh-token):")
print(creds.refresh_token)
```

- [ ] **Step 2: Client**

```python
# clients/google_contacts.py
"""Google People API v1 I/O. Credentials from env (Terraform injects the two
schedule-owned OAuth client secrets and people's own refresh token as env
vars). Built WITHOUT scopes — google-auth would send them on refresh and the
token server rejects scopes it did not explicitly grant (see schedule's
google_calendar.py)."""

import logging
import threading
from typing import Any

import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

PERSON_FIELDS = "names,emailAddresses,memberships,biographies,metadata"
_READ_MASK = PERSON_FIELDS


class SyncTokenExpired(Exception):
    pass


_credentials: Credentials | None = None
_local = threading.local()
_warmed = False


def _creds() -> Credentials:
    global _credentials
    if _credentials is None:
        _credentials = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        )
    return _credentials


def _svc() -> Any:
    svc = getattr(_local, "svc", None)
    if svc is None:
        svc = build("people", "v1", credentials=_creds(), cache_discovery=False)
        _local.svc = svc
    return svc


def _warmup() -> None:
    """searchContacts requires a warm-up request with an empty query before
    the first real search in a session, else results may be stale/empty."""
    global _warmed
    if not _warmed:
        _svc().people().searchContacts(query="", readMask=_READ_MASK).execute()
        _warmed = True


def search_by_email(email: str) -> dict | None:
    _warmup()
    resp = _svc().people().searchContacts(query=email, readMask=_READ_MASK, pageSize=5).execute()
    for r in resp.get("results", []):
        person = r.get("person", {})
        for e in person.get("emailAddresses", []):
            if (e.get("value") or "").strip().lower() == email.lower():
                return person
    return None


def create_contact(display_name: str | None, email: str, group_resource_name: str | None) -> dict:
    body: dict[str, Any] = {"emailAddresses": [{"value": email}]}
    if display_name:
        parts = display_name.strip().split(" ", 1)
        name = {"givenName": parts[0]}
        if len(parts) > 1:
            name["familyName"] = parts[1]
        body["names"] = [name]
    if group_resource_name:
        body["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": group_resource_name}}]
    return _svc().people().createContact(body=body, personFields=PERSON_FIELDS).execute()


def get_person(resource_name: str) -> dict:
    return _svc().people().get(resourceName=resource_name, personFields=PERSON_FIELDS).execute()


def update_biography(resource_name: str, etag: str, text: str) -> dict:
    return (
        _svc()
        .people()
        .updateContact(
            resourceName=resource_name,
            updatePersonFields="biographies",
            personFields=PERSON_FIELDS,
            body={"etag": etag, "biographies": [{"value": text, "contentType": "TEXT_PLAIN"}]},
        )
        .execute()
    )


def list_groups() -> dict[str, dict]:
    out: dict[str, dict] = {}
    token = None
    while True:
        resp = _svc().contactGroups().list(pageSize=200, pageToken=token).execute()
        for g in resp.get("contactGroups", []):
            out[g.get("formattedName") or g.get("name")] = {
                "resourceName": g["resourceName"],
                "groupType": g.get("groupType"),
            }
        token = resp.get("nextPageToken")
        if not token:
            return out


def ensure_group(name: str) -> str:
    groups = list_groups()
    if name in groups:
        return groups[name]["resourceName"]
    created = _svc().contactGroups().create(body={"contactGroup": {"name": name}}).execute()
    return created["resourceName"]


def modify_group_members(group_resource_name: str, add: list[str], remove: list[str]) -> None:
    body: dict[str, Any] = {}
    if add:
        body["resourceNamesToAdd"] = add
    if remove:
        body["resourceNamesToRemove"] = remove
    if body:
        _svc().contactGroups().members().modify(resourceName=group_resource_name, body=body).execute()


def list_connections(sync_token: str | None) -> tuple[list[dict], str]:
    """Full or incremental listing. Raises SyncTokenExpired on 410."""
    people_out: list[dict] = []
    page_token = None
    next_sync = ""
    while True:
        kwargs: dict[str, Any] = {
            "resourceName": "people/me",
            "personFields": PERSON_FIELDS,
            "pageSize": 1000,
            "requestSyncToken": True,
        }
        if sync_token:
            kwargs["syncToken"] = sync_token
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = _svc().people().connections().list(**kwargs).execute()
        except HttpError as e:
            if e.resp.status == 410:
                raise SyncTokenExpired() from e
            raise
        people_out.extend(resp.get("connections", []))
        next_sync = resp.get("nextSyncToken") or next_sync
        page_token = resp.get("nextPageToken")
        if not page_token:
            return people_out, next_sync
```

- [ ] **Step 3: Verify import, lint, types**

```bash
.venv/bin/ruff check clients/google_contacts.py scripts/get_google_contacts_token.py
.venv/bin/mypy clients/google_contacts.py
.venv/bin/python -c "import clients.google_contacts"
```

Expected: clean. (Fix the import order ruff flags — `os` belongs with the stdlib block.)

- [ ] **Step 4: Commit**

```bash
git add clients/google_contacts.py scripts/get_google_contacts_token.py
git commit -m "feat: Google People API client and refresh-token script"
```

---

### Task 8: `services/google_contacts_sync.py`

**Files:**
- Create: `services/google_contacts_sync.py`
- Test: `tests/test_google_contacts_sync.py`

**Interfaces:**
- Consumes: `clients.google_contacts.*` (Task 7), `repo.people.*`, `repo.sync_state.*` (Task 3).
- Produces: `ensure_contact(conn, row) -> dict`; `run_sync(conn) -> dict[str, int]` (`updated, linked, created, deleted`); `apply_person(conn, person: dict, groups: dict[str, dict]) -> str | None` (returns the change kind); `relationship_label(person, groups) -> str | None`; `sync_one(conn, row) -> dict`; `primary_email(person) -> str | None`; `display_name(person) -> str | None`; `notes(person) -> str | None`; `GROUP_NAME` from env `GOOGLE_CONTACT_GROUP` default `"Inbox"`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_google_contacts_sync.py
import pytest

import clients.google_contacts as gc
import repo.people as people_repo
import repo.sync_state as sync_state
from services import google_contacts_sync as sync

GROUPS = {
    "myContacts": {"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP"},
    "Inbox": {"resourceName": "contactGroups/inbox1", "groupType": "USER_CONTACT_GROUP"},
    "Family": {"resourceName": "contactGroups/fam1", "groupType": "USER_CONTACT_GROUP"},
}


def person(rn="people/c1", email="alice@example.com", name="Alice Example", groups=(), bio=None, deleted=False, etag="e1"):
    p = {
        "resourceName": rn,
        "etag": etag,
        "emailAddresses": [{"value": email}] if email else [],
        "names": [{"displayName": name}] if name else [],
        "memberships": [{"contactGroupMembership": {"contactGroupResourceName": g}} for g in groups],
        "metadata": {"deleted": deleted},
    }
    if bio:
        p["biographies"] = [{"value": bio}]
    return p


class FakeRepo:
    def __init__(self, rows=()):
        self.rows = {r["email"]: dict(r) for r in rows}
        self.log = []

    def _by_rn(self, rn):
        return next((r for r in self.rows.values() if r.get("google_resource_name") == rn), None)

    def get(self, conn, email):
        return self.rows.get(email)

    def get_by_google_resource(self, conn, rn):
        return self._by_rn(rn)

    def set_google(self, conn, email, **kw):
        self.rows[email].update(google_resource_name=kw["resource_name"], google_etag=kw["etag"])
        for k in ("display_name", "notes", "relationship_label"):
            if kw.get(k) is not None:
                self.rows[email][k] = kw[k]
        self.log.append(("set_google", email))

    def update_from_google(self, conn, rn, **kw):
        r = self._by_rn(rn)
        r.update(google_etag=kw["etag"], notes=kw["notes"], relationship_label=kw["relationship_label"])
        if kw["display_name"]:
            r["display_name"] = kw["display_name"]
        self.log.append(("update", rn))

    def mark_google_deleted(self, conn, rn):
        r = self._by_rn(rn)
        r.update(google_deleted_at="now", google_resource_name=None)
        self.log.append(("deleted", rn))

    def create_from_google(self, conn, email, **kw):
        self.rows[email] = {"email": email, "eligible": True, "google_resource_name": kw["resource_name"], **kw}
        self.log.append(("created", email))
        return self.rows[email]


class FakeGC:
    def __init__(self, found=None, connections=(), token="tok2", expire_first=False):
        self.found = found
        self.created = []
        self.connections = list(connections)
        self.token = token
        self.expire_first = expire_first
        self.calls = []

    def search_by_email(self, email):
        return self.found

    def create_contact(self, name, email, group):
        self.created.append((name, email, group))
        return person(rn="people/new", email=email, name=name)

    def ensure_group(self, name):
        return "contactGroups/inbox1"

    def list_groups(self):
        return GROUPS

    def list_connections(self, sync_token):
        self.calls.append(sync_token)
        if self.expire_first and sync_token:
            self.expire_first = False
            raise gc.SyncTokenExpired()
        return self.connections, self.token

    def get_person(self, rn):
        return next(p for p in self.connections if p["resourceName"] == rn)


@pytest.fixture
def wire(monkeypatch):
    def _wire(fake_gc, fake_repo, token="tok1"):
        for n in ("search_by_email", "create_contact", "ensure_group", "list_groups", "list_connections", "get_person"):
            monkeypatch.setattr(gc, n, getattr(fake_gc, n))
        for n in ("get", "get_by_google_resource", "set_google", "update_from_google", "mark_google_deleted", "create_from_google"):
            monkeypatch.setattr(people_repo, n, getattr(fake_repo, n))
        saved = {}
        monkeypatch.setattr(sync_state, "get_token", lambda conn: token)
        monkeypatch.setattr(sync_state, "set_token", lambda conn, t, s: saved.update(token=t, status=s))
        monkeypatch.delenv("GOOGLE_CONTACT_GROUP", raising=False)
        return saved
    return _wire


def row(email, **kw):
    base = {"email": email, "display_name": None, "eligible": True, "google_resource_name": None, "google_deleted_at": None}
    base.update(kw)
    return base


def test_relationship_label_ignores_system_and_inbox_groups():
    p = person(groups=["contactGroups/myContacts", "contactGroups/inbox1", "contactGroups/fam1"])
    assert sync.relationship_label(p, GROUPS) == "family"


def test_relationship_label_none_when_only_system():
    assert sync.relationship_label(person(groups=["contactGroups/myContacts"]), GROUPS) is None


def test_ensure_contact_links_existing(wire):
    fr = FakeRepo([row("alice@example.com")])
    fg = FakeGC(found=person(groups=["contactGroups/fam1"], bio="old friend"))
    wire(fg, fr)
    sync.ensure_contact(None, fr.rows["alice@example.com"])
    r = fr.rows["alice@example.com"]
    assert r["google_resource_name"] == "people/c1" and fg.created == []
    assert r["display_name"] == "Alice Example" and r["notes"] == "old friend" and r["relationship_label"] == "family"


def test_ensure_contact_creates_in_inbox_group(wire):
    fr = FakeRepo([row("alice@example.com", display_name="Alice")])
    fg = FakeGC(found=None)
    wire(fg, fr)
    sync.ensure_contact(None, fr.rows["alice@example.com"])
    assert fg.created == [("Alice", "alice@example.com", "contactGroups/inbox1")]
    assert fr.rows["alice@example.com"]["google_resource_name"] == "people/new"


def test_ensure_contact_skips_linked_and_deleted(wire):
    fr = FakeRepo([row("a@x.com", google_resource_name="people/c9"), row("b@x.com", google_deleted_at="2026-01-01")])
    fg = FakeGC(found=None)
    wire(fg, fr)
    sync.ensure_contact(None, fr.rows["a@x.com"])
    sync.ensure_contact(None, fr.rows["b@x.com"])
    assert fg.created == []


def test_ensure_contact_swallows_google_errors(wire, monkeypatch):
    fr = FakeRepo([row("a@x.com")])
    wire(FakeGC(found=None), fr)

    def boom(email):
        raise RuntimeError("quota")

    monkeypatch.setattr(gc, "search_by_email", boom)
    out = sync.ensure_contact(None, fr.rows["a@x.com"])
    assert out["google_resource_name"] is None


def test_run_sync_updates_links_creates_deletes(wire):
    fr = FakeRepo([
        row("linked@x.com", google_resource_name="people/l1"),
        row("hand@x.com"),
        row("gone@x.com", google_resource_name="people/g1"),
    ])
    fg = FakeGC(connections=[
        person(rn="people/l1", email="linked@x.com", name="Linked Person", groups=["contactGroups/fam1"], bio="n"),
        person(rn="people/h1", email="hand@x.com", name="Hand Made"),
        person(rn="people/u1", email="unknown@x.com", name="Unknown One"),
        person(rn="people/g1", email=None, name=None, deleted=True),
    ])
    saved = wire(fg, fr)
    counts = sync.run_sync(None)
    assert counts == {"updated": 1, "linked": 1, "created": 1, "deleted": 1}
    assert fr.rows["linked@x.com"]["relationship_label"] == "family"
    assert fr.rows["hand@x.com"]["google_resource_name"] == "people/h1"
    assert fr.rows["unknown@x.com"]["eligible"] is True
    assert fr.rows["gone@x.com"]["google_deleted_at"] == "now"
    assert saved == {"token": "tok2", "status": "ok"}


def test_run_sync_falls_back_to_full_on_expired_token(wire):
    fr = FakeRepo()
    fg = FakeGC(connections=[], expire_first=True)
    wire(fg, fr, token="stale")
    sync.run_sync(None)
    assert fg.calls == ["stale", None]
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_google_contacts_sync.py -q` — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# services/google_contacts_sync.py
"""Google Contacts is the source of truth for identity (spec §4.3, §7).
ensure_contact links or creates a contact for a newly eligible person;
run_sync pulls Google's changes into the derived index by sync token."""

import logging
import os
from typing import Any

import clients.google_contacts as gc
import clients.otel as otel
from repo import people, sync_state

logger = logging.getLogger(__name__)

_SYSTEM = "SYSTEM_CONTACT_GROUP"


def group_name() -> str:
    return os.environ.get("GOOGLE_CONTACT_GROUP", "Inbox")


def primary_email(person: dict) -> str | None:
    for e in person.get("emailAddresses", []):
        v = (e.get("value") or "").strip().lower()
        if v:
            return v
    return None


def display_name(person: dict) -> str | None:
    for n in person.get("names", []):
        if n.get("displayName"):
            return n["displayName"]
    return None


def notes(person: dict) -> str | None:
    for b in person.get("biographies", []):
        if b.get("value"):
            return b["value"]
    return None


def relationship_label(person: dict, groups: dict[str, dict]) -> str | None:
    """First user-defined group the contact belongs to, lowercased, skipping
    system groups and people's own '<GOOGLE_CONTACT_GROUP>' group."""
    by_rn = {g["resourceName"]: (name, g.get("groupType")) for name, g in groups.items()}
    for m in person.get("memberships", []):
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if not rn or rn not in by_rn:
            continue
        name, kind = by_rn[rn]
        if kind == _SYSTEM or name == group_name():
            continue
        return name.lower()
    return None


def _link(conn: Any, email: str, person: dict, groups: dict[str, dict]) -> None:
    people.set_google(
        conn,
        email,
        resource_name=person["resourceName"],
        etag=person.get("etag"),
        display_name=display_name(person),
        notes=notes(person),
        relationship_label=relationship_label(person, groups),
    )


def ensure_contact(conn: Any, row: dict) -> dict:
    if row.get("google_resource_name") or row.get("google_deleted_at") or not row.get("eligible"):
        return row
    try:
        groups = gc.list_groups()
        found = gc.search_by_email(row["email"])
        if found:
            _link(conn, row["email"], found, groups)
        else:
            created = gc.create_contact(row.get("display_name"), row["email"], gc.ensure_group(group_name()))
            otel.google_contacts_created.add(1)
            _link(conn, row["email"], created, groups)
        return people.get(conn, row["email"]) or row
    except Exception:
        otel.external_errors.add(1, {"system": "google"})
        logger.warning("Google ensure_contact failed for %s", row["email"], exc_info=True)
        return row


def apply_person(conn: Any, person: dict, groups: dict[str, dict]) -> str | None:
    rn = person["resourceName"]
    linked = people.get_by_google_resource(conn, rn)
    if (person.get("metadata") or {}).get("deleted"):
        if linked:
            people.mark_google_deleted(conn, rn)
            return "deleted"
        return None
    label = relationship_label(person, groups)
    if linked:
        people.update_from_google(
            conn, rn, etag=person.get("etag"), display_name=display_name(person), notes=notes(person), relationship_label=label
        )
        return "updated"
    email = primary_email(person)
    if not email:
        return None
    row = people.get(conn, email)
    if row and not row.get("google_resource_name") and not row.get("google_deleted_at"):
        _link(conn, email, person, groups)
        return "linked"
    if row is None:
        people.create_from_google(
            conn, email, display_name=display_name(person), resource_name=rn, etag=person.get("etag"), notes=notes(person), relationship_label=label
        )
        return "created"
    return None


def run_sync(conn: Any) -> dict[str, int]:
    counts = {"updated": 0, "linked": 0, "created": 0, "deleted": 0}
    token = sync_state.get_token(conn)
    try:
        try:
            persons, next_token = gc.list_connections(token)
        except gc.SyncTokenExpired:
            logger.warning("Google sync token expired — full resync")
            persons, next_token = gc.list_connections(None)
        groups = gc.list_groups()
        for p in persons:
            kind = apply_person(conn, p, groups)
            if kind:
                counts[kind] += 1
                otel.google_sync_changes.add(1, {"kind": kind})
        sync_state.set_token(conn, next_token, "ok")
    except Exception as e:
        otel.external_errors.add(1, {"system": "google"})
        sync_state.set_token(conn, token, f"error: {type(e).__name__}")
        raise
    return counts


def sync_one(conn: Any, row: dict) -> dict:
    """Re-pull one linked person (after a PATCH or a manual edit)."""
    rn = row.get("google_resource_name")
    if not rn:
        return row
    apply_person(conn, gc.get_person(rn), gc.list_groups())
    return people.get(conn, row["email"]) or row
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_google_contacts_sync.py -q` — Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/google_contacts_sync.py tests/test_google_contacts_sync.py
git commit -m "feat: Google Contacts sync service (ensure, nightly sync, relationship labels)"
```

---

### Task 9: Handlers and `main.py`

**Files:**
- Create: `handlers/email_classified.py`, `handlers/email_sent.py`, `handlers/sync.py`, `services/sync_auth.py`, `main.py`
- Test: `tests/test_handlers.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `services.ingest`, `services.google_contacts_sync`, `services.hubspot_mirror`, `clients.db.get_conn`.
- Produces: `email_classified.handle(event: dict) -> None`, `email_sent.handle(event: dict) -> None`, `sync.run() -> dict` (`{"google": {...}, "hubspot": {...}}`), `sync_auth.is_authorized(header: str | None) -> bool`; CF entry points `process`, `sync`.

- [ ] **Step 1: Failing handler tests**

```python
# tests/test_handlers.py
from datetime import UTC, datetime

import pytest

import clients.db as db
import services.google_contacts_sync as gsync
import services.hubspot_mirror as mirror
import services.ingest as ingest
from handlers import email_classified, email_sent
from models.types import IngestResult


class FakeConn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def calls(monkeypatch):
    log = []
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(gsync, "ensure_contact", lambda c, r: (log.append(("google", r["email"])), r)[1])
    monkeypatch.setattr(mirror, "ensure_contact", lambda c, r: (log.append(("hubspot", r["email"])), r)[1])
    monkeypatch.setattr(mirror, "log_email", lambda r, e: log.append(("log", r["email"])))
    return log, conn


def _event(category="review"):
    return {
        "event": "email_classified", "message_id": "m1", "category": category, "importance": "P2",
        "confidence": 0.9, "subject": "Hi", "sender": "alice@x.com", "sender_display": "Alice",
        "to": ["me@x.com"], "cc": [], "received_at": "2026-09-03T12:00:00Z", "tags": [],
        "reasoning": "", "body": "b", "body_html": None, "web_link": None,
    }


def test_classified_newly_eligible_runs_google_and_hubspot(monkeypatch, calls):
    log, conn = calls
    row = {"email": "alice@x.com", "eligible": True, "hubspot_contact_id": None}
    monkeypatch.setattr(ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=True))
    email_classified.handle(_event())
    assert conn.commits == 1  # DB durable before side effects
    assert ("google", "alice@x.com") in log and ("hubspot", "alice@x.com") in log
    assert ("log", "alice@x.com") in log  # log_email is called; mirror decides based on hubspot id


def test_classified_not_eligible_skips_external(monkeypatch, calls):
    log, _ = calls
    row = {"email": "bot@x.com", "eligible": False, "hubspot_contact_id": None}
    monkeypatch.setattr(ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=False))
    email_classified.handle(_event("ignore"))
    assert ("google", "bot@x.com") not in log and ("hubspot", "bot@x.com") not in log


def test_classified_already_eligible_still_ensures_hubspot_and_logs(monkeypatch, calls):
    log, _ = calls
    row = {"email": "alice@x.com", "eligible": True, "google_resource_name": "people/c1", "hubspot_contact_id": "hs1"}
    monkeypatch.setattr(ingest, "record_inbound", lambda conn, **kw: IngestResult(row=row, newly_eligible=False))
    email_classified.handle(_event())
    assert ("hubspot", "alice@x.com") in log and ("log", "alice@x.com") in log


def test_classified_passes_parsed_fields(monkeypatch, calls):
    seen = {}

    def rec(conn, **kw):
        seen.update(kw)
        return IngestResult(row={"email": "alice@x.com", "eligible": False}, newly_eligible=False)

    monkeypatch.setattr(ingest, "record_inbound", rec)
    email_classified.handle(_event())
    assert seen["sender"] == "alice@x.com" and seen["category"] == "review"
    assert seen["received_at"] == datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_sent_records_to_and_cc(monkeypatch, calls):
    log, conn = calls
    seen = {}

    def rec(conn, **kw):
        seen.update(kw)
        return [IngestResult(row={"email": "bob@x.com", "eligible": True}, newly_eligible=True)]

    monkeypatch.setattr(ingest, "record_outbound", rec)
    email_sent.handle({
        "event": "email_sent", "graph_message_id": "g1", "conversation_id": None,
        "sent_at": "2026-09-03T12:00:00Z", "from": "me@x.com", "to": ["bob@x.com"], "cc": ["carol@x.com"], "subject": "Re",
    })
    assert seen["recipients"] == ["bob@x.com", "carol@x.com"]
    assert conn.commits == 1
    assert ("google", "bob@x.com") in log and ("hubspot", "bob@x.com") in log
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_handlers.py -q` — Expected: `ModuleNotFoundError: handlers`.

- [ ] **Step 3: Implement handlers**

```python
# handlers/email_classified.py
"""email_classified → counters + eligibility (durable), then Google/HubSpot
side effects (best-effort). Spec §6.1."""

import logging

import clients.otel as otel
from clients.db import get_conn
from services import google_contacts_sync, hubspot_mirror, ingest

logger = logging.getLogger(__name__)


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_classified"})
    with get_conn() as conn:
        res = ingest.record_inbound(
            conn,
            sender=event["sender"],
            display=event.get("sender_display"),
            received_at=ingest.parse_ts(event["received_at"]),
            category=event.get("category", ""),
        )
        conn.commit()
        row = res.row
        if row.get("eligible"):
            row = google_contacts_sync.ensure_contact(conn, row)
            row = hubspot_mirror.ensure_contact(conn, row)
        hubspot_mirror.log_email(row, event)
        conn.commit()
    logger.info(
        "email_classified %s from %s — eligible=%s newly=%s",
        event.get("message_id"), row.get("email"), row.get("eligible"), res.newly_eligible,
    )
```

```python
# handlers/email_sent.py
"""email_sent → my_response_count / last_contacted for each To+Cc recipient;
writing to someone makes them eligible. Spec §6.2."""

import logging

import clients.otel as otel
from clients.db import get_conn
from services import google_contacts_sync, hubspot_mirror, ingest

logger = logging.getLogger(__name__)


def handle(event: dict) -> None:
    otel.events_received.add(1, {"event": "email_sent"})
    recipients = list(event.get("to") or []) + list(event.get("cc") or [])
    with get_conn() as conn:
        results = ingest.record_outbound(
            conn, recipients=recipients, display_by_email=None, sent_at=ingest.parse_ts(event["sent_at"])
        )
        conn.commit()
        for res in results:
            if res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
        conn.commit()
    logger.info("email_sent %s → %d recipients", event.get("graph_message_id"), len(results))
```

```python
# handlers/sync.py
"""Nightly: Google Contacts incremental sync, then HubSpot reconcile."""

import logging

from clients.db import get_conn
from services import google_contacts_sync, hubspot_mirror

logger = logging.getLogger(__name__)


def run() -> dict:
    with get_conn() as conn:
        google = google_contacts_sync.run_sync(conn)
        conn.commit()
        hubspot = hubspot_mirror.reconcile(conn)
        conn.commit()
    logger.info("sync complete google=%s hubspot=%s", google, hubspot)
    return {"google": google, "hubspot": hubspot}
```

```python
# services/sync_auth.py
import os
import secrets


def is_authorized(header: str | None) -> bool:
    expected = os.environ.get("PEOPLE_SYNC_TOKEN", "")
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header.removeprefix("Bearer "), expected)
```

- [ ] **Step 4: Run handler tests** — `.venv/bin/pytest tests/test_handlers.py -q` — Expected: pass.

- [ ] **Step 5: Failing `main.py` tests**

```python
# tests/test_main.py
import base64
import json

import pytest

import main
from handlers import email_classified, email_sent, sync as sync_handler


class Event:
    def __init__(self, payload):
        self.data = {"message": {"data": base64.b64encode(json.dumps(payload).encode()).decode(), "attributes": {}}}


@pytest.fixture
def seen(monkeypatch):
    log = []
    monkeypatch.setattr(email_classified, "handle", lambda e: log.append(("classified", e["message_id"])))
    monkeypatch.setattr(email_sent, "handle", lambda e: log.append(("sent", e["graph_message_id"])))
    return log


def test_process_routes_classified(seen):
    main.process(Event({"event": "email_classified", "message_id": "m1"}))
    assert seen == [("classified", "m1")]


def test_process_routes_sent(seen):
    main.process(Event({"event": "email_sent", "graph_message_id": "g1"}))
    assert seen == [("sent", "g1")]


def test_process_ignores_unknown(seen):
    main.process(Event({"event": "label_applied"}))
    assert seen == []


class Req:
    def __init__(self, method="POST", auth=None):
        self.method = method
        self.headers = {"Authorization": auth} if auth else {}


def test_sync_requires_bearer(monkeypatch):
    monkeypatch.setenv("PEOPLE_SYNC_TOKEN", "t0k")
    monkeypatch.setattr(sync_handler, "run", lambda: {"google": {}, "hubspot": {}})
    assert main.sync(Req(auth="Bearer nope"))[1] == 401
    body, status = main.sync(Req(auth="Bearer t0k"))
    assert status == 200 and json.loads(body) == {"google": {}, "hubspot": {}}


def test_sync_rejects_get(monkeypatch):
    monkeypatch.setenv("PEOPLE_SYNC_TOKEN", "t0k")
    assert main.sync(Req(method="GET", auth="Bearer t0k"))[1] == 405
```

- [ ] **Step 6: Run to verify failure** — `.venv/bin/pytest tests/test_main.py -q` — Expected: `ModuleNotFoundError: main` / attribute errors.

- [ ] **Step 7: Implement `main.py`**

```python
# main.py
"""
Cloud Function entry points for the people service.

process — Pub/Sub trigger on the inbox-owned email-events topic; routes
          email_classified and email_sent to handlers, ignores everything else.
sync    — HTTP trigger; POST with bearer PEOPLE_SYNC_TOKEN runs the nightly
          Google Contacts sync + HubSpot reconcile (Cloud Scheduler people-sync).

LAYER RULE: transport only — decode, route, flush telemetry, count errors.

Env: CLOUD_SQL_CONNECTION_NAME / POSTGRES_* — people DB
     GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN — People API
     GOOGLE_CONTACT_GROUP — group new contacts are added to (default "Inbox")
     HUBSPOT_TOKEN / HUBSPOT_OWNER_ID / HUBSPOT_MAX_CONTACTS / HUBSPOT_WRITES_ENABLED
     AUTOMATED_SENDER_PATTERN / AUTOMATED_SENDER_DOMAINS / OWN_ADDRESSES — eligibility
     PEOPLE_SYNC_TOKEN — bearer for POST /sync
     GRAFANA_OTLP_ENDPOINT / GRAFANA_OTLP_TOKEN — OTel export (optional)
"""

import base64
import json
import logging
import os

# force=True: the CF runtime pre-attaches a root handler, making plain
# basicConfig a no-op and dropping INFO logs (see tasks main.py).
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)

import functions_framework
from cloudevents.http import CloudEvent

import clients.otel as otel
from handlers import email_classified, email_sent
from handlers import sync as sync_handler
from services import sync_auth

logger = logging.getLogger(__name__)

otel.setup_telemetry(os.environ.get("K_SERVICE", "people-local"))


@functions_framework.cloud_event
def process(cloud_event: CloudEvent) -> None:
    data = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    otel.flush()
    kind = data.get("event")
    try:
        match kind:
            case "email_classified":
                email_classified.handle(data)
            case "email_sent":
                email_sent.handle(data)
            case other:
                logger.info("Ignoring event type %r", other)
    except Exception:
        otel.errors.add(1, {"handler": str(kind or "unknown")})
        raise
    finally:
        otel.flush()


@functions_framework.http
def sync(request):
    otel.flush()
    try:
        if request.method != "POST":
            return "", 405
        if not sync_auth.is_authorized(request.headers.get("Authorization")):
            return "", 401
        result = sync_handler.run()
        return json.dumps(result), 200, {"Content-Type": "application/json"}
    except Exception:
        otel.errors.add(1, {"handler": "sync"})
        raise
    finally:
        otel.flush()
```

- [ ] **Step 8: Run all tests + lint** — `.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ main.py` — Expected: pass, clean.

- [ ] **Step 9: Commit**

```bash
git add handlers/ services/sync_auth.py main.py tests/test_handlers.py tests/test_main.py
git commit -m "feat: event handlers, sync handler, and CF entry points"
```

---

### Task 10: `services/person_edit.py` (PATCH write-through)

**Files:**
- Create: `services/person_edit.py`
- Test: `tests/test_person_edit.py`

**Interfaces:**
- Consumes: `clients.google_contacts.update_biography/ensure_group/list_groups/modify_group_members/get_person`, `services.google_contacts_sync.sync_one`, `repo.people.get`.
- Produces: `person_edit.update(conn, email, *, notes: str | None = None, relationship_label: str | None = None) -> dict`; raises `person_edit.NotFound`, `person_edit.NotLinked`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_person_edit.py
import pytest

import clients.google_contacts as gc
import repo.people as people_repo
import services.google_contacts_sync as gsync
from services import person_edit

GROUPS = {
    "myContacts": {"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP"},
    "Inbox": {"resourceName": "contactGroups/inbox1", "groupType": "USER_CONTACT_GROUP"},
    "Family": {"resourceName": "contactGroups/fam1", "groupType": "USER_CONTACT_GROUP"},
    "Colleague": {"resourceName": "contactGroups/col1", "groupType": "USER_CONTACT_GROUP"},
}


@pytest.fixture
def wire(monkeypatch):
    log = []
    row = {"email": "a@x.com", "eligible": True, "google_resource_name": "people/c1", "google_etag": "e1"}
    monkeypatch.setattr(people_repo, "get", lambda conn, email: row if email == "a@x.com" else None)
    monkeypatch.setattr(gc, "list_groups", lambda: GROUPS)
    monkeypatch.setattr(gc, "ensure_group", lambda name: GROUPS.get(name, {"resourceName": f"contactGroups/new-{name}"})["resourceName"])
    monkeypatch.setattr(gc, "update_biography", lambda rn, etag, text: log.append(("bio", rn, etag, text)))
    monkeypatch.setattr(gc, "modify_group_members", lambda g, add, remove: log.append(("group", g, add, remove)))
    monkeypatch.setattr(
        gc, "get_person",
        lambda rn: {"resourceName": rn, "etag": "e1", "memberships": [
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/fam1"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/inbox1"}},
        ]},
    )
    monkeypatch.setattr(gsync, "sync_one", lambda conn, r: (log.append(("sync", r["email"])), {**r, "synced": True})[1])
    return log


def test_notes_write_through_then_sync(wire):
    out = person_edit.update(None, "a@x.com", notes="met at conf")
    assert wire[0] == ("bio", "people/c1", "e1", "met at conf")
    assert wire[-1] == ("sync", "a@x.com") and out["synced"] is True


def test_label_moves_between_user_groups_but_keeps_inbox(wire):
    person_edit.update(None, "a@x.com", relationship_label="colleague")
    group_calls = [c for c in wire if c[0] == "group"]
    assert ("group", "contactGroups/col1", ["people/c1"], []) in group_calls
    assert ("group", "contactGroups/fam1", [], ["people/c1"]) in group_calls
    assert not any(c[1] == "contactGroups/inbox1" for c in group_calls)


def test_unknown_email_raises(wire):
    with pytest.raises(person_edit.NotFound):
        person_edit.update(None, "nobody@x.com", notes="x")


def test_unlinked_raises(monkeypatch, wire):
    monkeypatch.setattr(people_repo, "get", lambda conn, email: {"email": email, "eligible": False, "google_resource_name": None})
    with pytest.raises(person_edit.NotLinked):
        person_edit.update(None, "a@x.com", notes="x")
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# services/person_edit.py
"""PATCH /people/{email}: write to Google Contacts first (it is the truth),
then refresh the DB row from Google. Spec §9."""

from typing import Any

import clients.google_contacts as gc
from repo import people
from services import google_contacts_sync as gsync


class NotFound(Exception):
    pass


class NotLinked(Exception):
    pass


def _set_label(person_rn: str, label: str) -> None:
    groups = gc.list_groups()
    target = gc.ensure_group(label.strip().capitalize())
    current = gc.get_person(person_rn)
    keep = {gsync.group_name()}
    for m in current.get("memberships", []):
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if not rn or rn == target:
            continue
        name_kind = next(((n, g.get("groupType")) for n, g in groups.items() if g["resourceName"] == rn), None)
        if name_kind is None or name_kind[1] == "SYSTEM_CONTACT_GROUP" or name_kind[0] in keep:
            continue
        gc.modify_group_members(rn, [], [person_rn])
    gc.modify_group_members(target, [person_rn], [])


def update(conn: Any, email: str, *, notes: str | None = None, relationship_label: str | None = None) -> dict:
    row = people.get(conn, email)
    if row is None:
        raise NotFound(email)
    rn = row.get("google_resource_name")
    if not rn:
        raise NotLinked(email)
    if notes is not None:
        gc.update_biography(rn, row.get("google_etag") or "", notes)
    if relationship_label is not None:
        _set_label(rn, relationship_label)
    return gsync.sync_one(conn, row)
```

- [ ] **Step 4: Run tests** — pass.

- [ ] **Step 5: Commit**

```bash
git add services/person_edit.py tests/test_person_edit.py
git commit -m "feat: person edit write-through to Google Contacts"
```

---

### Task 11: `people-api`

**Files:**
- Modify: `api/main.py` (routers), `api/auth.py` (env var name `PEOPLE_API_TOKEN`)
- Create: `api/routers/people.py`, `api/routers/search.py`, `api/routers/__init__.py`, `scripts/test-api-local.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `repo.people.get/search/recent`, `services.person_edit.update`, `services.google_contacts_sync.sync_one`, `clients.db.get_conn`.
- Produces: routes per spec §9; `PersonOut` pydantic model with the response shape in §9.

- [ ] **Step 1: Failing tests**

```python
# tests/test_api.py
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import clients.db as db
import repo.people as people_repo
import services.google_contacts_sync as gsync
import services.person_edit as person_edit
from api.main import app

TS = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def row(email="alice@x.com", **kw):
    base = {
        "email": email, "display_name": "Alice", "first_seen": TS, "last_seen": TS, "last_contacted": None,
        "message_count": 3, "my_response_count": 1, "relationship_label": "family", "notes": None,
        "eligible": True, "automated": False, "google_resource_name": "people/c1", "google_etag": "e",
        "google_deleted_at": None, "hubspot_contact_id": "hs1", "hubspot_synced_at": TS, "updated_at": TS,
        "last_interaction": TS,
    }
    base.update(kw)
    return base


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.delenv("PEOPLE_API_TOKEN", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setattr(db, "get_conn", lambda: Conn())
    monkeypatch.setattr(people_repo, "get", lambda conn, email: row() if email == "alice@x.com" else None)
    monkeypatch.setattr(people_repo, "search", lambda conn, q, limit: [row()] if "ali" in q else [])
    monkeypatch.setattr(people_repo, "recent", lambda conn, limit, eligible_only=True: [row()][:limit])


client = TestClient(app)


def test_get_person_shape():
    r = client.get("/people/alice@x.com")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "alice@x.com" and body["message_count"] == 3
    assert body["in_google_contacts"] is True and body["in_hubspot"] is True
    assert "google_resource_name" not in body and "hubspot_contact_id" not in body


def test_get_person_404():
    assert client.get("/people/nobody@x.com").status_code == 404


def test_get_person_normalizes_case():
    assert client.get("/people/Alice@X.com").status_code == 200


def test_search():
    r = client.post("/search", json={"q": "ali", "limit": 5})
    assert r.status_code == 200 and r.json()["results"][0]["email"] == "alice@x.com"
    assert client.post("/search", json={"q": "zzz"}).json()["results"] == []


def test_recent():
    r = client.get("/people?recent=1")
    assert r.status_code == 200 and len(r.json()["results"]) == 1


def test_patch_write_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(person_edit, "update", lambda conn, email, **kw: (seen.update(kw), row(notes=kw.get("notes")))[1])
    r = client.patch("/people/alice@x.com", json={"notes": "hi"})
    assert r.status_code == 200 and seen == {"notes": "hi", "relationship_label": None}
    assert r.json()["notes"] == "hi"


def test_patch_not_linked_is_409(monkeypatch):
    def raise_unlinked(conn, email, **kw):
        raise person_edit.NotLinked(email)

    monkeypatch.setattr(person_edit, "update", raise_unlinked)
    assert client.patch("/people/alice@x.com", json={"notes": "hi"}).status_code == 409


def test_sync_one(monkeypatch):
    monkeypatch.setattr(gsync, "sync_one", lambda conn, r: row(display_name="Alice Updated"))
    r = client.post("/people/alice@x.com/sync")
    assert r.status_code == 200 and r.json()["display_name"] == "Alice Updated"


def test_auth_fails_closed_on_cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "people-api")
    assert client.get("/people/alice@x.com").status_code == 503


def test_auth_bearer(monkeypatch):
    monkeypatch.setenv("PEOPLE_API_TOKEN", "t0k")
    assert client.get("/people/alice@x.com").status_code == 401
    assert client.get("/people/alice@x.com", headers={"Authorization": "Bearer t0k"}).status_code == 200
```

- [ ] **Step 2: Run to verify failure** — Expected: import errors on `api.routers.people`.

- [ ] **Step 3: Implement**

`api/auth.py`: copy of tasks' with `TASKS_API_TOKEN` → `PEOPLE_API_TOKEN` and docstring updated.

```python
# api/routers/people.py
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import verify_token
from clients.db import get_conn
from repo import people
from services import google_contacts_sync, person_edit
from services.eligibility import normalize

router = APIRouter(dependencies=[Depends(verify_token)])


class PersonOut(BaseModel):
    email: str
    display_name: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    last_contacted: datetime | None
    message_count: int
    my_response_count: int
    relationship_label: str | None
    notes: str | None
    eligible: bool
    automated: bool
    in_google_contacts: bool
    in_hubspot: bool


class PersonList(BaseModel):
    results: list[PersonOut]


class PersonPatch(BaseModel):
    notes: str | None = None
    relationship_label: str | None = None


def to_out(row: dict) -> PersonOut:
    return PersonOut(
        email=row["email"],
        display_name=row.get("display_name"),
        first_seen=row.get("first_seen"),
        last_seen=row.get("last_seen"),
        last_contacted=row.get("last_contacted"),
        message_count=row.get("message_count") or 0,
        my_response_count=row.get("my_response_count") or 0,
        relationship_label=row.get("relationship_label"),
        notes=row.get("notes"),
        eligible=bool(row.get("eligible")),
        automated=bool(row.get("automated")),
        in_google_contacts=bool(row.get("google_resource_name")),
        in_hubspot=bool(row.get("hubspot_contact_id")),
    )


@router.get("/people", response_model=PersonList)
def list_recent(recent: int = Query(default=20, ge=1, le=200), eligible_only: bool = True) -> PersonList:
    with get_conn() as conn:
        return PersonList(results=[to_out(r) for r in people.recent(conn, recent, eligible_only)])


@router.get("/people/{email}", response_model=PersonOut)
def get_person(email: str) -> PersonOut:
    with get_conn() as conn:
        row = people.get(conn, normalize(email))
    if row is None:
        raise HTTPException(status_code=404)
    return to_out(row)


@router.patch("/people/{email}", response_model=PersonOut)
def patch_person(email: str, body: PersonPatch) -> PersonOut:
    try:
        with get_conn() as conn:
            row = person_edit.update(
                conn, normalize(email), notes=body.notes, relationship_label=body.relationship_label
            )
            conn.commit()
    except person_edit.NotFound:
        raise HTTPException(status_code=404)
    except person_edit.NotLinked:
        raise HTTPException(status_code=409, detail="person has no Google contact")
    return to_out(row)


@router.post("/people/{email}/sync", response_model=PersonOut)
def sync_person(email: str) -> PersonOut:
    with get_conn() as conn:
        row = people.get(conn, normalize(email))
        if row is None:
            raise HTTPException(status_code=404)
        row = google_contacts_sync.sync_one(conn, row)
        conn.commit()
    return to_out(row)
```

```python
# api/routers/search.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.auth import verify_token
from api.routers.people import PersonList, to_out
from clients.db import get_conn
from repo import people

router = APIRouter(dependencies=[Depends(verify_token)])


class SearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=100)


@router.post("/search", response_model=PersonList)
def search(body: SearchRequest) -> PersonList:
    with get_conn() as conn:
        return PersonList(results=[to_out(r) for r in people.search(conn, body.q, body.limit)])
```

`api/main.py`: tasks' file with title `people-api`, default service name `people-api-local`, and

```python
from api.routers import people, search

app.include_router(people.router)
app.include_router(search.router)
```

`scripts/test-api-local.py` (used by `deploy-api.yml`):

```python
#!/usr/bin/env python3
"""Smoke test people-api: --base URL; token from PEOPLE_API_TOKEN."""
import argparse
import os
import sys

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base", default="http://127.0.0.1:8080")
args = p.parse_args()
h = {"Authorization": f"Bearer {os.environ.get('PEOPLE_API_TOKEN', '')}"}
r = httpx.get(f"{args.base}/health", timeout=30)
assert r.status_code == 200, r.text
r = httpx.get(f"{args.base}/people?recent=1", headers=h, timeout=30)
assert r.status_code == 200, r.text
print("people-api smoke OK")
sys.exit(0)
```

- [ ] **Step 4: Run tests + full CI** — `.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py` — Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add api/ scripts/test-api-local.py tests/test_api.py
git commit -m "feat: people-api — fetch, search, recent, patch, sync"
```

---

### Task 12: Import script (local, MSAL device code)

**Files:**
- Create: `scripts/import_contacts.py`, `clients/graph_local.py`
- Test: `tests/test_import_contacts.py` (the pure aggregation function only)

**Interfaces:**
- Consumes: `services.ingest.record_inbound/record_outbound`, `services.google_contacts_sync.ensure_contact`, `services.hubspot_mirror.ensure_contact`.
- Produces: `clients.graph_local.GraphLocal` (`authenticate()`, `iter_messages(folder: str, since: datetime) -> Iterator[dict]`), `import_contacts.run(conn, messages: Iterable[dict], *, own: set[str], dry_run: bool) -> dict[str, int]`.

- [ ] **Step 1: Failing test for `run`**

```python
# tests/test_import_contacts.py
from datetime import UTC, datetime
import importlib.util
from pathlib import Path

import pytest

import services.google_contacts_sync as gsync
import services.hubspot_mirror as mirror
import services.ingest as ingest
from models.types import IngestResult

spec = importlib.util.spec_from_file_location("import_contacts", Path("scripts/import_contacts.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def wired(monkeypatch):
    log = []
    monkeypatch.setattr(ingest, "record_inbound", lambda conn, **kw: (log.append(("in", kw["sender"])), IngestResult({"email": kw["sender"], "eligible": True}, True))[1])
    monkeypatch.setattr(ingest, "record_outbound", lambda conn, **kw: [IngestResult({"email": r, "eligible": True}, True) for r in kw["recipients"] if not log.append(("out", r))])
    monkeypatch.setattr(gsync, "ensure_contact", lambda c, r: (log.append(("google", r["email"])), r)[1])
    monkeypatch.setattr(mirror, "ensure_contact", lambda c, r: (log.append(("hubspot", r["email"])), r)[1])
    return log


def msg(folder, frm, to, ts="2026-09-03T12:00:00Z"):
    return {"folder": folder, "from": frm, "to": to, "cc": [], "from_name": None, "received_at": ts, "sent_at": ts, "category": "review"}


def test_run_routes_inbox_and_sent(wired):
    counts = mod.run(None, [msg("inbox", "alice@x.com", ["me@x.com"]), msg("sentitems", "me@x.com", ["bob@x.com"])], own={"me@x.com"}, dry_run=False)
    assert ("in", "alice@x.com") in wired and ("out", "bob@x.com") in wired
    assert counts["inbound"] == 1 and counts["outbound"] == 1
    assert ("google", "alice@x.com") in wired and ("google", "bob@x.com") in wired


def test_dry_run_skips_side_effects(wired):
    mod.run(None, [msg("inbox", "alice@x.com", ["me@x.com"])], own={"me@x.com"}, dry_run=True)
    assert not any(k in ("google", "hubspot") for k, _ in wired)
```

- [ ] **Step 2: Run to verify failure** — Expected: `FileNotFoundError` / attribute error.

- [ ] **Step 3: Implement the local Graph client**

```python
# clients/graph_local.py
"""Local-only Graph reader for scripts/import_contacts.py. Device-code MSAL
with its own cache file (~/.people-token-cache.json) — never the shared
msal-token-cache secret. Public client: CLIENT_ID + TENANT_ID, no secret."""

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import msal
import requests

_CACHE = Path.home() / ".people-token-cache.json"
_SCOPES = ["Mail.Read"]
_GRAPH = "https://graph.microsoft.com/v1.0"


class GraphLocal:
    def __init__(self) -> None:
        self._token: str | None = None

    def authenticate(self) -> None:
        cache = msal.SerializableTokenCache()
        if _CACHE.exists():
            cache.deserialize(_CACHE.read_text())
        app = msal.PublicClientApplication(
            os.environ["CLIENT_ID"],
            authority=f"https://login.microsoftonline.com/{os.environ['TENANT_ID']}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        result = app.acquire_token_silent(_SCOPES, account=accounts[0]) if accounts else None
        if not result:
            flow = app.initiate_device_flow(scopes=_SCOPES)
            print(flow["message"])
            result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(json.dumps(result))
        self._token = result["access_token"]
        if cache.has_state_changed:
            _CACHE.write_text(cache.serialize())

    def iter_messages(self, folder: str, since: datetime) -> Iterator[dict]:
        """Yields normalized dicts: folder, from, from_name, to, cc, received_at, sent_at."""
        url = (
            f"{_GRAPH}/me/mailFolders/{folder}/messages?$top=100"
            f"&$select=from,toRecipients,ccRecipients,receivedDateTime,sentDateTime"
            f"&$filter=receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&$orderby=receivedDateTime desc"
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        while url:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            for m in data.get("value", []):
                frm = (m.get("from") or {}).get("emailAddress") or {}
                yield {
                    "folder": folder,
                    "from": (frm.get("address") or "").lower(),
                    "from_name": frm.get("name"),
                    "to": [r["emailAddress"]["address"].lower() for r in m.get("toRecipients", []) if r.get("emailAddress", {}).get("address")],
                    "cc": [r["emailAddress"]["address"].lower() for r in m.get("ccRecipients", []) if r.get("emailAddress", {}).get("address")],
                    "received_at": m.get("receivedDateTime"),
                    "sent_at": m.get("sentDateTime"),
                    "category": "review",  # historical mail was never classified; treat as non-ignore
                }
            url = data.get("@odata.nextLink")
```

- [ ] **Step 4: Implement the script**

```python
#!/usr/bin/env python3
# scripts/import_contacts.py
"""Backfill people from Inbox + Sent Items history (spec §12). Local only.

  python scripts/import_contacts.py --days 365 [--dry-run] [--reset-counters]

Runs against whatever DB clients/db.py resolves from env (Cloud SQL connector
in .env by default). Counters are not idempotent: run once on an empty table,
or with --reset-counters to zero them first.
"""

import argparse
import os
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from services import google_contacts_sync, hubspot_mirror, ingest  # noqa: E402
from services.eligibility import is_own  # noqa: E402


def run(conn, messages: Iterable[dict], *, own: set[str], dry_run: bool) -> dict[str, int]:
    counts = {"inbound": 0, "outbound": 0, "newly_eligible": 0}
    for m in messages:
        if m["folder"] == "sentitems":
            results = ingest.record_outbound(
                conn, recipients=m["to"] + m["cc"], display_by_email=None, sent_at=ingest.parse_ts(m["sent_at"])
            )
            counts["outbound"] += 1
        else:
            if not m["from"] or m["from"] in own:
                continue
            results = [
                ingest.record_inbound(
                    conn, sender=m["from"], display=m.get("from_name"), received_at=ingest.parse_ts(m["received_at"]), category=m["category"]
                )
            ]
            counts["inbound"] += 1
        for res in results:
            if res.newly_eligible:
                counts["newly_eligible"] += 1
            if not dry_run and res.row.get("eligible"):
                row = google_contacts_sync.ensure_contact(conn, res.row)
                hubspot_mirror.ensure_contact(conn, row)
    return counts


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reset-counters", action="store_true")
    args = p.parse_args()

    from clients.db import get_conn
    from clients.graph_local import GraphLocal

    g = GraphLocal()
    g.authenticate()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    own = {a.strip().lower() for a in os.environ.get("OWN_ADDRESSES", "").split(",") if a.strip()}

    def stream():
        yield from g.iter_messages("inbox", since)
        yield from g.iter_messages("sentitems", since)

    with get_conn() as conn:
        if args.reset_counters and not args.dry_run:
            conn.execute("UPDATE people SET message_count = 0, my_response_count = 0")
            conn.commit()
        counts = run(conn, stream(), own=own, dry_run=args.dry_run)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print(counts)


if __name__ == "__main__":
    main()
```

(`is_own` import is unused in `run` — drop it if ruff complains; `own` is passed explicitly so `run` is testable without env.)

- [ ] **Step 5: Run tests + lint** — `.venv/bin/pytest tests/test_import_contacts.py -q && .venv/bin/ruff check .` — Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add clients/graph_local.py scripts/import_contacts.py tests/test_import_contacts.py
git commit -m "feat: local import script over Inbox + Sent Items history"
```

---

### Task 13: Terraform

**Files:**
- Create: `terraform/main.tf`, `variables.tf`, `secrets.tf`, `cloudsql.tf`, `pubsub.tf`, `iam.tf`, `cloud_functions.tf`, `scheduler.tf`, `api.tf`, `terraform.tfvars.example`, `project.auto.tfvars`

**Interfaces:**
- Produces: secrets `people-db-password`, `people-api-token`, `people-sync-token`, `google-contacts-refresh-token` (no version), imported `hubspot-token`; SAs `people-process-cf`, `people-sync-cf`, `people-api`; IAM grant of `secretAccessor` on `people-api-token` to `inbox-process-cf@bens-project-462804.iam.gserviceaccount.com` (inbox reads it as a data source in Phase B); outputs `people_api_url`, `sync_url`.

- [ ] **Step 1: `main.tf`, `project.auto.tfvars`, `pubsub.tf`, `cloudsql.tf`**

`main.tf` = tasks' with `prefix = "people"`. `project.auto.tfvars`: `project_id = "bens-project-462804"`. `pubsub.tf` = tasks' verbatim. `cloudsql.tf`:

```hcl
data "google_sql_database_instance" "inbox" {
  name = "inbox"
}

resource "google_sql_database" "people" {
  instance = data.google_sql_database_instance.inbox.name
  name     = "people"
}

resource "google_sql_user" "people" {
  instance = data.google_sql_database_instance.inbox.name
  name     = "people"
  password = random_password.people_db_password.result
}
```

- [ ] **Step 2: `variables.tf`**

```hcl
variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "deployer_sa" {
  description = "GitHub Actions deployer SA email (GCP_DEPLOYER_SA secret)"
  type        = string
}
variable "hubspot_owner_id" {
  description = "HubSpot owner id assigned to created contacts (personal — tfvars / GH var only)"
  type        = string
}
variable "hubspot_max_contacts" {
  type    = number
  default = 1000
}
variable "hubspot_writes_enabled" {
  description = "Phase C flips this to true (spec §11)"
  type        = bool
  default     = false
}
variable "own_addresses" {
  description = "Comma-separated addresses that are Ben (never contacts)"
  type        = string
}
variable "automated_sender_domains" {
  description = "Comma-separated domains that are never people"
  type        = string
  default     = ""
}
variable "google_contact_group" {
  type    = string
  default = "Inbox"
}
variable "inbox_process_sa" {
  description = "inbox-process CF service account — granted accessor on people-api-token so inbox can call people-api"
  type        = string
  default     = "inbox-process-cf@bens-project-462804.iam.gserviceaccount.com"
}
```

- [ ] **Step 3: `secrets.tf`**

```hcl
# Shared, owned elsewhere — data sources only.
data "google_secret_manager_secret" "shared" {
  for_each = toset([
    "grafana-otlp-endpoint",         # platform state (~/src/infra)
    "grafana-otlp-token",            # platform state
    "google-calendar-client-id",     # schedule — same OAuth client, contacts scope on OUR refresh token
    "google-calendar-client-secret", # schedule
  ])
  secret_id = each.key
  project   = var.project_id
}

# hubspot-token ALREADY EXISTS (inbox terraform created it). Import before the
# first apply, then inbox `terraform state rm`s it (companion plan Task 9):
#   terraform import google_secret_manager_secret.hubspot_token projects/${PROJECT}/secrets/hubspot-token
# Versions are not managed here: rotate with `gcloud secrets versions add`.
resource "google_secret_manager_secret" "hubspot_token" {
  secret_id = "hubspot-token"
  replication { auto {} }
}

# Minted locally by scripts/get_google_contacts_token.py, added with gcloud.
resource "google_secret_manager_secret" "google_contacts_refresh_token" {
  secret_id = "google-contacts-refresh-token"
  replication { auto {} }
}

resource "random_password" "people_db_password" {
  length  = 64
  special = false
}
resource "google_secret_manager_secret" "people_db_password" {
  secret_id = "people-db-password"
  replication { auto {} }
}
resource "google_secret_manager_secret_version" "people_db_password" {
  secret      = google_secret_manager_secret.people_db_password.id
  secret_data = random_password.people_db_password.result
}

resource "random_password" "people_api_token" {
  length  = 64
  special = false
}
resource "google_secret_manager_secret" "people_api_token" {
  secret_id = "people-api-token"
  replication { auto {} }
}
resource "google_secret_manager_secret_version" "people_api_token" {
  secret      = google_secret_manager_secret.people_api_token.id
  secret_data = random_password.people_api_token.result
}

resource "random_password" "people_sync_token" {
  length  = 64
  special = false
}
resource "google_secret_manager_secret" "people_sync_token" {
  secret_id = "people-sync-token"
  replication { auto {} }
}
resource "google_secret_manager_secret_version" "people_sync_token" {
  secret      = google_secret_manager_secret.people_sync_token.id
  secret_data = random_password.people_sync_token.result
}
```

Add `random = { source = "hashicorp/random" }` to `required_providers` in `main.tf`.

- [ ] **Step 4: `iam.tf`**

```hcl
locals {
  cf_sas = {
    process = google_service_account.people_process_cf.email
    sync    = google_service_account.people_sync_cf.email
  }
  owned_secrets = {
    hubspot   = google_secret_manager_secret.hubspot_token.secret_id
    google_rt = google_secret_manager_secret.google_contacts_refresh_token.secret_id
    db        = google_secret_manager_secret.people_db_password.secret_id
  }
}

resource "google_service_account" "people_process_cf" {
  account_id   = "people-process-cf"
  display_name = "People Process Cloud Function"
}

resource "google_service_account" "people_sync_cf" {
  account_id   = "people-sync-cf"
  display_name = "People Sync Cloud Function"
}

resource "google_secret_manager_secret_iam_member" "cf_shared" {
  for_each = {
    for pair in setproduct(keys(local.cf_sas), keys(data.google_secret_manager_secret.shared)) :
    "${pair[0]}-${pair[1]}" => { sa = local.cf_sas[pair[0]], secret = pair[1] }
  }
  secret_id = data.google_secret_manager_secret.shared[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.sa}"
}

resource "google_secret_manager_secret_iam_member" "cf_owned" {
  for_each = {
    for pair in setproduct(keys(local.cf_sas), keys(local.owned_secrets)) :
    "${pair[0]}-${pair[1]}" => { sa = local.cf_sas[pair[0]], secret = local.owned_secrets[pair[1]] }
  }
  secret_id = each.value.secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.sa}"
}

resource "google_secret_manager_secret_iam_member" "sync_cf_sync_token" {
  secret_id = google_secret_manager_secret.people_sync_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.people_sync_cf.email}"
}

resource "google_project_iam_member" "cf_cloudsql" {
  for_each = local.cf_sas
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = "serviceAccount:${each.value}"
}

# Inbox calls people-api at classify time (companion plan Task 4). The secret
# owner grants; inbox references the secret as a data source.
resource "google_secret_manager_secret_iam_member" "inbox_process_api_token" {
  secret_id = google_secret_manager_secret.people_api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.inbox_process_sa}"
}
```

- [ ] **Step 5: `cloud_functions.tf`**

Start from tasks' file: bucket `${var.project_id}-people-cf-source`, zip `people.zip`, object prefix `people-`. Replace `common_env`:

```hcl
  common_env = {
    GCP_PROJECT_ID            = var.project_id
    CLOUD_SQL_CONNECTION_NAME = data.google_sql_database_instance.inbox.connection_name
    POSTGRES_USER             = google_sql_user.people.name
    POSTGRES_DB               = google_sql_database.people.name
    HUBSPOT_OWNER_ID          = var.hubspot_owner_id
    HUBSPOT_MAX_CONTACTS      = tostring(var.hubspot_max_contacts)
    HUBSPOT_WRITES_ENABLED    = tostring(var.hubspot_writes_enabled)
    OWN_ADDRESSES             = var.own_addresses
    AUTOMATED_SENDER_DOMAINS  = var.automated_sender_domains
    GOOGLE_CONTACT_GROUP      = var.google_contact_group
  }
```

Two functions. `people_process`: name `people-process`, entry `process`, SA `people_process_cf`, `timeout_seconds = 120`, `available_memory = "512Mi"`, `event_trigger` on `data.google_pubsub_topic.email_events.id` with `RETRY_POLICY_RETRY`. `people_sync`: name `people-sync`, entry `sync`, SA `people_sync_cf`, `timeout_seconds = 540` (a full Google list + HubSpot page-through), public invoker bindings like tasks' webhook (Cloud Scheduler calls it with the bearer token; app-level auth). Both get these `secret_environment_variables`:

| key | secret |
|---|---|
| `HUBSPOT_TOKEN` | `google_secret_manager_secret.hubspot_token.secret_id` |
| `GOOGLE_CLIENT_ID` | `data.google_secret_manager_secret.shared["google-calendar-client-id"].secret_id` |
| `GOOGLE_CLIENT_SECRET` | `data.google_secret_manager_secret.shared["google-calendar-client-secret"].secret_id` |
| `GOOGLE_REFRESH_TOKEN` | `google_secret_manager_secret.google_contacts_refresh_token.secret_id` |
| `POSTGRES_PASSWORD` | `google_secret_manager_secret.people_db_password.secret_id` |
| `GRAFANA_OTLP_ENDPOINT` / `GRAFANA_OTLP_TOKEN` | shared |

`people_sync` additionally: `PEOPLE_SYNC_TOKEN` ← `google_secret_manager_secret.people_sync_token.secret_id`. Output `sync_url = google_cloudfunctions2_function.people_sync.service_config[0].uri`.

- [ ] **Step 6: `scheduler.tf`**

```hcl
resource "google_cloud_scheduler_job" "people_sync" {
  name      = "people-sync"
  schedule  = "0 4 * * *"
  time_zone = "America/New_York"
  region    = var.region

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.people_sync.service_config[0].uri
    body        = base64encode("{}")
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${random_password.people_sync_token.result}"
    }
  }
}
```

- [ ] **Step 7: `api.tf`**

Tasks' `api.tf` with: AR repo `people`, image `people/people-api:latest`, SA `people-api`, service `people-api`, env `GCP_PROJECT_ID`, `CLOUD_SQL_CONNECTION_NAME`, `POSTGRES_USER`, `POSTGRES_DB`, `GOOGLE_CONTACT_GROUP`; secret envs `PEOPLE_API_TOKEN` (people_api_token), `POSTGRES_PASSWORD`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GRAFANA_OTLP_*`. Remove the tasks-specific `ASANA_*` envs and the `tasks_api_token` variable/version (the token is `random_password` here). IAM: SA accessor on shared + `people_api_token` + `people_db_password` + `google_contacts_refresh_token`; `cloudsql.client`; AR reader; deployer AR writer + run developer. Output `people_api_url`.

- [ ] **Step 8: `terraform.tfvars.example`**

```hcl
deployer_sa              = "github-deployer@bens-project-462804.iam.gserviceaccount.com"
hubspot_owner_id         = "<numeric owner id from HubSpot>"
own_addresses            = "you@example.com,alias@example.com"
automated_sender_domains = "group.calendar.google.com,bcc.na2.hubspot.com"
# hubspot_writes_enabled = true   # Phase C
```

- [ ] **Step 9: Validate**

```bash
cd terraform && terraform init -backend=false && terraform validate && terraform fmt -check
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 10: Commit**

```bash
git add terraform/
git commit -m "infra: people terraform — CFs, scheduler, api, DB, secrets, IAM"
```

---

### Task 14: CI/CD workflows

**Files:**
- Modify: `.github/workflows/deploy.yml`, `.github/workflows/deploy-api.yml`, `.github/workflows/ci.yml` (from the scaffold)

- [ ] **Step 1: `deploy.yml` env block**

```yaml
        env:
          TF_VAR_project_id: bens-project-462804
          TF_VAR_deployer_sa: ${{ secrets.GCP_DEPLOYER_SA }}
          TF_VAR_hubspot_owner_id: ${{ vars.HUBSPOT_OWNER_ID }}
          TF_VAR_own_addresses: ${{ vars.OWN_ADDRESSES }}
          TF_VAR_automated_sender_domains: ${{ vars.AUTOMATED_SENDER_DOMAINS }}
          TF_VAR_hubspot_writes_enabled: ${{ vars.HUBSPOT_WRITES_ENABLED || 'false' }}
```

(`hubspot_max_contacts` and `google_contact_group` keep their defaults.)

- [ ] **Step 2: `deploy-api.yml`**

Replace every `tasks` with `people`, `TASKS_API_TOKEN` with `PEOPLE_API_TOKEN`, and the token source with `${{ secrets.PEOPLE_API_TOKEN }}`. (The token is generated by Terraform; Task 15 Step 6 copies it into the GitHub secret.)

- [ ] **Step 3: `ci.yml`** — tasks' verbatim (already includes `api/` in mypy and `terraform validate`).

- [ ] **Step 4: Run local CI, commit**

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && (cd terraform && terraform validate)
git add .github/
git commit -m "ci: deploy workflows for people CFs and people-api"
```

---

### Task 15: First deploy (Phase A) — manual runbook

**Files:** none new (`terraform/terraform.tfvars` is gitignored). Record what you did in the PR description at Task 17.

- [ ] **Step 1: tfvars and GitHub configuration**

```bash
cd ~/src/people/terraform && cp terraform.tfvars.example terraform.tfvars   # fill in real values
gh secret set GCP_WIF_PROVIDER --repo bdrolet/people --body "$(gh secret list --repo bdrolet/tasks >/dev/null && op read 'op://Personal/GCP WIF/provider')"
gh secret set GCP_DEPLOYER_SA  --repo bdrolet/people --body "<deployer sa email>"
gh variable set HUBSPOT_OWNER_ID --repo bdrolet/people --body "<id>"
gh variable set OWN_ADDRESSES --repo bdrolet/people --body "<comma list>"
gh variable set AUTOMATED_SENDER_DOMAINS --repo bdrolet/people --body "group.calendar.google.com,bcc.na2.hubspot.com"
```

(Take `GCP_WIF_PROVIDER` / `GCP_DEPLOYER_SA` from the tasks repo's settings if 1Password has no entry.) Also add the WIF provider's attribute condition for `repository == "bdrolet/people"` if the pool restricts by repo — check `~/src/infra` for where the pool is defined.

- [ ] **Step 2: Import the existing HubSpot secret, then plan**

```bash
cd ~/src/people/terraform && terraform init
terraform import google_secret_manager_secret.hubspot_token projects/bens-project-462804/secrets/hubspot-token
```

Then `/terraform-plan`. Expected: creates DB + user, 3 SAs, 4 owned secrets (+3 versions), CF bucket, 2 CFs, scheduler job, AR repo, Cloud Run service (will fail until an image exists — see Step 4), IAM. **No destroys.** The Cloud Run resource depends on an image; apply the AR repo first:

```bash
terraform apply -target=google_artifact_registry_repository.people
```

- [ ] **Step 3: Build and push the first API image**

```bash
cd ~/src/people
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker build -t us-central1-docker.pkg.dev/bens-project-462804/people/people-api:latest .
docker push us-central1-docker.pkg.dev/bens-project-462804/people/people-api:latest
```

- [ ] **Step 4: Full apply** — `/terraform-apply`. Expected: all resources created; outputs `people_api_url`, `sync_url`.

- [ ] **Step 5: Schema + Google refresh token**

```bash
cd ~/src/people
PW=$(gcloud secrets versions access latest --secret people-db-password)
CLOUD_SQL_CONNECTION_NAME=bens-project-462804:us-central1:inbox POSTGRES_USER=people POSTGRES_PASSWORD="$PW" POSTGRES_DB=people .venv/bin/python scripts/migrate_db.py

GOOGLE_CLIENT_ID=$(gcloud secrets versions access latest --secret google-calendar-client-id) \
GOOGLE_CLIENT_SECRET=$(gcloud secrets versions access latest --secret google-calendar-client-secret) \
.venv/bin/python scripts/get_google_contacts_token.py
# paste the printed token:
printf '%s' "<token>" | gcloud secrets versions add google-contacts-refresh-token --data-file=-
```

Expected: `Migration complete`; a new version on `google-contacts-refresh-token`. The CFs pick it up on next cold start (env secret pinned to `latest`).

- [ ] **Step 6: GitHub secret for the API smoke test**

```bash
gh secret set PEOPLE_API_TOKEN --repo bdrolet/people --body "$(gcloud secrets versions access latest --secret people-api-token)"
```

- [ ] **Step 7: First Google sync (bootstrap) and smoke tests**

```bash
SYNC_URL=$(cd terraform && terraform output -raw sync_url)
curl -s -X POST "$SYNC_URL" -H "Authorization: Bearer $(gcloud secrets versions access latest --secret people-sync-token)"
```

Expected: JSON with `google.created` roughly equal to the number of Google Contacts with an email, `hubspot` all zeros (writes disabled). Then:

```bash
API=$(cd terraform && terraform output -raw people_api_url)
PEOPLE_API_TOKEN=$(gcloud secrets versions access latest --secret people-api-token) .venv/bin/python scripts/test-api-local.py --base "$API"
```

Expected: `people-api smoke OK`.

- [ ] **Step 8: Gate A — a live event lands**

Send yourself an email from a non-automated address (or wait for one). Then:

```bash
gcloud functions logs read people-process --region us-central1 --limit 20
curl -s "$API/people/<sender>" -H "Authorization: Bearer $PEOPLE_API_TOKEN"
```

Expected: a log line `email_classified <id> from <sender> — eligible=True newly=True`, the API returns the row with `message_count: 1`, and (if the sender was new) a contact appears in Google Contacts under the `Inbox` group. Record the message id and sender in the PR.

---

### Task 16: Skills, CLAUDE.md, README

**Files:**
- Create: `.claude/skills/{people-architecture,deploy-people,fetch-people-logs,querying-people-db,adding-people-secret,adding-observability,querying-grafana-metrics,testing-people-handlers,verifying-pr-locally,importing-contacts}/SKILL.md`
- Create (global): `~/.claude/skills/{searching-people,fetching-person,editing-person}/SKILL.md`
- Modify (global): `~/.claude/skills/adding-referral-contact/SKILL.md`
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: Repo skills** — run the `setting-up-service-repo` skill's Phase 2 agent (`agents/create-claude-skills.md`) with repo `people`, CFs `people-process` / `people-sync`, API `people-api`, DB `people`. Then hand-edit:
  - `querying-people-db`: connection is `POSTGRES_USER=people POSTGRES_DB=people`, password from `people-db-password`; sample queries: recent people by last interaction, eligible-not-in-hubspot count, managed count.
  - `testing-people-handlers`: how to craft an `email_classified` / `email_sent` JSON and invoke `handlers.*.handle` locally against the prod DB with `HUBSPOT_WRITES_ENABLED=false`.
  - `importing-contacts`: the §12 usage, `--dry-run` first, prerequisites (`CLIENT_ID`, `TENANT_ID`, `OWN_ADDRESSES` in `.env`).
  - `people-architecture`: the spec's §1, §3, §4.3, §5, §8.1 condensed.

- [ ] **Step 2: Global skills** — model on `~/.claude/skills/searching-tasks`, `fetching-task`, `editing-tasks`: base URL from `cd ~/src/people/terraform && terraform output -raw people_api_url`, token via `gcloud secrets versions access latest --secret people-api-token`. `searching-people` covers `POST /search` and `GET /people?recent=`; `fetching-person` covers `GET /people/{email}`; `editing-person` covers `PATCH` (notes / relationship_label) then shows the returned row.

- [ ] **Step 3: Repoint `adding-referral-contact`** — project root `/Users/ben/src/people`; bulk import → `python scripts/import_contacts.py --days 365`; single contact → `PATCH` via `editing-person` is not creation — say that a person becomes a contact by emailing or being emailed, and point to `searching-people`; keep the "Phase 2 Enrichment (not yet built)" section, replacing `clients/pdl.py` paths with `~/src/people/clients/pdl.py`.

- [ ] **Step 4: CLAUDE.md** — fill the scaffold skeleton with: the dividing rule (spec §1), the Stack table (spec §3), event schema (`email_classified` fields used: sender, sender_display, category, received_at, subject, body, body_html; `email_sent` fields: from, to, cc, sent_at), the source-of-truth table (§4.3), eligibility (§5), HubSpot cap rules (§8.1), layer rules, secrets table (owned vs shared), the development workflow paragraph copied from schedule's CLAUDE.md (feature branch + `/pr-open`), local dev commands, and "Phase C: set `hubspot_writes_enabled = true`".

- [ ] **Step 5: README.md** — tasks' README structure: one-liner → how it works → project structure → local dev → deployment → first-time setup (Task 15 condensed) → Mermaid diagram (inbox → email-events → people-process → {people DB, Google Contacts, HubSpot}; people-api ← inbox + skills; people-sync ← scheduler).

- [ ] **Step 6: Commit**

```bash
git add .claude/ CLAUDE.md README.md
git commit -m "docs: skills, CLAUDE.md, README"
```

---

### Task 17: PR, merge, verify CI deploys

- [ ] **Step 1: Local CI green** — run the full command from Global Constraints. Expected: all pass.

- [ ] **Step 2: Open the PR** — `/pr-open`. Title `feat: people service v1 (Phase A)`. Body: link the spec, list what was deployed manually in Task 15 (import of `hubspot-token`, first sync counts, Gate A evidence), note `HUBSPOT_WRITES_ENABLED=false`.

- [ ] **Step 3: Merge** after CI passes; confirm `Deploy Functions` and `Deploy API` workflows succeed on `main` (they re-apply the same state — expected no-op apart from the source zip hash).

- [ ] **Step 4: Hand off** — the inbox companion plan may now start. Do **not** proceed to Task 18 until that plan's Gate B has passed.

---

### Task 18: Phase C — turn the HubSpot mirror on

**Prerequisite:** inbox companion plan complete (inbox no longer writes HubSpot; `email_sent` flowing).

- [ ] **Step 1: Baseline** — `people-sync` has run at least one night since Phase B so `my_response_count` and `last_contacted` are populated for recent correspondents. Check: `SELECT count(*) FROM people WHERE my_response_count > 0;` via `querying-people-db`. Expected: > 0.

- [ ] **Step 2: Dry read of the eviction set** — before enabling, look at what the first reconcile will archive:

```sql
-- managed will be empty until adopt runs; preview by email against HubSpot's list instead:
SELECT count(*) FROM people WHERE eligible;                                   -- candidates
SELECT email, last_seen, last_contacted FROM people WHERE eligible
ORDER BY GREATEST(COALESCE(last_seen,'epoch'), COALESCE(last_contacted,'epoch')) DESC OFFSET 1000 LIMIT 20;  -- first to be left out
```

- [ ] **Step 3: Flip the flag**

```bash
gh variable set HUBSPOT_WRITES_ENABLED --repo bdrolet/people --body "true"
# and locally in terraform.tfvars: hubspot_writes_enabled = true
```

`/terraform-plan` → expect only env var changes on both CFs. `/terraform-apply`.

- [ ] **Step 4: Run the reconcile by hand and review**

```bash
curl -s -X POST "$(cd terraform && terraform output -raw sync_url)" -H "Authorization: Bearer $(gcloud secrets versions access latest --secret people-sync-token)"
```

Expected: `hubspot.adopted` ≈ number of HubSpot contacts inbox created that match eligible rows; `hubspot.evicted` = (HubSpot total − 1000) if over cap; `hubspot.filled` ≥ 0. Spot-check in HubSpot → Contacts that the total is ≤ 1000 and a recent correspondent is present. Archived contacts are recoverable for 90 days from HubSpot's Contacts → Actions → Restore.

- [ ] **Step 5: Confirm engagements resume** — after the next inbound email from a mirrored contact, its HubSpot timeline shows a new Email engagement; `people_hubspot_engagements_logged` increments in Grafana (`querying-grafana-metrics`).

- [ ] **Step 6: Update CLAUDE.md** — change the Phase C note to "shipped"; commit via a small PR.

---

## Self-review

**Spec coverage.** §3 components → Tasks 1, 9, 11, 13; §4 data model → Task 3; §5 eligibility → Tasks 2, 4; §6 events → Tasks 4, 9; §7 Google → Tasks 7, 8, 15 Step 5/7; §8 HubSpot → Tasks 5, 6, 18; §9 API → Task 11; §11 cutover Phase A → Task 15, Phase C → Task 18 (Phase B is the companion plan); §12 import → Task 12; §13 skills → Task 16; §14 metrics → Task 1 Step 4 plus per-service `otel.*` calls; §15 tests → every task's Step 1. Gap noted: the spec's `people_upserts` metric label is `direction` — matched in Task 4. The spec says outbound engagements are not logged (§6.2) — matched, `email_sent.handle` never calls `log_email`.

**Type consistency.** `IngestResult(row, newly_eligible)` used identically in Tasks 4, 9, 12. `people.set_google(conn, email, *, resource_name, etag, display_name, notes, relationship_label)` matches Task 8's `_link`. `hubspot_mirror.ensure_contact(conn, row) -> dict` and `google_contacts_sync.ensure_contact(conn, row) -> dict` are chained in that order in Tasks 9 and 12. `sync_state.set_token(conn, token, status)` matches Task 8. `person_edit.update(conn, email, *, notes, relationship_label)` matches Task 11's router. Event key for the sender of a sent mail is `"from"` everywhere (`EmailSentEvent` docstring, Task 9 handler ignores it, Task 12 uses `m["from"]` only for inbound).

**Placeholders.** None: every code step carries the code; the two "copy from tasks" steps name the exact source file and the exact edits.
