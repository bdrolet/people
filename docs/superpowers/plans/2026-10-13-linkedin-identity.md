# LinkedIn Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a LinkedIn profile URL a third person identifier, so the outreach seed can turn every warm LinkedIn-only connection into a Google Contact and a `people` row, and enter them in the referral pipeline.

**Architecture:** `people.linkedin_url` (normal form `linkedin.com/in/<slug>`, UNIQUE) joins email and phone in `people_has_an_identifier`. Google Contacts stays the source of truth: `contact_fields.derive()` reads the URL from a contact's `urls`, and every `apply_person` write path drops a URL another row already holds. `identity.classify`, the API's `{ident:path}` routes and the CLI resolve a person by URL. The pure seed planner sorts each unlinked warm LinkedIn row into `link`, `in_google`, `possible_duplicate` or `create`, using `people` rows plus one full Google listing (`list_url_index`). The seed script then links or creates each person through `person_create.create`, committing one person at a time.

**Tech Stack:** Python 3.13, Postgres (pg8000 on Cloud SQL / psycopg3 locally), Google People API v1 (googleapiclient), FastAPI/Starlette, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-13-linkedin-identity-design.md`. Read all of it (§1–§9) before starting. It builds on `2026-09-24-person-identity-design.md` (identity, `people_has_an_identifier`), `2026-09-28-adopt-emailless-contacts-design.md` (adoption), `2026-09-28-post-people-design.md` (`POST /people`, promotion) and `2026-10-13-outreach-working-queue-design.md` (the seed and queue).

## Global Constraints

- **Repo is public.** No real names, profile URLs, ids, counts per person or message text in code, tests, fixtures, commit messages or logs. Fixtures use `example.com` addresses, `+1555010…` numbers and slugs like `linkedin.com/in/jane-example`. Seed output that names people prints to Ben's terminal only, never to logs or metrics.
- **Normal form.** A stored or compared LinkedIn URL is always `services/linkedin_export.py::normalize_profile_url`'s output: `linkedin.com/in/<slug>`, lowercase, no scheme, no subdomain, no query, fragment or trailing slash. Anything else is `None` and identifies no one. Google contacts are written with `https://www.linkedin.com/in/<slug>`.
- **Identifier rule (spec D2).** Every `people` row has an email, at least one E.164 phone or a `linkedin_url`. No code path may write a row that breaks this, or write a `linkedin_url` another row holds (it aborts the whole nightly sync).
- **Google first (CLAUDE.md source-of-truth table).** A LinkedIn-only person is created in Google Contacts first, and the row is derived by `apply_person`. Nothing writes `people.linkedin_url` except the three Google-derived writers in `repo/people.py`.
- **No HubSpot (spec D5).** LinkedIn-only people have no email, so `eligible_not_in_hubspot` already excludes them. Do not change it.
- **Layer rules (CLAUDE.md).** `clients/` I/O only; `repo/` takes an open connection, never commits, decides nothing; `services/` business logic (`services/outreach_seed.py` stays pure, with no I/O); scripts and routers orchestrate. The one sanctioned `clients/` → `services/` import is the normalizer, function-local, as `list_phone_index` already does.
- **`repo/schema.sql` holds no `%` character** (pg8000 paramstyle). Use `position(...)`, never `LIKE '%…%'`. Every change is idempotent (`IF NOT EXISTS`, guarded `DO $$`).
- **pg8000 gotcha.** `clients/db.py::_adapt_params` rewrites an all-numeric Python list parameter into a string. Never pass a list of ints as a query parameter.
- **Metric names** keep the `people.` prefix. The only metric change is a new `kind` attribute value, `linkedin_conflict`, on the existing `people.google_sync_changes`.
- **Branch.** Implementation goes on `linkedin-identity` (it already holds the spec and this plan, pushed to origin), so spec, plan and code ship in one PR. Open the PR with the `/pr-open` skill (Task 12), never by hand.
- **Commit trailer.** End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01K9k7y6huvLXkCKWk4DTcwC
  ```
- **Format first.** Code blocks here are not pre-formatted. Run `.venv/bin/ruff check --fix . && .venv/bin/ruff format .` before the checks.
- **Checks before every commit:**
  ```bash
  .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
  ```
- **Real-Postgres tests.** `tests/test_schema.py` and `tests/test_outreach_db.py` are skipped unless `TEST_DATABASE_URL` is set. CI sets it (`.github/workflows/ci.yml`). Locally, use a throwaway container before every commit that touches SQL:
  ```bash
  docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
  export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
  ```
  If Docker is unavailable, say so in the task report and rely on CI; do not claim the SQL was verified locally.
- **Production schema first.** `scripts/migrate_db.py` runs against production before the PR merges (Task 13). It is Ben's step, never an agent's.

## Decisions this plan makes where the spec is silent or loose

Each is called out so a reviewer can reject it on its own.

1. **UNIQUE is an index, not a column constraint.** `CREATE UNIQUE INDEX IF NOT EXISTS people_linkedin_url_key ON people (linkedin_url)` behaves exactly like `TEXT UNIQUE` (many NULLs allowed) and is idempotent without a `DO $$` block.
2. **The normalizer accepts a country subdomain and trims sub-paths.** `https://uk.linkedin.com/in/jane-example/details/experience/` normalizes to `linkedin.com/in/jane-example`. URLs pasted into Google Contacts take those shapes; the export never does (measured 2026-10-13: every one of 1,885 connection URLs and every message sender URL is already `linkedin.com/in/<slug>`), so no stored key changes.
3. **`contact_fields.profile_urls()` sits beside `identifiers()`** instead of making `identifiers()` a 3-tuple. Spec §5 step 3 says `identifiers` "returns it". A sibling function keeps the 2-tuple contract its tests and caller rely on, and it reads the same way at the one call site.
4. **The three writers take `linkedin_url: str | None = None`.** `apply_person` always passes it (through `**derived`), so the default only spares tests and one-off scripts that build rows by hand. `update_from_google` writes it unconditionally, including NULL, like every other Google-owned column.
5. **One helper, `_claim_url`, guards every write path.** It runs right after `derive()` and replaces a URL owned by a *different* row with `None`. `_link` now takes the row, not the email, so it knows the row's own id.
6. **`linkedin_conflict` counts only contacts the conflict left unadoptable**, as spec §4 says. A contact that keeps an email or phone is written without the URL and counted as usual (`created`, `updated`, `linked`). The dropped URL shows up in `querying-people-db`'s duplicate checks, not in a counter.
7. **`check_identifier_kept(email, live, fields)` reads unsubmitted fields from the live Google contact.** It replaces `check_phone_removal`. Removing every phone is fine while a URL remains, and removing the URL is fine while a phone remains.
8. **`PersonOut` gains `linkedin_url`.** `fetching-person` needs to show the new identifier, and `GET /people/{url}` should echo what it matched on.
9. **Retries: reads retry 429 and 5xx; creates retry only 429.** `clients/google_contacts.py::_execute` passes `num_retries=5` to googleapiclient for idempotent calls. `createContact`, `contactGroups.create` and `deleteContact` retry only HTTP 429, with 1, 2, 4, 8 and 16 second backoff: Google refuses a 429 before doing anything, but a 5xx may have created the contact already.
10. **`list_url_index()` returns `(by_url, contacts)`.** `by_url` maps a normalized URL to `[{"resource_name", "display_name"}]`, like `list_phone_index`. `contacts` lists every contact, for the name check. `person_create.create` calls it once per create that carries a URL: one listing, about one request per 1,000 contacts.
11. **`matched_by_url` is printed by `import_linkedin`, not persisted.** Adding it to `linkedin_imports` would be a second schema change for a number nobody queries.
12. **The possible-duplicate rule skips single-word names**, and its Google side ignores contacts that already have a `people` row (the `people` side has already checked those).
13. **The seed exits 1 when any create failed**, so a partial run is visible in the shell. A `link` is processed even when the person is already in the pipeline: the connection still needs its `person_id`, and `ensure_contact` is a no-op for them.
14. **The `today` column width is a constant in `services/outreach_today.py`**, `len(" · ".join(("email", "imessage", "linkedin", "whatsapp")))`. That keeps the pure renderer free of `services/outreach.py`'s repo imports.

## Review Focus

1. **A LinkedIn-only row is re-synced every night.** Its own URL is "claimed", by itself. Expected: it keeps the URL and updates normally, run after run. Pinned in Task 4 (`test_a_linked_row_keeps_its_own_url`).
2. **Two Google contacts carry the same profile URL during the post-merge full sync.** Expected: the sync finishes; the second contact is written without the URL or, with nothing else, counted `linkedin_conflict`. Pinned in Task 4 (`test_run_sync_counts_linkedin_conflict`, `test_a_claimed_url_is_dropped_when_linking_by_email`).
3. **The seed's Google create succeeds but its DB commit fails, and Ben re-runs it.** Expected: no second Google contact; the orphan is listed as `in Google, not synced`. Pinned in Task 9 (`test_an_orphaned_google_contact_is_reported_not_recreated`).
4. **Ben pastes a browser URL (`https://www.linkedin.com/in/Jane-Example/?trk=x`) into `outreach mark` or the API.** Expected: it resolves to the person. Pinned in Task 5 (`test_profile_urls_are_linkedin`, `test_get_by_encoded_linkedin_url`).
5. **Ben PATCHes `urls` on a LinkedIn-only person with only their website.** Expected: 409, nothing written to Google. Pinned in Task 3 (`test_removing_the_last_linkedin_url_is_a_conflict`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repo/schema.sql` | Modify | `people.linkedin_url`, its UNIQUE index, the widened `people_has_an_identifier`. |
| `repo/people.py` | Modify | `linkedin_url` in `_COLUMNS` and the three Google writers; `get_by_linkedin_url`, `linkedin_owner`; `names_for_matching` returns it. |
| `services/linkedin_export.py`, `models/linkedin.py`, `scripts/import_linkedin.py` | Modify | Strict normalizer; `match_people` URL step; `matched_by_url`. |
| `services/contact_fields.py`, `services/person_edit.py` | Modify | `derive()` → `linkedin_url`; `profile_urls`; `check_identifier_kept`. |
| `services/google_contacts_sync.py` | Modify | `_claim_url` on every write path; rewritten last-identifier guard; `linkedin_conflict`. |
| `services/identity.py`, `services/person_lookup.py`, `api/routers/people.py` | Modify | `linkedin` kind; lookup by URL; `{ident:path}`; `PersonOut.linkedin_url`. |
| `clients/google_contacts.py` | Modify | `_execute` with retries; `list_url_index`. |
| `services/person_create.py` | Modify | URL as an identifier; duplicate refusal on both sides. |
| `services/outreach_seed.py`, `repo/outreach.py`, `repo/linkedin.py` | Modify | Four LinkedIn outcomes, `name_key`, `contact_for`; `people_seed_rows`, first/last names; `link_connection`. |
| `scripts/outreach_seed.py` | Modify | Link and create, commit per person, four-count summary. |
| `services/outreach_today.py` | Modify | Channel column width. |
| `CLAUDE.md`, `.claude/skills/*/SKILL.md`, the spec | Modify | Document the third identifier (Task 11). |
| `tests/…` | Modify | As each task says. |

---

### Task 1: Schema and the people repo

**Files:**
- Modify: `repo/schema.sql` (append at the very end)
- Modify: `repo/people.py`
- Test: `tests/test_schema.py`, `tests/test_repo_people.py`, `tests/test_repo_linkedin.py`

**Interfaces:**
- Produces: column `people.linkedin_url TEXT`, index `people_linkedin_url_key`; `repo.people.get_by_linkedin_url(conn, url: str) -> dict | None`; `repo.people.linkedin_owner(conn, url: str) -> int | None`; `set_google`, `update_from_google`, `create_from_google` each take `linkedin_url: str | None = None`; `_COLUMNS` and `names_for_matching` rows carry `linkedin_url`.

- [ ] **Step 1: Write the failing real-Postgres tests**

Append to `tests/test_schema.py`:

```python
# --- LinkedIn profile URL as an identifier (2026-10-13-linkedin-identity-design.md §3) ---

JANE = "linkedin.com/in/jane-example"


def test_a_linkedin_only_person_satisfies_the_identifier_check(conn):
    conn.execute("INSERT INTO people (first_seen, linkedin_url) VALUES (now(), %s)", (JANE,))


def test_identifier_check_still_rejects_none_of_the_three(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO people (first_seen, linkedin_url) VALUES (now(), NULL)")


def test_linkedin_url_is_unique_and_nulls_coexist(conn):
    conn.execute("INSERT INTO people (first_seen, linkedin_url) VALUES (now(), %s)", (JANE,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO people (first_seen, linkedin_url) VALUES (now(), %s)", (JANE,))


def test_many_people_have_no_linkedin_url(conn):
    _person(conn, "a@example.com")
    _person(conn, "b@example.com")
    assert conn.execute("select count(*) from people where linkedin_url is null").fetchone()[0] == 2


def test_identifier_check_upgrade_runs_on_an_older_database(conn):
    # A production database has the two-identifier CHECK from the person-identity migration.
    conn.execute("ALTER TABLE people DROP CONSTRAINT people_has_an_identifier")
    conn.execute(
        "ALTER TABLE people ADD CONSTRAINT people_has_an_identifier"
        " CHECK (email IS NOT NULL OR cardinality(phone_numbers) > 0)"
    )
    _apply(conn, SCHEMA.read_text())
    _apply(conn, SCHEMA.read_text())
    conn.execute("INSERT INTO people (first_seen, linkedin_url) VALUES (now(), %s)", (JANE,))
    n = conn.execute(
        "select count(*) from pg_constraint"
        " where conrelid = 'people'::regclass and conname = 'people_has_an_identifier'"
    ).fetchone()[0]
    assert n == 1


def test_migration_resumes_before_the_linkedin_block(conn):
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    text = SCHEMA.read_text()
    cut = text.index("-- LinkedIn profile URL as a third identifier")
    _apply(conn, text[:cut])
    _apply(conn, text)
    conn.execute("INSERT INTO people (first_seen, linkedin_url) VALUES (now(), %s)", (JANE,))


def test_google_writers_round_trip_linkedin_url(conn):
    row = people_repo.create_from_google(
        conn,
        None,
        display_name=None,
        resource_name="people/c1",
        etag=None,
        notes=None,
        phone_numbers=[],
        company=None,
        job_title=None,
        google_fields=json.dumps({}),
        linkedin_url=JANE,
    )
    assert row["linkedin_url"] == JANE
    people_repo.update_from_google(
        conn,
        "people/c1",
        etag="e2",
        display_name=None,
        notes=None,
        phone_numbers=["+15550100001"],
        company=None,
        job_title=None,
        google_fields=json.dumps({}),
        linkedin_url="linkedin.com/in/jane-other",
    )
    assert people_repo.get_by_linkedin_url(conn, "linkedin.com/in/jane-other")["id"] == row["id"]
    assert people_repo.linkedin_owner(conn, "linkedin.com/in/jane-other") == row["id"]
    assert people_repo.linkedin_owner(conn, JANE) is None

    _person(conn, "b@example.com")
    people_repo.set_google(
        conn,
        "b@example.com",
        resource_name="people/c2",
        etag=None,
        phone_numbers=[],
        company=None,
        job_title=None,
        google_fields=json.dumps({}),
        linkedin_url="linkedin.com/in/bob-example",
    )
    assert people_repo.get(conn, "b@example.com")["linkedin_url"] == "linkedin.com/in/bob-example"
```

- [ ] **Step 2: Write the failing repo unit tests**

Append to `tests/test_repo_people.py`:

```python
def test_columns_include_linkedin_url():
    assert "linkedin_url" in people._COLUMNS


def test_get_by_linkedin_url_selects_on_the_column():
    conn = FakeConn(results=[[{"id": 4}]])
    assert people.get_by_linkedin_url(conn, "linkedin.com/in/jane-example")["id"] == 4
    sql, params = conn.calls[0]
    assert "WHERE linkedin_url = %s" in sql and params == ("linkedin.com/in/jane-example",)


def test_linkedin_owner_selects_on_the_column():
    conn = FakeConn(results=[[{"id": 9}]])
    assert people.linkedin_owner(conn, "linkedin.com/in/jane-example") == 9
    sql, params = conn.calls[0]
    assert sql == "SELECT id FROM people WHERE linkedin_url = %s"
    assert params == ("linkedin.com/in/jane-example",)


def test_linkedin_owner_returns_none_when_unclaimed():
    assert people.linkedin_owner(FakeConn(results=[[]]), "linkedin.com/in/x") is None


def test_update_from_google_writes_linkedin_url_even_when_null():
    conn = FakeConn()
    people.update_from_google(
        conn,
        "people/c1",
        etag="e1",
        display_name=None,
        notes=None,
        phone_numbers=["+15550100001"],
        company=None,
        job_title=None,
        google_fields={},
    )
    sql, params = conn.calls[0]
    assert "linkedin_url = %s" in sql
    assert params[-1] == "people/c1" and params[-2] is None


def test_create_from_google_inserts_and_upserts_linkedin_url():
    conn = FakeConn(results=[[{"id": 5, "email": None}]])
    people.create_from_google(
        conn,
        None,
        display_name=None,
        resource_name="people/c1",
        etag=None,
        notes=None,
        phone_numbers=[],
        company=None,
        job_title=None,
        google_fields={},
        linkedin_url="linkedin.com/in/jane-example",
    )
    sql, params = conn.calls[0]
    assert "linkedin_url = EXCLUDED.linkedin_url" in sql
    assert "linkedin.com/in/jane-example" in params


def test_set_google_writes_linkedin_url():
    conn = FakeConn()
    people.set_google(
        conn,
        "a@example.com",
        resource_name="people/c1",
        etag=None,
        phone_numbers=[],
        company=None,
        job_title=None,
        google_fields={},
        linkedin_url="linkedin.com/in/jane-example",
    )
    sql, params = conn.calls[0]
    assert "linkedin_url = %s" in sql and "linkedin.com/in/jane-example" in params
```

In `tests/test_repo_linkedin.py`, replace `test_names_for_matching` with:

```python
def test_names_for_matching():
    found = [{"id": 1, "email": "a@b.c", "display_name": "A", "linkedin_url": None}]
    conn = FakeConn(results=[found])
    assert people.names_for_matching(conn) == found
    assert conn.calls[0] == ("SELECT id, email, display_name, linkedin_url FROM people", None)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_schema.py tests/test_repo_people.py tests/test_repo_linkedin.py -q` (with `TEST_DATABASE_URL` set)
Expected: FAIL. You should see `column "linkedin_url" does not exist`, `AttributeError: module 'repo.people' has no attribute 'linkedin_owner'`, and the `names_for_matching` SQL mismatch.

- [ ] **Step 4: Change the schema**

Append at the very end of `repo/schema.sql`:

```sql

-- LinkedIn profile URL as a third identifier
-- (docs/superpowers/specs/2026-10-13-linkedin-identity-design.md §3). Normal form
-- linkedin.com/in/<slug>, derived from the Google contact's urls; NULL for most rows.
-- UNIQUE so two rows never claim one profile (many NULLs are fine).
ALTER TABLE people ADD COLUMN IF NOT EXISTS linkedin_url TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS people_linkedin_url_key ON people (linkedin_url);

-- people_has_an_identifier gains the URL. Replaced once: a database whose CHECK already
-- names linkedin_url is left alone, so the file stays safe to re-run. position(), not LIKE
-- with wildcards: this file holds no percent sign, so no driver's paramstyle can misread it.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'people'::regclass
          AND conname = 'people_has_an_identifier'
          AND position('linkedin_url' in pg_get_constraintdef(oid)) > 0
    ) THEN
        ALTER TABLE people DROP CONSTRAINT IF EXISTS people_has_an_identifier;
        ALTER TABLE people ADD CONSTRAINT people_has_an_identifier
            CHECK (email IS NOT NULL OR cardinality(phone_numbers) > 0 OR linkedin_url IS NOT NULL);
    END IF;
END $$;
```

- [ ] **Step 5: Change `repo/people.py`**

In `_COLUMNS`, add `linkedin_url` after `google_fields`:

```python
    hubspot_synced_at, phone_numbers, company, job_title, google_fields, linkedin_url, updated_at,
```

After `get_by_phone`, add:

```python
def get_by_linkedin_url(conn: Any, url: str) -> dict | None:
    """`url` is already normalized (services/linkedin_export.py::normalize_profile_url);
    the UNIQUE index means at most one row."""
    return conn.execute(f"SELECT {_COLUMNS} FROM people WHERE linkedin_url = %s", (url,)).fetchone()
```

After `email_owner`, add:

```python
def linkedin_owner(conn: Any, url: str) -> int | None:
    """The id of the person holding this profile URL, or None. Guards every
    apply_person write: a claimed URL violates people_linkedin_url_key and
    aborts the sync (linkedin-identity design §4)."""
    row = conn.execute("SELECT id FROM people WHERE linkedin_url = %s", (url,)).fetchone()
    return row["id"] if row else None
```

In `set_google`, add the keyword argument and column:

```python
def set_google(
    conn: Any,
    email: str,
    *,
    resource_name: str,
    etag: str | None,
    display_name: str | None = None,
    notes: str | None = None,
    phone_numbers: list[str],
    company: str | None,
    job_title: str | None,
    google_fields: dict,
    linkedin_url: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE people SET
            google_resource_name = %s,
            google_etag          = %s,
            display_name         = COALESCE(%s, display_name),
            notes                = COALESCE(%s, notes),
            phone_numbers        = %s::text[],
            company              = %s,
            job_title            = %s,
            google_fields        = %s::jsonb,
            linkedin_url         = %s,
            updated_at           = now()
        WHERE email = %s
        """,
        (
            resource_name,
            etag,
            display_name,
            notes,
            phone_numbers,
            company,
            job_title,
            google_fields,
            linkedin_url,
            _norm(email),
        ),
    )
```

In `update_from_google`, add `linkedin_url: str | None = None` after `google_fields: dict,` in the signature. Add `"linkedin_url = %s",` to `set_clauses` right after `"google_fields = %s::jsonb",`, and `linkedin_url,` to `params` right after `google_fields,`. Do not touch the `email` handling.

In `create_from_google`, add `linkedin_url: str | None = None` after `google_fields: dict,` and replace the statement with:

```python
    return conn.execute(
        f"""
        INSERT INTO people (email, display_name, first_seen, eligible, automated,
                            google_resource_name, google_etag, notes,
                            phone_numbers, company, job_title, google_fields, linkedin_url)
        VALUES (%s, %s, now(), TRUE, FALSE, %s, %s, %s, %s::text[], %s, %s, %s::jsonb, %s)
        ON CONFLICT (email) DO UPDATE SET
            google_resource_name = EXCLUDED.google_resource_name,
            google_etag = EXCLUDED.google_etag,
            display_name = COALESCE(EXCLUDED.display_name, people.display_name),
            notes = EXCLUDED.notes,
            phone_numbers = EXCLUDED.phone_numbers, company = EXCLUDED.company,
            job_title = EXCLUDED.job_title, google_fields = EXCLUDED.google_fields,
            linkedin_url = EXCLUDED.linkedin_url,
            eligible = TRUE, updated_at = now()
        RETURNING {_COLUMNS}
        """,
        (
            _norm(email),
            display_name,
            resource_name,
            etag,
            notes,
            phone_numbers,
            company,
            job_title,
            google_fields,
            linkedin_url,
        ),
    ).fetchone()
```

Replace `names_for_matching`:

```python
def names_for_matching(conn: Any) -> list[dict]:
    """Every row's id, email, display_name and LinkedIn URL, for scripts/import_linkedin.py's
    soft link (URL first, then email, then name)."""
    return conn.execute("SELECT id, email, display_name, linkedin_url FROM people").fetchall()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schema.py tests/test_repo_people.py tests/test_repo_linkedin.py -q` (with `TEST_DATABASE_URL` set)
Expected: PASS, including the existing `test_migration_is_idempotent` and `test_migration_resumes_after_a_partial_apply`.

- [ ] **Step 7: Commit**

```bash
git add repo/schema.sql repo/people.py tests/test_schema.py tests/test_repo_people.py tests/test_repo_linkedin.py
git commit -m "people: linkedin_url column, third identifier, owner lookup"
```

---

### Task 2: Strict normalizer and the URL match step

**Files:**
- Modify: `services/linkedin_export.py:39-55` (`_URL_PREFIX`, `normalize_profile_url`), `:328-351` (`match_people`)
- Modify: `models/linkedin.py` (`LinkedInConnection.match_method` comment, `LinkedInSnapshot.matched_by_url`)
- Modify: `scripts/import_linkedin.py` (`summary`)
- Test: `tests/test_linkedin_export.py`, `tests/test_import_linkedin.py`

**Interfaces:**
- Consumes: `names_for_matching` rows with `linkedin_url` (Task 1).
- Produces: `normalize_profile_url(url) -> str | None` that returns `None` for anything but a member profile; `match_method == "url"`; `LinkedInSnapshot.matched_by_url: int`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_linkedin_export.py`, add these cases to the `test_normalize_profile_url` parametrize list (keep the existing ones):

```python
        ("https://uk.linkedin.com/in/jane-example", "linkedin.com/in/jane-example"),
        (
            "https://www.linkedin.com/in/jane-example/details/experience/",
            "linkedin.com/in/jane-example",
        ),
        ("https://www.linkedin.com/company/acme/", None),
        ("linkedin.com", None),
        ("https://www.linkedin.com/in/", None),
        ("https://example.com/in/jane-example", None),
        ("jane@example.com", None),
        ("+15550100001", None),
```

Append:

```python
def test_a_non_profile_sender_is_dropped_like_a_blank():
    messages, skipped = lx._parse_messages(
        [
            {
                "CONVERSATION ID": "c1",
                "DATE": "2026-01-02 10:00:00 UTC",
                "FROM": "Acme",
                "SENDER PROFILE URL": "https://www.linkedin.com/company/acme",
                "RECIPIENT PROFILE URLS": "https://www.linkedin.com/in/me-example,https://www.linkedin.com/company/acme",
            }
        ]
    )
    assert skipped == 0
    assert messages[0].sender_profile_url is None
    assert messages[0].recipient_profile_urls == ["linkedin.com/in/me-example"]


def test_match_people_links_by_profile_url_first():
    s = snapshot(connections=[connection(email="alice@example.com")])
    lx.match_people(
        s,
        [
            {"id": 21, "email": "alice@example.com", "display_name": "Alice Example"},
            {
                "id": 30,
                "email": None,
                "display_name": "Someone Else",
                "linkedin_url": "linkedin.com/in/alice-example",
            },
        ],
    )
    assert (s.connections[0].person_id, s.connections[0].match_method) == (30, "url")
    assert (s.matched_by_url, s.matched_by_email, s.matched_by_name) == (1, 0, 0)


def test_a_url_linked_person_is_not_reused_by_name():
    s = snapshot(connections=[connection(), connection(slug="alice-example-2")])
    lx.match_people(
        s,
        [
            {
                "id": 30,
                "email": None,
                "display_name": "Alice Example",
                "linkedin_url": "linkedin.com/in/alice-example",
            }
        ],
    )
    links = {c.profile_url: (c.person_id, c.match_method) for c in s.connections}
    assert links == {
        "linkedin.com/in/alice-example": (30, "url"),
        "linkedin.com/in/alice-example-2": (None, None),
    }
```

Before writing the CSV-row test, check `parse_timestamp`'s accepted formats in `services/linkedin_export.py` (`_DATE_FORMATS` and `parse_timestamp`). Use a `DATE` value one of them parses; `"2026-01-02 10:00:00 UTC"` is the export's format.

In `tests/test_import_linkedin.py`, change the expected summary line (currently line 98) to:

```python
        "connections 6 (url-matched 0, email-matched 1, name-matched 0, unmatched 5)",
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_linkedin_export.py tests/test_import_linkedin.py -q`
Expected: FAIL. The company URL comes back as `linkedin.com/company/acme`, `matched_by_url` doesn't exist, and the summary text doesn't match.

- [ ] **Step 3: Implement**

In `services/linkedin_export.py`, replace `_URL_PREFIX` and `normalize_profile_url`:

```python
_URL_PREFIX = re.compile(r"^(https?://)?([a-z]{2,3}\.)?", re.IGNORECASE)  # www., uk., …
_PROFILE = re.compile(r"^linkedin\.com/in/([^/\s]+)")
```

```python
def normalize_profile_url(url: str | None) -> str | None:
    """linkedin.com/in/<slug>, or None for anything that is not a member profile: a
    company page, a bare domain, a blank (linkedin-identity design §3). The one
    normalizer shared by the export parser, Google derivation and identity
    classification. A sub-path after the slug (/details/…) is dropped."""
    u = (url or "").strip()
    u = _URL_PREFIX.sub("", u).split("?", 1)[0].split("#", 1)[0].rstrip("/")
    m = _PROFILE.match(unquote(u).lower())
    return f"linkedin.com/in/{m.group(1)}" if m else None
```

Replace `match_people`:

```python
def match_people(snapshot: LinkedInSnapshot, people_rows: list[dict]) -> None:
    """Soft-link connections to people rows (§5.4): exact profile URL first
    (linkedin-identity design §3), then exact email, then a normalized name unique
    among people and among connections. Wrong links are worse than missing ones."""
    people_by_url = {r["linkedin_url"]: r["id"] for r in people_rows if r.get("linkedin_url")}
    people_by_email = {normalize_email(r["email"]): r["id"] for r in people_rows if r.get("email")}
    for c in snapshot.connections:
        c.person_id = c.match_method = None
        if c.profile_url in people_by_url:
            c.person_id, c.match_method = people_by_url[c.profile_url], "url"
        elif c.email and c.email in people_by_email:
            c.person_id, c.match_method = people_by_email[c.email], "email"
    claimed = {c.person_id for c in snapshot.connections if c.person_id}

    people_by_name = _unique_by_name((r.get("display_name") or "", r["id"]) for r in people_rows)
    connection_names = _unique_by_name((c.full_name, c.profile_url) for c in snapshot.connections)
    for c in snapshot.connections:
        if c.person_id:
            continue
        key = normalize_name(c.full_name)
        person_id = people_by_name.get(key)
        if person_id and person_id not in claimed and connection_names.get(key) == c.profile_url:
            c.person_id, c.match_method = person_id, "name"
            claimed.add(person_id)

    snapshot.matched_by_url = sum(c.match_method == "url" for c in snapshot.connections)
    snapshot.matched_by_email = sum(c.match_method == "email" for c in snapshot.connections)
    snapshot.matched_by_name = sum(c.match_method == "name" for c in snapshot.connections)
```

In `models/linkedin.py`, change the `match_method` comment to `# 'url' | 'email' | 'name' | None` and add, right above `matched_by_email: int = 0`:

```python
    matched_by_url: int = 0  # printed by scripts/import_linkedin.py; not stored in linkedin_imports
```

In `scripts/import_linkedin.py::summary`, change the first two lines that use the counts:

```python
    unmatched = n - s.matched_by_url - s.matched_by_email - s.matched_by_name
```

```python
        f"connections {n:,} (url-matched {s.matched_by_url:,}, email-matched {s.matched_by_email:,}, "
        f"name-matched {s.matched_by_name:,}, unmatched {unmatched:,})",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_linkedin_export.py tests/test_import_linkedin.py tests/test_repo_linkedin.py -q`
Expected: PASS, including the existing `test_match_people` and the non-ASCII slug cases.

- [ ] **Step 5: Commit**

```bash
git add services/linkedin_export.py models/linkedin.py scripts/import_linkedin.py tests/test_linkedin_export.py tests/test_import_linkedin.py
git commit -m "linkedin: profile-only normalizer, link connections by URL first"
```

---

### Task 3: Derive the URL from Google, keep an identifier on PATCH

**Files:**
- Modify: `services/contact_fields.py`
- Modify: `services/person_edit.py:138-142`
- Test: `tests/test_contact_fields.py`, `tests/test_person_edit.py`

**Interfaces:**
- Consumes: `normalize_profile_url` (Task 2).
- Produces: `contact_fields.derive(person)["linkedin_url"]: str | None`; `contact_fields.profile_urls(contact: dict) -> list[str]`; `contact_fields.check_identifier_kept(email: str | None, live: dict, fields: dict) -> None` (raises `IdentifierRuleError`). `check_phone_removal` is removed.

- [ ] **Step 1: Write the failing tests**

In `tests/test_contact_fields.py`, change the first assertion of `test_derive_handles_missing_and_empty_payloads` to:

```python
    assert got == {
        "phone_numbers": [],
        "company": None,
        "job_title": None,
        "google_fields": {},
        "linkedin_url": None,
    }
```

Append:

```python
# --- LinkedIn profile URL (linkedin-identity design §3, §4) -----------------

JANE_URLS = [{"value": "https://www.linkedin.com/in/jane-example"}]
A_PHONE = [{"value": "+15550100001"}]


def test_derive_returns_the_first_profile_url():
    got = cf.derive(
        {
            "urls": [
                {"value": "https://example.com"},
                {"value": "https://www.linkedin.com/company/acme"},
                {"value": "https://www.linkedin.com/in/Jane-Example/"},
                {"value": "https://www.linkedin.com/in/someone-else"},
            ]
        }
    )
    assert got["linkedin_url"] == "linkedin.com/in/jane-example"
    assert "urls" in got["google_fields"]


def test_derive_has_no_linkedin_url_without_a_profile():
    assert cf.derive({"urls": [{"value": "https://example.com"}, {}]})["linkedin_url"] is None


def test_profile_urls_normalizes_and_deduplicates():
    contact = {
        "urls": [
            {"value": "https://www.linkedin.com/in/Jane-Example/"},
            {"value": "linkedin.com/in/jane-example"},
            {"value": "https://www.linkedin.com/company/acme"},
            {"value": None},
        ]
    }
    assert cf.profile_urls(contact) == ["linkedin.com/in/jane-example"]
    assert cf.profile_urls({}) == []


def test_identifier_kept_refuses_removing_the_last_url():
    with pytest.raises(cf.IdentifierRuleError):
        cf.check_identifier_kept(None, {"urls": JANE_URLS}, {"urls": [{"value": "https://example.com"}]})


def test_identifier_kept_refuses_clearing_the_last_phone():
    with pytest.raises(cf.IdentifierRuleError):
        cf.check_identifier_kept(None, {"phoneNumbers": A_PHONE}, {"phoneNumbers": []})


def test_identifier_kept_allows_clearing_phones_while_a_url_remains():
    cf.check_identifier_kept(None, {"urls": JANE_URLS, "phoneNumbers": A_PHONE}, {"phoneNumbers": []})


def test_identifier_kept_allows_swapping_the_url_for_a_phone():
    cf.check_identifier_kept(None, {"urls": JANE_URLS}, {"urls": [], "phoneNumbers": A_PHONE})


def test_identifier_kept_ignores_a_person_with_an_email():
    cf.check_identifier_kept("a@example.com", {}, {"urls": [], "phoneNumbers": []})
```

In `tests/test_person_edit.py`, append after the phone-removal tests:

```python
def _linkedin_only(monkeypatch, contact_gc, *, phones=()):
    monkeypatch.setattr(
        person_edit.people,
        "get_by_id",
        lambda conn, pid: {"id": 7, "email": None, "google_resource_name": "people/c1"},
    )
    contact_gc.live = {
        "etag": "etag-1",
        "urls": [{"value": "https://www.linkedin.com/in/jane-example"}],
        "phoneNumbers": [{"value": p} for p in phones],
        "memberships": [],
    }


def test_removing_the_last_linkedin_url_is_a_conflict(contact_gc, monkeypatch):
    # Review Focus 5.
    _linkedin_only(monkeypatch, contact_gc)
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 7, contact={"urls": [{"value": "https://example.com"}]})
    assert contact_gc.updates == []


def test_clearing_phones_on_a_person_with_a_linkedin_url_is_allowed(contact_gc, monkeypatch):
    _linkedin_only(monkeypatch, contact_gc, phones=["+15550100001"])
    person_edit.update(None, 7, contact={"phoneNumbers": []})
    assert contact_gc.updates and contact_gc.updates[0][2] == {"phoneNumbers": []}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_contact_fields.py tests/test_person_edit.py -q`
Expected: FAIL. You should see `KeyError: 'linkedin_url'`, missing `profile_urls`/`check_identifier_kept`, and no `Conflict` raised on the URL removal.

- [ ] **Step 3: Implement `services/contact_fields.py`**

Add the import next to the others:

```python
from services.linkedin_export import normalize_profile_url
```

Replace `IdentifierRuleError` and `check_phone_removal` with:

```python
class IdentifierRuleError(Exception):
    """A submitted `phoneNumbers` or `urls` list would leave a person with no
    email address, no phone number and no LinkedIn profile URL. -> 409."""
```

```python
def check_identifier_kept(email: str | None, live: dict, fields: dict) -> None:
    """Raises IdentifierRuleError when applying `fields` to the live Google contact
    would leave a person with no email, no usable phone and no LinkedIn profile URL
    (people_has_an_identifier; linkedin-identity design §4). A field not being
    submitted keeps its live value. A person with an email address is unaffected."""
    if email:
        return
    phones = fields["phoneNumbers"] if "phoneNumbers" in fields else live.get("phoneNumbers", [])
    if any(normalize_handle(str(p.get("value") or "")) is not None for p in phones):
        return
    if profile_urls({"urls": fields["urls"] if "urls" in fields else live.get("urls", [])}):
        return
    raise IdentifierRuleError(
        "a person must keep an email address, a phone number or a LinkedIn profile URL; "
        "removing the last one goes through the Google UI"
    )
```

After `identifiers`, add:

```python
def profile_urls(contact: dict) -> list[str]:
    """The normalized LinkedIn profile URLs a contact map carries, in order, without
    repeats. Company pages and other URLs are not identifiers and are left out."""
    raw = (normalize_profile_url(str(u.get("value") or "")) for u in contact.get("urls", []))
    return list(dict.fromkeys(u for u in raw if u))
```

In `derive`, update the docstring's return shape to include `"linkedin_url": str | None`, and add before the `return`:

```python
    urls = profile_urls(person)
```

and add `"linkedin_url": urls[0] if urls else None,` to the returned dict.

- [ ] **Step 4: Implement `services/person_edit.py`**

Replace the `if "phoneNumbers" in fields:` block with:

```python
        if "phoneNumbers" in fields or "urls" in fields:
            try:
                contact_fields.check_identifier_kept(row["email"], live, fields)
            except contact_fields.IdentifierRuleError as e:
                raise Conflict(str(e)) from e
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_contact_fields.py tests/test_person_edit.py tests/test_google_contacts.py -q`
Expected: PASS, including the three existing phone-removal tests in `test_person_edit.py`.

- [ ] **Step 6: Commit**

```bash
git add services/contact_fields.py services/person_edit.py tests/test_contact_fields.py tests/test_person_edit.py
git commit -m "contact fields: derive linkedin_url, keep an identifier on PATCH"
```

---

### Task 4: Sync guards on every write path

**Files:**
- Modify: `services/google_contacts_sync.py` (`_link`, `ensure_contact`, `apply_person`, `run_sync`)
- Test: `tests/test_google_contacts_sync.py`

**Interfaces:**
- Consumes: `derive()["linkedin_url"]` (Task 3); `people.linkedin_owner` and the writers' `linkedin_url` (Task 1).
- Produces: `apply_person` may return `"linkedin_conflict"`; `run_sync`'s counts carry a `linkedin_conflict` key; `_link(conn, row, person)` (it was `_link(conn, email, person)`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_google_contacts_sync.py`, give `person()` a `urls=()` parameter (after `phones=()`) and, after the `if phones:` block, add:

```python
    if urls:
        p["urls"] = [{"value": v} for v in urls]
```

Append:

```python
# --- LinkedIn profile URL (linkedin-identity design §3, §4) -----------------

JANE = "https://www.linkedin.com/in/jane-example"
JANE_KEY = "linkedin.com/in/jane-example"


def _capture(monkeypatch, name):
    got = {}
    monkeypatch.setattr(
        sync.people, name, lambda conn, key, **kw: got.update({"key": key, **kw}) or {"id": 1}
    )
    return got


def test_a_linkedin_only_contact_is_adopted(monkeypatch):
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: None)
    created = _capture(monkeypatch, "create_from_google")
    assert sync.apply_person(None, person(email=None, urls=[JANE])) == "created"
    assert created["key"] is None and created["linkedin_url"] == JANE_KEY


def test_a_claimed_url_alone_is_not_adopted_and_is_counted(monkeypatch):
    calls = []
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 99)
    monkeypatch.setattr(sync.people, "create_from_google", lambda *a, **k: calls.append(1))
    assert sync.apply_person(None, person(email=None, urls=[JANE])) == "linkedin_conflict"
    assert calls == []


def test_a_claimed_url_is_dropped_but_a_phone_still_adopts(monkeypatch):
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 99)
    created = _capture(monkeypatch, "create_from_google")
    kind = sync.apply_person(None, person(email=None, phones=["+15550100001"], urls=[JANE]))
    assert kind == "created" and created["linkedin_url"] is None


def test_a_claimed_url_is_dropped_on_the_linked_update(monkeypatch):
    monkeypatch.setattr(
        sync.people, "get_by_google_resource", lambda conn, rn: linked_row(email="a@example.com")
    )
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 99)
    updated = _capture(monkeypatch, "update_from_google")
    assert sync.apply_person(None, person(email="a@example.com", urls=[JANE])) == "updated"
    assert updated["linkedin_url"] is None


def test_a_linked_row_keeps_its_own_url(monkeypatch):
    # Review Focus 1: the owner check must exclude the row itself.
    monkeypatch.setattr(
        sync.people,
        "get_by_google_resource",
        lambda conn, rn: {
            "id": 7,
            "email": None,
            "google_resource_name": rn,
            "linkedin_url": JANE_KEY,
        },
    )
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 7)
    updated = _capture(monkeypatch, "update_from_google")
    for _ in range(2):
        assert sync.apply_person(None, person(email=None, urls=[JANE])) == "updated"
        assert updated["linkedin_url"] == JANE_KEY


def test_a_claimed_url_is_dropped_when_linking_by_email(monkeypatch):
    # Review Focus 2.
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(
        sync.people,
        "get",
        lambda conn, email: {
            "id": 5,
            "email": email,
            "google_resource_name": None,
            "google_deleted_at": None,
        },
    )
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 99)
    linked = _capture(monkeypatch, "set_google")
    assert sync.apply_person(None, person(email="alice@example.com", urls=[JANE])) == "linked"
    assert linked["key"] == "alice@example.com" and linked["linkedin_url"] is None


def test_a_claimed_url_is_dropped_on_email_create(monkeypatch):
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(sync.people, "get", lambda conn, email: None)
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 99)
    created = _capture(monkeypatch, "create_from_google")
    assert sync.apply_person(None, person(email="alice@example.com", urls=[JANE])) == "created"
    assert created["linkedin_url"] is None


def test_a_linkedin_only_row_losing_its_url_is_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sync.people,
        "get_by_google_resource",
        lambda conn, rn: {"id": 7, "email": None, "google_resource_name": rn},
    )
    monkeypatch.setattr(sync.people, "update_from_google", lambda conn, rn, **kw: calls.append(kw))
    assert sync.apply_person(None, person(email=None)) == "skipped"
    assert calls == []


def test_a_phone_only_row_swapping_its_phone_for_a_url_updates(monkeypatch):
    monkeypatch.setattr(
        sync.people,
        "get_by_google_resource",
        lambda conn, rn: {"id": 7, "email": None, "google_resource_name": rn},
    )
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: None)
    updated = _capture(monkeypatch, "update_from_google")
    assert sync.apply_person(None, person(email=None, urls=[JANE])) == "updated"
    assert updated["phone_numbers"] == [] and updated["linkedin_url"] == JANE_KEY


def test_a_linkedin_only_row_gaining_an_email_is_promoted(monkeypatch):
    monkeypatch.setattr(
        sync.people,
        "get_by_google_resource",
        lambda conn, rn: {"id": 7, "email": None, "google_resource_name": rn},
    )
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 7)
    monkeypatch.setattr(sync.people, "email_owner", lambda conn, email: None)
    updated = _capture(monkeypatch, "update_from_google")
    kind = sync.apply_person(None, person(email="jane@example.com", urls=[JANE]))
    assert kind == "promoted" and updated["email"] == "jane@example.com"


def test_run_sync_counts_linkedin_conflict(monkeypatch):
    # Review Focus 2: the run finishes and reports the conflict as a count only.
    monkeypatch.setattr(
        sync.gc,
        "list_connections",
        lambda token: ([person(rn="people/c2", email=None, urls=[JANE])], "tok"),
    )
    monkeypatch.setattr(sync.sync_state, "get_token", lambda conn: None)
    monkeypatch.setattr(sync.sync_state, "set_token", lambda conn, t, s: None)
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(sync.people, "linkedin_owner", lambda conn, url: 99)
    counts = sync.run_sync(None)
    assert counts["linkedin_conflict"] == 1 and counts["skipped"] == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_google_contacts_sync.py -q`
Expected: FAIL. The URL is written through unguarded, `"linkedin_conflict"` is never returned, and the URL-swap row is `skipped`.

- [ ] **Step 3: Implement**

In `services/google_contacts_sync.py`, replace `_link` and add `_claim_url` above it:

```python
def _claim_url(conn: Any, derived: dict, own_id: int | None) -> bool:
    """Drop a profile URL that a different row already holds (linkedin-identity
    design §4): writing it would violate people_linkedin_url_key and abort the whole
    sync. `own_id` is the row being written, which may already hold its own URL.
    Returns True when a URL was dropped."""
    url = derived["linkedin_url"]
    if url is None:
        return False
    owner = people.linkedin_owner(conn, url)
    if owner is None or owner == own_id:
        return False
    derived["linkedin_url"] = None
    return True


def _link(conn: Any, row: dict, person: dict) -> None:
    derived = contact_fields.derive(person)
    _claim_url(conn, derived, row.get("id"))
    people.set_google(
        conn,
        row["email"],
        resource_name=person["resourceName"],
        etag=person.get("etag"),
        display_name=display_name(person),
        notes=notes(person),
        **derived,
    )
    _write_labels(conn, person)
```

In `ensure_contact`, change `_link(conn, row["email"], person)` to `_link(conn, row, person)`.

Replace `apply_person` from `derived = contact_fields.derive(person)` to the end of the function with:

```python
    derived = contact_fields.derive(person)
    if linked:
        _claim_url(conn, derived, linked.get("id"))
        if (
            linked.get("email") is None
            and not derived["phone_numbers"]
            and not derived["linkedin_url"]
        ):
            # An email-less row's phones and LinkedIn URL are its only identifiers —
            # never overwrite them with nothing (people_has_an_identifier).
            return "skipped"
        promote = None
        if linked.get("email") is None:
            # A row that already has an email is never touched here — Google
            # is the truth for most fields, but silently changing a person's
            # identity key on a nightly path is not something to do by
            # accident (spec §6.2).
            candidate = primary_email(person)
            if candidate and people.email_owner(conn, candidate) is None:
                # Only promote an unclaimed address: writing a claimed one
                # violates people_email_key and aborts the entire sync.
                promote = candidate
        people.update_from_google(
            conn,
            rn,
            etag=person.get("etag"),
            display_name=display_name(person),
            notes=notes(person),
            email=promote,
            **derived,
        )
        _write_labels(conn, person)
        return "promoted" if promote else "updated"
    email = primary_email(person)
    if not email:
        # Adopt a contact that has no email address, provided it has a phone we
        # can normalize or a LinkedIn profile URL no other row holds —
        # people_has_an_identifier requires one of the three (linkedin-identity
        # design §3). Contacts with none are counted, never written.
        dropped = _claim_url(conn, derived, None)
        if not derived["phone_numbers"] and not derived["linkedin_url"]:
            return "linkedin_conflict" if dropped else "skipped"
        people.create_from_google(
            conn,
            None,
            display_name=display_name(person),
            resource_name=rn,
            etag=person.get("etag"),
            notes=notes(person),
            **derived,
        )
        _write_labels(conn, person)
        return "created"
    row = people.get(conn, email)
    if row and not row.get("google_resource_name") and not row.get("google_deleted_at"):
        _link(conn, row, person)
        return "linked"
    if row is None:
        _claim_url(conn, derived, None)
        people.create_from_google(
            conn,
            email,
            display_name=display_name(person),
            resource_name=rn,
            etag=person.get("etag"),
            notes=notes(person),
            **derived,
        )
        _write_labels(conn, person)
        return "created"
    return None
```

In `run_sync`, add the key:

```python
    counts = {
        "updated": 0,
        "linked": 0,
        "created": 0,
        "deleted": 0,
        "skipped": 0,
        "promoted": 0,
        "linkedin_conflict": 0,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_google_contacts_sync.py tests/test_person_create.py tests/test_handlers.py -q`
Expected: PASS, including every existing adoption, promotion and `ensure_contact` test.

- [ ] **Step 5: Commit**

```bash
git add services/google_contacts_sync.py tests/test_google_contacts_sync.py
git commit -m "sync: derive linkedin_url, drop a claimed URL on every write path"
```

---

### Task 5: Address a person by LinkedIn URL

**Files:**
- Modify: `services/identity.py`, `services/person_lookup.py`, `api/routers/people.py`
- Test: `tests/test_identity.py`, `tests/test_person_lookup.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `normalize_profile_url` (Task 2); `people.get_by_linkedin_url` (Task 1).
- Produces: `identity.Kind = Literal["email", "linkedin", "phone", "id"]`; `classify` returns `("linkedin", "linkedin.com/in/<slug>")`; routes `GET/PATCH /people/{ident:path}`, `POST /people/{ident:path}/sync`; `PersonOut.linkedin_url: str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_identity.py`:

```python
def test_profile_urls_are_linkedin():
    # Review Focus 4: whatever Ben pastes from a browser resolves.
    for raw in (
        "linkedin.com/in/jane-example",
        "https://www.linkedin.com/in/Jane-Example/?trk=x",
        "https://uk.linkedin.com/in/jane-example",
    ):
        assert identity.classify(raw) == ("linkedin", "linkedin.com/in/jane-example")


def test_a_numeric_slug_is_linkedin_not_an_id_or_a_phone():
    assert identity.classify("linkedin.com/in/5550100001") == (
        "linkedin",
        "linkedin.com/in/5550100001",
    )


def test_an_email_mentioning_linkedin_is_an_email():
    assert identity.classify("jane@linkedin.com") == ("email", "jane@linkedin.com")


def test_a_company_page_identifies_no_one():
    assert identity.classify("https://www.linkedin.com/company/acme") is None
```

In `tests/test_person_lookup.py`, add to the `people` fixture:

```python
    monkeypatch.setattr(
        person_lookup.people,
        "get_by_linkedin_url",
        lambda c, u: {"id": 5} if u == "linkedin.com/in/jane-example" else None,
    )
```

and append:

```python
def test_resolves_a_linkedin_url(people):
    assert person_lookup.resolve(None, "https://www.linkedin.com/in/Jane-Example/")["id"] == 5


def test_an_unknown_linkedin_url_is_not_found(people):
    with pytest.raises(person_lookup.NotFound):
        person_lookup.resolve(None, "linkedin.com/in/nobody-example")
```

In `tests/test_api.py`, add to `_wire`:

```python
    monkeypatch.setattr(people_repo, "get_by_linkedin_url", lambda conn, url: None)
```

and append:

```python
JANE_KEY = "linkedin.com/in/jane-example"


def _jane(monkeypatch):
    monkeypatch.setattr(
        people_repo,
        "get_by_linkedin_url",
        lambda conn, url: row(id=9, email=None, linkedin_url=url) if url == JANE_KEY else None,
    )


def test_get_by_encoded_linkedin_url(monkeypatch):
    # Review Focus 4. Starlette routes on the decoded path, so %2F arrives as "/".
    _jane(monkeypatch)
    for path in ("/people/linkedin.com%2Fin%2Fjane-example", "/people/linkedin.com/in/jane-example"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.json()["id"] == 9 and r.json()["linkedin_url"] == JANE_KEY


def test_patch_by_encoded_linkedin_url(monkeypatch):
    _jane(monkeypatch)
    monkeypatch.setattr(person_edit, "update", lambda conn, pid, **kw: row(id=pid, email=None))
    r = client.patch("/people/linkedin.com%2Fin%2Fjane-example", json={"notes": "hi"})
    assert r.status_code == 200 and r.json()["id"] == 9


def test_sync_by_encoded_linkedin_url(monkeypatch):
    _jane(monkeypatch)
    monkeypatch.setattr(gsync, "sync_one", lambda conn, r: r)
    r = client.post("/people/linkedin.com%2Fin%2Fjane-example/sync")
    assert r.status_code == 200 and r.json()["id"] == 9


def test_an_unknown_linkedin_url_is_404():
    assert client.get("/people/linkedin.com%2Fin%2Fnobody-example").status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_identity.py tests/test_person_lookup.py tests/test_api.py -q`
Expected: FAIL. `classify` returns `None` for the URLs, and the encoded-path requests 404.

- [ ] **Step 3: Implement**

`services/identity.py`:

```python
"""Classify a path identifier as an email, LinkedIn profile URL, phone number, or
numeric person id (spec §5.1; linkedin-identity design §3). Pure logic, no I/O —
imported by api/routers/people.py and services/person_lookup.py."""

from typing import Literal

from services.eligibility import normalize
from services.imessage_export import normalize_handle
from services.linkedin_export import normalize_profile_url

Kind = Literal["email", "linkedin", "phone", "id"]


def classify(ident: str) -> tuple[Kind, str | int] | None:
    """Email first, then a LinkedIn profile URL, then phone, then id. Returns the
    kind and the normalized value, or None when the value cannot identify anyone
    (the router turns None into a 404)."""
    raw = (ident or "").strip()
    if not raw:
        return None

    if "@" in raw:
        local, _, domain = raw.partition("@")
        if not local.strip() or not domain.strip():
            return None
        return ("email", normalize(raw))

    url = normalize_profile_url(raw)
    if url is not None:
        return ("linkedin", url)

    phone = normalize_handle(raw)
    if phone is not None:
        return ("phone", phone)

    if raw.isdigit():
        return ("id", int(raw))

    return None
```

In `services/person_lookup.py::resolve`, add a branch after the `email` one:

```python
    elif kind == "linkedin":
        row = people.get_by_linkedin_url(conn, str(value))
```

Update the module docstring's first line to "Resolve an email, LinkedIn profile URL, E.164 phone, or numeric id to one people row, for scripts".

In `api/routers/people.py`:
- add `linkedin_url: str | None = None` to `PersonOut`, after `contact`;
- add `linkedin_url=row.get("linkedin_url"),` to `to_out`'s `PersonOut(...)` call;
- in `resolve_person`, add the same `elif kind == "linkedin":` branch as above, and change its docstring's first sentence to "Classify `ident` — email, LinkedIn profile URL, phone, or numeric id (spec §5.1) — and look up the matching row.";
- change the three decorators to:

```python
@router.get("/people/{ident:path}", response_model=PersonOut)
```

```python
@router.patch("/people/{ident:path}", response_model=PersonOut)
```

```python
@router.post("/people/{ident:path}/sync", response_model=PersonOut)
```

Add one comment above the GET route:

```python
# {ident:path}: Starlette routes on the decoded path, so a percent-encoded LinkedIn
# URL (linkedin.com%2Fin%2F…) arrives with real slashes (linkedin-identity design §3).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_identity.py tests/test_person_lookup.py tests/test_api.py tests/test_outreach_cli.py -q`
Expected: PASS. In particular, every existing `test_api.py` route test still passes: email, `%2B` phone, raw `+` phone, id, `/labels/{name}` with a slash, and `/sync`.

- [ ] **Step 5: Commit**

```bash
git add services/identity.py services/person_lookup.py api/routers/people.py tests/test_identity.py tests/test_person_lookup.py tests/test_api.py
git commit -m "identity: resolve a person by LinkedIn profile URL"
```

---

### Task 6: Google client retries and the URL index

**Files:**
- Modify: `clients/google_contacts.py`
- Test: `tests/test_google_contacts.py`

**Interfaces:**
- Produces: `gc._execute(request, *, idempotent: bool = True) -> Any`; `gc._RETRIES = 5`; `gc._sleep` (patchable); `gc.list_url_index() -> tuple[dict[str, list[dict]], list[dict]]`, where entries are `{"resource_name": str, "display_name": str | None}`.

- [ ] **Step 1: Update the existing fakes**

Idempotent calls now pass `num_retries`. Let every fake accept it:

```bash
sed -i '' 's/def execute(self):/def execute(self, num_retries=0):/' tests/test_google_contacts.py
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_google_contacts.py`:

```python
class _Req:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def execute(self, num_retries=0):
        self.calls.append(num_retries)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_an_idempotent_request_lets_googleapiclient_retry():
    req = _Req([{"ok": 1}])
    assert gc._execute(req) == {"ok": 1}
    assert req.calls == [gc._RETRIES]


def test_a_create_retries_only_rate_limits(monkeypatch):
    slept = []
    monkeypatch.setattr(gc, "_sleep", slept.append)
    req = _Req([_http_error(429, None), _http_error(429, None), {"resourceName": "people/c1"}])
    assert gc._execute(req, idempotent=False) == {"resourceName": "people/c1"}
    assert req.calls == [0, 0, 0] and slept == [1, 2]


def test_a_create_never_retries_a_server_error(monkeypatch):
    # A 5xx may have created the contact already; retrying could make a second one.
    monkeypatch.setattr(gc, "_sleep", lambda s: pytest.fail("must not retry"))
    with pytest.raises(HttpError):
        gc._execute(_Req([_http_error(500, None)]), idempotent=False)


def test_a_create_gives_up_after_the_last_retry(monkeypatch):
    monkeypatch.setattr(gc, "_sleep", lambda s: None)
    req = _Req([_http_error(429, None)] * (gc._RETRIES + 1))
    with pytest.raises(HttpError):
        gc._execute(req, idempotent=False)
    assert len(req.calls) == gc._RETRIES + 1


def test_list_url_index_normalizes_pages_and_keeps_every_contact(monkeypatch):
    pages = [
        {
            "connections": [
                {
                    "resourceName": "people/c1",
                    "names": [{"displayName": "Jane Example"}],
                    "urls": [
                        {"value": "https://www.linkedin.com/in/Jane-Example/"},
                        {"value": "https://www.linkedin.com/company/acme"},
                    ],
                },
                {"resourceName": "people/c2", "names": [{"displayName": "Lee Example"}]},
            ],
            "nextPageToken": "p2",
        },
        {"connections": [{"resourceName": "people/c3", "urls": [{"value": "linkedin.com/in/jane-example"}]}]},
    ]
    seen = []

    class _List:
        def __init__(self, kw):
            self.kw = kw

        def execute(self, num_retries=0):
            seen.append((self.kw.get("pageToken"), self.kw["personFields"], num_retries))
            return pages[len(seen) - 1]

    class _Connections:
        def list(self, **kw):
            return _List(kw)

    class _People:
        def connections(self):
            return _Connections()

    class _Svc:
        def people(self):
            return _People()

    monkeypatch.setattr(gc, "_svc", lambda: _Svc())
    by_url, contacts = gc.list_url_index()
    assert by_url == {
        "linkedin.com/in/jane-example": [
            {"resource_name": "people/c1", "display_name": "Jane Example"},
            {"resource_name": "people/c3", "display_name": None},
        ]
    }
    assert [c["resource_name"] for c in contacts] == ["people/c1", "people/c2", "people/c3"]
    assert seen == [
        (None, "names,urls,metadata", gc._RETRIES),
        ("p2", "names,urls,metadata", gc._RETRIES),
    ]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_google_contacts.py -q`
Expected: FAIL. `_execute`, `_RETRIES` and `list_url_index` don't exist yet.

- [ ] **Step 4: Implement**

In `clients/google_contacts.py`, add `import time` to the imports and, after `_READ_MASK`:

```python
_RETRIES = 5
_sleep = time.sleep  # patched in tests


def _execute(request: Any, *, idempotent: bool = True) -> Any:
    """Run one People API request, retrying rate limits (linkedin-identity design §5).
    Idempotent requests use googleapiclient's own retry (HTTP 429 and 5xx, with
    backoff). A create or delete retries only HTTP 429, which Google returns before
    doing anything: after a 5xx the contact may already exist, and a retry would
    make a second one."""
    if idempotent:
        return request.execute(num_retries=_RETRIES)
    for attempt in range(_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status != 429 or attempt == _RETRIES:
                raise
            _sleep(2**attempt)
    raise AssertionError("unreachable")
```

Replace every `.execute()` in the module with `_execute(...)` around the request:
- idempotent (default): `searchContacts` (both, in `_warmup` and `search_by_email`), `people().get`, `updateContact`, `contactGroups().list` (both), `contactGroups().members().modify`, `connections().list` (in `list_phone_index` and `list_connections`);
- `idempotent=False`: `createContact` (in `create_contact` and `create_person`), `contactGroups().create` (in `ensure_group`), `deleteContact`.

For example:

```python
    return _execute(
        _svc().people().createContact(body=full_body, personFields=PERSON_FIELDS),
        idempotent=False,
    )
```

```python
        resp = _execute(_svc().contactGroups().list(pageSize=200, pageToken=token))
```

In `list_connections`, keep the `try/except HttpError` around the call; `_execute` re-raises the 400 unchanged.

After `list_phone_index`, add:

```python
_URL_FIELDS = "names,urls,metadata"


def list_url_index() -> tuple[dict[str, list[dict]], list[dict]]:
    """(normalized LinkedIn profile URL -> the contacts carrying it, every contact),
    for the seed's and POST /people's duplicate checks (linkedin-identity design §5).
    A full listing like list_phone_index, because searchContacts does not index
    urls. Entries are {"resource_name", "display_name"}. No sync token: the nightly
    sync owns it."""
    # Layer-rule exception, as in list_phone_index: one normalizer, not two.
    from services.linkedin_export import normalize_profile_url

    by_url: dict[str, list[dict]] = {}
    contacts: list[dict] = []
    page_token = None
    while True:
        resp = _execute(
            _svc()
            .people()
            .connections()
            .list(
                resourceName="people/me",
                personFields=_URL_FIELDS,
                pageSize=1000,
                pageToken=page_token,
            )
        )
        for person in resp.get("connections", []):
            entry = {
                "resource_name": person["resourceName"],
                "display_name": (person.get("names") or [{}])[0].get("displayName"),
            }
            contacts.append(entry)
            for u in person.get("urls", []):
                url = normalize_profile_url(str(u.get("value") or ""))
                if url and entry not in by_url.setdefault(url, []):
                    by_url[url].append(entry)
        page_token = resp.get("nextPageToken")
        if not page_token:
            return by_url, contacts
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_google_contacts.py tests/test_google_contacts_sync.py tests/test_person_create.py -q`
Expected: PASS, including the existing expired-sync-token translation tests.

- [ ] **Step 6: Commit**

```bash
git add clients/google_contacts.py tests/test_google_contacts.py
git commit -m "google contacts: retry rate limits, list contacts by LinkedIn URL"
```

---

### Task 7: `POST /people` accepts a LinkedIn URL as the identifier

**Files:**
- Modify: `services/person_create.py`
- Test: `tests/test_person_create.py`

**Interfaces:**
- Consumes: `contact_fields.profile_urls` (Task 3); `people.get_by_linkedin_url` (Task 1); `gc.list_url_index` (Task 6); `apply_person` adopting a URL-only contact (Task 4).
- Produces: `person_create.create` accepts a URL-only contact; it raises `Duplicate(candidates=[id])` for a `people.linkedin_url` match and `Duplicate(candidates=[])` for a Google-only match.

- [ ] **Step 1: Write the failing tests**

In `tests/test_person_create.py`'s `wired` fixture, add:

```python
    monkeypatch.setattr(person_create.people, "get_by_linkedin_url", lambda conn, url: None)
    monkeypatch.setattr(person_create.gc, "list_url_index", lambda: ({}, []))
```

Append:

```python
LINKEDIN_ONLY = {
    "names": [{"givenName": "Jane", "familyName": "Example"}],
    "urls": [{"value": "https://www.linkedin.com/in/jane-example", "type": "profile"}],
}


def test_creates_a_linkedin_only_person(wired):
    row = person_create.create(None, contact=LINKEDIN_ONLY, labels=["from-linkedin"])
    assert row["id"] == 7
    assert wired["created_body"]["urls"] == LINKEDIN_ONLY["urls"]
    assert wired["edits"] == [(7, {"notes": None, "labels": {"add": ["from-linkedin"]}})]


def test_refuses_a_duplicate_linkedin_url(wired, monkeypatch):
    monkeypatch.setattr(
        person_create.people,
        "get_by_linkedin_url",
        lambda conn, url: {"id": 14} if url == "linkedin.com/in/jane-example" else None,
    )
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact=LINKEDIN_ONLY)
    assert e.value.candidates == [14]
    assert wired["created_body"] is None


def test_refuses_a_google_only_linkedin_url(wired, monkeypatch):
    monkeypatch.setattr(
        person_create.gc,
        "list_url_index",
        lambda: (
            {"linkedin.com/in/jane-example": [{"resource_name": "people/c9", "display_name": "Jane"}]},
            [],
        ),
    )
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact=LINKEDIN_ONLY)
    assert e.value.candidates == []
    assert wired["created_body"] is None


def test_a_company_page_is_not_an_identifier(wired):
    with pytest.raises(person_create.Invalid):
        person_create.create(
            None, contact={"urls": [{"value": "https://www.linkedin.com/company/acme"}]}
        )
    assert wired["created_body"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_person_create.py -q`
Expected: FAIL with `Invalid: a contact needs an email address or a phone number` on the URL-only contact.

- [ ] **Step 3: Implement**

In `services/person_create.py::create`, replace from `emails, phones = contact_fields.identifiers(fields)` through `raise Duplicate("person exists", candidates=[])` with:

```python
    emails, phones = contact_fields.identifiers(fields)
    urls = contact_fields.profile_urls(fields)
    if not emails and not phones and not urls:
        raise Invalid("a contact needs an email address, a phone number or a LinkedIn profile URL")

    candidates: list[int] = []
    google_only = False
    for email in emails:
        row = people.get(conn, email)
        if row is not None:
            candidates.append(row["id"])
    for phone in phones:
        for row in people.get_by_phone(conn, phone):
            candidates.append(row["id"])
    for url in urls:
        row = people.get_by_linkedin_url(conn, url)
        if row is not None:
            candidates.append(row["id"])
    if candidates:
        raise Duplicate("person exists", candidates=candidates)

    for email in emails:
        if gc.search_by_email(email) is not None:
            google_only = True
    if phones:
        phone_index = gc.list_phone_index()
        for phone in phones:
            if phone_index.get(phone):
                google_only = True
    if urls:
        # searchContacts does not index urls, so this is a full listing
        # (linkedin-identity design §5).
        url_index, _ = gc.list_url_index()
        if any(url_index.get(url) for url in urls):
            google_only = True
    if google_only:
        raise Duplicate("person exists", candidates=[])
```

Update `Invalid`'s docstring if it names the two identifiers.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_person_create.py tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/person_create.py tests/test_person_create.py
git commit -m "people: create a person from a LinkedIn profile URL"
```

---

### Task 8: The seed planner classifies unlinked LinkedIn rows

**Files:**
- Modify: `services/outreach_seed.py`
- Modify: `repo/outreach.py` (`linkedin_seed_rows`, new `people_seed_rows`)
- Modify: `repo/linkedin.py` (new `link_connection`)
- Test: `tests/test_outreach_seed.py`, `tests/test_repo_outreach.py`, `tests/test_repo_linkedin.py`

**Interfaces:**
- Consumes: `normalize_name` (existing, `services/linkedin_export.py`).
- Produces:
  - `outreach_seed.LINKEDIN_KINDS = ("link", "in_google", "possible_duplicate", "create")`;
  - `outreach_seed.LinkedInOutcome(kind, row, person_id=None, candidates=[], google_names=[])`;
  - `SeedPlan.linkedin: list[LinkedInOutcome]` and `SeedPlan.linkedin_of(kind) -> list[LinkedInOutcome]`;
  - `SeedPlan.unlinked` now holds only iMessage and WhatsApp rows;
  - `outreach_seed.plan(..., people_rows=None, google_urls=None, google_contacts=None)`;
  - `outreach_seed.name_key(name: str | None) -> tuple[str, str] | None`;
  - `outreach_seed.contact_for(row: dict) -> dict`;
  - `repo.outreach.people_seed_rows(conn) -> list[dict]` (`id, display_name, linkedin_url, google_resource_name`);
  - `linkedin_seed_rows` rows gain `first_name`, `last_name`;
  - `repo.linkedin.link_connection(conn, profile_url: str, person_id: int) -> None`.

- [ ] **Step 1: Write the failing repo tests**

Append to `tests/test_repo_outreach.py`:

```python
def test_people_seed_rows_select_identity_columns():
    conn = FakeConn()
    outreach.people_seed_rows(conn)
    sql, _ = conn.calls[0]
    assert sql == "SELECT id, display_name, linkedin_url, google_resource_name FROM people"


def test_linkedin_seed_rows_carry_first_and_last_names():
    conn = FakeConn()
    outreach.linkedin_seed_rows(conn)
    sql, _ = conn.calls[0]
    assert "first_name" in sql and "last_name" in sql
```

Check the top of `tests/test_repo_outreach.py` for the module alias it imports `repo/outreach.py` under and what its `FakeConn.calls` holds. Use the same alias and shape (the file's `FakeConn.execute` is at line 25).

Append to `tests/test_repo_linkedin.py`:

```python
def test_link_connection_sets_person_and_method():
    conn = FakeConn()
    linkedin.link_connection(conn, "linkedin.com/in/jane-example", 9)
    assert conn.calls[0] == (
        "UPDATE linkedin_connections SET person_id = %s, match_method = 'url' WHERE profile_url = %s",
        (9, "linkedin.com/in/jane-example"),
    )
```

Use the module alias `tests/test_repo_linkedin.py` already imports `repo/linkedin.py` under.

- [ ] **Step 2: Write the failing planner tests**

In `tests/test_outreach_seed.py`, replace `test_rows_without_a_person_are_listed_not_inserted` with the version below. Insert everything after it **just above `class Conn:`**, not at the end of the file: Task 9 replaces the file from `class Conn:` to the end, and uses `li` and `person_row`.

```python
def test_rows_without_a_person_are_listed_not_inserted():
    p = _plan(linkedin_rows=[linkedin(None)], whatsapp_rows=[handle(None)])
    assert p.candidates == {}
    assert [(u.source, u.detail) for u in p.unlinked] == [("whatsapp", "+15550100009")]
    assert [o.kind for o in p.linkedin] == ["create"]
```

Insert just above `class Conn:`:

```python
def li(slug, pid=None, first="Jane", last="Example", **kw):
    return linkedin(
        pid,
        profile_url=f"linkedin.com/in/{slug}",
        first_name=first,
        last_name=last,
        full_name=f"{first} {last}",
    ) | kw


def person_row(pid, name, url=None, rn=None):
    return {"id": pid, "display_name": name, "linkedin_url": url, "google_resource_name": rn}


def test_unlinked_linkedin_rows_get_one_outcome_each():
    p = _plan(
        linkedin_rows=[
            li("a", first="Ann", last="Alpha"),
            li("b", first="Bea", last="Google"),
            li("c", first="Jonathan", last="Smith"),
            li("d", first="Kimberly", last="Lee"),
            li("e", first="Dee", last="New"),
        ],
        people_rows=[
            person_row(5, "Pat Linked", url="linkedin.com/in/a", rn="people/c5"),
            person_row(6, "Jon Smith", rn="people/c6"),
        ],
        google_urls={"linkedin.com/in/b": [{"resource_name": "people/c9", "display_name": "B G"}]},
        google_contacts=[
            {"resource_name": "people/c9", "display_name": "B G"},
            {"resource_name": "people/c10", "display_name": "Kim Lee"},
            {"resource_name": "people/c6", "display_name": "Jon Smith"},  # has a row: not Google-only
        ],
    )
    got = {
        o.row["profile_url"]: (o.kind, o.person_id, o.candidates, o.google_names)
        for o in p.linkedin
    }
    assert got == {
        "linkedin.com/in/a": ("link", 5, [], []),
        "linkedin.com/in/b": ("in_google", None, [], []),
        "linkedin.com/in/c": ("possible_duplicate", None, [6], []),
        "linkedin.com/in/d": ("possible_duplicate", None, [], ["Kim Lee"]),
        "linkedin.com/in/e": ("create", None, [], []),
    }
    assert [o.row["profile_url"] for o in p.linkedin_of("create")] == ["linkedin.com/in/e"]
    assert p.candidates == {}


def test_a_url_link_wins_over_a_name_match():
    p = _plan(
        linkedin_rows=[li("a", first="Jon", last="Smith")],
        people_rows=[
            person_row(5, "Someone", url="linkedin.com/in/a"),
            person_row(6, "Jon Smith"),
        ],
    )
    assert [(o.kind, o.person_id) for o in p.linkedin] == [("link", 5)]


def test_cold_unlinked_rows_get_no_outcome():
    p = _plan(linkedin_rows=[li("a", last_message_at=OLD), li("b", position="Recruiter")])
    assert p.linkedin == []


def test_name_key_is_last_name_and_first_initial():
    assert seed.name_key("Jon Smith") == ("smith", "j")
    assert seed.name_key("Jonathan  Smith") == ("smith", "j")
    assert seed.name_key("Jane Q. Example") == ("example", "j")
    assert seed.name_key("Élodie Accent") == ("accent", "e")
    assert seed.name_key("Cher") is None
    assert seed.name_key(None) is None


def test_contact_for_builds_the_google_contact():
    assert seed.contact_for(li("jane-example", company="Example Health", position="CTO")) == {
        "names": [{"givenName": "Jane", "familyName": "Example"}],
        "organizations": [{"name": "Example Health", "title": "CTO"}],
        "urls": [{"value": "https://www.linkedin.com/in/jane-example", "type": "profile"}],
    }


def test_contact_for_without_names_or_company():
    row = li("x", first=None, last=None, company=None, position=None) | {"full_name": "X Example"}
    assert seed.contact_for(row) == {
        "names": [{"givenName": "X Example"}],
        "urls": [{"value": "https://www.linkedin.com/in/x", "type": "profile"}],
    }
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_seed.py tests/test_repo_outreach.py tests/test_repo_linkedin.py -q`
Expected: FAIL. `plan()` rejects `people_rows`, `SeedPlan` has no `linkedin`, and `people_seed_rows`/`link_connection` don't exist.

- [ ] **Step 4: Implement the repo functions**

In `repo/outreach.py`, change `linkedin_seed_rows`'s column list and add `people_seed_rows` after it:

```python
def linkedin_seed_rows(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT person_id, full_name, first_name, last_name, company, position, profile_url,
               message_count, my_message_count, last_message_at
        FROM linkedin_connections
        """
    ).fetchall()


def people_seed_rows(conn: Any) -> list[dict]:
    """What the seed needs to link, or flag as a possible duplicate, an unlinked LinkedIn
    connection (linkedin-identity design §5)."""
    return conn.execute(
        "SELECT id, display_name, linkedin_url, google_resource_name FROM people"
    ).fetchall()
```

In `repo/linkedin.py`, after `repoint_person`:

```python
def link_connection(conn: Any, profile_url: str, person_id: int) -> None:
    """Link one connection to a person by its profile URL (the seed's link and create
    paths, linkedin-identity design §5). The next import_linkedin re-derives the same
    link through match_people's URL step."""
    conn.execute(
        "UPDATE linkedin_connections SET person_id = %s, match_method = 'url' WHERE profile_url = %s",
        (person_id, profile_url),
    )
```

- [ ] **Step 5: Implement the planner**

Replace `services/outreach_seed.py` with:

```python
"""Propose referral candidates from the warm-pool bars measured 2026-10-12 (outreach
spec §7.1). Pure: the script fetches the rows, this decides. Nobody already in the
pipeline is proposed again.

A warm LinkedIn connection with no people row gets one outcome (linkedin-identity
design §5): `link` to the row already holding its URL, `in_google` when only a Google
contact does, `possible_duplicate` of a person with the same last name and first
initial, or `create`. iMessage and WhatsApp rows with no people row are listed for
Ben (the creating-person path), never inserted."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from services.linkedin_export import normalize_name

SOURCES = ("email", "imessage", "linkedin", "whatsapp")
RECRUITER_TERMS = re.compile(r"recruit|talent|sourcer|sourcing|headhunt", re.IGNORECASE)
LINKEDIN_KINDS = ("link", "in_google", "possible_duplicate", "create")


@dataclass
class Unlinked:
    source: str
    name: str | None
    detail: str  # the phone handle


@dataclass
class LinkedInOutcome:
    kind: str  # one of LINKEDIN_KINDS
    row: dict  # a repo/outreach.py::linkedin_seed_rows row
    person_id: int | None = None  # link: the people row holding this URL
    candidates: list[int] = field(default_factory=list)  # possible_duplicate: people ids
    google_names: list[str] = field(default_factory=list)  # possible_duplicate: no row yet


@dataclass
class SeedPlan:
    candidates: dict[int, set[str]] = field(default_factory=dict)  # person id -> sources
    already_in: set[int] = field(default_factory=set)
    unlinked: list[Unlinked] = field(default_factory=list)
    linkedin: list[LinkedInOutcome] = field(default_factory=list)

    def by_source(self) -> dict[str, int]:
        return {s: sum(1 for srcs in self.candidates.values() if s in srcs) for s in SOURCES}

    def linkedin_of(self, kind: str) -> list[LinkedInOutcome]:
        return [o for o in self.linkedin if o.kind == kind]


def name_key(name: str | None) -> tuple[str, str] | None:
    """(last name, first initial), normalized: the possible-duplicate rule. "Jon Smith"
    and "Jonathan Smith" share one. None for a single-word or empty name."""
    parts = normalize_name(name).split()
    if len(parts) < 2:
        return None
    return (parts[-1], parts[0][0])


def _full_name(row: dict) -> str:
    joined = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p)
    return joined or row["full_name"]


def contact_for(row: dict) -> dict:
    """The Google contact a LinkedIn-only person is created with (design §5)."""
    if row.get("first_name"):
        name = {"givenName": row["first_name"]}
        if row.get("last_name"):
            name["familyName"] = row["last_name"]
    else:
        name = {"givenName": row["full_name"]}
    contact: dict = {
        "names": [name],
        "urls": [{"value": f"https://www.{row['profile_url']}", "type": "profile"}],
    }
    org = {k: v for k, v in (("name", row.get("company")), ("title", row.get("position"))) if v}
    if org:
        contact["organizations"] = [org]
    return contact


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
    people_rows: list[dict] | None = None,
    google_urls: dict[str, list[dict]] | None = None,
    google_contacts: list[dict] | None = None,
) -> SeedPlan:
    cutoff = now - timedelta(days=window_days)
    result = SeedPlan()
    people_rows = people_rows or []
    google_urls = google_urls or {}

    people_by_url = {r["linkedin_url"]: r["id"] for r in people_rows if r.get("linkedin_url")}
    people_by_name: dict[tuple[str, str], list[int]] = {}
    for r in people_rows:
        if key := name_key(r.get("display_name")):
            people_by_name.setdefault(key, []).append(r["id"])
    linked_rns = {r["google_resource_name"] for r in people_rows if r.get("google_resource_name")}
    google_by_name: dict[tuple[str, str], list[str]] = {}
    for c in google_contacts or []:
        if c["resource_name"] in linked_rns:
            continue  # the people side already checked this one
        if key := name_key(c.get("display_name")):
            google_by_name.setdefault(key, []).append(c["display_name"])

    def classify(row: dict) -> LinkedInOutcome:
        url = row["profile_url"]
        if url in people_by_url:
            return LinkedInOutcome("link", row, person_id=people_by_url[url])
        if google_urls.get(url):
            return LinkedInOutcome("in_google", row)
        key = name_key(_full_name(row))
        ids = people_by_name.get(key, []) if key else []
        names = google_by_name.get(key, []) if key else []
        if ids or names:
            return LinkedInOutcome(
                "possible_duplicate", row, candidates=sorted(ids), google_names=sorted(names)
            )
        return LinkedInOutcome("create", row)

    def add(person_id: int, source: str) -> None:
        if person_id in existing:
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
            add(r["id"], "email")
    for r in linkedin_rows:
        if (
            _two_way(r)
            and not RECRUITER_TERMS.search(r["position"] or "")
            and _recent(r["last_message_at"], cutoff)
        ):
            if r["person_id"] is None:
                result.linkedin.append(classify(r))
            else:
                add(r["person_id"], "linkedin")
    for source, rows in (("imessage", imessage_rows), ("whatsapp", whatsapp_rows)):
        for r in rows:
            if (
                _two_way(r)
                and r["message_count"] >= min_messages
                and _recent(r["last_message_at"], cutoff)
            ):
                if r["person_id"] is None:
                    result.unlinked.append(Unlinked(source, r["display_name"], r["handle"]))
                else:
                    add(r["person_id"], source)
    return result
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_seed.py tests/test_repo_outreach.py tests/test_repo_linkedin.py -q`
Expected: the planner and repo tests PASS. `test_dry_run_inserts_nothing`, `test_a_real_run_inserts_candidates_as_seed` and `test_summary_counts_and_lists_unlinked_people` may now fail, because the script hasn't changed yet. Task 9 rewrites them. Note that in the task report rather than patching around it.

- [ ] **Step 7: Commit**

```bash
git add services/outreach_seed.py repo/outreach.py repo/linkedin.py tests/test_outreach_seed.py tests/test_repo_outreach.py tests/test_repo_linkedin.py
git commit -m "outreach seed: classify unlinked LinkedIn rows (link, in Google, duplicate, create)"
```

---

### Task 9: The seed script links and creates

**Files:**
- Modify: `scripts/outreach_seed.py`
- Test: `tests/test_outreach_seed.py`

**Interfaces:**
- Consumes: `outreach_seed.plan(...)`, `LINKEDIN_KINDS`, `SeedPlan.linkedin_of`, `contact_for` (Task 8); `outreach_repo.people_seed_rows`, `linkedin_repo.link_connection` (Task 8); `gc.list_url_index` (Task 6); `person_create.create` (Task 7); `outreach.ensure_contact` (existing).
- Produces:
  - `scripts.outreach_seed.SeedRun(plan, entered=0, linked=0, created=0, failures=[])`;
  - `run(...) -> SeedRun`;
  - `summary(r: SeedRun, *, dry_run: bool) -> str`;
  - `FROM_LINKEDIN_LABEL = "from-linkedin"`;
  - the process exits 1 when any create failed.

- [ ] **Step 1: Write the failing tests**

In `tests/test_outreach_seed.py`, replace everything from `class Conn:` to the end of the file with:

```python
class Conn:
    def __init__(self):
        self.rolled_back = 0
        self.commits = 0

    def rollback(self):
        self.rolled_back += 1

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, *, linkedin_rows=(), people_rows=(), google=None, create=None):
    repo = script.outreach_repo
    monkeypatch.setattr(repo, "email_seed_rows", lambda c: [email(1), email(2)])
    monkeypatch.setattr(repo, "linkedin_seed_rows", lambda c: list(linkedin_rows))
    monkeypatch.setattr(repo, "imessage_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "whatsapp_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "pipeline_person_ids", lambda c: {2})
    monkeypatch.setattr(repo, "people_seed_rows", lambda c: list(people_rows))
    monkeypatch.setattr(script.gc, "list_url_index", lambda: google or ({}, []))
    log = {"inserted": [], "linked": [], "created": []}
    monkeypatch.setattr(
        script.outreach,
        "ensure_contact",
        lambda c, pid, *, at, source: log["inserted"].append((pid, source)) or 1,
    )
    monkeypatch.setattr(
        script.linkedin_repo,
        "link_connection",
        lambda c, url, pid: log["linked"].append((url, pid)),
    )

    def fake_create(c, *, contact, labels):
        log["created"].append((contact["urls"][0]["value"], labels))
        if create:
            create(contact)
        return {"id": 100 + len(log["created"])}

    monkeypatch.setattr(script.person_create, "create", fake_create)
    return log


def _run(conn, *, dry_run=False):
    return script.run(lambda: conn, now=NOW, window_days=730, min_messages=10, dry_run=dry_run)


def test_dry_run_writes_nothing(monkeypatch):
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("a"), li("e", first="Dee", last="New")],
        people_rows=[person_row(5, "Pat Linked", url="linkedin.com/in/a")],
    )
    conn = Conn()
    r = _run(conn, dry_run=True)
    assert log == {"inserted": [], "linked": [], "created": []}
    assert conn.rolled_back and conn.commits == 0 and set(r.plan.candidates) == {1}
    text = script.summary(r, dry_run=True)
    assert "DRY RUN" in text
    assert "link 1, create 1, possible duplicate 0, in Google not synced 0" in text


def test_a_real_run_inserts_candidates_as_seed(monkeypatch):
    log = _wire(monkeypatch)
    r = _run(Conn())
    assert log["inserted"] == [(1, "seed")] and r.entered == 1


def test_link_enters_the_person_without_google(monkeypatch):
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("a")],
        people_rows=[person_row(5, "Pat Linked", url="linkedin.com/in/a")],
    )
    r = _run(Conn())
    assert log["linked"] == [("linkedin.com/in/a", 5)]
    assert (5, "seed") in log["inserted"]
    assert log["created"] == [] and r.linked == 1


def test_create_labels_links_and_commits_per_person(monkeypatch):
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New"), li("e", first="Eve", last="Other")],
    )
    conn = Conn()
    r = _run(conn)
    assert log["created"] == [
        ("https://www.linkedin.com/in/d", ["from-linkedin"]),
        ("https://www.linkedin.com/in/e", ["from-linkedin"]),
    ]
    assert log["linked"] == [("linkedin.com/in/d", 101), ("linkedin.com/in/e", 102)]
    assert (101, "seed") in log["inserted"] and (102, "seed") in log["inserted"]
    assert r.created == 2 and conn.commits == 3  # the candidates, then one per person


def test_one_failure_does_not_stop_the_run(monkeypatch):
    def create(contact):
        if contact["urls"][0]["value"].endswith("/d"):
            raise RuntimeError("quota")

    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New"), li("e", first="Eve", last="Other")],
        create=create,
    )
    conn = Conn()
    r = _run(conn)
    assert r.created == 1 and [name for name, _ in r.failures] == ["Dee New"]
    assert conn.rolled_back == 1
    assert log["linked"] == [("linkedin.com/in/e", 102)]
    assert "Failed:" in script.summary(r, dry_run=False)


def test_a_rerun_links_instead_of_creating(monkeypatch):
    log = _wire(monkeypatch, linkedin_rows=[li("d", first="Dee", last="New")])
    _run(Conn())
    created_id = log["linked"][0][1]
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New")],
        people_rows=[person_row(created_id, "Dee New", url="linkedin.com/in/d")],
    )
    r = _run(Conn())
    assert log["created"] == [] and log["linked"] == [("linkedin.com/in/d", created_id)]
    assert r.linked == 1


def test_an_orphaned_google_contact_is_reported_not_recreated(monkeypatch):
    # Review Focus 3: the create reached Google but the commit did not.
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New")],
        google=(
            {"linkedin.com/in/d": [{"resource_name": "people/c9", "display_name": "Dee New"}]},
            [{"resource_name": "people/c9", "display_name": "Dee New"}],
        ),
    )
    r = _run(Conn())
    assert log["created"] == [] and [o.kind for o in r.plan.linkedin] == ["in_google"]
    assert "In Google, not synced" in script.summary(r, dry_run=False)


def test_summary_lists_possible_duplicates_and_unlinked_handles():
    p = seed.SeedPlan(
        candidates={1: {"email"}},
        already_in={2},
        unlinked=[seed.Unlinked("whatsapp", "Lee Example", "+15550100009")],
        linkedin=[
            seed.LinkedInOutcome(
                "possible_duplicate",
                li("c", first="Jonathan", last="Smith"),
                candidates=[6],
                google_names=["Jon Smith"],
            )
        ],
    )
    text = script.summary(script.SeedRun(p), dry_run=True)
    assert "candidates 1" in text and "already in the pipeline 1" in text
    assert "Possible duplicate" in text and "person 6" in text and "Jon Smith" in text
    assert "Lee Example" in text and "creating-person" in text


def test_main_exits_1_when_a_create_failed(monkeypatch):
    _wire(monkeypatch, linkedin_rows=[li("d", first="Dee", last="New")], create=_raise)
    monkeypatch.setattr(script.otel, "setup_telemetry", lambda name: None)
    with pytest.raises(SystemExit) as e:
        script.main_with([], lambda: Conn())
    assert e.value.code == 1


def _raise(contact):
    raise RuntimeError("quota")
```

Add `import pytest` to the file's imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_seed.py -q`
Expected: FAIL. `script.gc`, `script.linkedin_repo`, `script.person_create` and `SeedRun` don't exist.

- [ ] **Step 3: Implement**

Replace `scripts/outreach_seed.py` from the module docstring to the end with:

```python
"""Propose referral candidates and insert them at `candidate`
(docs/superpowers/specs/2026-10-12-referral-outreach-tooling-design.md §7.1). Local only.

  python scripts/outreach_seed.py [--window-days 730] [--min-messages 10] [--dry-run]

Idempotent: a person already in the pipeline is never touched, so it never downgrades
anyone. A warm LinkedIn connection with no people row is linked to the row already
holding its URL, or created as a Google Contact labelled `from-linkedin` and entered
(docs/superpowers/specs/2026-10-13-linkedin-identity-design.md §5). Possible
duplicates and contacts Google has but people hasn't synced are listed, never
created. Each person is committed on its own, so one failure never undoes the rest;
the run exits 1 if any create failed. Names print to this terminal only.
"""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.google_contacts as gc
import clients.otel as otel
from repo import linkedin as linkedin_repo
from repo import outreach as outreach_repo
from services import outreach, outreach_seed, person_create

FROM_LINKEDIN_LABEL = "from-linkedin"


@dataclass
class SeedRun:
    plan: outreach_seed.SeedPlan
    entered: int = 0  # candidates newly entered at `candidate`, every source
    linked: int = 0
    created: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)


def _enter(conn: Any, profile_url: str, person_id: int, now: datetime) -> None:
    linkedin_repo.link_connection(conn, profile_url, person_id)
    outreach.ensure_contact(conn, person_id, at=now, source="seed")


def _reason(e: Exception) -> str:
    if isinstance(e, person_create.Duplicate):
        return f"already exists (candidates {e.candidates or 'in Google only'})"
    return f"{type(e).__name__}: {e}"


def run(
    get_conn: Callable[[], Any],
    *,
    now: datetime,
    window_days: int,
    min_messages: int,
    dry_run: bool,
) -> SeedRun:
    google_urls, google_contacts = gc.list_url_index()
    with get_conn() as conn:
        result = SeedRun(
            outreach_seed.plan(
                email_rows=outreach_repo.email_seed_rows(conn),
                linkedin_rows=outreach_repo.linkedin_seed_rows(conn),
                imessage_rows=outreach_repo.imessage_seed_rows(conn),
                whatsapp_rows=outreach_repo.whatsapp_seed_rows(conn),
                existing=outreach_repo.pipeline_person_ids(conn),
                now=now,
                window_days=window_days,
                min_messages=min_messages,
                people_rows=outreach_repo.people_seed_rows(conn),
                google_urls=google_urls,
                google_contacts=google_contacts,
            )
        )
        if dry_run:
            conn.rollback()
            return result
        for person_id in sorted(result.plan.candidates):
            if outreach.ensure_contact(conn, person_id, at=now, source="seed") is not None:
                result.entered += 1
        conn.commit()
        for o in result.plan.linkedin_of("link"):
            assert o.person_id is not None
            _enter(conn, o.row["profile_url"], o.person_id, now)
            conn.commit()
            result.linked += 1
        for o in result.plan.linkedin_of("create"):
            try:
                row = person_create.create(
                    conn,
                    contact=outreach_seed.contact_for(o.row),
                    labels=[FROM_LINKEDIN_LABEL],
                )
                _enter(conn, o.row["profile_url"], row["id"], now)
                conn.commit()
                result.created += 1
            except Exception as e:  # one person's failure never stops the run (design §5)
                conn.rollback()
                result.failures.append((o.row["full_name"], _reason(e)))
    return result


_TITLES = {
    "link": "Link",
    "create": "Create",
    "possible_duplicate": "Possible duplicate (not created)",
    "in_google": "In Google, not synced (not created; the nightly sync adopts it)",
}


def _linkedin_line(o: outreach_seed.LinkedInOutcome) -> str:
    who = f"  {o.row['full_name']}  {o.row['profile_url']}"
    if o.kind == "link":
        return f"{who}  → person {o.person_id}"
    if o.kind == "possible_duplicate":
        found = [f"person {i}" for i in o.candidates]
        found += [f"in Google, no row: {n}" for n in o.google_names]
        return f"{who}  ~ {', '.join(found)}"
    return who


def summary(r: SeedRun, *, dry_run: bool) -> str:
    p = r.plan
    lines = ["DRY RUN — nothing written"] if dry_run else []
    by_source = ", ".join(f"{s} {n}" for s, n in p.by_source().items())
    lines.append(
        f"candidates {len(p.candidates)} ({by_source}); already in the pipeline {len(p.already_in)}"
    )
    li = {k: p.linkedin_of(k) for k in outreach_seed.LINKEDIN_KINDS}
    lines.append(
        "LinkedIn without a people row: "
        f"link {len(li['link'])}, create {len(li['create'])}, "
        f"possible duplicate {len(li['possible_duplicate'])}, "
        f"in Google not synced {len(li['in_google'])}"
    )
    if not dry_run:
        lines.append(
            f"entered {r.entered}; linked {r.linked}; created {r.created}; "
            f"failed {len(r.failures)}"
        )
    for kind in ("link", "create", "possible_duplicate", "in_google"):
        if li[kind]:
            lines.append(f"{_TITLES[kind]}:")
            lines += [_linkedin_line(o) for o in li[kind]]
    if r.failures:
        lines.append("Failed:")
        lines += [f"  {name}  {reason}" for name, reason in r.failures]
    if p.unlinked:
        lines.append(
            f"{len(p.unlinked)} warm iMessage/WhatsApp contacts have no people row; "
            "add them with creating-person:"
        )
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
        r = run(
            get_conn,
            now=datetime.now(UTC),
            window_days=args.window_days,
            min_messages=args.min_messages,
            dry_run=args.dry_run,
        )
        print(summary(r, dry_run=args.dry_run))
    finally:
        otel.flush()
    if r.failures:
        sys.exit(1)


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
```

If `ruff` flags the `assert` in non-test code (rule S101 is not enabled in this repo today), replace it with `if o.person_id is None: continue`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_seed.py -q`
Expected: PASS, every test in the file.

- [ ] **Step 5: Commit**

```bash
git add scripts/outreach_seed.py tests/test_outreach_seed.py
git commit -m "outreach seed: link and create LinkedIn-only people, one commit each"
```

---

### Task 10: `outreach today` channel column

**Files:**
- Modify: `services/outreach_today.py:160-164`
- Test: `tests/test_outreach_today.py`

**Interfaces:**
- Produces: `_CHANNELS_WIDTH` (module constant, 38).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_outreach_today.py`:

```python
def test_last_starts_in_one_column_whatever_the_channels():
    all_four = _queued(pid=8, rank=2) | {
        "channels": {
            "email": True,
            "imessage": True,
            "linkedin": "linkedin.com/in/x",
            "whatsapp": True,
        }
    }
    one = _queued(pid=9, rank=None) | {
        "channels": {"email": True, "imessage": False, "linkedin": False, "whatsapp": False}
    }
    t = build(_counts(next_up=[all_four, one]), TARGETS, now=_at(date(2026, 10, 14), 10))
    lines = [line for line in render(t).splitlines() if " last 2026-09-30" in line]
    assert len(lines) == 2
    assert "email · imessage · linkedin · whatsapp last 2026-09-30" in lines[0]
    assert lines[0].index(" last ") == lines[1].index(" last ")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_outreach_today.py -q`
Expected: FAIL. The four-channel line reads `…whatsapplast 2026-09-30` (the padding is only 24).

- [ ] **Step 3: Implement**

In `services/outreach_today.py`, above `_brief_line`:

```python
# Wide enough for every channel at once, so `last` lines up whatever mix a person has
# (linkedin-identity design §6). The order is services/outreach.py::_channels's.
_CHANNELS_WIDTH = len(" · ".join(("email", "imessage", "linkedin", "whatsapp")))
```

and change the return line of `_brief_line` to:

```python
    return f"  {n}. {_name(item['person']):<18}{channels:<{_CHANNELS_WIDTH}} last {last}{rank}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_today.py tests/test_outreach_cli.py -q`
Expected: PASS. If a CLI test asserts the old `today` line verbatim, update its expected string to the new column. Don't change the code back.

- [ ] **Step 5: Commit**

```bash
git add services/outreach_today.py tests/test_outreach_today.py
git commit -m "outreach today: pad the channel column for all four channels"
```

---

### Task 11: Docs and skills

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.claude/skills/people-architecture/SKILL.md`, `.claude/skills/creating-person/SKILL.md`, `.claude/skills/fetching-person/SKILL.md`, `.claude/skills/editing-person/SKILL.md`, `.claude/skills/searching-people/SKILL.md`, `.claude/skills/importing-linkedin/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`, `.claude/skills/working-outreach-queue/SKILL.md`
- Modify: `docs/superpowers/specs/2026-10-13-linkedin-identity-design.md` (status line)

**Interfaces:** none (documentation only).

- [ ] **Step 1: `CLAUDE.md`**

Make each of these edits. Read the surrounding paragraph first and match its voice:
- **Stack table, Database row:** after `editable-contact-field columns \`phone_numbers\`, \`company\`, \`job_title\`, \`google_fields\``, add `, and the identifier \`linkedin_url\``.
- **Stack table, Sync CF row:** change "adopts an email-less contact with a usable phone number" to "adopts an email-less contact with a usable phone number or a LinkedIn profile URL".
- **Code layout:** for `services/identity.py`, change the comment to `classify(ident) → email | linkedin | phone | id (spec §5.1) — pure, no I/O`. For `services/outreach_seed.py`, change it to `pure: warm-pool bars over raw rows → SeedPlan; unlinked LinkedIn rows → link / in_google / possible_duplicate / create`. For `scripts/outreach_seed.py`, change it to `propose outreach candidates, link or create LinkedIn-only people — --dry-run`.
- **Source-of-truth table:** add a row after `phone_numbers, company, job_title, google_fields`:

  `| \`linkedin_url\` | Google Contacts (\`urls\`) | Google → DB on link, nightly sync, and after every \`PATCH\`, normalized to \`linkedin.com/in/<slug>\`; dropped (never written) when another row already holds it. Never pushed to HubSpot. |`
- **Identity section:** after the "Piece 3" paragraphs, add:

  ```markdown
  **Piece 4 — LinkedIn profile URL (2026-10-13,**
  `docs/superpowers/specs/2026-10-13-linkedin-identity-design.md`**).**
  `people.linkedin_url` (UNIQUE, normal form `linkedin.com/in/<slug>`) is a third
  identifier: `people_has_an_identifier` is now email, phone, or LinkedIn URL.
  `contact_fields.derive` reads it from the Google contact's `urls`;
  `apply_person` adopts a contact whose only identifier is the URL, and drops a URL
  another row already holds on every write path (a contact left with nothing is
  counted `linkedin_conflict`). `{ident}` accepts a profile URL — the API routes are
  `{ident:path}`, so a percent-encoded URL works. `POST /people` accepts a URL-only
  contact and refuses a duplicate URL on either side (`list_url_index` is a full
  listing; `searchContacts` doesn't index `urls`). The outreach seed creates a
  Google contact (label `from-linkedin`) for every warm LinkedIn connection with
  no row, unless it may duplicate someone. LinkedIn-only people are never mirrored
  to HubSpot.
  ```
- **Local dev, Referral outreach block:** under `outreach_seed.py --dry-run`, add the comment `# also lists LinkedIn link/create/possible duplicate/in Google`.

- [ ] **Step 2: Skills**

Edit each skill. Search for the quoted phrase, and replace or extend it with the sentence given:
- `fetching-person` (line ~9, "have the email, phone, or id"): change to "have the email, phone, LinkedIn profile URL, or id". Where the skill documents the response, add: `linkedin_url` — the person's normalized LinkedIn profile (`linkedin.com/in/<slug>`) or `null`. Where it shows how to call the API, add an example: `curl … "$PEOPLE_API/people/linkedin.com%2Fin%2Fjane-example"` (a raw `linkedin.com/in/jane-example` also works).
- `editing-person` (line ~180, "unknown email, phone, or id"): change to "unknown email, phone, LinkedIn URL, or id". Next to the phone-removal 409, add: "A `urls` list that drops a LinkedIn-only person's profile URL is also a `409` (`a person must keep an email address, a phone number or a LinkedIn profile URL`). A `urls` PATCH replaces the whole list, so include the contact's existing URLs."
- `searching-people` (line ~195, "take the email, phone, or id"): change to "take the email, phone, LinkedIn URL, or id".
- `creating-person`: where it says a contact needs an email or a phone, change it to "an email, a phone that normalizes to E.164, or a LinkedIn profile URL (`urls: [{\"value\": \"https://www.linkedin.com/in/<slug>\", \"type\": \"profile\"}]`)". Add that a duplicate URL is refused like a duplicate email or phone.
- `importing-linkedin`: where it explains matching, add that connections link by exact profile URL first (`match_method = 'url'`, printed as `url-matched`), then email, then name.
- `querying-people-db`: in the `people` row of the table (line ~55), add `linkedin_url` (nullable, **unique**, `linkedin.com/in/<slug>`). Change the identifier constraint wording to "email, a phone, or a LinkedIn URL".
- `people-architecture`: wherever it lists the identifiers or the adoption rule, add the LinkedIn URL with the same one-line rule as `CLAUDE.md`'s Piece 4.
- `working-outreach-queue` (line ~100, "`creating-person` (needs an email or an E.164 phone)"): change to "`creating-person` (needs an email, an E.164 phone, or a LinkedIn profile URL)". Where it describes the seed, add: "The seed creates LinkedIn-only people itself (label `from-linkedin`) and lists possible duplicates for Ben to resolve by adding the LinkedIn URL to the existing contact with `editing-person`. A LinkedIn-only person has no email, so their brief is never email-proven: record it with `outreach mark <person> briefed --channel linkedin` after Ben confirms."

- [ ] **Step 3: Spec status**

In `docs/superpowers/specs/2026-10-13-linkedin-identity-design.md`, change `**Status:** design.` to `**Status:** implemented (PR pending).` and leave the rest of the status paragraph as it is.

- [ ] **Step 4: Check the docs name only real things**

Run:
```bash
grep -rn "linkedin_url\|list_url_index\|check_identifier_kept\|link_connection\|from-linkedin" CLAUDE.md .claude/skills/ | cut -c1-160
```
Expected: every name it prints exists in the code from Tasks 1–9.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .claude/skills/ docs/superpowers/specs/2026-10-13-linkedin-identity-design.md
git commit -m "docs: LinkedIn profile URL as a person identifier"
```

---

### Task 12: Whole-branch verification and the PR

**Files:** none new.

- [ ] **Step 1: Full checks with the real-Postgres tests**

```bash
docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
docker stop people-test-pg
```

Expected: all pass, with `tests/test_schema.py` and `tests/test_outreach_db.py` **not** skipped (check the summary's skip count).

- [ ] **Step 2: Public-repo sweep**

```bash
git diff origin/main --stat
git diff origin/main | grep -niE "linkedin\.com/in/[a-z0-9-]+" | grep -viE "jane-example|jane-other|bob-example|alice-example|me-example|nobody-example|someone-else|/in/(a|b|c|d|e|x|l[0-9]+|5550100001|lee|<slug>)\b|<slug>"
```

Expected: the second command prints nothing. Anything it prints is a real profile slug and must be replaced with a placeholder.

- [ ] **Step 3: Dry run locally against production, read-only**

With `.env` from `scripts/fetch-env.sh`:

```bash
.venv/bin/python scripts/outreach_seed.py --dry-run | head -5
```

Expected: it prints the candidate line and the four LinkedIn counts, and writes nothing. This runs before production is migrated (Task 13), so `people_seed_rows` fails on the missing `linkedin_url` column. If it does, say so in the PR and skip this step; do not migrate production yourself.

- [ ] **Step 4: Open the PR**

Use the `/pr-open` skill on branch `linkedin-identity` (base `main`). The description must state:
- **Run `scripts/migrate_db.py` against production before merging.** The functions and people-api read and write `people.linkedin_url` as soon as they deploy.
- The rollout steps of Task 13.
- Decisions 1–14 above, briefly.
- Whether the real-Postgres tests ran locally (Docker) or only in CI.

---

### Task 13: Rollout (Ben runs these; production-touching)

Not for an agent to run unattended. Listed so the PR carries them (spec §8).

1. **Migrate production before merging**, from the branch (needs `.env` from `scripts/fetch-env.sh`):
   ```bash
   .venv/bin/python scripts/migrate_db.py
   ```
   It only adds a nullable column and its UNIQUE index, and loosens the CHECK, so the code already deployed keeps working.
2. **Merge the PR.** `deploy.yml` and `deploy-api.yml` deploy the functions and people-api. The nightly sync starts deriving `linkedin_url`.
3. **One full sync**, so existing contacts that carry a LinkedIn URL get it on their row, and URL-only contacts are adopted:
   ```bash
   .venv/bin/python scripts/clear_sync_token.py
   gcloud scheduler jobs run people-sync --location us-central1
   ```
   Then check the run's counts with `fetch-people-logs`. `linkedin_conflict` counts only contacts the conflict left with no identifier (a contact that keeps an email or phone has a claimed URL dropped silently), so 0 does not prove no URL is shared. Merge any it reports in Google before step 6.
4. **Re-import LinkedIn** (optional; it keeps the snapshot's match counts truthful):
   ```bash
   .venv/bin/python scripts/import_linkedin.py <latest export>
   ```
5. **Dry run and review:**
   ```bash
   .venv/bin/python scripts/outreach_seed.py --dry-run
   ```
   - For each possible duplicate that really is the same person, add the LinkedIn URL to the existing contact with `editing-person`. A `urls` PATCH replaces the whole list, so include the contact's existing URLs. Re-run the dry run; the person now shows as `link`.
   - An `in Google, not synced` entry clears after the next nightly sync, or at once with `POST /people/{ident}/sync` on that contact.
6. **Seed for real**, then reassess the queue:
   ```bash
   .venv/bin/python scripts/outreach_seed.py
   .venv/bin/python scripts/outreach.py queue --stage candidate
   ```
   The candidate count should grow by the `entered` count plus the people newly linked or created who weren't already in the pipeline. Then reassess with the `working-outreach-queue` skill. New contacts carry the `from-linkedin` label in Google Contacts.
