# Adopt Email-less Google Contacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop skipping the 553 Google contacts that have no email address — adopt the 449 with a usable phone number as eligible `people` rows, count the 104 that cannot be adopted, and make phone-only people readable and editable.

**Architecture:** Delete the email gate in `apply_person`'s creation path and create the row keyed on `google_resource_name` alone; clear the stored Google sync token once so the next nightly run is a full pass. Three supporting fixes let a row with no email actually work end to end: `_norm(None)` must be NULL not `""`, `sync_one` must re-fetch by id, and the email add-only rule must not demand a keyed address that does not exist.

**Tech Stack:** Python 3.13, Postgres (pg8000 on Cloud SQL, psycopg3 locally), Google People API v1, FastAPI, pytest, Docker (throwaway Postgres for constraint tests).

**Spec:** `docs/superpowers/specs/2026-09-28-adopt-emailless-contacts-design.md` — read it before Task 1. This is **piece 2 of 3**; piece 3 (`POST /people`) is a separate spec and is NOT in this plan.

## Global Constraints

- **Branch:** `adopt-emailless-contacts` (exists, spec committed). Never commit to `main`; open the PR with `/pr-open`.
- **NO SCHEMA CHANGE.** Every column and constraint this piece needs already exists. A task that edits `repo/schema.sql` is out of scope — say so rather than doing it.
- **DEPLOY ORDER: code first, then data.** Merge and deploy, *then* clear the sync token. New code against current data is a no-op. The inverse order caused a production outage during piece 1.
- **`GET /people/{email}` is a production contract.** Inbox's classify-time lookup calls it. Its behavior must not change.
- **Adopted rows:** `email IS NULL`, `phone_numbers` populated, `eligible = TRUE`, `automated = FALSE`, counters 0.
- **"Parseable phone" means `services.imessage_export.normalize_handle` returns a value** — the same rule the iMessage import uses, so adoption and matching agree. A contact whose only phone is a short code is NOT adoptable.
- **No contact names, phone numbers, or addresses in logs or committed files.** The repo is public; the skipped contacts are reported as a COUNT only.
- **Phone-only people never reach HubSpot** — `eligible_not_in_hubspot` already excludes rows without an email. Do not change that guard.
- **Layer rules** (CLAUDE.md): `clients/` I/O only; `repo/` DB-only taking an open connection; `services/` pure or orchestration; `api/routers/` thin transport.
- **Python 3.13:** `X | None`, never `Optional[X]`.
- **Every task ends green:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
- **Tests never** touch the production database or the real Google API. Test data uses `+1555010xxxx` and `example.com` only.
- **Commit trailers** on every commit:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01HYuAELaRczksHnqP7uo6yo`

## Review Focus

Five failure modes found by reading the current code against the spec. Each is real today, and each line names the task that owns its test.

1. **A second email-less adoption in the same run aborts the sync.** `_norm(None)` returns `""`, which is not NULL, so the CHECK passes but the UNIQUE index collides on the second row. One adoption looks fine; 449 do not. *Test in Task 1: adopt TWO email-less contacts against real Postgres.*
2. **`PATCH` on a phone-only person returns a stale row.** `sync_one` ends with `people.get(conn, row["email"])`, which is `None` for these people, so it silently falls back to the pre-edit row — the caller sees their change missing. *Test in Task 3.*
3. **Adding an email to a phone-only contact is always rejected.** `check_email_addition` unions `normalize(keyed_email)` into the must-survive set; with no keyed email that is `""`, which no submission contains, so it raises every time. *Test in Task 3.*
4. **A contact whose only phone is a short code must be skipped, not adopted with an empty phone list** — otherwise it violates the CHECK constraint at write time and aborts the run. *Test in Task 2.*
5. **An adopted contact on the NEXT sync run must take the update path, not create a duplicate.** It has no email, so only `google_resource_name` can find it. *Test in Task 2.*

---

## Task 1: `_norm` and `create_from_google` accept no email

**Files:**
- Modify: `repo/people.py:19-20` (`_norm`), `create_from_google`
- Modify: `tests/test_repo_people.py`, `tests/test_schema.py`

**Interfaces:**
- Produces: `_norm(email: str | None) -> str | None` (NULL for None or blank); `create_from_google(conn, email: str | None, *, display_name, resource_name, etag, notes, relationship_label, phone_numbers, company, job_title, google_fields) -> dict`. Consumed by Task 2.

- [ ] **Step 1: Write the failing unit tests**

In `tests/test_repo_people.py`:

```python
def test_norm_returns_none_for_missing_email():
    # Review Focus 1: "" is not NULL — it passes the CHECK and collides on UNIQUE.
    assert people._norm(None) is None
    assert people._norm("   ") is None
    assert people._norm(" Alice@Example.COM ") == "alice@example.com"


def test_create_from_google_accepts_no_email():
    conn = FakeConn(results=[[{"id": 5, "email": None}]])
    row = people.create_from_google(
        conn,
        None,
        display_name="Alice Example",
        resource_name="people/c1",
        etag="e1",
        notes=None,
        relationship_label=None,
        phone_numbers=["+15550100001"],
        company=None,
        job_title=None,
        google_fields={},
    )
    assert row["email"] is None
    sql, params = conn.calls[0]
    assert "INSERT INTO people" in sql
    assert params[0] is None          # email bound as NULL, never ""
```

- [ ] **Step 2: Write the failing real-Postgres test**

In `tests/test_schema.py` (its `conn` fixture applies `repo/schema.sql` and skips without `TEST_DATABASE_URL`):

```python
def test_two_emailless_people_coexist(conn):
    """Review Focus 1: the case that fails if _norm(None) returns ''."""
    for rn, phone in (("people/c1", "+15550100001"), ("people/c2", "+15550100002")):
        conn.execute(
            "INSERT INTO people (email, first_seen, eligible, automated,"
            " google_resource_name, phone_numbers)"
            " VALUES (NULL, now(), TRUE, FALSE, %s, %s)",
            (rn, [phone]),
        )
    assert conn.execute("select count(*) from people where email is null").fetchone()[0] == 2


def test_empty_string_email_would_collide(conn):
    """Documents WHY NULL matters: two empty strings are not distinct."""
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('', now())")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO people (email, first_seen) VALUES ('', now())")
```

- [ ] **Step 3: Run both to verify they fail**

```bash
.venv/bin/pytest tests/test_repo_people.py -k norm -v
docker run -d --rm --name pg-t1 -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
until docker exec pg-t1 pg_isready -q; do sleep 1; done
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -k emailless -v
```

Expected: the `_norm` test FAILS (`'' is not None`); the schema tests PASS already (the constraint permits NULLs) — they are regression guards, so note that in the report rather than forcing a failure.

- [ ] **Step 4: Implement**

```python
def _norm(email: str | None) -> str | None:
    """None for a missing or blank address. NOT "" — an empty string passes the
    people_has_an_identifier CHECK and then collides on the UNIQUE index, so the
    second email-less adoption would abort the sync (spec §5.2.1)."""
    normalized = (email or "").strip().lower()
    return normalized or None
```

Then audit every `_norm` caller. `upsert_inbound`, `upsert_outbound`, `set_flags`, `get`, `set_hubspot`, `clear_hubspot` all receive a real address from an event or a route; a `None` there was already meaningless, and returning `None` makes it fail loudly at the database instead of silently matching a `""` row. Change `create_from_google`'s `email` parameter to `str | None` and leave its `ON CONFLICT (email)` clause alone — a NULL email never conflicts, which is exactly right.

- [ ] **Step 5: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -v
docker stop pg-t1
```

- [ ] **Step 6: Commit**

```bash
git add repo/people.py tests/test_repo_people.py tests/test_schema.py
git commit -m "feat: people rows without an email address"
```

---

## Task 2: Adopt in `apply_person`, count in `run_sync`

**Files:**
- Modify: `services/google_contacts_sync.py` (`apply_person` creation path, `run_sync` counts)
- Modify: `tests/test_google_contacts_sync.py`

**Interfaces:**
- Consumes: Task 1's `create_from_google(conn, None, ...)`.
- Produces: `apply_person` returns `"skipped"` for an unadoptable contact; `run_sync`'s counts dict includes `"skipped"`.

- [ ] **Step 1: Write the failing tests**

Follow the monkeypatch style already in `tests/test_google_contacts_sync.py`.

```python
def person(rn="people/c1", email=None, phones=(), name="Alice Example"):
    p = {"resourceName": rn, "etag": "e1", "names": [{"displayName": name}]}
    if email:
        p["emailAddresses"] = [{"value": email}]
    if phones:
        p["phoneNumbers"] = [{"value": v} for v in phones]
    return p


def test_emailless_contact_with_a_phone_is_adopted(monkeypatch):
    created = {}
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(gsync.people, "create_from_google",
                        lambda conn, email, **kw: created.update({"email": email, **kw}))
    kind = gsync.apply_person(None, person(phones=["(555) 010-0001"]), {})
    assert kind == "created"
    assert created["email"] is None
    assert created["phone_numbers"] == ["+15550100001"]


def test_emailless_contact_without_a_usable_phone_is_skipped(monkeypatch):
    # Review Focus 4: a short code is not a usable phone; adopting would violate
    # the CHECK constraint and abort the run.
    calls = []
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(gsync.people, "create_from_google",
                        lambda *a, **k: calls.append(1))
    assert gsync.apply_person(None, person(phones=["262966"]), {}) == "skipped"
    assert gsync.apply_person(None, person(phones=[]), {}) == "skipped"
    assert calls == []


def test_adopted_contact_updates_on_the_next_run(monkeypatch):
    # Review Focus 5: only google_resource_name can find it — no email exists.
    updated = {}
    monkeypatch.setattr(gsync.people, "get_by_google_resource",
                        lambda conn, rn: {"id": 7, "email": None, "google_resource_name": rn})
    monkeypatch.setattr(gsync.people, "update_from_google",
                        lambda conn, rn, **kw: updated.update({"rn": rn, **kw}))
    assert gsync.apply_person(None, person(phones=["+15550100001"]), {}) == "updated"
    assert updated["rn"] == "people/c1"


def test_contact_with_an_email_behaves_exactly_as_before(monkeypatch):
    created = {}
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(gsync.people, "get", lambda conn, email: None)
    monkeypatch.setattr(gsync.people, "create_from_google",
                        lambda conn, email, **kw: created.update({"email": email, **kw}))
    assert gsync.apply_person(None, person(email="alice@example.com"), {}) == "created"
    assert created["email"] == "alice@example.com"


def test_run_sync_counts_skipped(monkeypatch):
    monkeypatch.setattr(gsync.gc, "list_connections",
                        lambda token: ([person(phones=[]), person(rn="people/c2", phones=[])], "tok"))
    monkeypatch.setattr(gsync.gc, "list_groups", lambda: {})
    monkeypatch.setattr(gsync.sync_state, "get_token", lambda conn: None)
    monkeypatch.setattr(gsync.sync_state, "set_token", lambda conn, t, s: None)
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: None)
    counts = gsync.run_sync(None)
    assert counts["skipped"] == 2
    assert counts["created"] == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_google_contacts_sync.py -v
```

Expected: FAIL — `apply_person` returns `None` for an email-less contact, and `counts` has no `"skipped"` key.

- [ ] **Step 3: Implement**

In `apply_person`, replace the creation path's email gate:

```python
    email = primary_email(person)
    derived = contact_fields.derive(person)
    if not email:
        # Adopt a contact that has no email address, provided it has a phone we
        # can normalize — people_has_an_identifier requires one or the other
        # (spec §5.1). Contacts with neither are counted, never written.
        if not derived["phone_numbers"]:
            return "skipped"
        people.create_from_google(
            conn,
            None,
            display_name=display_name(person),
            resource_name=rn,
            etag=person.get("etag"),
            notes=notes(person),
            relationship_label=label,
            **derived,
        )
        return "created"
```

Keep the existing email path below it unchanged, and reuse `derived` in it rather than calling `contact_fields.derive(person)` twice. Add `"skipped": 0` to `run_sync`'s counts dict.

`otel.google_sync_changes` already labels by `kind`, so `skipped` becomes a metric dimension with no extra code.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add services/google_contacts_sync.py tests/test_google_contacts_sync.py
git commit -m "feat: adopt Google contacts that have no email address"
```

---

## Task 3: Make a phone-only person editable

**Files:**
- Modify: `services/person_edit.py` (`update` signature and lookup), `services/google_contacts_sync.py` (`sync_one`), `services/contact_fields.py` (`check_email_addition`)
- Modify: `api/routers/people.py` (two `person_edit.update` call sites)
- Modify: `tests/test_person_edit.py`, `tests/test_contact_fields.py`, `tests/test_api.py`

**Interfaces:**
- Produces: `person_edit.update(conn, person_id: int, *, notes=None, relationship_label=None, contact=None) -> dict`; `check_email_addition(live, submitted, keyed_email: str | None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_contact_fields.py
def test_email_addition_allows_the_first_address_for_a_person_with_none():
    # Review Focus 3: with no keyed email, normalize(None) is "" — which no
    # submission contains, so the old code raised on every such request.
    cf.check_email_addition({}, [{"value": "new@example.com"}], None)


def test_email_addition_still_blocks_removal_for_a_person_with_one():
    live = {"emailAddresses": [{"value": "alice@example.com"}]}
    with pytest.raises(cf.EmailRuleError):
        cf.check_email_addition(live, [], "alice@example.com")
```

```python
# tests/test_person_edit.py
def test_update_is_keyed_by_person_id(gc, monkeypatch):
    seen = {}
    monkeypatch.setattr(person_edit.people, "get_by_id",
                        lambda conn, pid: seen.setdefault("pid", pid) and None or
                        {"id": pid, "email": "alice@example.com",
                         "google_resource_name": "people/c1"})
    person_edit.update(None, 42, notes="hi")
    assert seen["pid"] == 42


def test_update_raises_not_found_for_an_unknown_id(monkeypatch):
    monkeypatch.setattr(person_edit.people, "get_by_id", lambda conn, pid: None)
    with pytest.raises(person_edit.NotFound):
        person_edit.update(None, 999, notes="hi")


def test_update_works_for_a_person_with_no_email(gc, monkeypatch):
    monkeypatch.setattr(person_edit.people, "get_by_id", lambda conn, pid: {
        "id": 7, "email": None, "google_resource_name": "people/c1"})
    person_edit.update(None, 7, contact={"phoneNumbers": [{"value": "+15550100002"}]})
    assert gc.updates and "phoneNumbers" in gc.updates[0][2]
```

```python
# tests/test_google_contacts_sync.py
def test_sync_one_refetches_by_id(monkeypatch):
    # Review Focus 2: row["email"] is None for a phone-only person, so the old
    # people.get(conn, row["email"]) silently returned the stale pre-edit row.
    monkeypatch.setattr(gsync.gc, "get_person", lambda rn: person(phones=["+15550100001"]))
    monkeypatch.setattr(gsync.gc, "list_groups", lambda: {})
    monkeypatch.setattr(gsync, "apply_person", lambda conn, p, g: "updated")
    monkeypatch.setattr(gsync.people, "get_by_id", lambda conn, pid: {"id": pid, "fresh": True})
    out = gsync.sync_one(None, {"id": 7, "email": None, "google_resource_name": "people/c1"})
    assert out["fresh"] is True
```

```python
# tests/test_api.py
def test_patch_a_phone_only_person_by_phone(client, monkeypatch):
    """Newly reachable: resolution finds the row, person_edit takes its id."""
    captured = {}
    monkeypatch.setattr("api.routers.people.person_edit.update",
                        lambda conn, pid, **kw: captured.setdefault("pid", pid) or
                        person_row(id=8, email=None))
    monkeypatch.setattr("api.routers.people.people_repo.get_by_phone",
                        lambda conn, e164: [person_row(id=8, email=None)])
    r = client.patch("/people/%2B15550100001", json={"notes": "met at a conference"})
    assert r.status_code == 200
    assert captured["pid"] == 8
```

Adapt each fixture to the real conventions in those files (`gc` fake, `_wire`, `person_row`) — the assertions are what matter.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_person_edit.py tests/test_contact_fields.py -v
```

Expected: FAIL — `update` still takes an email; `check_email_addition` raises with a `None` keyed email.

- [ ] **Step 3: Implement**

- `person_edit.update(conn, person_id: int, ...)`: `row = people.get_by_id(conn, person_id)`; `NotFound(str(person_id))` when absent. Everything else — Google first, then refresh, `NotLinked`, the error mapping — unchanged. Pass `row["email"]` to `check_email_addition` as before; it is now allowed to be `None`.
- `check_email_addition(live, submitted, keyed_email: str | None)`: only add the keyed address to the must-survive set when it is truthy after normalization. Every existing address in `live` is still required, so removals are still refused.
- `sync_one`: end with `people.get_by_id(conn, row["id"]) or row`.
- `api/routers/people.py`: both `person_edit.update(conn, email, ...)` call sites become `person_edit.update(conn, target["id"], ...)`, where `target` is the already-resolved row. This also removes the redundant re-fetch the piece-1 review flagged.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/mypy services/ api/
```

- [ ] **Step 5: Commit**

```bash
git add services/person_edit.py services/google_contacts_sync.py services/contact_fields.py api/routers/people.py tests/
git commit -m "feat: edit a person who has no email address"
```

---

## Task 4: `scripts/clear_sync_token.py`

**Files:**
- Create: `scripts/clear_sync_token.py`
- Create: `tests/test_clear_sync_token.py`

**Interfaces:**
- Consumes: `repo/sync_state.py::get_token`, `set_token`.
- Produces: a CLI that clears the stored Google sync token so the next `run_sync` is a full pass.

- [ ] **Step 1: Write the failing test**

```python
from scripts import clear_sync_token


class FakeConn:
    def __init__(self):
        self.calls = []
        self.committed = False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        class C:
            def fetchone(self_inner):
                return {"sync_token": "old-token"}
        return C()

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_clears_the_token_and_reports_it(capsys):
    conn = FakeConn()
    clear_sync_token.run(lambda: conn)
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "UPDATE sync_state" in joined or "INSERT INTO sync_state" in joined
    out = capsys.readouterr().out
    assert "cleared" in out.lower()
    assert "old-token" not in out          # a token is a credential-ish value
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_clear_sync_token.py -v
```

Expected: FAIL — `ModuleNotFoundError: scripts.clear_sync_token`.

- [ ] **Step 3: Implement**

Follow `scripts/import_linkedin.py`'s shape: shebang, docstring citing spec §6.2, `sys.path` insert, `load_dotenv()`, then imports. `run(get_conn)` reads the current token, calls `sync_state.set_token(conn, None, "cleared for full resync")`, commits, and prints whether a token was present — never the token's value. `main()` calls `run(clients.db.get_conn)`.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add scripts/clear_sync_token.py tests/test_clear_sync_token.py
git commit -m "feat: clear the Google sync token for a full resync"
```

---

## Task 5: Docs and skills

**Files:**
- Modify: `.claude/skills/people-architecture/SKILL.md`, `querying-people-db/SKILL.md`, `editing-person/SKILL.md`, `fetching-person/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `people-architecture`**

The sync now adopts email-less Google contacts that have a usable phone number, counts the rest as `skipped`, and adopted people are `eligible` but can never reach HubSpot. Note `scripts/clear_sync_token.py` and what it is for.

- [ ] **Step 2: Update `querying-people-db`**

Adopted rows are `email IS NULL AND google_resource_name IS NOT NULL` — the rollback selector too. Include the duplicate-detection query from spec §5.4 verbatim, and note it should return zero rows.

- [ ] **Step 3: Update the person skills**

`editing-person`: a person with no email can be edited by phone or id, and a *first* email address may be added to them (later additions still cannot remove one). `fetching-person`: `email` may be null; adopted contacts have zero message counts because no mail has been seen from them.

- [ ] **Step 4: Update `CLAUDE.md`**

The adoption rule in the Stack/sync description, `scripts/clear_sync_token.py` in the code layout, and a source-of-truth note that an adopted row's identity is its phone number until an email appears.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/ruff check .
git add .claude/skills CLAUDE.md
git commit -m "docs: adopting email-less contacts"
```

---

## Task 6: Deploy, adopt, verify, PR

**This task's order is load-bearing: code first, then data.**

- [ ] **Step 1: Open the PR and merge it**

Use `/pr-open`, wait for CI, merge. The deploy must land BEFORE the token is cleared — new code against current data adopts nothing, which is the safe direction.

- [ ] **Step 2: Capture the before-state**

```bash
(set -a; source .env; set +a; .venv/bin/python -c "
from clients.db import get_conn
with get_conn() as c:
    for q, label in [('select count(*) n from people', 'people'),
                     ('select count(*) n from people where email is null', 'email-less'),
                     ('select count(*) n from imessage_handles where person_id is not null', 'handles linked')]:
        print(f'{label:16}', c.execute(q).fetchone()['n'])
")
```

- [ ] **Step 3: Clear the token and run the sync**

```bash
(set -a; source .env; set +a; .venv/bin/python scripts/clear_sync_token.py)
TOKEN=$(gcloud secrets versions access latest --secret people-sync-token --project bens-project-462804)
curl -s -X POST "$(cd ~/src/people/terraform && terraform output -raw people_sync_url)" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

If the sync URL output is not available, run the handler locally per the `testing-people-handlers` skill instead. Read the returned counts against spec §6.3: ~449 created, ~104 skipped, ~397 updated.

- [ ] **Step 4: Verify the after-state**

```bash
(set -a; source .env; set +a; .venv/bin/python -c "
from clients.db import get_conn
with get_conn() as c:
    print('people            :', c.execute('select count(*) n from people').fetchone()['n'], '(expect ~983)')
    print('email-less        :', c.execute('select count(*) n from people where email is null').fetchone()['n'], '(expect ~449)')
    print('broken rows       :', c.execute('select count(*) n from people where email is null and cardinality(phone_numbers) = 0').fetchone()['n'], '(MUST be 0)')
    print('hubspot queue     :', c.execute('select count(*) n from people where eligible and email is null and hubspot_contact_id is not null').fetchone()['n'], '(MUST be 0)')
    dupes = c.execute(\"\"\"
        select count(*) n from people a
        join jsonb_array_elements(a.google_fields -> 'emailAddresses') e on true
        join people p on lower(p.email) = lower(e ->> 'value')
        where a.email is null and p.id <> a.id
    \"\"\").fetchone()['n']
    print('duplicate pairs   :', dupes, '(expect 0)')
")
```

- [ ] **Step 5: Re-run the iMessage importer and check the payoff**

```bash
(set -a; source .env; set +a; .venv/bin/python scripts/import_imessage.py)
(set -a; source .env; set +a; .venv/bin/python -c "
from clients.db import get_conn
with get_conn() as c:
    print('handles linked to a person:', c.execute('select count(*) n from imessage_handles where person_id is not null').fetchone()['n'], '(was 76; 159 were candidates)')
")
```

If this number barely moves, adoption did not do what the spec claims — investigate before doing anything else.

- [ ] **Step 6: Spot-check a phone-only person through the API**

Run the API locally, `GET` an adopted person by phone and by id, and `PATCH` a note onto them — the path that was unreachable before this piece. Redact personal values from any report.

---

## Self-Review

**Spec coverage:** §5.1 adoption → Task 2; §5.2 adopted-row shape → Tasks 1, 2; §5.2.1 `_norm` → Task 1; §5.3 editing → Task 3; §5.4 duplicate detection → Tasks 5, 6; §5.5 skipped count → Task 2; §6.1 deploy order → Task 6; §6.2 token script → Task 4; §6.3 expected results → Task 6; §6.4 rollback → Task 5 (documented); §7 testing → spread across Tasks 1–3.

**Type consistency:** `_norm(email: str | None) -> str | None` (Task 1) matches `create_from_google`'s `email: str | None` and Task 2's `create_from_google(conn, None, ...)`. `person_edit.update(conn, person_id: int, ...)` (Task 3) matches the router's `target["id"]`. `check_email_addition(..., keyed_email: str | None)` (Task 3) matches `row["email"]` being nullable.

**Review Focus coverage:** (1) two adoptions → Task 1. (2) stale `sync_one` row → Task 3. (3) email rule with no keyed address → Task 3. (4) short-code-only contact skipped → Task 2. (5) adopted contact updates on the next run → Task 2.

**Known gap, deliberate:** the 17 contacts whose phones do not parse are counted with the 87 as `skipped`, with no breakdown between the two reasons. Distinguishing them would mean logging which contacts had unparseable numbers, which edges toward the contact list Ben explicitly did not want persisted. The distinction is recoverable ad hoc against the Google API, as it was during design.
