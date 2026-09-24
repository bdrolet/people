# Editable Google Contact Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `PATCH /people/{email}` write any Google-supported contact field, and serve those fields back from four new columns on `people`.

**Architecture:** One generic `contact` map on the existing PATCH body is validated against an allowlist, then written to Google in a single `updateContact` call; the DB row is refreshed from Google exactly as it is today. Read-back comes from three typed columns (`phone_numbers`, `company`, `job_title`) plus a `google_fields` JSONB blob, all derived from the same Google payload by one pure function.

**Tech Stack:** Python 3.13, FastAPI, Google People API v1 (`googleapiclient`), Postgres (pg8000 on Cloud SQL / psycopg3 locally), pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-contact-field-edits-design.md` — read it before Task 1; every task cites its sections.

## Global Constraints

- **Branch:** `contact-field-edits` (exists, spec committed). Never commit to `main`; open the PR with `/pr-open`.
- **Google is the source of truth.** Write Google first, then refresh the DB from it. Nothing in this change writes these fields from event data, and none of them go to HubSpot.
- **One input path.** `phone_numbers`, `company`, `job_title` are DERIVED on refresh. They are never request inputs and never written directly by a caller.
- **`notes` and `relationship_label` behavior must not change.** They keep their own dedicated PATCH fields; `biographies` and `memberships` are rejected inside `contact`.
- **Email addresses are add-only** (spec §5.3) — every existing address and the row's keyed address must survive any `emailAddresses` write.
- **ALLOWLIST IS VERIFIED.** A live probe against the People API confirmed that of the candidate fields, only `metadata` and `photos` are invalid `updatePersonFields` mask paths. All 24 others are accepted. `biographies` and `memberships` are excluded by POLICY, not by the API.
- **Layer rules** (CLAUDE.md): `clients/` I/O only; `repo/` DB-only taking an open connection; `services/` pure business logic; `api/routers/` thin transport; `models/` pure types.
- **Python 3.13:** `X | None`, never `Optional[X]`.
- **Every task ends green:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
- **Tests never** connect to the production database, call the real Google API, or read the user's real `chat.db`. Test data uses `+1555010xxxx` and `example.com` only.
- **Commit trailers** on every commit:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01Diz1yX2VxC8bBQTB7xzT7M`

## Review Focus

Five input classes the spec implies but does not give a task; each line's test is attached to the task named.

1. **An empty list clears a field in Google.** `{"phoneNumbers": []}` is the only way to delete every phone number, so it must be allowed — but a caller can wipe a field by accident. Expected: allowed, and the post-write refresh leaves `phone_numbers` as `{}` rather than stale values. *Test in Task 2 (derive) and Task 4 (write path).*
2. **`{"emailAddresses": []}` must be refused**, not treated as "no addresses submitted" — it would strip the linkage address. Expected: 409, same as any removal. *Test in Task 2.*
3. **`contact` absent vs `{}`.** Absent means "don't touch fields"; `{}` means "present but nothing to write" and must issue no `updateContact`. Expected: neither errors, neither writes. *Test in Task 4.*
4. **An `organizations` entry with no `metadata.primary`,** or several entries where none is primary. Expected: first entry wins; never a crash, never NULL when data exists. *Test in Task 2.*
5. **A contact edited in Google between the read and the write** (stale etag). Expected: 409 telling the caller to retry, not a 500 and not a silent overwrite. *Test in Task 4 and Task 6.*

---

## Task 1: Schema — four columns, three indexes

**Files:**
- Modify: `repo/schema.sql` (append to the `people` section)
- Modify: `tests/test_schema.py`

**Interfaces:**
- Produces: columns `people.phone_numbers TEXT[]`, `people.company TEXT`, `people.job_title TEXT`, `people.google_fields JSONB`. Consumed by Tasks 5, 6.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schema.py` (it already has the `conn` fixture that applies `repo/schema.sql` to a scratch database and skips without `TEST_DATABASE_URL`):

```python
def test_people_has_contact_field_columns(conn):
    cols = {
        r[0]: r[1]
        for r in conn.execute(
            "select column_name, data_type from information_schema.columns"
            " where table_name = 'people'"
        ).fetchall()
    }
    assert cols["phone_numbers"] == "ARRAY"
    assert cols["company"] == "text"
    assert cols["job_title"] == "text"
    assert cols["google_fields"] == "jsonb"


def test_contact_field_defaults_and_jsonb_roundtrip(conn):
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('alice@example.com', now())")
    row = conn.execute(
        "select phone_numbers, company, job_title, google_fields from people"
    ).fetchone()
    assert row[0] == [] and row[1] is None and row[2] is None and row[3] == {}

    conn.execute(
        "UPDATE people SET phone_numbers = %s, google_fields = %s WHERE email = %s",
        (["+15550100001"], '{"birthdays": [{"date": {"month": 4, "day": 2}}]}', "alice@example.com"),
    )
    row = conn.execute("select phone_numbers, google_fields from people").fetchone()
    assert row[0] == ["+15550100001"]
    assert row[1]["birthdays"][0]["date"]["month"] == 4


def test_contact_field_indexes_exist(conn):
    idx = {r[0] for r in conn.execute(
        "select indexname from pg_indexes where tablename = 'people'").fetchall()}
    assert {"people_phone_numbers_idx", "people_google_fields_idx",
            "people_company_trgm_idx"} <= idx
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker run -d --rm --name pg-t1 -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
# wait for readiness (e.g. until docker exec pg-t1 pg_isready -q; do sleep 1; done)
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -v
```

Expected: FAIL — `KeyError: 'phone_numbers'`.

- [ ] **Step 3: Append the columns and indexes to `repo/schema.sql`**

Place directly after the existing `people` indexes, before the `sync_state` table:

```sql
-- Editable Google contact fields
-- (docs/superpowers/specs/2026-09-24-contact-field-edits-design.md §4).
-- All four are Google-owned and refreshed from it; the three typed columns are a
-- derived index over google_fields, not a separate source.
ALTER TABLE people
  ADD COLUMN IF NOT EXISTS phone_numbers TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS company       TEXT,
  ADD COLUMN IF NOT EXISTS job_title     TEXT,
  ADD COLUMN IF NOT EXISTS google_fields JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS people_phone_numbers_idx ON people USING gin (phone_numbers);
CREATE INDEX IF NOT EXISTS people_google_fields_idx ON people USING gin (google_fields);
CREATE INDEX IF NOT EXISTS people_company_trgm_idx  ON people USING gin (company gin_trgm_ops);
```

`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is idempotent and safe to re-run against the live database, matching how `migrate_db.py` is used.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -v
docker stop pg-t1
```

Expected: all pass, including the pre-existing schema tests.

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql tests/test_schema.py
git commit -m "feat: contact field columns on people"
```

---

## Task 2: `services/contact_fields.py` — validation and derivation

**Files:**
- Create: `services/contact_fields.py`
- Create: `tests/test_contact_fields.py`

**Interfaces:**
- Consumes: `services.eligibility.normalize`, `services.imessage_export.normalize_handle`.
- Produces:

```python
WRITABLE_FIELDS: frozenset[str]          # the 24 API-accepted, policy-allowed keys
OWNED_ELSEWHERE: dict[str, str]          # {"biographies": "notes", "memberships": "relationship_label"}

class ValidationError(Exception): ...    # -> 400
class EmailRuleError(Exception): ...     # -> 409

def validate(contact: dict) -> dict:
    """Allowlist + shape check. Returns the normalized map (bare objects wrapped
    in a one-element list). Raises ValidationError."""

def check_email_addition(live: dict, submitted: list[dict], keyed_email: str) -> None:
    """Raises EmailRuleError if any existing address, or the keyed address,
    is missing from `submitted`."""

def derive(person: dict) -> dict:
    """Google person payload -> {"phone_numbers": list[str], "company": str | None,
    "job_title": str | None, "google_fields": dict}."""
```

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from services import contact_fields as cf


# --- validate -------------------------------------------------------------

def test_validate_accepts_allowlisted_fields():
    out = cf.validate({"phoneNumbers": [{"value": "+15550100001"}],
                       "names": [{"givenName": "Alice"}]})
    assert out["names"] == [{"givenName": "Alice"}]


def test_validate_wraps_a_bare_object_in_a_list():
    assert cf.validate({"birthdays": {"date": {"month": 4, "day": 2}}}) == {
        "birthdays": [{"date": {"month": 4, "day": 2}}]
    }


def test_validate_allows_empty_list_to_clear_a_field():
    # Review Focus 1: the only way to delete every phone number.
    assert cf.validate({"phoneNumbers": []}) == {"phoneNumbers": []}


def test_validate_rejects_fields_owned_elsewhere():
    with pytest.raises(cf.ValidationError, match="notes"):
        cf.validate({"biographies": [{"value": "hi"}]})
    with pytest.raises(cf.ValidationError, match="relationship_label"):
        cf.validate({"memberships": []})


def test_validate_rejects_unknown_and_unwritable_fields():
    with pytest.raises(cf.ValidationError, match="photos"):
        cf.validate({"photos": []})
    with pytest.raises(cf.ValidationError, match="nope"):
        cf.validate({"nope": []})


def test_validate_rejects_bad_shapes():
    with pytest.raises(cf.ValidationError):
        cf.validate({"phoneNumbers": "+15550100001"})   # scalar
    with pytest.raises(cf.ValidationError):
        cf.validate({"phoneNumbers": [["nested"]]})     # list of non-objects


# --- check_email_addition -------------------------------------------------

LIVE = {"emailAddresses": [{"value": "Alice@Example.com"}, {"value": "a2@example.com"}]}


def test_email_addition_allows_adding():
    cf.check_email_addition(
        LIVE,
        [{"value": "alice@example.com"}, {"value": "a2@example.com"}, {"value": "new@example.com"}],
        "alice@example.com",
    )


def test_email_addition_ignores_case_and_whitespace():
    cf.check_email_addition(
        LIVE, [{"value": " ALICE@example.com "}, {"value": "a2@example.com"}], "alice@example.com"
    )


def test_email_addition_rejects_removal():
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(LIVE, [{"value": "alice@example.com"}], "alice@example.com")


def test_email_addition_rejects_empty_list():
    # Review Focus 2: {} would strip the linkage address.
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(LIVE, [], "alice@example.com")


def test_email_addition_rejects_dropping_the_keyed_address():
    live = {"emailAddresses": [{"value": "alice@example.com"}]}
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(live, [{"value": "other@example.com"}], "alice@example.com")


# --- derive ---------------------------------------------------------------

def test_derive_normalizes_phone_numbers_to_e164():
    got = cf.derive({"phoneNumbers": [{"value": "(555) 010-0001"}, {"value": "+15550100002"}]})
    assert got["phone_numbers"] == ["+15550100001", "+15550100002"]


def test_derive_drops_unparseable_numbers_and_duplicates():
    got = cf.derive({"phoneNumbers": [{"value": "+15550100001"}, {"value": "555-010-0001"},
                                      {"value": "nonsense"}]})
    assert got["phone_numbers"] == ["+15550100001"]


def test_derive_prefers_the_primary_organization():
    got = cf.derive({"organizations": [
        {"name": "Second Co", "title": "Advisor"},
        {"name": "Example Health", "title": "CTO", "metadata": {"primary": True}},
    ]})
    assert (got["company"], got["job_title"]) == ("Example Health", "CTO")


def test_derive_falls_back_to_the_first_organization():
    # Review Focus 4: nothing flagged primary.
    got = cf.derive({"organizations": [{"name": "Only Co", "title": "Lead"}]})
    assert (got["company"], got["job_title"]) == ("Only Co", "Lead")


def test_derive_handles_missing_and_empty_payloads():
    got = cf.derive({})
    assert got == {"phone_numbers": [], "company": None, "job_title": None, "google_fields": {}}
    got = cf.derive({"phoneNumbers": [], "organizations": []})
    assert got["phone_numbers"] == [] and got["company"] is None


def test_derive_keeps_only_allowlisted_keys_in_google_fields():
    got = cf.derive({"names": [{"givenName": "Alice"}], "metadata": {"sources": []},
                     "biographies": [{"value": "hi"}], "memberships": []})
    assert "names" in got["google_fields"]
    assert "metadata" not in got["google_fields"]
    assert "biographies" not in got["google_fields"]   # owned by notes
    assert "memberships" not in got["google_fields"]   # owned by relationship_label
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_contact_fields.py -v
```

Expected: FAIL — `ModuleNotFoundError: services.contact_fields`.

- [ ] **Step 3: Implement**

`services/contact_fields.py`. Points that matter:

- `WRITABLE_FIELDS` is the 24 verified keys: `addresses, birthdays, calendarUrls, clientData, emailAddresses, events, externalIds, genders, imClients, interests, locales, locations, miscKeywords, names, nicknames, occupations, organizations, phoneNumbers, relations, sipAddresses, urls, userDefined` — plus `biographies` and `memberships`, which the API accepts but which `OWNED_ELSEWHERE` rejects with a message naming the PATCH field that owns them. `metadata` and `photos` are absent; the API rejects them as invalid mask paths.
- `validate` raises with all offending keys named, not just the first.
- `derive` reads `phoneNumbers[].value`, normalizes with `normalize_handle`, drops `None` results, de-duplicates while preserving order (`dict.fromkeys`).
- `derive`'s `google_fields` keeps only keys in `WRITABLE_FIELDS` minus `OWNED_ELSEWHERE`, so the blob never shadows `notes`/`relationship_label` or stores volatile `metadata`.
- Pure module: no I/O, no DB, no network.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/test_contact_fields.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/contact_fields.py tests/test_contact_fields.py
git commit -m "feat: contact field validation and derivation"
```

---

## Task 3: Google client — wider read mask, one write call

**Files:**
- Modify: `clients/google_contacts.py:19-20` (`PERSON_FIELDS`, `_READ_MASK`), and `update_biography`
- Modify: `tests/test_google_contacts.py`

**Interfaces:**
- Consumes: `services.contact_fields.WRITABLE_FIELDS` (module-level import is fine: `clients` → `services` already happens in this file for `normalize_handle`, and is documented there).
- Produces:

```python
def update_fields(resource_name: str, etag: str, fields: dict) -> dict:
    """One updateContact; updatePersonFields is the comma-joined sorted keys of `fields`."""
```
`update_biography(resource_name, etag, text)` is REMOVED; callers use `update_fields(rn, etag, {"biographies": [{"value": text, "contentType": "TEXT_PLAIN"}]})`.

- [ ] **Step 1: Write the failing tests**

Follow the fake-service pattern already in `tests/test_google_contacts.py`.

```python
def test_person_fields_covers_the_writable_allowlist():
    from services.contact_fields import OWNED_ELSEWHERE, WRITABLE_FIELDS

    mask = set(google_contacts.PERSON_FIELDS.split(","))
    assert WRITABLE_FIELDS <= mask
    assert {"memberships", "biographies", "metadata"} <= mask     # still read
    assert "photos" not in mask


def test_update_fields_sends_one_call_with_a_joined_mask(monkeypatch):
    calls = []

    class FakeReq:
        def execute(self):
            return {"resourceName": "people/c1"}

    class FakePeople:
        def updateContact(self, **kw):
            calls.append(kw)
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(google_contacts, "_svc", lambda: FakeService())
    google_contacts.update_fields(
        "people/c1", "etag-1",
        {"phoneNumbers": [{"value": "+15550100001"}], "names": [{"givenName": "Alice"}]},
    )
    assert len(calls) == 1
    assert calls[0]["updatePersonFields"] == "names,phoneNumbers"      # sorted, comma-joined
    assert calls[0]["body"]["etag"] == "etag-1"
    assert calls[0]["body"]["names"] == [{"givenName": "Alice"}]
    assert calls[0]["personFields"] == google_contacts.PERSON_FIELDS


def test_update_fields_with_no_fields_does_not_call_google(monkeypatch):
    def boom():
        raise AssertionError("must not call Google")

    monkeypatch.setattr(google_contacts, "_svc", boom)
    assert google_contacts.update_fields("people/c1", "etag-1", {}) == {}
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_google_contacts.py -v
```

Expected: FAIL — no attribute `update_fields`.

- [ ] **Step 3: Implement**

```python
PERSON_FIELDS = ",".join(
    sorted(WRITABLE_FIELDS | {"memberships", "biographies", "metadata"})
)
_READ_MASK = PERSON_FIELDS


def update_fields(resource_name: str, etag: str, fields: dict) -> dict:
    """One updateContact for every field being changed (spec §5.4). Google
    rejects a stale etag, so callers pass the etag from a fresh get_person."""
    if not fields:
        return {}
    body: dict[str, Any] = {"etag": etag, **fields}
    return (
        _svc()
        .people()
        .updateContact(
            resourceName=resource_name,
            updatePersonFields=",".join(sorted(fields)),
            personFields=PERSON_FIELDS,
            body=body,
        )
        .execute()
    )
```

Delete `update_biography` and update its only caller (`services/person_edit.py`) in Task 4. If the repo has other callers, grep and update them in this task.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/test_google_contacts.py -v && grep -rn "update_biography" --include=*.py . | grep -v test_
```

Expected: tests pass; the grep shows only `services/person_edit.py`, which Task 4 changes.

- [ ] **Step 5: Commit**

```bash
git add clients/google_contacts.py tests/test_google_contacts.py
git commit -m "feat: single updateContact write and wider read mask"
```

---

## Task 4: `person_edit` — orchestrate validate, email rule, one write

**Files:**
- Modify: `services/person_edit.py`
- Modify/create: `tests/test_person_edit.py`

**Interfaces:**
- Consumes: Tasks 2 and 3.
- Produces:

```python
def update(conn, email, *, notes=None, relationship_label=None, contact: dict | None = None) -> dict
class NotFound(Exception): ...      # -> 404, unchanged
class NotLinked(Exception): ...     # -> 409, unchanged
class Conflict(Exception): ...      # NEW -> 409: stale etag or an email-rule violation
class Invalid(Exception): ...       # NEW -> 400: validation failure or a Google 4xx, message carried
```

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from googleapiclient.errors import HttpError

from services import person_edit


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def http_error(status, message):
    import json
    return HttpError(FakeResp(status),
                     json.dumps({"error": {"message": message}}).encode())


@pytest.fixture
def gc(monkeypatch):
    """Fake clients.google_contacts as person_edit sees it."""
    class GC:
        def __init__(self):
            self.updates = []
            self.live = {
                "etag": "etag-1",
                "emailAddresses": [{"value": "alice@example.com"}],
                "memberships": [],
            }
            self.raise_on_update = None

        def get_person(self, rn):
            return self.live

        def update_fields(self, rn, etag, fields):
            if self.raise_on_update:
                raise self.raise_on_update
            self.updates.append((rn, etag, fields))
            return self.live

    fake = GC()
    monkeypatch.setattr(person_edit, "gc", fake)
    monkeypatch.setattr(person_edit.gsync, "sync_one", lambda conn, row: row)
    return fake


ROW = {"email": "alice@example.com", "google_resource_name": "people/c1"}


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setattr(person_edit.people, "get", lambda conn, email: dict(ROW))


def test_contact_and_notes_go_in_one_update(gc, repo):
    person_edit.update(None, "alice@example.com", notes="hi",
                       contact={"phoneNumbers": [{"value": "+15550100001"}]})
    assert len(gc.updates) == 1
    _, etag, fields = gc.updates[0]
    assert etag == "etag-1"
    assert set(fields) == {"biographies", "phoneNumbers"}


def test_contact_absent_writes_nothing(gc, repo):
    # Review Focus 3.
    person_edit.update(None, "alice@example.com")
    assert gc.updates == []


def test_contact_empty_dict_writes_nothing(gc, repo):
    # Review Focus 3: present but empty is a no-op, not an error.
    person_edit.update(None, "alice@example.com", contact={})
    assert gc.updates == []


def test_empty_list_clears_a_field(gc, repo):
    # Review Focus 1.
    person_edit.update(None, "alice@example.com", contact={"phoneNumbers": []})
    assert gc.updates[0][2] == {"phoneNumbers": []}


def test_rejected_field_raises_invalid_without_writing(gc, repo):
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, "alice@example.com", contact={"biographies": [{"value": "x"}]})
    assert gc.updates == []


def test_email_removal_raises_conflict_without_writing(gc, repo):
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, "alice@example.com",
                           contact={"emailAddresses": [{"value": "other@example.com"}]})
    assert gc.updates == []


def test_stale_etag_raises_conflict(gc, repo):
    # Review Focus 5.
    gc.raise_on_update = http_error(400, "FAILED_PRECONDITION: etag mismatch")
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, "alice@example.com", contact={"names": [{"givenName": "A"}]})


def test_other_google_4xx_raises_invalid_carrying_the_message(gc, repo):
    gc.raise_on_update = http_error(400, "Invalid birthday")
    with pytest.raises(person_edit.Invalid, match="Invalid birthday"):
        person_edit.update(None, "alice@example.com",
                           contact={"birthdays": [{"date": {"month": 13}}]})


def test_resync_still_runs_after_a_failed_write(gc, repo, monkeypatch):
    seen = []
    monkeypatch.setattr(person_edit.gsync, "sync_one",
                        lambda conn, row: seen.append(1) or row)
    gc.raise_on_update = http_error(400, "Invalid birthday")
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, "alice@example.com", contact={"birthdays": [{}]})
    assert seen == [1]
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_person_edit.py -v
```

Expected: FAIL — `person_edit.Invalid` does not exist.

- [ ] **Step 3: Implement**

Rewrite `update` to:

1. `row = people.get(...)`; `NotFound` / `NotLinked` exactly as today.
2. `live = gc.get_person(rn)`.
3. `fields = contact_fields.validate(contact)` when `contact is not None` (catch `ValidationError` → `Invalid`); if `emailAddresses` in `fields`, `contact_fields.check_email_addition(live, fields["emailAddresses"], row["email"])` (catch `EmailRuleError` → `Conflict`).
4. Add `biographies` to `fields` when `notes is not None`.
5. In a `try/finally` whose `finally` is the existing `sync_one` refresh: `gc.update_fields(rn, live.get("etag") or "", fields)` — skipped when `fields` is empty — then `_set_label(...)` when `relationship_label is not None`.
6. Map `HttpError`: status 400/412 whose message contains `FAILED_PRECONDITION` or `etag` → `Conflict`; other 4xx → `Invalid(message)`; 5xx re-raised untouched.

Validation and the email check happen BEFORE any write, so a rejected request changes nothing.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/test_person_edit.py -v
```

- [ ] **Step 5: Commit**

```bash
git add services/person_edit.py tests/test_person_edit.py
git commit -m "feat: person_edit writes arbitrary contact fields"
```

---

## Task 5: Persist the derived fields

**Files:**
- Modify: `repo/people.py` (`_COLUMNS`, `update_from_google`, `create_from_google`)
- Modify: `services/google_contacts_sync.py` (call `contact_fields.derive`)
- Modify: `tests/test_repo_people.py`, `tests/test_google_contacts_sync.py`

**Interfaces:**
- Consumes: `contact_fields.derive` (Task 2), the columns (Task 1).
- Produces: rows returned by `repo.people` carry `phone_numbers`, `company`, `job_title`, `google_fields`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repo_people.py
def test_update_from_google_persists_contact_fields():
    conn = FakeConn()
    people.update_from_google(
        conn, "people/c1", etag="e1", display_name="Alice", notes=None,
        relationship_label=None, phone_numbers=["+15550100001"],
        company="Example Health", job_title="CTO",
        google_fields={"names": [{"givenName": "Alice"}]},
    )
    sql, params = conn.calls[0]
    assert "phone_numbers = %s::text[]" in sql and "google_fields = %s::jsonb" in sql
    assert ["+15550100001"] in params
    assert "Example Health" in params and "CTO" in params
    assert {"names": [{"givenName": "Alice"}]} in params


def test_columns_include_contact_fields():
    assert "phone_numbers" in people._COLUMNS and "google_fields" in people._COLUMNS
```

```python
# tests/test_google_contacts_sync.py
def test_sync_one_derives_contact_fields(monkeypatch):
    """The row written back carries what derive() produced from the Google payload."""
    person = {
        "resourceName": "people/c1", "etag": "e1",
        "names": [{"displayName": "Alice Example"}],
        "phoneNumbers": [{"value": "(555) 010-0001"}],
        "organizations": [{"name": "Example Health", "title": "CTO"}],
    }
    captured = {}
    monkeypatch.setattr(gsync.gc, "get_person", lambda rn: person)
    monkeypatch.setattr(gsync.people, "update_from_google",
                        lambda conn, rn, **kw: captured.update(kw))
    monkeypatch.setattr(gsync.people, "get", lambda conn, email: {"email": "alice@example.com"})
    gsync.sync_one(None, {"email": "alice@example.com", "google_resource_name": "people/c1"})
    assert captured["phone_numbers"] == ["+15550100001"]
    assert captured["company"] == "Example Health"
    assert captured["google_fields"]["organizations"][0]["title"] == "CTO"
```

Adjust the monkeypatch targets to the real names in `services/google_contacts_sync.py` — read it first; the point is that `sync_one` passes derived values through.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_repo_people.py tests/test_google_contacts_sync.py -v
```

Expected: FAIL — `update_from_google() got an unexpected keyword argument 'phone_numbers'`.

- [ ] **Step 3: Implement**

- `_COLUMNS` gains the four names so every `RETURNING` carries them.
- `update_from_google` gains four keyword-only parameters and writes them (Google owns them, so overwrite unconditionally, exactly as `notes` is handled).
- `create_from_google` gains the same and inserts them.
- `services/google_contacts_sync.py`: wherever it currently builds the arguments for `update_from_google` / `create_from_google` (the link path, `run_sync`, and `sync_one`), call `contact_fields.derive(person)` once and splat the result. One call site per path; do not derive in the repo layer.
- pg8000 and psycopg3 both bind a Python list to `text[]` and a dict to `jsonb` only with an explicit cast — follow the `_CASTS` precedent in `repo/linkedin.py` and write `%s::text[]` / `%s::jsonb` in the SQL.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add repo/people.py services/google_contacts_sync.py tests/
git commit -m "feat: persist derived contact fields on sync and link"
```

---

## Task 6: API — accept `contact`, return the new fields, search on company

**Files:**
- Modify: `api/routers/people.py` (`PersonPatch`, `PersonOut`, `to_out`, `patch_person`)
- Modify: `api/routers/search.py`
- Modify: `repo/people.py` (`search`)
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: Tasks 4 and 5.
- Produces: the API surface in spec §6.

- [ ] **Step 1: Write the failing tests**

```python
def test_patch_accepts_contact_fields(client, fake_db, patched_person_edit):
    body = client.patch("/people/alice@example.com",
                        json={"contact": {"phoneNumbers": [{"value": "+15550100001"}]}}).json()
    assert body["phone_numbers"] == ["+15550100001"]
    assert body["company"] == "Example Health"
    assert body["contact"]["phoneNumbers"][0]["value"] == "+15550100001"


def test_patch_rejected_field_is_400(client, fake_db, patched_person_edit_raising_invalid):
    r = client.patch("/people/alice@example.com", json={"contact": {"photos": []}})
    assert r.status_code == 400
    assert "photos" in r.text


def test_patch_email_removal_is_409(client, fake_db, patched_person_edit_raising_conflict):
    r = client.patch("/people/alice@example.com",
                     json={"contact": {"emailAddresses": [{"value": "other@example.com"}]}})
    assert r.status_code == 409


def test_stale_etag_is_409(client, fake_db, patched_person_edit_raising_conflict):
    # Review Focus 5 at the transport layer.
    r = client.patch("/people/alice@example.com", json={"contact": {"names": [{"givenName": "A"}]}})
    assert r.status_code == 409


def test_person_detail_carries_contact_fields(client, fake_db):
    fake_db.queue(person_row_with_contact_fields(), None, None)
    body = client.get("/people/alice@example.com").json()
    assert body["phone_numbers"] == ["+15550100001"]
    assert body["job_title"] == "CTO"
    assert body["contact"]["names"][0]["givenName"] == "Alice"


def test_list_omits_the_contact_blob_but_keeps_typed_columns(client, fake_db):
    fake_db.queue([person_row_with_contact_fields()])
    row = client.get("/people?recent=5").json()["results"][0]
    assert row["contact"] is None
    assert row["company"] == "Example Health"


def test_search_matches_on_company(client, fake_db):
    fake_db.queue([person_row_with_contact_fields()], [], [])
    body = client.post("/search", json={"q": "Example Health"}).json()
    assert body["results"][0]["company"] == "Example Health"
```

Build the fixtures in the style `tests/test_api.py` already uses; `patched_person_edit*` monkeypatch `api.routers.people.person_edit.update` to return a row or raise the matching exception.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_api.py -k contact -v
```

- [ ] **Step 3: Implement**

- `PersonPatch` gains `contact: dict | None = None`.
- `PersonOut` gains `phone_numbers: list[str] = []`, `company: str | None = None`, `job_title: str | None = None`, `contact: dict | None = None`.
- `to_out(row, ..., include_contact: bool = False)` fills the typed columns always and `contact` only when asked; detail and PATCH pass `include_contact=True`, list paths do not.
- `patch_person` passes `contact=body.contact` and maps the new exceptions: `person_edit.Invalid` → `HTTPException(400, detail=str(e))`, `person_edit.Conflict` → `HTTPException(409, detail=str(e))`. Existing `NotFound`/`NotLinked` mappings stay.
- `repo.people.search` adds `company` to the ILIKE/trigram predicate.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/mypy api/
```

- [ ] **Step 5: Commit**

```bash
git add api/ repo/people.py tests/test_api.py
git commit -m "feat: PATCH contact fields and serve them from the API"
```

---

## Task 7: Skills and docs

**Files:**
- Modify: `.claude/skills/editing-person/SKILL.md`, `fetching-person/SKILL.md`, `searching-people/SKILL.md`, `querying-people-db/SKILL.md`, `people-architecture/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `editing-person`**

A worked `curl` per common edit — phone, name, organization, birthday — using `gcloud auth print-identity-token` for auth, as the sibling skills do. State plainly: `notes` and `relationship_label` keep their own fields and must not be sent inside `contact`; email addresses can only be added; a 409 means either an email removal or a concurrent edit (retry).

- [ ] **Step 2: Update the reading skills**

`fetching-person` and `searching-people`: the four new response fields, and that `contact` is null in list responses. `querying-people-db`: the four columns with an example JSONB query (e.g. birthdays this month) and a `phone_numbers` lookup.

- [ ] **Step 3: Update `people-architecture` and `CLAUDE.md`**

Source-of-truth row from spec §4.1; `services/contact_fields.py` in the code layout; the new columns in the Stack table's Database row; a note that widening the read mask causes one full resync on the next nightly run.

- [ ] **Step 4: Verify**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/ruff check .
```

- [ ] **Step 5: Commit**

```bash
git add .claude/skills CLAUDE.md
git commit -m "docs: editable contact fields"
```

---

## Task 8: Migrate, verify live, open the PR

- [ ] **Step 1: Apply the schema to the real database**

```bash
(set -a; source .env; set +a; .venv/bin/python scripts/migrate_db.py)
```

Expected: `Migration complete`. Additive and idempotent, so it is safe against the running service.

- [ ] **Step 2: Confirm the columns landed**

```bash
(set -a; source .env; set +a; .venv/bin/python -c "
from clients.db import get_conn
with get_conn() as c:
    print([r['column_name'] for r in c.execute(\"select column_name from information_schema.columns where table_name='people' and column_name in ('phone_numbers','company','job_title','google_fields')\").fetchall()])
")
```

- [ ] **Step 3: Verify locally against the real Google account**

Run the API locally and PATCH a REAL contact you own, then read it back:

```bash
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8088) &
curl -s -X PATCH localhost:8088/people/<a-real-linked-email> \
  -H 'Content-Type: application/json' \
  -d '{"contact": {"organizations": [{"name": "Test Co", "title": "Tester"}]}}' | jq '{company, job_title}'
curl -s localhost:8088/people/<same-email> | jq '{phone_numbers, company, job_title}'
```

Confirm the same values appear in the Google Contacts UI, then PATCH the field back to its previous value (or clear it with `{"organizations": []}`) so the real contact is left as it was. Print no personal data into the PR.

- [ ] **Step 4: Verify the rejections against the live API**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH localhost:8088/people/<a-real-linked-email> \
  -H 'Content-Type: application/json' -d '{"contact": {"biographies": [{"value": "x"}]}}'   # 400
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH localhost:8088/people/<a-real-linked-email> \
  -H 'Content-Type: application/json' -d '{"contact": {"emailAddresses": []}}'              # 409
```

- [ ] **Step 5: Open the PR**

Use `/pr-open` (CLAUDE.md requires it). Cover the new columns, the generic `contact` map, the add-only email rule, the single-write change, and the expected one-off full resync on the next nightly run. Include the verification results with personal data redacted.

---

## Self-Review

**Spec coverage:** §4 columns → Task 1; §4.1 source-of-truth row → Task 7; §4.2 read mask and resync note → Tasks 3, 7; §5.1 allowlist → Task 2; §5.2 shape validation → Task 2; §5.3 email add-only → Tasks 2, 4, 6; §5.4 one write → Tasks 3, 4; §5.5 error mapping → Tasks 4, 6; §6 API → Task 6; §7 skills → Task 7; §8 observability → no work needed (existing middleware covers the unchanged route); §9 testing → spread across Tasks 1–6; §10 follow-up → explicitly out of scope; §11 rollout → Task 8.

**Type consistency:** `validate`/`check_email_addition`/`derive` signatures match their use in Task 4; `update_fields(resource_name, etag, fields)` matches Task 4's calls; `derive`'s four output keys match the `update_from_google` keyword names in Task 5 and the `PersonOut` fields in Task 6.

**Review Focus coverage:** (1) empty list clears → Tasks 2, 4. (2) `emailAddresses: []` refused → Task 2. (3) absent vs `{}` → Task 4. (4) organization with no primary → Task 2. (5) stale etag → Tasks 4, 6.

**Known gap, deliberate:** `update_biography` is removed rather than kept as a shim. It has one caller in this repo, changed in the same branch; a deprecated alias would outlive its usefulness immediately.
