# `POST /people` and Email Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /people` so a contact can be created deliberately — from an email, a phone, or both — and promote an adopted row's `email` once Google has one.

**Architecture:** `POST` validates, refuses duplicates on all four lookup paths, creates the Google contact, then hands the result to the sync's own `apply_person` so row creation stays in one place. Promotion lives in `apply_person`'s linked branch and writes the email only when the address is unclaimed, because a claimed address would violate the unique constraint and abort the nightly sync.

**Tech Stack:** Python 3.13, FastAPI, Google People API v1, Postgres (pg8000 on Cloud SQL, psycopg3 locally), pytest, Docker (throwaway Postgres for constraint tests).

**Spec:** `docs/superpowers/specs/2026-09-28-post-people-design.md` — read it before Task 1. **Piece 3 of 3**; pieces 1 (`#14`) and 2 (`#15`) are merged and deployed.

## Global Constraints

- **Branch:** `post-people` (exists, spec committed). Never commit to `main`; open the PR with `/pr-open`.
- **NO SCHEMA CHANGE.** Every column and constraint already exists. A task that edits `repo/schema.sql` is out of scope — say so rather than doing it.
- **Deploy order:** code first. No data step is required at all this time.
- **`GET /people/{ident}` and `PATCH /people/{ident}` are production contracts.** Inbox calls the GET. Neither may change behavior.
- **A promotion must never abort the sync.** `apply_person` runs for every linked contact every night. Writing an email that another row already owns violates `people_email_key` and rolls back the whole run — the same failure mode as piece 2's phone-clearing bug. Promote only when `people.email_owner` returns `None`.
- **`update_from_google`'s new `email` parameter is written ONLY when not `None`**, so every existing caller behaves exactly as before.
- **"Usable phone" means `services.imessage_export.normalize_handle` returns a value** — the same rule adoption and iMessage matching use. A short code is not an identifier.
- **A person must have an email or a phone.** Enforced in the API as a `400`, not left to the CHECK constraint.
- **Layer rules** (CLAUDE.md): `clients/` I/O only; `repo/` DB-only taking an open connection; `services/` orchestration with no HTTP status codes; `api/routers/` thin transport.
- **Python 3.13:** `X | None`, never `Optional[X]`.
- **Every task ends green:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
- **Tests never** touch the production database or the real Google API. Test data uses `+1555010xxxx` and `example.com` only.
- **Commit trailers** on every commit:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01HYuAELaRczksHnqP7uo6yo`

## Review Focus

Five failure modes the spec implies; each line names the task that owns its test.

1. **A promotion to an address another row owns must not abort the sync.** This is the highest-consequence path in the plan: it runs nightly for every linked contact. Expected: skip the write, keep both rows, count it as `updated` not `promoted`. *Test in Task 3, plus a real-Postgres test proving the constraint would fire without the guard.*
2. **`POST` with a phone that does not normalize** (a short code, or `"call me"`) and no email must be a `400`, not a CHECK violation at write time. *Test in Tasks 1 and 4.*
3. **A duplicate that exists only in Google**, not yet adopted, must still be refused — with an empty `candidates` list, since there is no `people.id` to return. *Test in Task 2.*
4. **`POST` succeeding in Google but failing locally** must not report success. Expected: the caller learns the contact exists in Google without a row, and the next sync repairs it. *Test in Task 2.*
5. **A row that already has an email must never be re-written by promotion**, including when Google's primary address differs from the stored one. Google is the truth for most fields, but silently changing a person's identity key on a nightly path is not something to do by accident. *Test in Task 3.*

---

## Task 1: `contact_fields.identifiers`

**Files:**
- Modify: `services/contact_fields.py`
- Modify: `tests/test_contact_fields.py`

**Interfaces:**
- Consumes: `services.eligibility.normalize`, `services.imessage_export.normalize_handle`.
- Produces:

```python
def identifiers(contact: dict) -> tuple[list[str], list[str]]:
    """(emails, phones) a submitted contact map would give a person.
    Emails are normalized; phones are E.164 and exclude anything
    normalize_handle rejects. Either list may be empty."""
```
Consumed by Tasks 2 and 4.

- [ ] **Step 1: Write the failing tests**

```python
def test_identifiers_returns_normalized_emails_and_phones():
    emails, phones = cf.identifiers(
        {"emailAddresses": [{"value": " Alice@Example.COM "}],
         "phoneNumbers": [{"value": "(555) 010-0001"}]}
    )
    assert emails == ["alice@example.com"]
    assert phones == ["+15550100001"]


def test_identifiers_drops_unusable_phones():
    # Review Focus 2: a short code is not an identifier.
    _, phones = cf.identifiers({"phoneNumbers": [{"value": "262966"},
                                                 {"value": "call me"}]})
    assert phones == []


def test_identifiers_handles_a_contact_with_neither():
    assert cf.identifiers({"names": [{"givenName": "Alice"}]}) == ([], [])


def test_identifiers_ignores_blank_and_non_string_values():
    emails, phones = cf.identifiers(
        {"emailAddresses": [{"value": ""}, {"value": None}, {}],
         "phoneNumbers": [{"value": 12345}]}
    )
    assert emails == [] and phones == []


def test_identifiers_deduplicates_preserving_order():
    emails, _ = cf.identifiers(
        {"emailAddresses": [{"value": "a@example.com"}, {"value": "A@EXAMPLE.COM"},
                            {"value": "b@example.com"}]}
    )
    assert emails == ["a@example.com", "b@example.com"]
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_contact_fields.py -k identifiers -v
```

Expected: FAIL — `module 'services.contact_fields' has no attribute 'identifiers'`.

- [ ] **Step 3: Implement**

Read each `value` defensively with `str(e.get("value") or "")` — the same coercion `check_email_addition` already uses, so a non-string cannot raise. Normalize emails with `services.eligibility.normalize` and drop empties; normalize phones with `normalize_handle` and drop `None`. De-duplicate each list with `dict.fromkeys`, the idiom used elsewhere in this repo. Pure function, no I/O.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/test_contact_fields.py -v
```

- [ ] **Step 5: Commit**

```bash
git add services/contact_fields.py tests/test_contact_fields.py
git commit -m "feat: extract the identifiers a contact map would give a person"
```

---

## Task 2: `services/person_create.py`

**Files:**
- Create: `services/person_create.py`
- Create: `tests/test_person_create.py`
- Modify: `clients/google_contacts.py` (add `create_person`)
- Modify: `tests/test_google_contacts.py`

**Interfaces:**
- Consumes: Task 1's `identifiers`; `contact_fields.validate`; `google_contacts_sync.apply_person`, `group_name`; `repo.people.get`, `get_by_phone`, `get_by_google_resource`; `person_edit.update`.
- Produces:

```python
class Invalid(Exception): ...      # -> 400
class Duplicate(Exception): ...    # -> 409; .candidates: list[int]

def create(conn, *, contact: dict, notes: str | None = None,
           relationship_label: str | None = None) -> dict:
    """Spec §5. Validate, refuse duplicates, create in Google, then let
    apply_person create the row. Returns the row."""
```
and in `clients/google_contacts.py`:
```python
def create_person(body: dict, group_resource_name: str | None) -> dict:
    """createContact with a caller-supplied body plus group membership."""
```

- [ ] **Step 1: Write the failing client test**

```python
def test_create_person_sends_the_body_and_group(monkeypatch):
    calls = []

    class FakeReq:
        def execute(self):
            return {"resourceName": "people/c1"}

    class FakePeople:
        def createContact(self, **kw):
            calls.append(kw)
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(google_contacts, "_svc", lambda: FakeService())
    google_contacts.create_person(
        {"phoneNumbers": [{"value": "+15550100001"}]}, "contactGroups/abc"
    )
    body = calls[0]["body"]
    assert body["phoneNumbers"] == [{"value": "+15550100001"}]
    assert body["memberships"] == [
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}}
    ]
    assert calls[0]["personFields"] == google_contacts.PERSON_FIELDS


def test_create_person_without_a_group_sends_no_membership(monkeypatch):
    calls = []

    class FakeReq:
        def execute(self):
            return {"resourceName": "people/c1"}

    class FakePeople:
        def createContact(self, **kw):
            calls.append(kw)
            return FakeReq()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(google_contacts, "_svc", lambda: FakeService())
    google_contacts.create_person({"names": [{"givenName": "Alice"}]}, None)
    assert "memberships" not in calls[0]["body"]
```

- [ ] **Step 2: Write the failing service tests**

```python
import pytest

from services import person_create


@pytest.fixture
def wired(monkeypatch):
    """No duplicates anywhere; Google creates; apply_person makes a row."""
    state = {"created_body": None, "applied": None, "edits": []}

    monkeypatch.setattr(person_create.people, "get", lambda conn, email: None)
    monkeypatch.setattr(person_create.people, "get_by_phone", lambda conn, ph: [])
    monkeypatch.setattr(person_create.gc, "search_by_email", lambda email: None)
    monkeypatch.setattr(person_create.gc, "list_phone_index", lambda: {})
    monkeypatch.setattr(person_create.gc, "list_groups", lambda: {})
    monkeypatch.setattr(person_create.gc, "ensure_group", lambda name: "contactGroups/abc")

    def fake_create_person(body, group):
        state["created_body"] = body
        return {"resourceName": "people/c1", "etag": "e1"}

    monkeypatch.setattr(person_create.gc, "create_person", fake_create_person)
    monkeypatch.setattr(person_create.gsync, "apply_person",
                        lambda conn, person, groups: state.__setitem__("applied", person) or "created")
    monkeypatch.setattr(person_create.people, "get_by_google_resource",
                        lambda conn, rn: {"id": 7, "email": None, "google_resource_name": rn})
    monkeypatch.setattr(person_create.person_edit, "update",
                        lambda conn, pid, **kw: state["edits"].append((pid, kw)) or {"id": pid})
    return state


PHONE_ONLY = {"phoneNumbers": [{"value": "+15550100001"}]}


def test_creates_a_phone_only_person(wired):
    row = person_create.create(None, contact=PHONE_ONLY)
    assert row["id"] == 7
    assert wired["created_body"]["phoneNumbers"] == [{"value": "+15550100001"}]
    assert wired["applied"]["resourceName"] == "people/c1"
    assert wired["edits"] == []          # no notes or label given


def test_applies_notes_and_label_when_given(wired):
    person_create.create(None, contact=PHONE_ONLY, notes="hi", relationship_label="colleague")
    assert wired["edits"] == [(7, {"notes": "hi", "relationship_label": "colleague"})]


def test_rejects_a_contact_with_no_usable_identifier(wired):
    # Review Focus 2.
    with pytest.raises(person_create.Invalid):
        person_create.create(None, contact={"phoneNumbers": [{"value": "262966"}]})
    assert wired["created_body"] is None          # nothing reached Google


def test_rejects_a_field_owned_elsewhere(wired):
    with pytest.raises(person_create.Invalid):
        person_create.create(None, contact={"biographies": [{"value": "x"}]})
    assert wired["created_body"] is None


def test_refuses_a_duplicate_person_row_by_email(wired, monkeypatch):
    monkeypatch.setattr(person_create.people, "get",
                        lambda conn, email: {"id": 11, "email": email})
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact={"emailAddresses": [{"value": "a@example.com"}]})
    assert e.value.candidates == [11]
    assert wired["created_body"] is None


def test_refuses_a_duplicate_person_row_by_phone(wired, monkeypatch):
    monkeypatch.setattr(person_create.people, "get_by_phone",
                        lambda conn, ph: [{"id": 12}, {"id": 13}])
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact=PHONE_ONLY)
    assert e.value.candidates == [12, 13]
    assert wired["created_body"] is None


def test_refuses_a_google_only_duplicate_with_no_candidates(wired, monkeypatch):
    # Review Focus 3: exists in Google, not yet adopted — no people.id to give.
    monkeypatch.setattr(person_create.gc, "search_by_email",
                        lambda email: {"resourceName": "people/c9"})
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact={"emailAddresses": [{"value": "a@example.com"}]})
    assert e.value.candidates == []
    assert wired["created_body"] is None


def test_refuses_a_google_only_duplicate_by_phone(wired, monkeypatch):
    monkeypatch.setattr(person_create.gc, "list_phone_index",
                        lambda: {"+15550100001": [{"resource_name": "people/c9",
                                                   "display_name": "Alice"}]})
    with pytest.raises(person_create.Duplicate) as e:
        person_create.create(None, contact=PHONE_ONLY)
    assert e.value.candidates == []


def test_raises_invalid_when_no_row_appears(wired, monkeypatch):
    # Review Focus 4: created in Google but no local row — do not report success.
    monkeypatch.setattr(person_create.people, "get_by_google_resource",
                        lambda conn, rn: None)
    with pytest.raises(person_create.Invalid):
        person_create.create(None, contact=PHONE_ONLY)
```

- [ ] **Step 3: Run both to verify failure**

```bash
.venv/bin/pytest tests/test_person_create.py tests/test_google_contacts.py -v
```

Expected: FAIL — `ModuleNotFoundError: services.person_create`, and no attribute `create_person`.

- [ ] **Step 4: Implement**

`clients/google_contacts.py::create_person(body, group_resource_name)` — copy the membership-appending shape from `create_contact` but take the whole body from the caller; leave `create_contact` untouched, because `ensure_contact` still uses it.

`services/person_create.py`, in this order (nothing touches Google until every check passes):

1. `fields = contact_fields.validate(contact)`, mapping `ValidationError` → `Invalid`.
2. `emails, phones = contact_fields.identifiers(fields)`; if both are empty, raise `Invalid("a contact needs an email address or a phone number")`.
3. Duplicate check, collecting `people.id` values: `people.get` per email; `people.get_by_phone` per phone; then `gc.search_by_email` per email and `gc.list_phone_index()` once for the phones. Raise `Duplicate(candidates=[...])` on any hit — ids when the match was local, an empty list when it was Google-only.
4. Resolve the group as `ensure_contact` does: `groups = gc.list_groups()`, then `(groups.get(group_name()) or {}).get("resourceName") or gc.ensure_group(group_name())`.
5. `created = gc.create_person(fields, target)`.
6. `gsync.apply_person(conn, created, groups)`, then
   `row = people.get_by_google_resource(conn, created["resourceName"])`; if `row` is `None`, raise `Invalid` saying the contact was created in Google but no row was made and the next sync will pick it up.
7. If `notes` or `relationship_label` is not `None`, `person_edit.update(conn, row["id"], notes=notes, relationship_label=relationship_label)` and return its row; otherwise return `row`.

Let a Google `HttpError` propagate — Task 4's router maps it, exactly as the PATCH route already does.

- [ ] **Step 5: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add services/person_create.py clients/google_contacts.py tests/
git commit -m "feat: create a person deliberately, refusing duplicates"
```

---

## Task 3: Email promotion

**Files:**
- Modify: `repo/people.py` (`email_owner`, `update_from_google`)
- Modify: `services/google_contacts_sync.py` (`apply_person`, `run_sync`)
- Modify: `tests/test_repo_people.py`, `tests/test_google_contacts_sync.py`, `tests/test_schema.py`

**Interfaces:**
- Produces: `people.email_owner(conn, email: str) -> int | None`; `update_from_google(..., email: str | None = None)` writing `email` only when not `None`; `apply_person` returning `"promoted"`; `run_sync` counting `promoted`.

- [ ] **Step 1: Write the failing repo tests**

```python
def test_email_owner_selects_on_email():
    conn = FakeConn(results=[[{"id": 9}]])
    assert people.email_owner(conn, " Alice@Example.com ") == 9
    sql, params = conn.calls[0]
    assert "WHERE email = %s" in sql and params == ("alice@example.com",)


def test_email_owner_returns_none_when_unclaimed():
    assert people.email_owner(FakeConn(results=[[]]), "a@example.com") is None


def test_update_from_google_omits_email_by_default():
    conn = FakeConn()
    people.update_from_google(
        conn, "people/c1", etag="e1", display_name="Alice", notes=None,
        relationship_label=None, phone_numbers=["+15550100001"], company=None,
        job_title=None, google_fields={},
    )
    sql, _ = conn.calls[0]
    assert "email = " not in sql          # every existing caller unaffected


def test_update_from_google_writes_email_when_passed():
    conn = FakeConn()
    people.update_from_google(
        conn, "people/c1", etag="e1", display_name="Alice", notes=None,
        relationship_label=None, phone_numbers=["+15550100001"], company=None,
        job_title=None, google_fields={}, email="alice@example.com",
    )
    sql, params = conn.calls[0]
    assert "email = %s" in sql
    assert "alice@example.com" in params
```

- [ ] **Step 2: Write the failing sync tests**

```python
def linked_row(email=None, pid=7):
    return {"id": pid, "email": email, "google_resource_name": "people/c1"}


def test_promotes_an_unclaimed_address(monkeypatch):
    captured = {}
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: linked_row())
    monkeypatch.setattr(gsync.people, "email_owner", lambda conn, email: None)
    monkeypatch.setattr(gsync.people, "update_from_google",
                        lambda conn, rn, **kw: captured.update(kw))
    kind = gsync.apply_person(
        None, person(email="alice@example.com", phones=["+15550100001"]), {}
    )
    assert kind == "promoted"
    assert captured["email"] == "alice@example.com"


def test_does_not_promote_a_claimed_address(monkeypatch):
    # Review Focus 1: writing it would violate people_email_key and abort the run.
    captured = {}
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: linked_row())
    monkeypatch.setattr(gsync.people, "email_owner", lambda conn, email: 99)
    monkeypatch.setattr(gsync.people, "update_from_google",
                        lambda conn, rn, **kw: captured.update(kw))
    kind = gsync.apply_person(
        None, person(email="taken@example.com", phones=["+15550100001"]), {}
    )
    assert kind == "updated"
    assert captured["email"] is None


def test_never_rewrites_an_existing_email(monkeypatch):
    # Review Focus 5: Google's primary address differs from the stored one.
    captured = {}
    monkeypatch.setattr(gsync.people, "get_by_google_resource",
                        lambda conn, rn: linked_row(email="old@example.com"))
    monkeypatch.setattr(gsync.people, "email_owner",
                        lambda conn, email: pytest.fail("must not be consulted"))
    monkeypatch.setattr(gsync.people, "update_from_google",
                        lambda conn, rn, **kw: captured.update(kw))
    assert gsync.apply_person(None, person(email="new@example.com", phones=[]), {}) == "updated"
    assert captured["email"] is None


def test_run_sync_counts_promoted(monkeypatch):
    monkeypatch.setattr(gsync.gc, "list_connections",
                        lambda token: ([person(email="alice@example.com",
                                               phones=["+15550100001"])], "tok"))
    monkeypatch.setattr(gsync.gc, "list_groups", lambda: {})
    monkeypatch.setattr(gsync.sync_state, "get_token", lambda conn: None)
    monkeypatch.setattr(gsync.sync_state, "set_token", lambda conn, t, s: None)
    monkeypatch.setattr(gsync.people, "get_by_google_resource", lambda conn, rn: linked_row())
    monkeypatch.setattr(gsync.people, "email_owner", lambda conn, email: None)
    monkeypatch.setattr(gsync.people, "update_from_google", lambda conn, rn, **kw: None)
    assert gsync.run_sync(None)["promoted"] == 1
```

Reuse the `person(...)` helper already in that test file; if its signature differs, adapt these calls to it rather than adding a second helper.

- [ ] **Step 3: Write the failing real-Postgres test**

In `tests/test_schema.py`, proving the guard is load-bearing:

```python
def test_promoting_to_a_claimed_address_violates_the_unique_index(conn):
    """Review Focus 1: this is what the email_owner guard prevents."""
    conn.execute("INSERT INTO people (email, first_seen) VALUES ('taken@example.com', now())")
    conn.execute(
        "INSERT INTO people (email, first_seen, google_resource_name, phone_numbers)"
        " VALUES (NULL, now(), 'people/c1', %s)",
        (["+15550100001"],),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "UPDATE people SET email = 'taken@example.com' WHERE google_resource_name = 'people/c1'"
        )
```

- [ ] **Step 4: Run all three to verify failure**

```bash
.venv/bin/pytest tests/test_repo_people.py tests/test_google_contacts_sync.py -v
docker run -d --rm --name pg-t3 -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
until docker exec pg-t3 pg_isready -q; do sleep 1; done
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -k promoting -v
```

Expected: the repo and sync tests FAIL (no `email_owner`); the schema test PASSES already — it documents the constraint the guard exists for, so note that in the report rather than contriving a failure.

- [ ] **Step 5: Implement**

`repo/people.py`:
```python
def email_owner(conn: Any, email: str) -> int | None:
    """The id of the person holding this address, or None. Guards promotion:
    writing a claimed address violates people_email_key and aborts the sync."""
    row = conn.execute("SELECT id FROM people WHERE email = %s", (_norm(email),)).fetchone()
    return row["id"] if row else None
```
`update_from_google` gains `email: str | None = None` and builds its SET list so `email = %s` appears only when `email is not None`. Keep every other column exactly as it is.

`services/google_contacts_sync.py::apply_person`, in the linked branch after piece 2's skip guard:
```python
    promote = None
    if linked.get("email") is None:
        candidate = primary_email(person)
        if candidate and people.email_owner(conn, candidate) is None:
            promote = candidate
    people.update_from_google(conn, rn, ..., email=promote)
    return "promoted" if promote else "updated"
```
Add `"promoted": 0` to `run_sync`'s counts dict.

- [ ] **Step 6: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres .venv/bin/pytest tests/test_schema.py -v
docker stop pg-t3
```

- [ ] **Step 7: Commit**

```bash
git add repo/people.py services/google_contacts_sync.py tests/
git commit -m "feat: promote an adopted row's email when the address is unclaimed"
```

---

## Task 4: `POST /people`

**Files:**
- Modify: `api/routers/people.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 2's `person_create.create`, `Invalid`, `Duplicate`.
- Produces: `POST /people` → 201 `PersonOut`; 400 on `Invalid` or a Google 4xx; 409 on `Duplicate` with `{"error": "person exists", "candidates": [...]}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_post_creates_a_person(client, monkeypatch):
    monkeypatch.setattr("api.routers.people.person_create.create",
                        lambda conn, **kw: person_row(id=7, email=None))
    r = client.post("/people", json={"contact": {"phoneNumbers": [{"value": "+15550100001"}]}})
    assert r.status_code == 201
    assert r.json()["id"] == 7


def test_post_passes_notes_and_label_through(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("api.routers.people.person_create.create",
                        lambda conn, **kw: seen.update(kw) or person_row(id=7))
    client.post("/people", json={"contact": {"emailAddresses": [{"value": "a@example.com"}]},
                                 "notes": "hi", "relationship_label": "colleague"})
    assert seen["notes"] == "hi" and seen["relationship_label"] == "colleague"


def test_post_invalid_is_400_with_the_message(client, monkeypatch):
    # Review Focus 2 at the transport layer.
    def boom(conn, **kw):
        raise person_create.Invalid("a contact needs an email address or a phone number")

    monkeypatch.setattr("api.routers.people.person_create.create", boom)
    r = client.post("/people", json={"contact": {"phoneNumbers": [{"value": "262966"}]}})
    assert r.status_code == 400
    assert "phone number" in r.text


def test_post_duplicate_is_409_with_candidates(client, monkeypatch):
    def boom(conn, **kw):
        raise person_create.Duplicate(candidates=[11, 12])

    monkeypatch.setattr("api.routers.people.person_create.create", boom)
    r = client.post("/people", json={"contact": {"emailAddresses": [{"value": "a@example.com"}]}})
    assert r.status_code == 409
    assert r.json()["detail"]["candidates"] == [11, 12]
    assert r.json()["detail"]["error"] == "person exists"


def test_post_requires_a_contact_map(client):
    assert client.post("/people", json={"notes": "hi"}).status_code == 422
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_api.py -k post -v
```

Expected: FAIL — 405 Method Not Allowed, since no `POST /people` route exists.

- [ ] **Step 3: Implement**

```python
class PersonCreate(BaseModel):
    contact: dict
    notes: str | None = None
    relationship_label: str | None = None


@router.post("/people", response_model=PersonOut, status_code=201)
def create_person(body: PersonCreate) -> PersonOut:
    try:
        with db.get_conn() as conn:
            row = person_create.create(
                conn, contact=body.contact, notes=body.notes,
                relationship_label=body.relationship_label,
            )
            conn.commit()
    except person_create.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except person_create.Duplicate as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "person exists", "candidates": e.candidates},
        ) from e
    return to_out(row, include_contact=True)
```

`contact` is required, so a body without it is FastAPI's own 422 — that is correct and needs no code. Map a Google `HttpError` the same way `patch_person` already does, so a malformed field returns Google's message as a 400. Do not touch the GET, PATCH or sync routes.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/mypy api/
```

- [ ] **Step 5: Commit**

```bash
git add api/routers/people.py tests/test_api.py
git commit -m "feat: POST /people"
```

---

## Task 5: Docs and skills

**Files:**
- Create: `.claude/skills/creating-person/SKILL.md`
- Modify: `.claude/skills/editing-person/SKILL.md`, `fetching-person/SKILL.md`, `people-architecture/SKILL.md`, `querying-people-db/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write `creating-person`**

Frontmatter description triggering on "add a contact", "create a contact", "add X to my contacts", "I met someone". Body: the `POST /people` shape with a worked `curl` using `TOKEN=$(gcloud auth print-identity-token)` against `https://people-api.drolet.cloud`; that an email or a usable phone is required (a short code is not); that `notes` and `relationship_label` are separate fields and rejected inside `contact`; that a duplicate returns 409 with candidate ids and the remedy is `PATCH`; and that a Google-only duplicate returns 409 with an empty candidate list, meaning run a sync first.

- [ ] **Step 2: Update the neighbouring skills**

`editing-person`: cross-reference `creating-person` for someone who does not exist yet. `fetching-person`: unchanged behavior, one line that a person may now have been created by hand. `people-architecture`: `POST /people` in the API surface, `services/person_create.py` in the layout, and promotion — including that promotion makes a person mirrorable to HubSpot for the first time. `querying-people-db`: promoted rows are indistinguishable from ordinary ones after the fact; the duplicate query from the piece-2 spec is what finds a promotion that was refused.

- [ ] **Step 3: Update `CLAUDE.md`**

`POST /people` in the API row, `services/person_create.py` in the code layout, and a source-of-truth note that a hand-created contact is created in Google first and the row is derived from it.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/ruff check .
git add .claude/skills CLAUDE.md
git commit -m "docs: creating a person"
```

---

## Task 6: Verify live, open the PR

- [ ] **Step 1: Open the PR and merge it**

`/pr-open`, wait for CI, merge. No data step is needed; there is no schema change and no token to clear.

- [ ] **Step 2: Create a real contact through the deployed API**

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -s -X POST https://people-api.drolet.cloud/people \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"contact": {"names": [{"givenName": "Plan", "familyName": "Testcontact"}],
                   "phoneNumbers": [{"value": "+15550100099", "type": "mobile"}]},
       "notes": "created by the piece-3 verification"}' | jq '{id, email, phone_numbers, notes}'
```

Expect 201, `email: null`, the phone in `phone_numbers`, and the note. Confirm the contact appears in the Google Contacts UI.

- [ ] **Step 3: Read it back by phone and by id**

```bash
curl -s "https://people-api.drolet.cloud/people/%2B15550100099" -H "Authorization: Bearer $TOKEN" | jq '{id, email}'
```

- [ ] **Step 4: Prove the duplicate refusal**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://people-api.drolet.cloud/people \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"contact": {"phoneNumbers": [{"value": "+15550100099"}]}}'        # expect 409
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://people-api.drolet.cloud/people \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"contact": {"phoneNumbers": [{"value": "262966"}]}}'              # expect 400
```

- [ ] **Step 5: Prove promotion end to end**

`PATCH` an email onto the test contact, then trigger a sync and read the `promoted` count:

```bash
curl -s -X PATCH "https://people-api.drolet.cloud/people/%2B15550100099" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"contact": {"emailAddresses": [{"value": "plan-test@example.com"}]}}' > /dev/null
SYNC_URL=$(cd terraform && terraform output -raw sync_url)
SYNC_TOKEN=$(gcloud secrets versions access latest --secret people-sync-token --project bens-project-462804)
curl -s -X POST "$SYNC_URL" -H "Authorization: Bearer $SYNC_TOKEN" | jq .google
```

Expect `promoted: 1`, and the person then resolvable by `plan-test@example.com`.

- [ ] **Step 6: Clean up the test contact**

Delete it in the Google Contacts UI, then trigger one more sync so the row is marked `google_deleted_at`. Confirm the row is no longer returned by `GET /people/plan-test@example.com`. Report the counts with no personal data.

---

## Self-Review

**Spec coverage:** §4 request/response → Task 4; §5.1 validation → Tasks 1, 2; §5.2 flow → Task 2; §5.3 duplicates → Task 2; §5.4 errors → Tasks 2, 4; §6.1 promotion → Task 3; §6.2 the guard → Task 3 (unit + real Postgres); §6.3 HubSpot consequence → Task 5 (documented); §6.4 counting → Task 3; §7 testing → spread across Tasks 1–4; §8 rollout → Task 6.

**Type consistency:** `identifiers(contact) -> tuple[list[str], list[str]]` (Task 1) matches Task 2's `emails, phones` unpacking. `Duplicate.candidates: list[int]` (Task 2) matches Task 4's `detail["candidates"]`. `email_owner(conn, email) -> int | None` and `update_from_google(..., email=None)` (Task 3) match the `apply_person` call in the same task. `person_create.create(conn, *, contact, notes, relationship_label) -> dict` matches Task 4's router call.

**Review Focus coverage:** (1) claimed-address promotion → Task 3, unit and real Postgres. (2) unusable phone → Tasks 1 and 4. (3) Google-only duplicate → Task 2. (4) Google succeeds, row missing → Task 2. (5) existing email never rewritten → Task 3.

**Known gap, deliberate:** `POST` cannot create a person who has no Google contact, because creation goes through Google first. That is the design (Google is the source of truth), but it means the endpoint fails entirely when the People API is down, rather than queueing. Given this is a single-user service and the failure is loud, queueing is YAGNI.
