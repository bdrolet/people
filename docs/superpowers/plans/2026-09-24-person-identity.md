# Person Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `people`'s email primary key with a `BIGSERIAL id`, make `email` nullable and unique, and let a person be addressed by email, E.164 phone, or id — without changing which rows exist or what any current endpoint returns.

**Architecture:** A guarded, resumable migration moves the two child tables onto `person_id` before swapping the primary key (the child foreign keys depend on the email index, so the order is forced). A new pure `services/identity.py` classifies an identifier; the repo gains lookups by id and phone; the API resolves all three kinds on one route. Nothing creates rows that do not exist today.

**Tech Stack:** Python 3.13, Postgres (pg8000 on Cloud SQL, psycopg3 locally), FastAPI, pytest, Docker (throwaway Postgres for schema tests).

**Spec:** `docs/superpowers/specs/2026-09-24-person-identity-design.md` — read it before Task 1; every task cites its sections. This is **piece 1 of 3**; pieces 2 (adopt email-less Google contacts) and 3 (`POST /people`) are separate specs and are NOT in this plan.

## Global Constraints

- **Branch:** `person-identity` (exists, spec committed). Never commit to `main`; open the PR with `/pr-open`.
- **BEHAVIOR-PRESERVING.** No `people` row that does not exist today may be created, and no endpoint may change its result for an existing person. The sync still skips email-less Google contacts — adopting them is piece 2.
- **`GET /people/{email}` is a production contract.** Inbox's classify-time lookup calls it. It must behave byte-for-byte as before; Task 6 carries an explicit regression test.
- **The migration runs against the live database** via `scripts/migrate_db.py`. Every statement must be idempotent AND resumable after a crash between steps: each step tests the state it is about to change.
- **Migration order is forced** (proved against a real Postgres during design): child FKs reference `people(email)`, so `DROP CONSTRAINT people_pkey` fails while they exist. Order: add `id` → add `person_id` + backfill → drop `person_email` → swap PK → add `person_id` FKs → add CHECK.
- **`person_id` is internal** — never a durable external reference; the email address stays the human-facing handle.
- **Phone-only people can never reach HubSpot** (`clients/hubspot.py` keys on email). `eligible_not_in_hubspot` gains `AND email IS NOT NULL` in this piece, before piece 2 creates such rows.
- **Layer rules** (CLAUDE.md): `clients/` I/O only; `repo/` DB-only taking an open connection; `services/` pure or orchestration; `api/routers/` thin transport; `models/` pure types.
- **Python 3.13:** `X | None`, never `Optional[X]`.
- **Every task ends green:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
- **Tests never** touch the production database, the real Google API, or the user's real `chat.db`. Test data uses `+1555010xxxx` and `example.com` only.
- **Commit trailers** on every commit:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01Diz1yX2VxC8bBQTB7xzT7M`

## Review Focus

Five failure modes the spec implies; each line's test is attached to the task named.

1. **A partially-applied migration.** A crash between steps must resume cleanly on the next `migrate_db.py` run, not half-migrate the database. *Test in Task 1: apply a truncated prefix of the migration, then the whole file, and assert the end state.*
2. **An ambiguous phone number** (a household landline on two people) must return 409 with candidate ids, never an arbitrary pick. *Test in Tasks 3 and 6.*
3. **A `+` in the path, encoded or not.** `%2B15035550123` and a literal `+15035550123` must both resolve; a path segment does not decode `+` as a space, but the caller cannot be expected to know that. *Test in Task 6.*
4. **An identifier that is neither** — `alice`, an empty segment, a 5-digit short code — must 404, never 500. *Test in Tasks 2 and 6.*
5. **`ON CONFLICT (email)` after email becomes nullable.** The event handlers upsert on every email; two phone-only rows both carrying `NULL` email must coexist without violating the unique constraint. *Test in Task 1.*

---

## Task 1: The migration

**Files:**
- Modify: `repo/schema.sql` (append)
- Modify: `tests/test_schema.py`

**Interfaces:**
- Produces: `people.id` (PK, BIGSERIAL), `people.email` (nullable, UNIQUE), `people_has_an_identifier` CHECK, `imessage_handles.person_id`, `linkedin_connections.person_id` (both `BIGINT REFERENCES people(id) ON DELETE SET NULL`), and the absence of both `person_email` columns. Consumed by every later task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schema.py` (its `conn` fixture applies `repo/schema.sql` to a scratch database and skips without `TEST_DATABASE_URL`).

```python
def _apply(conn, sql: str) -> None:
    conn.execute(sql)


def test_people_is_keyed_by_id_and_email_is_nullable_unique(conn):
    pk = conn.execute(
        "select a.attname from pg_index i"
        " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
        " where i.indrelid = 'people'::regclass and i.indisprimary"
    ).fetchall()
    assert [r[0] for r in pk] == ["id"]
    nullable = conn.execute(
        "select is_nullable from information_schema.columns"
        " where table_name = 'people' and column_name = 'email'"
    ).fetchone()[0]
    assert nullable == "YES"
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('a@example.com', now())")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO people (email, first_seen) VALUES ('a@example.com', now())")


def test_identifier_check_rejects_a_person_with_neither(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO people (first_seen) VALUES (now())")


def test_phone_only_people_coexist_with_null_emails(conn):
    # Review Focus 5: many NULLs are allowed under a unique constraint.
    conn.execute(
        "INSERT INTO people (first_seen, phone_numbers) VALUES (now(), %s), (now(), %s)",
        (["+15550100001"], ["+15550100002"]),
    )
    assert conn.execute("select count(*) from people where email is null").fetchone()[0] == 2


def test_on_conflict_email_still_upserts(conn):
    # Review Focus 5: the event handlers depend on this.
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('b@example.com', now())")
    conn.execute(
        "INSERT INTO people (email, first_seen, message_count) VALUES ('b@example.com', now(), 1)"
        " ON CONFLICT (email) DO UPDATE SET message_count = people.message_count + 1"
    )
    assert conn.execute(
        "select message_count from people where email = 'b@example.com'"
    ).fetchone()[0] == 1


def test_child_links_use_person_id_and_null_out_on_delete(conn):
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('c@example.com', now())")
    pid = conn.execute("select id from people where email='c@example.com'").fetchone()[0]
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_id) VALUES ('+15550100003', %s)", (pid,)
    )
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_id, snapshot_at)"
        " VALUES ('linkedin.com/in/c', 'C Example', %s, now())",
        (pid,),
    )
    conn.execute("DELETE FROM people WHERE id = %s", (pid,))
    assert conn.execute("select person_id from imessage_handles").fetchone()[0] is None
    assert conn.execute("select person_id from linkedin_connections").fetchone()[0] is None
    for table in ("imessage_handles", "linkedin_connections"):
        assert conn.execute(
            "select count(*) from information_schema.columns"
            " where table_name = %s and column_name = 'person_email'",
            (table,),
        ).fetchone()[0] == 0


def test_migration_is_idempotent(conn):
    _apply(conn, SCHEMA.read_text())          # second full application
    _apply(conn, SCHEMA.read_text())          # third, for good measure


def test_migration_resumes_after_a_partial_apply(conn):
    """Review Focus 1: a crash between steps must not wedge the database."""
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    text = SCHEMA.read_text()
    cut = text.index("-- 3. Now nothing depends on the email PK index")
    _apply(conn, text[:cut])                  # stop mid-migration
    _apply(conn, text)                        # resume with the whole file
    pk = conn.execute(
        "select a.attname from pg_index i"
        " join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)"
        " where i.indrelid = 'people'::regclass and i.indisprimary"
    ).fetchall()
    assert [r[0] for r in pk] == ["id"]
```

`test_migration_resumes_after_a_partial_apply` depends on the comment `-- 3. Now nothing depends on the email PK index` appearing verbatim in `repo/schema.sql`; keep it exactly as the spec writes it.

- [ ] **Step 2: Run to verify failure**

```bash
docker run -d --rm --name pg-t1 -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
until docker exec pg-t1 pg_isready -q; do sleep 1; done
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -v
```

Expected: FAIL — the primary key is still `email`.

- [ ] **Step 3: Append the migration to `repo/schema.sql`**

Copy the five numbered blocks verbatim from spec §4.1, keeping their `-- 1.` … `-- 5.` comments (a test depends on the step-3 comment text). The order is not negotiable and was proved against a real Postgres during design: dropping the primary key first fails with `dependent_objects_still_exist` while the child foreign keys exist.

- [ ] **Step 4: Run to verify they pass**

```bash
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -v
docker stop pg-t1
```

Expected: all pass, including the pre-existing schema tests.

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql tests/test_schema.py
git commit -m "feat: key people by a surrogate id"
```

---

## Task 2: `services/identity.py`

**Files:**
- Create: `services/identity.py`
- Create: `tests/test_identity.py`

**Interfaces:**
- Consumes: `services.eligibility.normalize`, `services.imessage_export.normalize_handle`.
- Produces:

```python
Kind = Literal["email", "phone", "id"]

def classify(ident: str) -> tuple[Kind, str | int] | None:
    """Spec §5.1. Returns the kind and the normalized value, or None when the
    value cannot identify anyone (the router turns None into a 404)."""
```

- [ ] **Step 1: Write the failing tests**

```python
from services import identity


def test_email_is_normalized():
    assert identity.classify(" Alice@Example.COM ") == ("email", "alice@example.com")


def test_e164_phone_is_recognised():
    assert identity.classify("+15550100001") == ("phone", "+15550100001")


def test_local_format_phone_is_normalised():
    assert identity.classify("(555) 010-0001") == ("phone", "+15550100001")


def test_digits_are_an_id():
    assert identity.classify("42") == ("id", 42)


def test_short_code_is_not_a_phone():
    # normalize_handle rejects anything under 7 digits, so this is an id.
    assert identity.classify("262966") == ("id", 262966)


def test_unparseable_values_return_none():
    # Review Focus 4: these must 404, never 500.
    for bad in ("alice", "", "   ", "not-an-email@", "@example.com"):
        assert identity.classify(bad) is None


def test_email_wins_over_digits_when_both_present():
    assert identity.classify("12345@example.com") == ("email", "12345@example.com")
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_identity.py -v
```

Expected: FAIL — `ModuleNotFoundError: services.identity`.

- [ ] **Step 3: Implement**

Order per spec §5.1: `@` present and the normalized value has a non-empty local and domain part → email; else `normalize_handle(ident)` returns a value → phone; else all digits → `("id", int(ident))`; else `None`. Pure module, no I/O.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/test_identity.py -v
```

- [ ] **Step 5: Commit**

```bash
git add services/identity.py tests/test_identity.py
git commit -m "feat: identifier classification"
```

---

## Task 3: `repo/people.py` — id, lookups, HubSpot guard

**Files:**
- Modify: `repo/people.py`
- Modify: `tests/test_repo_people.py`

**Interfaces:**
- Consumes: Task 1's columns.
- Produces:

```python
def get_by_id(conn, person_id: int) -> dict | None
def get_by_phone(conn, e164: str) -> list[dict]   # several rows = ambiguous (spec §5.1)
```
plus `id` in `_COLUMNS` (so every row and every `RETURNING` carries it) and the email guard on `eligible_not_in_hubspot`.

- [ ] **Step 1: Write the failing tests**

```python
def test_columns_include_id():
    assert "id" in people._COLUMNS


def test_get_by_id_selects_on_id():
    conn = FakeConn(results=[[{"id": 7, "email": "a@example.com"}]])
    assert people.get_by_id(conn, 7)["id"] == 7
    sql, params = conn.calls[0]
    assert "WHERE id = %s" in sql and params == (7,)


def test_get_by_phone_returns_every_match():
    # Review Focus 2: a shared landline resolves to more than one person.
    conn = FakeConn(results=[[{"id": 1, "email": "a@example.com"},
                              {"id": 2, "email": "b@example.com"}]])
    rows = people.get_by_phone(conn, "+15550100001")
    assert [r["id"] for r in rows] == [1, 2]
    sql, params = conn.calls[0]
    assert "phone_numbers" in sql and params == ("+15550100001",)


def test_eligible_not_in_hubspot_excludes_people_without_an_email():
    conn = FakeConn(results=[[]])
    people.eligible_not_in_hubspot(conn, 10)
    sql, _ = conn.calls[0]
    assert "email IS NOT NULL" in sql
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_repo_people.py -v
```

Expected: FAIL — no attribute `get_by_id`.

- [ ] **Step 3: Implement**

Add `id` as the first entry in `_COLUMNS`. `get_by_id` selects `WHERE id = %s`. `get_by_phone` selects `WHERE %s = ANY(phone_numbers)` ordered by the existing `_LAST_INTERACTION` descending, returning a list. Add `AND email IS NOT NULL` to `eligible_not_in_hubspot`'s WHERE clause with a comment citing spec §5.2 (HubSpot keys on email; phone-only people cannot be mirrored). Leave every other query alone — `upsert_inbound`/`upsert_outbound` keep `ON CONFLICT (email)`.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add repo/people.py tests/test_repo_people.py
git commit -m "feat: look people up by id or phone; keep phone-only people out of HubSpot"
```

---

## Task 4: iMessage links move to `person_id`

**Files:**
- Modify: `models/imessage.py`, `repo/imessage.py`, `services/imessage_export.py`
- Modify: `tests/test_repo_imessage.py`, `tests/test_imessage_export.py`

**Interfaces:**
- Consumes: Tasks 1 and 3.
- Produces: `IMessageHandle.person_id: int | None` replacing `person_email`; `repo.imessage` writes `person_id` and its read queries expose `person_email` by joining `people`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_imessage_export.py
def test_match_handles_links_email_to_person_id():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("bob@example.com")])
    ex.match_handles(b, {}, [{"id": 11, "email": "bob@example.com", "display_name": "Bob",
                              "google_resource_name": None}])
    assert b.handles[0].person_id == 11
    assert b.handles[0].match_method == "email"


def test_match_handles_links_phone_via_google_to_person_id():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {"+15550100001": [{"resource_name": "people/c1", "display_name": "Alice Example"}]}
    ex.match_handles(b, index, [{"id": 12, "email": "alice@example.com", "display_name": "Alice",
                                 "google_resource_name": "people/c1"}])
    assert b.handles[0].person_id == 12
```

```python
# tests/test_repo_imessage.py
def test_upsert_handles_writes_person_id():
    conn = FakeConn()
    imessage.upsert_handles(conn, [IMessageHandle("+15550100001", person_id=11)])
    sql, params = conn.calls[0]
    assert "person_id" in sql and "person_email" not in sql
    assert 11 in params


def test_handle_reads_expose_person_email_via_join():
    conn = FakeConn(results=[[]])
    imessage.handles(conn, limit=5)
    sql, _ = conn.calls[0]
    assert "LEFT JOIN people" in sql and "p.email AS person_email" in sql


def test_unmatched_filter_uses_person_id():
    conn = FakeConn(results=[[]])
    imessage.handles(conn, unmatched=True, limit=5)
    sql, _ = conn.calls[0]
    assert "person_id IS NULL" in sql
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_imessage_export.py tests/test_repo_imessage.py -v
```

Expected: FAIL — `IMessageHandle` has no `person_id`.

- [ ] **Step 3: Implement**

`models/imessage.py`: rename the field to `person_id: int | None = None`. `services/imessage_export.py::match_handles` takes `people_rows` that now carry `id`; set `person_id` from the matched row (both the email path and the Google path, including the duplicate-contact resolution added earlier — keep that logic, only the field it sets changes). `repo/imessage.py`: `_HANDLE_INSERT` / `_HANDLE_UPDATE` use `person_id`; `summary_for_person` takes a `person_id`; the `unmatched` filter becomes `person_id IS NULL AND google_resource_name IS NULL`; read queries `LEFT JOIN people p ON p.id = h.person_id` and select `p.email AS person_email` so the API keeps serving it.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add models/imessage.py repo/imessage.py services/imessage_export.py tests/
git commit -m "feat: iMessage handles link by person_id"
```

---

## Task 5: LinkedIn links move to `person_id`

**Files:**
- Modify: `models/linkedin.py`, `repo/linkedin.py`, `services/linkedin_export.py`
- Modify: `tests/test_repo_linkedin.py`, `tests/test_linkedin_export.py`

**Interfaces:**
- Consumes: Tasks 1 and 3.
- Produces: `LinkedInConnection.person_id: int | None` replacing `person_email`; `repo.linkedin` writes `person_id` and exposes `person_email` by joining `people`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_linkedin_export.py
def test_match_people_links_by_email_to_person_id():
    snap = snapshot(connections=[connection(email="alice@example.com")])
    linkedin_export.match_people(snap, [{"id": 21, "email": "alice@example.com",
                                         "display_name": "Alice Example"}])
    assert snap.connections[0].person_id == 21
    assert snap.connections[0].match_method == "email"


def test_match_people_links_by_unique_name_to_person_id():
    snap = snapshot(connections=[connection(email=None)])
    linkedin_export.match_people(snap, [{"id": 22, "email": "alice@example.com",
                                         "display_name": "Alice Example"}])
    assert snap.connections[0].person_id == 22
    assert snap.connections[0].match_method == "name"
```

```python
# tests/test_repo_linkedin.py
def test_replace_snapshot_writes_person_id():
    conn = FakeConn()
    linkedin.replace_snapshot(conn, snapshot(connections=[connection(person_id=21)]))
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "person_id" in joined and "person_email" not in joined


def test_connection_for_person_takes_a_person_id():
    conn = FakeConn(results=[[]])
    linkedin.connection_for_person(conn, 21)
    sql, params = conn.calls[0]
    assert "person_id = %s" in sql and params[0] == 21
```

`match_people`'s existing signature takes the rows returned by `repo.people.names_for_matching`; that helper must now also select `id` — update it in this task and assert the new column in `tests/test_repo_people.py`.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_linkedin_export.py tests/test_repo_linkedin.py -v
```

Expected: FAIL — `LinkedInConnection` has no `person_id`.

- [ ] **Step 3: Implement**

Mirror Task 4: rename the model field, set it from the matched row's `id` in both the email and unique-name paths, write `person_id` in `replace_snapshot`, take a `person_id` in `connection_for_person`, make the `unmatched` filter `person_id IS NULL`, and `LEFT JOIN people` in the read queries to keep serving `person_email`. Add `id` to `repo.people.names_for_matching`'s SELECT.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add models/linkedin.py repo/linkedin.py services/linkedin_export.py repo/people.py tests/
git commit -m "feat: LinkedIn connections link by person_id"
```

---

## Task 6: API — resolve any identifier

**Files:**
- Modify: `api/routers/people.py`, `api/routers/imessage.py`, `api/routers/linkedin.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: `GET|PATCH /people/{ident}` and `POST /people/{ident}/sync` resolving email, phone, or id; `PersonOut.id: int`; `PersonOut.email: str | None`; `person_id` on the iMessage and LinkedIn payloads.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_by_email_is_unchanged(client, fake_db):
    """The production contract: inbox calls this path."""
    fake_db.queue(person_row(), None, None)
    body = client.get("/people/alice@example.com").json()
    assert body["email"] == "alice@example.com"
    assert body["display_name"] == person_row()["display_name"]


def test_get_by_id(client, fake_db):
    fake_db.queue(person_row(id=7), None, None)
    assert client.get("/people/7").json()["id"] == 7


def test_get_by_encoded_phone(client, fake_db):
    fake_db.queue([person_row(id=8)], None, None)
    assert client.get("/people/%2B15550100001").json()["id"] == 8


def test_get_by_unencoded_phone(client, fake_db):
    # Review Focus 3: a literal + in a path segment is not a space.
    fake_db.queue([person_row(id=8)], None, None)
    assert client.get("/people/+15550100001").json()["id"] == 8


def test_ambiguous_phone_is_409_with_candidates(client, fake_db):
    # Review Focus 2.
    fake_db.queue([person_row(id=1), person_row(id=2)])
    r = client.get("/people/%2B15550100001")
    assert r.status_code == 409
    assert r.json()["detail"]["candidates"] == [1, 2]


def test_unresolvable_identifier_is_404(client, fake_db):
    # Review Focus 4: never a 500.
    assert client.get("/people/alice").status_code == 404


def test_person_out_carries_id_and_nullable_email(client, fake_db):
    fake_db.queue(person_row(id=9, email=None, phone_numbers=["+15550100001"]), None, None)
    body = client.get("/people/9").json()
    assert body["id"] == 9 and body["email"] is None


def test_imessage_handle_carries_person_id(client, fake_db):
    fake_db.queue([handle_row(person_id=11, person_email="alice@example.com")])
    row = client.get("/imessage/handles?limit=1").json()["results"][0]
    assert row["person_id"] == 11 and row["person_email"] == "alice@example.com"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_api.py -k "ident or person_id or ambiguous" -v
```

- [ ] **Step 3: Implement**

Add a helper in `api/routers/people.py` that calls `identity.classify`, then dispatches: email → `people_repo.get`; id → `get_by_id`; phone → `get_by_phone`, raising `HTTPException(409, detail={"error": "ambiguous phone", "candidates": [r["id"] for r in rows]})` when more than one row comes back, and 404 when none. `classify` returning `None` → 404. Use it in the GET, PATCH and sync routes. `PersonOut` gains `id: int` and its `email` becomes `str | None`. `IMessageHandleOut` and `LinkedInConnectionOut` gain `person_id: int | None` and keep `person_email: str | None`.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/mypy api/
```

- [ ] **Step 5: Commit**

```bash
git add api/ tests/test_api.py
git commit -m "feat: address a person by email, phone, or id"
```

---

## Task 7: Docs and skills

**Files:**
- Modify: `.claude/skills/fetching-person/SKILL.md`, `editing-person/SKILL.md`, `searching-people/SKILL.md`, `querying-people-db/SKILL.md`, `people-architecture/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the person skills**

`fetching-person` and `editing-person`: one route takes an email, an E.164 phone (percent-encode the `+`), or a numeric id; responses carry `id`; `email` may be null for a phone-only person; an ambiguous phone returns 409 with candidate ids, and the caller retries with one.

- [ ] **Step 2: Update the data skills**

`querying-people-db`: `people.id` is the key, `email` is nullable and unique, `person_id` replaces `person_email` on `imessage_handles` and `linkedin_connections` (with a join example), and the `people_has_an_identifier` constraint. `searching-people`: results carry `id`.

- [ ] **Step 3: Update `people-architecture` and `CLAUDE.md`**

The identity model and its source-of-truth row; `services/identity.py` in the code layout; that person ids are internal and not durable external references; that phone-only people are never mirrored to HubSpot.

- [ ] **Step 4: Verify**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/ruff check .
```

- [ ] **Step 5: Commit**

```bash
git add .claude/skills CLAUDE.md
git commit -m "docs: person identity"
```

---

## Task 8: Migrate, verify live, open the PR

- [ ] **Step 1: Apply the migration to the real database**

```bash
(set -a; source .env; set +a; .venv/bin/python scripts/migrate_db.py)
```

Expected: `Migration complete`.

- [ ] **Step 2: Confirm the end state and that no link was lost**

```bash
(set -a; source .env; set +a; .venv/bin/python -c "
from clients.db import get_conn
with get_conn() as c:
    print('pk:', [r['attname'] for r in c.execute(\"select a.attname from pg_index i join pg_attribute a on a.attrelid=i.indrelid and a.attnum=any(i.indkey) where i.indrelid='people'::regclass and i.indisprimary\").fetchall()])
    print('handles linked:', c.execute('select count(*) n from imessage_handles where person_id is not null').fetchone()['n'], '(expected 75)')
    print('connections linked:', c.execute('select count(*) n from linkedin_connections where person_id is not null').fetchone()['n'], '(expected 5)')
    print('people:', c.execute('select count(*) n from people').fetchone()['n'], '(expected 511)')
")
```

The link counts must match exactly. A lower number means the backfill lost rows — stop and investigate before merging.

- [ ] **Step 3: Verify the production path locally**

```bash
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8090) &
curl -s localhost:8090/people/<a-real-linked-email> | jq '{id, email, display_name}'
curl -s "localhost:8090/people/$(python3 -c "import urllib.parse;print(urllib.parse.quote('+1XXXXXXXXXX', safe=''))")" | jq '{id, email}'
curl -s -o /dev/null -w '%{http_code}\n' localhost:8090/people/alice     # 404
```

Use a phone from a linked handle (`select handle from imessage_handles where person_id is not null limit 1`). Redact real values from the PR.

- [ ] **Step 4: Re-run both importers**

```bash
(set -a; source .env; set +a; .venv/bin/python scripts/import_imessage.py)
(set -a; source .env; set +a; .venv/bin/python scripts/import_linkedin.py <latest-export-path>)
```

Then re-check the link counts from Step 2: they must be at least as high as before. This proves the importers write the new column.

- [ ] **Step 5: Open the PR**

Use `/pr-open`. Cover the key change, the forced migration order, the behavior-preserving guarantee, the nullable `PersonOut.email`, and the HubSpot guard that prepares piece 2. Include the verification counts.

---

## Self-Review

**Spec coverage:** §4.1 migration → Task 1; §4.2 constraint → Task 1; §4.3 ids are internal → Task 7 (docs); §5.1 resolution → Tasks 2, 3, 6; §5.2 HubSpot guard → Task 3; §5.3 API → Task 6; §6 testing → spread across Tasks 1–6; §7 rollout → Task 8.

**Type consistency:** `classify` returns `tuple[Kind, str | int] | None` in Task 2 and is consumed that way in Task 6. `get_by_phone` returns a **list** in Tasks 3 and 6 (the ambiguity case depends on it). `person_id: int | None` is the field name in Tasks 4, 5 and 6, and the column name in Task 1.

**Review Focus coverage:** (1) partial migration → Task 1. (2) ambiguous phone → Tasks 3, 6. (3) encoded and raw `+` → Task 6. (4) unresolvable identifier → Tasks 2, 6. (5) `ON CONFLICT (email)` with nullable email → Task 1.

**Known gap, deliberate:** `repo.people.get` keeps taking an email rather than becoming id-first. Every caller today has an email in hand, and changing them all would enlarge a migration that is already touching the primary key. Piece 3 can revisit it when creation needs an id-first path.
