# Multiple Labels per Person Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single `relationship_label` with `labels` — every user-defined Google contact group a person belongs to — indexed in the DB, editable with add/remove, and listable by label.

**Architecture:** Two new tables: `contact_groups` (rn → name, refreshed wholesale from `contactGroups.list` on every sync) and `people_labels` (person ↔ group rn). Names are joined at read time, so a rename in the Contacts UI costs no per-contact work. Writes go to Google first (`modify_group_members`), then the existing `sync_one` refresh re-derives the DB rows.

**Tech Stack:** Python 3.13, FastAPI, Postgres (pg8000 on Cloud SQL / psycopg3 locally), Google People API v1, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-02-multiple-labels-design.md`

## Global Constraints

- Google Contacts is the source of truth; the DB only reflects what Google holds. No code path writes `people_labels` except from a Google payload.
- System groups (`groupType == "SYSTEM_CONTACT_GROUP"`) and the `GOOGLE_CONTACT_GROUP` group (env, default `Inbox`) are never labels.
- Schema changes are **additive only** in this PR. `people.relationship_label` stays in the table, unused; it is dropped in a follow-up PR (spec §8 step 6).
- Layer rules (CLAUDE.md): `clients/` I/O only; `repo/` takes an open connection and never opens one; `services/` business logic; `api/routers/` thin transport.
- Repo is public: no personal names, label names, or counts in code, tests, or logs. Test fixtures use generic names ("Family", "Climbing", "investor").
- Every commit on branch `multiple-labels`; end each commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_011MDo2Pn8FaRDwNxt7tUNzR
  ```
- Checks before every commit: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q`.

## Review Focus

1. **A user label that shares a name with a Google built-in group** ("Family" user group alongside the system `contactGroups/family`). Expected: `PATCH {"labels": {"add": ["family"]}}` targets the user group; only a name that matches *no* user group and *is* a system name is rejected. Pinned in Task 2 (`test_resolve_prefers_a_user_group_over_a_system_name`).
2. **A contact in a group that `contact_groups` doesn't know yet** (created in the UI after the last refresh, or reached via `ensure_contact`, which doesn't refresh). Expected: the membership is skipped, not an FK violation that aborts the nightly sync. Pinned in Task 1 (`test_set_contact_labels_skips_unknown_groups`, real DB).
3. **`RETURNING {_COLUMNS}` with the new correlated `labels` subquery** on `INSERT … ON CONFLICT` paths (`upsert_inbound`, `create_from_google`). Expected: valid SQL, returns `labels` (`[]` for a new row). Pinned in Task 1 (`test_returning_columns_includes_labels`, real DB).
4. **Removing a label the person doesn't hold, or that doesn't exist.** Expected: no Google write, `200`. Pinned in Task 4 (`test_remove_unheld_or_unknown_label_is_a_noop`).
5. **Google returns a page of groups with no user groups at all** (e.g. a fresh account). Expected: `contact_groups` emptied and every membership cascades away — no error. Pinned in Task 1 (`test_replace_groups_with_nothing_clears_everything`, real DB).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repo/schema.sql` | Modify | Append `contact_groups`, `people_labels` DDL. |
| `repo/labels.py` | Create | `replace_groups`, `set_contact_labels`, `list_with_counts`, `find_by_name`. |
| `repo/people.py` | Modify | `_COLUMNS`: add `labels` subquery, drop `relationship_label`; drop `relationship_label` from `set_google` / `update_from_google` / `create_from_google`; add `with_label`. |
| `clients/google_contacts.py` | Modify | Add `list_groups_by_rn`. |
| `services/labels.py` | Create | Pure: `SYSTEM_GROUP_TYPE`, `LabelError`, `AmbiguousLabel`, `user_groups`, `membership_rns`, `normalize_change`, `match_name`, `resolve`. |
| `services/google_contacts_sync.py` | Modify | Delete `relationship_label` + `SYSTEM_GROUP_TYPE`; add `refresh_groups`; `apply_person(conn, person)` drops `groups`; write memberships on every row write. |
| `services/person_edit.py` | Modify | `relationship_label=` → `labels=`; `_set_label` → `_plan_labels` + `_apply_labels`. |
| `services/person_create.py` | Modify | `relationship_label=` → `labels: list[str] | None`. |
| `services/contact_fields.py` | Modify | `OWNED_ELSEWHERE["memberships"] = "labels"`. |
| `api/routers/people.py` | Modify | `PersonOut.labels`; `LabelChange`; `PersonPatch.labels`; `PersonCreate.labels`. |
| `api/routers/labels.py` | Create | `GET /labels`, `GET /labels/{name}`. |
| `api/main.py` | Modify | Register the labels router. |
| Tests | Modify/Create | `tests/test_repo_labels.py`, `tests/test_labels.py` (new); `test_schema.py`, `test_repo_people.py`, `test_google_contacts.py`, `test_google_contacts_sync.py`, `test_person_edit.py`, `test_person_create.py`, `test_contact_fields.py`, `test_api.py`. |
| Docs/skills | Modify | `CLAUDE.md`, `.claude/skills/{editing-person,creating-person,fetching-person,searching-people,people-architecture,querying-people-db}/SKILL.md`, spec §5.2 wording. |

---

### Task 1: Schema and the label repo

**Files:**
- Modify: `repo/schema.sql` (append at end)
- Create: `repo/labels.py`
- Modify: `repo/people.py:8-14` (`_COLUMNS`), add `with_label` after `recent` (~line 119)
- Create: `tests/test_repo_labels.py`
- Modify: `tests/test_repo_people.py`, `tests/test_schema.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `repo.labels.replace_groups(conn, groups: dict[str, str]) -> None` — `groups` is rn → name.
  - `repo.labels.set_contact_labels(conn, resource_name: str, group_rns: list[str]) -> None`
  - `repo.labels.list_with_counts(conn) -> list[dict]` — rows `{"name": str, "count": int}`.
  - `repo.labels.find_by_name(conn, name: str) -> list[dict]` — rows `{"resource_name": str, "name": str}`.
  - `repo.people.with_label(conn, group_rn: str) -> list[dict]` — person rows, `_COLUMNS` shape.
  - Every `_COLUMNS` row carries `labels: list[str]`.

- [ ] **Step 1: Write the failing SQL-shape tests**

Create `tests/test_repo_labels.py`:

```python
from repo import labels
from tests.test_repo_people import FakeConn


def test_replace_groups_upserts_each_then_deletes_the_rest():
    conn = FakeConn()
    labels.replace_groups(conn, {"contactGroups/a": "Climbing", "contactGroups/b": "investor"})
    sqls = [s for s, _ in conn.calls]
    assert sum("INSERT INTO contact_groups" in s for s in sqls) == 2
    assert "ON CONFLICT (resource_name) DO UPDATE SET name = EXCLUDED.name" in sqls[0]
    assert conn.calls[0][1] == ("contactGroups/a", "Climbing")
    delete_sql, delete_params = conn.calls[-1]
    assert "DELETE FROM contact_groups" in delete_sql and "ANY(%s::text[])" in delete_sql
    assert delete_params == (["contactGroups/a", "contactGroups/b"],)


def test_set_contact_labels_replaces_by_resource_name():
    conn = FakeConn()
    labels.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    (del_sql, del_params), (ins_sql, ins_params) = conn.calls
    assert "DELETE FROM people_labels" in del_sql and del_params == ("people/c1",)
    assert "INSERT INTO people_labels" in ins_sql and "JOIN contact_groups" in ins_sql
    assert ins_params == (["contactGroups/a"], "people/c1")


def test_list_with_counts_left_joins_so_empty_labels_appear():
    conn = FakeConn(results=[[{"name": "Climbing", "count": 0}]])
    assert labels.list_with_counts(conn) == [{"name": "Climbing", "count": 0}]
    assert "LEFT JOIN people_labels" in conn.calls[0][0]


def test_find_by_name_is_case_insensitive():
    conn = FakeConn(results=[[]])
    labels.find_by_name(conn, "Climbing")
    sql, params = conn.calls[0]
    assert "lower(name) = lower(%s)" in sql and params == ("Climbing",)
```

Append to `tests/test_repo_people.py`:

```python
def test_columns_include_labels_not_relationship_label():
    assert "AS labels" in people._COLUMNS
    assert "relationship_label" not in people._COLUMNS


def test_with_label_filters_by_group_and_orders_by_interaction():
    conn = FakeConn(results=[[{"id": 1}]])
    assert people.with_label(conn, "contactGroups/a") == [{"id": 1}]
    sql, params = conn.calls[0]
    assert "people_labels" in sql and "ORDER BY GREATEST" in sql
    assert params == ("contactGroups/a",)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_labels.py tests/test_repo_people.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'repo.labels'`, and `AssertionError` on `AS labels`.

- [ ] **Step 3: Append the DDL to `repo/schema.sql`**

```sql
-- Labels: Google contact groups, user-defined only — system groups and
-- GOOGLE_CONTACT_GROUP are never stored (docs/superpowers/specs/2026-10-02-multiple-labels-design.md §4).
-- contact_groups is replaced wholesale from contactGroups.list on every sync,
-- so a rename or delete in the Contacts UI lands without revisiting any
-- contact. Names live only here and are joined at read time.
CREATE TABLE IF NOT EXISTS contact_groups (
    resource_name TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Not unique: Google allows names differing only in case (services/labels.py::match_name).
CREATE INDEX IF NOT EXISTS contact_groups_lower_name_idx ON contact_groups (lower(name));

CREATE TABLE IF NOT EXISTS people_labels (
    person_id           BIGINT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    group_resource_name TEXT   NOT NULL REFERENCES contact_groups(resource_name) ON DELETE CASCADE,
    PRIMARY KEY (person_id, group_resource_name)
);
CREATE INDEX IF NOT EXISTS people_labels_group_idx ON people_labels (group_resource_name);
```

- [ ] **Step 4: Create `repo/labels.py`**

```python
"""contact_groups and people_labels: the label index (multiple-labels design §4).
Takes an open connection; never opens one."""

from typing import Any


def replace_groups(conn: Any, groups: dict[str, str]) -> None:
    """Make contact_groups exactly `groups` (rn -> name). A group no longer
    listed is deleted, and its people_labels rows cascade with it."""
    for rn, name in groups.items():
        conn.execute(
            """
            INSERT INTO contact_groups (resource_name, name) VALUES (%s, %s)
            ON CONFLICT (resource_name) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
            WHERE contact_groups.name IS DISTINCT FROM EXCLUDED.name
            """,
            (rn, name),
        )
    conn.execute(
        "DELETE FROM contact_groups WHERE NOT (resource_name = ANY(%s::text[]))",
        (list(groups),),
    )


def set_contact_labels(conn: Any, resource_name: str, group_rns: list[str]) -> None:
    """Replace the labels of the person linked to `resource_name`. An rn not in
    contact_groups (a system group, GOOGLE_CONTACT_GROUP, or a group newer than
    the last refresh) is dropped by the join — never an FK error that would
    abort the nightly sync."""
    conn.execute(
        """
        DELETE FROM people_labels
        WHERE person_id IN (SELECT id FROM people WHERE google_resource_name = %s)
        """,
        (resource_name,),
    )
    conn.execute(
        """
        INSERT INTO people_labels (person_id, group_resource_name)
        SELECT p.id, g.resource_name
        FROM people p JOIN contact_groups g ON g.resource_name = ANY(%s::text[])
        WHERE p.google_resource_name = %s
        ON CONFLICT DO NOTHING
        """,
        (group_rns, resource_name),
    )


def list_with_counts(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT g.name, count(pl.person_id)::int AS count
        FROM contact_groups g
        LEFT JOIN people_labels pl ON pl.group_resource_name = g.resource_name
        GROUP BY g.resource_name, g.name
        ORDER BY lower(g.name), g.name
        """
    ).fetchall()


def find_by_name(conn: Any, name: str) -> list[dict]:
    return conn.execute(
        "SELECT resource_name, name FROM contact_groups WHERE lower(name) = lower(%s)",
        (name,),
    ).fetchall()
```

- [ ] **Step 5: Update `_COLUMNS` and add `with_label` in `repo/people.py`**

Replace `_COLUMNS` (lines 8-14) with:

```python
_COLUMNS = """
    id, email, display_name, first_seen, last_seen, last_contacted, message_count,
    my_response_count, notes, eligible, automated,
    google_resource_name, google_etag, google_deleted_at, hubspot_contact_id,
    hubspot_synced_at, phone_numbers, company, job_title, google_fields, updated_at,
    GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction,
    ARRAY(
        SELECT g.name FROM people_labels pl
        JOIN contact_groups g ON g.resource_name = pl.group_resource_name
        WHERE pl.person_id = people.id
        ORDER BY lower(g.name), g.name
    ) AS labels
"""
```

After `recent(...)`, add:

```python
def with_label(conn: Any, group_rn: str) -> list[dict]:
    """Everyone carrying one label — no limit, no eligibility filter: a label is
    a deliberate list (multiple-labels design §6.2)."""
    return conn.execute(
        f"""
        SELECT {_COLUMNS} FROM people
        WHERE id IN (SELECT person_id FROM people_labels WHERE group_resource_name = %s)
        ORDER BY {_LAST_INTERACTION} DESC
        """,
        (group_rn,),
    ).fetchall()
```

Leave the `relationship_label` parameters on `set_google` / `update_from_google` / `create_from_google` alone in this task (Task 3 removes them with their callers).

- [ ] **Step 6: Write the real-DB tests in `tests/test_schema.py`**

These run only with `TEST_DATABASE_URL` set. Append:

```python
from repo import labels as labels_repo


def _linked(conn, rn, email):
    return people_repo.create_from_google(
        conn,
        email,
        display_name=None,
        resource_name=rn,
        etag=None,
        notes=None,
        relationship_label=None,
        phone_numbers=[],
        company=None,
        job_title=None,
        google_fields=json.dumps({}),
    )


def test_returning_columns_includes_labels(conn):
    # Review Focus 3: correlated subquery inside INSERT ... ON CONFLICT ... RETURNING
    # must be valid SQL on both write paths that use RETURNING {_COLUMNS}.
    _linked(conn, "people/c1", "a@example.com")  # create_from_google: raises if invalid
    people_repo.upsert_inbound(conn, "b@example.com", None, datetime.now(UTC))  # upsert path
    got = conn.execute(
        "SELECT labels FROM (SELECT " + people_repo._COLUMNS + " FROM people) s"
    ).fetchall()
    assert [r[0] for r in got] == [[], []]


def test_labels_join_reflects_a_rename(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "Climbing"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    labels_repo.replace_groups(conn, {"contactGroups/a": "Bouldering"})
    got = conn.execute(
        "SELECT labels FROM (SELECT " + people_repo._COLUMNS + " FROM people) s"
    ).fetchone()
    assert got[0] == ["Bouldering"]


def test_set_contact_labels_skips_unknown_groups(conn):
    # Review Focus 2: a group newer than the last refresh must not FK-fail.
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "Climbing"})
    labels_repo.set_contact_labels(
        conn, "people/c1", ["contactGroups/a", "contactGroups/unknown", "contactGroups/myContacts"]
    )
    assert conn.execute("SELECT count(*) FROM people_labels").fetchone()[0] == 1


def test_set_contact_labels_replaces_rather_than_appends(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A", "contactGroups/b": "B"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/b"])
    rows = conn.execute("SELECT group_resource_name FROM people_labels").fetchall()
    assert [r[0] for r in rows] == ["contactGroups/b"]


def test_deleting_a_group_cascades_memberships(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A", "contactGroups/b": "B"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a", "contactGroups/b"])
    labels_repo.replace_groups(conn, {"contactGroups/b": "B"})
    rows = conn.execute("SELECT group_resource_name FROM people_labels").fetchall()
    assert [r[0] for r in rows] == ["contactGroups/b"]


def test_replace_groups_with_nothing_clears_everything(conn):
    # Review Focus 5.
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    labels_repo.replace_groups(conn, {})
    assert conn.execute("SELECT count(*) FROM contact_groups").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM people_labels").fetchone()[0] == 0


def test_deleting_a_person_cascades_memberships(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "A"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    conn.execute("DELETE FROM people")
    assert conn.execute("SELECT count(*) FROM people_labels").fetchone()[0] == 0


def test_list_with_counts_includes_empty_labels(conn):
    _linked(conn, "people/c1", "a@example.com")
    labels_repo.replace_groups(conn, {"contactGroups/a": "climbing", "contactGroups/b": "Book Club"})
    labels_repo.set_contact_labels(conn, "people/c1", ["contactGroups/a"])
    got = [tuple(r) for r in labels_repo.list_with_counts(conn)]
    assert got == [("Book Club", 0), ("climbing", 1)]
```

The `conn` fixture is a plain psycopg connection, so rows are tuples.

- [ ] **Step 7: Run all tests**

Run: `.venv/bin/pytest tests/test_repo_labels.py tests/test_repo_people.py -q`
Expected: PASS.

Run the real-DB tests against a local Postgres:
```bash
createdb people_schema_test 2>/dev/null; TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -q
```
Expected: PASS (including `test_schema_is_idempotent`). If no local Postgres is available, say so in the task report — do not skip silently.

- [ ] **Step 8: Full checks, then commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
git add repo/schema.sql repo/labels.py repo/people.py tests/test_repo_labels.py tests/test_repo_people.py tests/test_schema.py
git commit -m "labels: contact_groups/people_labels tables and repo"
```

(`tests/test_api.py` still passes: its fake rows carry `relationship_label` and `PersonOut` still reads it until Task 5.)

---

### Task 2: Group listing by resource name, and the pure label rules

**Files:**
- Modify: `clients/google_contacts.py` (add after `list_groups`, ~line 144)
- Create: `services/labels.py`
- Create: `tests/test_labels.py`
- Modify: `tests/test_google_contacts.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `clients.google_contacts.list_groups_by_rn() -> dict[str, dict]` — rn → `{"name": str | None, "formattedName": str | None, "groupType": str | None}`.
  - `services.labels.SYSTEM_GROUP_TYPE: str = "SYSTEM_CONTACT_GROUP"`
  - `services.labels.LabelError(Exception)` → 400; `services.labels.AmbiguousLabel(LabelError)` → 409.
  - `services.labels.user_groups(by_rn: dict[str, dict], reserved: str) -> dict[str, str]` (rn → name)
  - `services.labels.membership_rns(person: dict) -> list[str]`
  - `services.labels.normalize_change(add: list[str], remove: list[str]) -> tuple[list[str], list[str]]`
  - `services.labels.match_name(candidates: dict[str, str], name: str) -> str | None`
  - `services.labels.resolve(by_rn: dict[str, dict], name: str, reserved: str) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_labels.py`:

```python
import pytest

from services import labels

SYSTEM = labels.SYSTEM_GROUP_TYPE
USER = "USER_CONTACT_GROUP"

BY_RN = {
    "contactGroups/myContacts": {"name": "myContacts", "formattedName": "My Contacts", "groupType": SYSTEM},
    "contactGroups/family": {"name": "family", "formattedName": "Family", "groupType": SYSTEM},
    "contactGroups/starred": {"name": "starred", "formattedName": "Starred", "groupType": SYSTEM},
    "contactGroups/inbox1": {"name": "Inbox", "formattedName": "Inbox", "groupType": USER},
    "contactGroups/climb": {"name": "Climbing", "formattedName": "Climbing", "groupType": USER},
    "contactGroups/inv": {"name": "investor", "formattedName": "investor", "groupType": USER},
}


def test_user_groups_drops_system_and_reserved():
    assert labels.user_groups(BY_RN, "Inbox") == {
        "contactGroups/climb": "Climbing",
        "contactGroups/inv": "investor",
    }


def test_user_groups_reserved_match_ignores_case():
    assert "contactGroups/inbox1" not in labels.user_groups(BY_RN, "inbox")


def test_membership_rns_returns_every_group_in_order_without_duplicates():
    person = {
        "memberships": [
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/climb"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/inv"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/climb"}},
            {"domainMembership": {"inViewerDomain": True}},
        ]
    }
    assert labels.membership_rns(person) == ["contactGroups/climb", "contactGroups/inv"]


def test_membership_rns_handles_no_memberships():
    assert labels.membership_rns({}) == []


def test_normalize_change_strips_and_collapses_duplicates():
    add, remove = labels.normalize_change([" Climbing ", "climbing", "investor"], [])
    assert add == ["Climbing", "investor"] and remove == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_normalize_change_rejects_blank(bad):
    with pytest.raises(labels.LabelError, match="blank"):
        labels.normalize_change([bad], [])
    with pytest.raises(labels.LabelError, match="blank"):
        labels.normalize_change([], [bad])


def test_normalize_change_rejects_add_and_remove_overlap():
    with pytest.raises(labels.LabelError, match="both"):
        labels.normalize_change(["Climbing"], ["CLIMBING"])


def test_match_name_case_insensitive():
    assert labels.match_name({"g/1": "Climbing"}, "climbing") == "g/1"


def test_match_name_exact_case_breaks_a_tie():
    assert labels.match_name({"g/1": "VIP", "g/2": "vip"}, "vip") == "g/2"


def test_match_name_ambiguous_without_exact():
    with pytest.raises(labels.AmbiguousLabel):
        labels.match_name({"g/1": "VIP", "g/2": "vip"}, "Vip")


def test_match_name_none_when_absent():
    assert labels.match_name({"g/1": "Climbing"}, "investor") is None


def test_resolve_finds_a_user_group():
    assert labels.resolve(BY_RN, "CLIMBING", "Inbox") == "contactGroups/climb"


def test_resolve_none_for_a_new_name():
    assert labels.resolve(BY_RN, "Book Club", "Inbox") is None


def test_resolve_rejects_the_reserved_group():
    with pytest.raises(labels.LabelError, match="reserved"):
        labels.resolve(BY_RN, "inbox", "Inbox")


@pytest.mark.parametrize("name", ["starred", "Starred", "My Contacts", "myContacts"])
def test_resolve_rejects_a_system_name_with_no_user_group(name):
    with pytest.raises(labels.LabelError, match="built-in"):
        labels.resolve(BY_RN, name, "Inbox")


def test_resolve_prefers_a_user_group_over_a_system_name():
    # Review Focus 1: a user "Family" label coexists with the built-in family group.
    by_rn = {
        **BY_RN,
        "contactGroups/fam1": {"name": "Family", "formattedName": "Family", "groupType": USER},
    }
    assert labels.resolve(by_rn, "family", "Inbox") == "contactGroups/fam1"


def test_ambiguous_is_a_label_error():
    assert issubclass(labels.AmbiguousLabel, labels.LabelError)
```

Append to `tests/test_google_contacts.py`:

```python
def test_list_groups_by_rn_pages_and_keeps_colliding_names(monkeypatch):
    pages = [
        {
            "contactGroups": [
                {"resourceName": "contactGroups/family", "name": "family",
                 "formattedName": "Family", "groupType": "SYSTEM_CONTACT_GROUP"},
            ],
            "nextPageToken": "p2",
        },
        {
            "contactGroups": [
                {"resourceName": "contactGroups/fam1", "name": "Family",
                 "formattedName": "Family", "groupType": "USER_CONTACT_GROUP"},
            ]
        },
    ]
    tokens = []

    class Req:
        def __init__(self, page):
            self.page = page

        def execute(self):
            return self.page

    class Groups:
        def list(self, pageSize, pageToken):
            tokens.append(pageToken)
            return Req(pages[len(tokens) - 1])

    class Svc:
        def contactGroups(self):
            return Groups()

    monkeypatch.setattr(gc, "_svc", lambda: Svc())
    got = gc.list_groups_by_rn()
    assert tokens == [None, "p2"]
    assert set(got) == {"contactGroups/family", "contactGroups/fam1"}
    assert got["contactGroups/fam1"] == {
        "name": "Family", "formattedName": "Family", "groupType": "USER_CONTACT_GROUP"
    }
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_labels.py tests/test_google_contacts.py -q`
Expected: FAIL — `ImportError: cannot import name 'labels' from 'services'` and `AttributeError: ... has no attribute 'list_groups_by_rn'`.

- [ ] **Step 3: Add `list_groups_by_rn` to `clients/google_contacts.py`** (after `list_groups`)

```python
def list_groups_by_rn() -> dict[str, dict]:
    """Every contact group keyed by resourceName. list_groups() keys by name,
    where a user "Family" label and the built-in "Family" group collide; resource
    names never do (multiple-labels design §3)."""
    out: dict[str, dict] = {}
    token = None
    while True:
        resp = _svc().contactGroups().list(pageSize=200, pageToken=token).execute()
        for g in resp.get("contactGroups", []):
            out[g["resourceName"]] = {
                "name": g.get("name"),
                "formattedName": g.get("formattedName"),
                "groupType": g.get("groupType"),
            }
        token = resp.get("nextPageToken")
        if not token:
            return out
```

- [ ] **Step 4: Create `services/labels.py`**

```python
"""Google contact groups as labels (multiple-labels design). Pure — no I/O.

A label is a user-defined contact group. System groups (myContacts, starred,
Google's built-in friends/family/coworkers) and people's own
GOOGLE_CONTACT_GROUP are never labels."""

SYSTEM_GROUP_TYPE = "SYSTEM_CONTACT_GROUP"


class LabelError(Exception):
    """Blank, contradictory, or reserved label name. -> 400."""


class AmbiguousLabel(LabelError):
    """A name matches several groups case-insensitively and none exactly. -> 409."""


def _display(group: dict) -> str:
    return group.get("formattedName") or group.get("name") or ""


def user_groups(by_rn: dict[str, dict], reserved: str) -> dict[str, str]:
    """rn -> name for every group that can be a label."""
    return {
        rn: _display(g)
        for rn, g in by_rn.items()
        if g.get("groupType") != SYSTEM_GROUP_TYPE and _display(g).lower() != reserved.lower()
    }


def membership_rns(person: dict) -> list[str]:
    """Every contact-group rn on a Google person payload, in order, deduplicated.
    Unfiltered: the DB join (repo/labels.py::set_contact_labels) keeps only rns
    that are labels."""
    out: list[str] = []
    for m in person.get("memberships", []):
        rn = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if rn and rn not in out:
            out.append(rn)
    return out


def _clean(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        s = (n or "").strip()
        if not s:
            raise LabelError("label names must not be blank")
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def normalize_change(add: list[str], remove: list[str]) -> tuple[list[str], list[str]]:
    """Strip names, collapse case-insensitive duplicates within a list, and
    reject blanks or a name in both lists (design §5.2)."""
    a, r = _clean(add), _clean(remove)
    both = {n.lower() for n in a} & {n.lower() for n in r}
    if both:
        raise LabelError(f"label in both add and remove: {', '.join(sorted(both))}")
    return a, r


def match_name(candidates: dict[str, str], name: str) -> str | None:
    """The rn whose name equals `name` ignoring case. An exact-case match wins a
    tie; several case-insensitive matches with no exact one is AmbiguousLabel."""
    hits = {rn: n for rn, n in candidates.items() if n.lower() == name.lower()}
    if not hits:
        return None
    exact = sorted(rn for rn, n in hits.items() if n == name)
    if exact:
        return exact[0]
    if len(hits) == 1:
        return next(iter(hits))
    raise AmbiguousLabel(
        f"label {name!r} matches {', '.join(sorted(hits.values()))}; use the exact spelling"
    )


def resolve(by_rn: dict[str, dict], name: str, reserved: str) -> str | None:
    """The user group `name` refers to, or None if no such label exists yet.
    A user group wins over a same-named system group; a name that matches only
    a system group, or the reserved group, is rejected."""
    if name.lower() == reserved.lower():
        raise LabelError(f"{reserved!r} is reserved for people's own use, not a label")
    rn = match_name(user_groups(by_rn, reserved), name)
    if rn is not None:
        return rn
    system = {
        n.lower()
        for g in by_rn.values()
        if g.get("groupType") == SYSTEM_GROUP_TYPE
        for n in (g.get("name"), g.get("formattedName"))
        if n
    }
    if name.lower() in system:
        raise LabelError(f"{name!r} is a built-in Google group, not a label")
    return None
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_labels.py tests/test_google_contacts.py -q`
Expected: PASS.

- [ ] **Step 6: Full checks, then commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
git add clients/google_contacts.py services/labels.py tests/test_labels.py tests/test_google_contacts.py
git commit -m "labels: list groups by resource name; pure label rules"
```

---

### Task 3: Sync writes every label; `relationship_label` leaves the write path

**Files:**
- Modify: `services/google_contacts_sync.py` (whole label path; `apply_person`, `_link`, `ensure_contact`, `run_sync`, `sync_one`)
- Modify: `repo/people.py` (`set_google`, `update_from_google`, `create_from_google`: drop `relationship_label`)
- Modify: `services/person_create.py:77` (`apply_person` call only — the `labels` parameter is Task 4)
- Modify: `services/person_edit.py` (`_set_label` still uses `gsync.SYSTEM_GROUP_TYPE`; repoint it to `labels.SYSTEM_GROUP_TYPE` so this task stays green — Task 4 deletes `_set_label`)
- Modify: `tests/test_google_contacts_sync.py`, `tests/test_repo_people.py`, `tests/test_schema.py`, `tests/test_person_create.py`

**Interfaces:**
- Consumes: `labels.user_groups`, `labels.membership_rns`, `labels.SYSTEM_GROUP_TYPE` (Task 2); `gc.list_groups_by_rn` (Task 2); `labels_repo.replace_groups`, `labels_repo.set_contact_labels` (Task 1).
- Produces:
  - `services.google_contacts_sync.refresh_groups(conn) -> None`
  - `services.google_contacts_sync.apply_person(conn, person: dict) -> str | None` — **the `groups` parameter is removed.**
  - `services.google_contacts_sync.sync_one(conn, row) -> dict` — refreshes groups, then applies.
  - `relationship_label()` and `SYSTEM_GROUP_TYPE` no longer exist on `google_contacts_sync`.
  - `repo.people.set_google / update_from_google / create_from_google` no longer accept `relationship_label`.

- [ ] **Step 1: Rewrite the sync test fixtures and add failing tests**

In `tests/test_google_contacts_sync.py`:

1. Add imports and an autouse fixture under the existing imports:

```python
import repo.labels as labels_repo
from services import labels

GROUPS_BY_RN = {
    "contactGroups/myContacts": {
        "name": "myContacts", "formattedName": "My Contacts", "groupType": labels.SYSTEM_GROUP_TYPE,
    },
    "contactGroups/inbox1": {"name": "Inbox", "formattedName": "Inbox", "groupType": "USER_CONTACT_GROUP"},
    "contactGroups/fam1": {"name": "Family", "formattedName": "Family", "groupType": "USER_CONTACT_GROUP"},
    "contactGroups/climb": {"name": "Climbing", "formattedName": "Climbing", "groupType": "USER_CONTACT_GROUP"},
}


@pytest.fixture(autouse=True)
def label_io(monkeypatch):
    """Every test: no real Google group listing, and label writes recorded."""
    log = {"replaced": [], "set": []}
    monkeypatch.setattr(gc, "list_groups_by_rn", lambda: GROUPS_BY_RN)
    monkeypatch.setattr(
        labels_repo, "replace_groups", lambda conn, groups: log["replaced"].append(dict(groups))
    )
    monkeypatch.setattr(
        labels_repo,
        "set_contact_labels",
        lambda conn, rn, rns: log["set"].append((rn, list(rns))),
    )
    monkeypatch.delenv("GOOGLE_CONTACT_GROUP", raising=False)
    return log
```

2. In `FakeRepo.set_google`, change `for k in ("display_name", "notes", "relationship_label"):` to `for k in ("display_name", "notes"):`. In `FakeRepo.update_from_google`, change the `r.update(...)` to `r.update(google_etag=kw["etag"], notes=kw["notes"])`.

3. Replace every `apply_person(None, <x>, {})` with `apply_person(None, <x>)`:

```bash
sed -i '' -E 's/apply_person\(None, (.*), \{\}\)/apply_person(None, \1)/' tests/test_google_contacts_sync.py
sed -i '' 's/lambda conn, p, g: "updated"/lambda conn, p: "updated"/' tests/test_google_contacts_sync.py
```

4. Delete `test_relationship_label_ignores_system_and_inbox_groups` and `test_relationship_label_none_when_only_system` (Task 2's `test_labels.py` covers the rules).

5. In `test_ensure_contact_links_existing`, replace the final `assert (... r["relationship_label"] == "family")` with:

```python
    assert r["display_name"] == "Alice Example" and r["notes"] == "old friend"
    assert label_io["set"] == [("people/c1", ["contactGroups/fam1"])]
```
and add `label_io` to the test's parameters.

6. In `test_run_sync_updates_links_creates_deletes`, add `label_io` to its parameters, and replace `assert fr.rows["linked@x.com"]["relationship_label"] == "family"` with:

```python
    assert ("people/l1", ["contactGroups/fam1"]) in label_io["set"]
    assert ("people/g1", []) in label_io["set"]  # deleted contact's labels cleared
    assert label_io["replaced"] == [{"contactGroups/fam1": "Family", "contactGroups/climb": "Climbing"}]
```

7. Append new tests:

```python
def test_apply_person_writes_every_group_not_just_the_first(label_io, monkeypatch):
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: linked_row("a@x.com"))
    monkeypatch.setattr(sync.people, "update_from_google", lambda conn, rn, **kw: None)
    p = person(
        email="a@x.com",
        groups=["contactGroups/myContacts", "contactGroups/inbox1",
                "contactGroups/fam1", "contactGroups/climb"],
    )
    assert sync.apply_person(None, p) == "updated"
    # Unfiltered here; the DB join drops myContacts and inbox1 (repo/labels.py).
    assert label_io["set"] == [(
        "people/c1",
        ["contactGroups/myContacts", "contactGroups/inbox1", "contactGroups/fam1", "contactGroups/climb"],
    )]


def test_apply_person_writes_labels_on_adoption(label_io, monkeypatch):
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    monkeypatch.setattr(sync.people, "create_from_google", lambda conn, email, **kw: {"id": 1})
    p = person(email=None, phones=["+15550100001"], groups=["contactGroups/climb"])
    assert sync.apply_person(None, p) == "created"
    assert label_io["set"] == [("people/c1", ["contactGroups/climb"])]


def test_apply_person_skipped_writes_no_labels(label_io, monkeypatch):
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: None)
    assert sync.apply_person(None, person(email=None, phones=[], groups=["contactGroups/climb"])) == "skipped"
    assert label_io["set"] == []


def test_deleted_contact_clears_labels_before_unlinking(label_io, monkeypatch):
    order = []
    monkeypatch.setattr(sync.people, "get_by_google_resource", lambda conn, rn: linked_row("a@x.com"))
    monkeypatch.setattr(
        labels_repo, "set_contact_labels", lambda conn, rn, rns: order.append(("labels", rn, rns))
    )
    monkeypatch.setattr(sync.people, "mark_google_deleted", lambda conn, rn: order.append(("deleted", rn)))
    assert sync.apply_person(None, person(deleted=True)) == "deleted"
    # set_contact_labels finds the row by google_resource_name, which
    # mark_google_deleted NULLs — so clearing must come first.
    assert order == [("labels", "people/c1", []), ("deleted", "people/c1")]


def test_run_sync_refreshes_groups_before_applying(label_io, monkeypatch):
    order = []
    monkeypatch.setattr(
        labels_repo, "replace_groups", lambda conn, groups: order.append("refresh")
    )
    monkeypatch.setattr(sync, "apply_person", lambda conn, p: order.append("apply") or "updated")
    monkeypatch.setattr(sync.gc, "list_connections", lambda token: ([person()], "tok"))
    monkeypatch.setattr(sync.sync_state, "get_token", lambda conn: None)
    monkeypatch.setattr(sync.sync_state, "set_token", lambda conn, t, s: None)
    sync.run_sync(None)
    assert order == ["refresh", "apply"]


def test_sync_one_refreshes_groups(label_io, monkeypatch):
    monkeypatch.setattr(sync.gc, "get_person", lambda rn: person())
    monkeypatch.setattr(sync, "apply_person", lambda conn, p: "updated")
    monkeypatch.setattr(sync.people, "get_by_id", lambda conn, pid: {"id": pid})
    sync.sync_one(None, {"id": 7, "email": "a@x.com", "google_resource_name": "people/c1"})
    assert len(label_io["replaced"]) == 1
```

(`linked_row` is defined later in the module; pytest resolves it at call time, so placement at the end is fine.)

In `tests/test_repo_people.py` and `tests/test_schema.py`, delete every `relationship_label=None,` argument line:

```bash
sed -i '' '/^ *relationship_label=None,$/d' tests/test_repo_people.py tests/test_schema.py
```

In `tests/test_person_create.py`, change the `apply_person` stub to the new arity:

```python
        lambda conn, person: state.__setitem__("applied", person) or "created",
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_google_contacts_sync.py tests/test_repo_people.py tests/test_person_create.py -q`
Expected: FAIL — `TypeError: apply_person() missing 1 required positional argument: 'groups'`, and `TypeError: ... missing ... 'relationship_label'` from the repo tests.

- [ ] **Step 3: Drop `relationship_label` from the repo writers**

In `repo/people.py`:

- `set_google`: delete the `relationship_label: str | None = None,` parameter, the `relationship_label   = COALESCE(%s, relationship_label),` SET line, and `relationship_label,` from the params tuple.
- `update_from_google`: delete the `relationship_label: str | None,` parameter, `"relationship_label = %s",` from `set_clauses`, and `relationship_label,` from `params`.
- `create_from_google`: delete the `relationship_label: str | None,` parameter; change the column list to `google_resource_name, google_etag, notes,` (drop `relationship_label`), drop one `%s` from `VALUES` so it reads `VALUES (%s, %s, now(), TRUE, FALSE, %s, %s, %s, %s::text[], %s, %s, %s::jsonb)`, change `notes = EXCLUDED.notes, relationship_label = EXCLUDED.relationship_label,` to `notes = EXCLUDED.notes,`, and drop `relationship_label,` from the params tuple.

The column itself stays in the table (Global Constraints); nothing writes it any more.

- [ ] **Step 4: Rewrite the label path in `services/google_contacts_sync.py`**

Imports become:

```python
import clients.google_contacts as gc
import clients.otel as otel
from repo import labels as labels_repo
from repo import people, sync_state
from services import contact_fields, labels
```

Delete `SYSTEM_GROUP_TYPE = "SYSTEM_CONTACT_GROUP"` and the whole `relationship_label(...)` function. Add after `notes(...)`:

```python
def refresh_groups(conn: Any) -> None:
    """Make contact_groups match Google's labels (multiple-labels design §4.2).
    Runs before apply_person, so a person's memberships can reference them."""
    labels_repo.replace_groups(conn, labels.user_groups(gc.list_groups_by_rn(), group_name()))


def _write_labels(conn: Any, person: dict) -> None:
    labels_repo.set_contact_labels(conn, person["resourceName"], labels.membership_rns(person))
```

`_link` loses `groups` and writes labels:

```python
def _link(conn: Any, email: str, person: dict) -> None:
    people.set_google(
        conn,
        email,
        resource_name=person["resourceName"],
        etag=person.get("etag"),
        display_name=display_name(person),
        notes=notes(person),
        **contact_fields.derive(person),
    )
    _write_labels(conn, person)
```

In `ensure_contact`, change `_link(conn, row["email"], person, groups)` to `_link(conn, row["email"], person)` (it keeps `groups = gc.list_groups()` for the `Inbox` target lookup).

`apply_person` — new signature `def apply_person(conn: Any, person: dict) -> str | None:`, and:

- Deleted branch becomes:
```python
    if (person.get("metadata") or {}).get("deleted"):
        if linked:
            # Clear first: set_contact_labels finds the row by
            # google_resource_name, which mark_google_deleted NULLs.
            labels_repo.set_contact_labels(conn, rn, [])
            people.mark_google_deleted(conn, rn)
            return "deleted"
        return None
```
- Delete the line `label = relationship_label(person, groups)`.
- Remove `relationship_label=label,` from all three repo calls (`update_from_google` and both `create_from_google`).
- After `people.update_from_google(...)`, before `return "promoted" if promote else "updated"`, add `_write_labels(conn, person)`.
- After each `people.create_from_google(...)` (adoption and the plain create), before `return "created"`, add `_write_labels(conn, person)`.
- Change `_link(conn, email, person, groups)` to `_link(conn, email, person)`.

`run_sync`: replace `groups = gc.list_groups()` with `refresh_groups(conn)` and `kind = apply_person(conn, p, groups)` with `kind = apply_person(conn, p)`.

`sync_one`: replace `apply_person(conn, gc.get_person(rn), gc.list_groups())` with:

```python
    refresh_groups(conn)
    apply_person(conn, gc.get_person(rn))
```

- [ ] **Step 5: Update the remaining callers**

`services/person_create.py`: change `gsync.apply_person(conn, created, groups)` to `gsync.apply_person(conn, created)`. (`groups` is still used for the `Inbox` target just above.)

`services/person_edit.py` `_set_label`: replace both `gsync.SYSTEM_GROUP_TYPE` with `label_rules.SYSTEM_GROUP_TYPE`, add `from services import labels as label_rules` to the imports, and replace `current = gsync.relationship_label(live, groups)` with:

```python
    user = label_rules.user_groups(gc.list_groups_by_rn(), gsync.group_name())
    current = next(
        (user[rn].lower() for rn in label_rules.membership_rns(live) if rn in user), None
    )
```

(This keeps `relationship_label=` PATCHes working until Task 4 replaces `_set_label` outright. In `tests/test_person_edit.py`'s `wire` fixture, add `monkeypatch.setattr(gc, "list_groups_by_rn", lambda: {g["resourceName"]: {"name": n, "formattedName": n, "groupType": g["groupType"]} for n, g in GROUPS.items()})`, and in `test_label_leaves_unrelated_groups_alone` patch `list_groups_by_rn` the same way from its local `groups`.)

- [ ] **Step 6: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_google_contacts_sync.py tests/test_repo_people.py tests/test_person_create.py tests/test_person_edit.py -q`
Expected: PASS.

Run: `createdb people_schema_test 2>/dev/null; TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -q`
Expected: PASS.

- [ ] **Step 7: Full checks, then commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
git add services/google_contacts_sync.py services/person_create.py services/person_edit.py repo/people.py tests/
git commit -m "labels: sync indexes every contact group; relationship_label no longer written"
```

---

### Task 4: Add and remove labels on edit and create

**Files:**
- Modify: `services/person_edit.py` (`update`, replace `_set_label`)
- Modify: `services/person_create.py` (`create`)
- Modify: `services/contact_fields.py:49`
- Modify: `tests/test_person_edit.py`, `tests/test_person_create.py`, `tests/test_contact_fields.py:29`, `tests/test_contact_fields.py:160`

**Interfaces:**
- Consumes: `label_rules.normalize_change`, `label_rules.resolve`, `label_rules.membership_rns`, `label_rules.LabelError`, `label_rules.AmbiguousLabel` (Task 2); `gc.list_groups_by_rn` (Task 2); `gsync.group_name`, `gsync.sync_one` (Task 3).
- Produces:
  - `services.person_edit.update(conn, person_id: int, *, notes: str | None = None, labels: dict | None = None, contact: dict | None = None) -> dict` — `labels` is `{"add": list[str], "remove": list[str]}`, either key optional. **`relationship_label` is removed.**
  - `services.person_create.create(conn, *, contact: dict, notes: str | None = None, labels: list[str] | None = None) -> dict` — **`relationship_label` is removed.**
  - `contact_fields.OWNED_ELSEWHERE["memberships"] == "labels"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_person_edit.py`, replace the `GROUPS` constant and `wire` fixture's group stubs with a by-rn form, and replace the four label tests (`test_label_moves_between_user_groups_but_keeps_inbox`, `test_label_leaves_unrelated_groups_alone`, `test_label_matches_existing_group_case_insensitively`, and the `relationship_label="colleague"` argument in `test_partial_failure_still_resyncs_and_reraises`).

New constant (replaces `GROUPS`):

```python
USER = "USER_CONTACT_GROUP"
SYSTEM = "SYSTEM_CONTACT_GROUP"
GROUPS_BY_RN = {
    "contactGroups/myContacts": {"name": "myContacts", "formattedName": "My Contacts", "groupType": SYSTEM},
    "contactGroups/starred": {"name": "starred", "formattedName": "Starred", "groupType": SYSTEM},
    "contactGroups/inbox1": {"name": "Inbox", "formattedName": "Inbox", "groupType": USER},
    "contactGroups/fam1": {"name": "Family", "formattedName": "Family", "groupType": USER},
    "contactGroups/col1": {"name": "Colleague", "formattedName": "Colleague", "groupType": USER},
}
```

In `wire`, replace the `list_groups` and `ensure_group` stubs with:

```python
    monkeypatch.setattr(gc, "list_groups_by_rn", lambda: GROUPS_BY_RN)
    monkeypatch.setattr(
        gc, "ensure_group", lambda name: (log.append(("create", name)), f"contactGroups/new-{name}")[1]
    )
```

(The live person in `wire` holds `fam1` and `inbox1` — unchanged.)

New tests replacing the old label tests:

```python
def group_calls(log):
    return [c for c in log if c[0] == "group"]


def test_add_puts_the_person_in_an_existing_group(wire):
    person_edit.update(None, 1, labels={"add": ["colleague"]})
    assert group_calls(wire) == [("group", "contactGroups/col1", ["people/c1"], [])]
    assert wire[-1] == ("sync", "a@x.com")


def test_add_never_removes_other_labels(wire):
    person_edit.update(None, 1, labels={"add": ["colleague"]})
    assert not any(c[3] for c in group_calls(wire))


def test_add_creates_a_missing_group(wire):
    person_edit.update(None, 1, labels={"add": ["Climbing"]})
    assert ("create", "Climbing") in wire
    assert group_calls(wire) == [("group", "contactGroups/new-Climbing", ["people/c1"], [])]


def test_add_of_a_held_label_is_skipped(wire):
    person_edit.update(None, 1, labels={"add": ["FAMILY"]})
    assert group_calls(wire) == []


def test_remove_takes_the_person_out(wire):
    person_edit.update(None, 1, labels={"remove": ["family"]})
    assert group_calls(wire) == [("group", "contactGroups/fam1", [], ["people/c1"])]


def test_remove_unheld_or_unknown_label_is_a_noop(wire):
    # Review Focus 4.
    person_edit.update(None, 1, labels={"remove": ["colleague", "Never Existed"]})
    assert group_calls(wire) == []
    assert not any(c[0] == "create" for c in wire)


def test_add_and_remove_together(wire):
    person_edit.update(None, 1, labels={"add": ["colleague"], "remove": ["family"]})
    assert group_calls(wire) == [
        ("group", "contactGroups/col1", ["people/c1"], []),
        ("group", "contactGroups/fam1", [], ["people/c1"]),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"add": ["  "]},
        {"add": ["x"], "remove": ["X"]},
        {"add": ["Inbox"]},
        {"remove": ["inbox"]},
        {"add": ["starred"]},
    ],
)
def test_invalid_label_change_rejects_before_any_write(wire, change):
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 1, notes="n", labels=change)
    assert wire == []  # no bio write, no group write, no create, no resync


def test_ambiguous_label_is_a_conflict(wire, monkeypatch):
    monkeypatch.setattr(
        gc,
        "list_groups_by_rn",
        lambda: {
            **GROUPS_BY_RN,
            "contactGroups/v1": {"name": "VIP", "formattedName": "VIP", "groupType": USER},
            "contactGroups/v2": {"name": "vip", "formattedName": "vip", "groupType": USER},
        },
    )
    with pytest.raises(person_edit.Conflict):
        person_edit.update(None, 1, labels={"add": ["Vip"]})
    assert group_calls(wire) == []


def test_label_google_4xx_is_invalid_and_still_resyncs(wire, monkeypatch):
    def boom(g, add, remove):
        raise http_error(400, "Group is read-only")

    monkeypatch.setattr(gc, "modify_group_members", boom)
    with pytest.raises(person_edit.Invalid):
        person_edit.update(None, 1, labels={"add": ["colleague"]})
    assert ("sync", "a@x.com") in wire
```

In `test_partial_failure_still_resyncs_and_reraises`, change `relationship_label="colleague"` to `labels={"add": ["colleague"]}`.

`http_error` is defined further down the module; fine at call time.

In `tests/test_person_create.py`, replace `test_applies_notes_and_label_when_given` with:

```python
def test_applies_notes_and_labels_when_given(wired):
    person_create.create(None, contact=PHONE_ONLY, notes="hi", labels=["colleague", "Climbing"])
    assert wired["edits"] == [(7, {"notes": "hi", "labels": {"add": ["colleague", "Climbing"]}})]


def test_labels_alone_trigger_the_edit(wired):
    person_create.create(None, contact=PHONE_ONLY, labels=["colleague"])
    assert wired["edits"] == [(7, {"notes": None, "labels": {"add": ["colleague"]}})]


def test_empty_labels_do_not_trigger_an_edit(wired):
    person_create.create(None, contact=PHONE_ONLY, labels=[])
    assert wired["edits"] == []
```

In `tests/test_contact_fields.py`, change `match="relationship_label"` (line 29) to `match="labels"`, and the comment at line 160 to `# owned by labels`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_person_edit.py tests/test_person_create.py tests/test_contact_fields.py -q`
Expected: FAIL — `TypeError: update() got an unexpected keyword argument 'labels'`, same for `create()`, and the `contact_fields` match failure.

- [ ] **Step 3: Replace `_set_label` in `services/person_edit.py`**

Add `from dataclasses import dataclass, field` to the imports. Delete `_set_label` and add:

```python
@dataclass
class _LabelPlan:
    add: list[str] = field(default_factory=list)  # group rns to add the person to
    create: list[str] = field(default_factory=list)  # names with no group yet
    remove: list[str] = field(default_factory=list)  # group rns to remove the person from


def _plan_labels(live: dict, change: dict) -> _LabelPlan:
    """Resolve a labels change against Google's live groups before any write
    (multiple-labels design §5.1), so a rejected request changes nothing.
    Adding a held label and removing an unheld one are both no-ops."""
    try:
        add, remove = label_rules.normalize_change(
            change.get("add") or [], change.get("remove") or []
        )
        by_rn = gc.list_groups_by_rn()
        held = set(label_rules.membership_rns(live))
        plan = _LabelPlan()
        for name in add:
            rn = label_rules.resolve(by_rn, name, gsync.group_name())
            if rn is None:
                plan.create.append(name)
            elif rn not in held:
                plan.add.append(rn)
        for name in remove:
            rn = label_rules.resolve(by_rn, name, gsync.group_name())
            if rn is not None and rn in held:
                plan.remove.append(rn)
    except label_rules.AmbiguousLabel as e:
        raise Conflict(str(e)) from e
    except label_rules.LabelError as e:
        raise Invalid(str(e)) from e
    return plan


def _apply_labels(person_rn: str, plan: _LabelPlan) -> None:
    for name in plan.create:
        plan.add.append(gc.ensure_group(name))
    for g in plan.add:
        gc.modify_group_members(g, [person_rn], [])
    for g in plan.remove:
        gc.modify_group_members(g, [], [person_rn])
```

- [ ] **Step 4: Rewire `update`**

Signature becomes:

```python
def update(
    conn: Any,
    person_id: int,
    *,
    notes: str | None = None,
    labels: dict | None = None,
    contact: dict | None = None,
) -> dict:
```

After the `if notes is not None: fields["biographies"] = ...` block (still before any write), add:

```python
    label_plan = _plan_labels(live, labels) if labels is not None else None
```

Replace the write block with:

```python
    try:
        try:
            if fields:
                gc.update_fields(rn, live.get("etag") or "", fields)
            if label_plan is not None:
                _apply_labels(rn, label_plan)
        except HttpError as e:
            if _is_stale_etag(e):
                raise Conflict(e.reason or "") from e
            status = e.resp.status
            if 400 <= status < 500:
                raise Invalid(e.reason or "") from e
            raise
    finally:
        # Google may now hold a partial result; refresh the DB from it either way.
        try:
            row = gsync.sync_one(conn, row)
        except Exception:
            logger.warning("post-edit resync failed for %s", person_id, exc_info=True)
    return row
```

Remove the `gsync.SYSTEM_GROUP_TYPE` / `user_groups` lines Task 3 added to `_set_label` (they went with it).

- [ ] **Step 5: Update `services/person_create.py`**

Change the parameter `relationship_label: str | None = None,` to `labels: list[str] | None = None,` and the tail to:

```python
    if notes is not None or labels:
        return person_edit.update(
            conn, row["id"], notes=notes, labels={"add": labels} if labels else None
        )
    return row
```

- [ ] **Step 6: Update `services/contact_fields.py`**

```python
OWNED_ELSEWHERE: dict[str, str] = {
    "biographies": "notes",
    "memberships": "labels",
}
```

- [ ] **Step 7: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_person_edit.py tests/test_person_create.py tests/test_contact_fields.py -q`
Expected: PASS.

`tests/test_api.py` and mypy now fail on `api/routers/people.py` still passing `relationship_label=` — Task 5 fixes the transport layer.

- [ ] **Step 8: Do not commit yet**

Tasks 4 and 5 land as one commit (Task 5 Step 7), so every commit on the branch keeps the suite green. A reviewer can still review Task 4's diff on its own before Task 5 starts.

---

### Task 5: API — `labels` on people, and the `/labels` routes

**Files:**
- Modify: `api/routers/people.py` (`PersonOut`, `PersonCreate`, `PersonPatch`, `to_out`, `create_person`, `patch_person`)
- Create: `api/routers/labels.py`
- Modify: `api/main.py:14,24`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `person_edit.update(..., labels=...)`, `person_create.create(..., labels=...)` (Task 4); `labels_repo.list_with_counts`, `labels_repo.find_by_name`, `people.with_label` (Task 1); `label_rules.match_name`, `label_rules.AmbiguousLabel` (Task 2).
- Produces (HTTP):
  - `PersonOut.labels: list[str]` everywhere; `PersonOut.relationship_label` removed.
  - `PATCH /people/{ident}` body `labels: {"add": [...], "remove": [...]}`.
  - `POST /people` body `labels: [...]`.
  - `GET /labels` → `{"results": [{"name": str, "count": int}]}`.
  - `GET /labels/{name}` → `PersonList`; `404` unknown; `409` ambiguous.

- [ ] **Step 1: Write the failing tests in `tests/test_api.py`**

In the `row()` fixture, replace `"relationship_label": "family",` with `"labels": ["family"],`.

Replace `test_patch_write_through`'s assertion with:

```python
    assert seen == {"notes": "hi", "labels": None, "contact": None}
```

Replace `test_patch_blank_label_is_422` with:

```python
def test_patch_passes_label_change_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        person_edit, "update", lambda conn, pid, **kw: (seen.update(kw), row())[1]
    )
    r = client.patch(
        "/people/alice@x.com", json={"labels": {"add": ["Climbing"], "remove": ["family"]}}
    )
    assert r.status_code == 200
    assert seen["labels"] == {"add": ["Climbing"], "remove": ["family"]}


def test_patch_label_change_defaults_missing_lists(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        person_edit, "update", lambda conn, pid, **kw: (seen.update(kw), row())[1]
    )
    client.patch("/people/alice@x.com", json={"labels": {"add": ["Climbing"]}})
    assert seen["labels"] == {"add": ["Climbing"], "remove": []}


def test_patch_invalid_label_is_400(monkeypatch):
    def boom(conn, pid, **kw):
        raise person_edit.Invalid("label names must not be blank")

    monkeypatch.setattr(person_edit, "update", boom)
    r = client.patch("/people/alice@x.com", json={"labels": {"add": [" "]}})
    assert r.status_code == 400 and "blank" in r.text


def test_person_out_carries_labels():
    r = client.get("/people/alice@x.com")
    assert r.json()["labels"] == ["family"]
    assert "relationship_label" not in r.json()
```

Replace `test_post_passes_notes_and_label_through` with:

```python
def test_post_passes_notes_and_labels_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "api.routers.people.person_create.create",
        lambda conn, **kw: seen.update(kw) or row(id=7),
    )
    client.post(
        "/people",
        json={
            "contact": {"emailAddresses": [{"value": "a@example.com"}]},
            "notes": "hi",
            "labels": ["colleague"],
        },
    )
    assert seen["notes"] == "hi" and seen["labels"] == ["colleague"]
```

Add `import repo.labels as labels_repo` to the imports, and append:

```python
def test_list_labels(monkeypatch):
    monkeypatch.setattr(
        labels_repo,
        "list_with_counts",
        lambda conn: [{"name": "Climbing", "count": 2}, {"name": "investor", "count": 0}],
    )
    r = client.get("/labels")
    assert r.status_code == 200
    assert r.json() == {
        "results": [{"name": "Climbing", "count": 2}, {"name": "investor", "count": 0}]
    }


def test_people_with_label(monkeypatch):
    monkeypatch.setattr(
        labels_repo,
        "find_by_name",
        lambda conn, name: [{"resource_name": "contactGroups/a", "name": "Climbing"}],
    )
    seen = {}

    def with_label(conn, rn):
        seen["rn"] = rn
        return [row()]

    monkeypatch.setattr(people_repo, "with_label", with_label)
    r = client.get("/labels/climbing")
    assert r.status_code == 200
    assert seen["rn"] == "contactGroups/a"
    assert [p["email"] for p in r.json()["results"]] == ["alice@x.com"]


def test_people_with_label_handles_spaces(monkeypatch):
    monkeypatch.setattr(
        labels_repo,
        "find_by_name",
        lambda conn, name: [{"resource_name": "contactGroups/b", "name": name}],
    )
    monkeypatch.setattr(people_repo, "with_label", lambda conn, rn: [])
    r = client.get("/labels/Book%20Club")
    assert r.status_code == 200 and r.json() == {"results": []}


def test_people_with_unknown_label_is_404(monkeypatch):
    monkeypatch.setattr(labels_repo, "find_by_name", lambda conn, name: [])
    assert client.get("/labels/nope").status_code == 404


def test_people_with_ambiguous_label_is_409(monkeypatch):
    monkeypatch.setattr(
        labels_repo,
        "find_by_name",
        lambda conn, name: [
            {"resource_name": "contactGroups/1", "name": "VIP"},
            {"resource_name": "contactGroups/2", "name": "vip"},
        ],
    )
    r = client.get("/labels/Vip")
    assert r.status_code == 409 and "exact spelling" in r.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -q`
Expected: FAIL — `relationship_label` keyword errors, `KeyError: 'labels'`, and `404` on `/labels`.

- [ ] **Step 3: Update `api/routers/people.py`**

In `PersonOut`, replace `relationship_label: str | None` with `labels: list[str] = []`.

Replace `PersonCreate` and `PersonPatch` (and delete the `_relationship_label_not_blank` validator — blank names are a `400` from the service, spec §5.2):

```python
class PersonCreate(BaseModel):
    contact: dict
    notes: str | None = None
    labels: list[str] | None = None


class LabelChange(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class PersonPatch(BaseModel):
    notes: str | None = None
    labels: LabelChange | None = None
    contact: dict | None = None
```

Drop `field_validator` from the pydantic import if nothing else uses it.

In `to_out`, replace `relationship_label=row.get("relationship_label"),` with `labels=row.get("labels") or [],`.

In `create_person`, replace `relationship_label=body.relationship_label,` with `labels=body.labels,`.

In `patch_person`, replace `relationship_label=body.relationship_label,` with:

```python
                labels=body.labels.model_dump() if body.labels is not None else None,
```

- [ ] **Step 4: Create `api/routers/labels.py`**

```python
"""Labels — Google contact groups (multiple-labels design §6.2). Served from
the DB alone; no Google calls on the read path."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.routers.people import PersonList, to_out
from clients import db
from repo import labels as labels_repo
from repo import people
from services import labels as label_rules

router = APIRouter()


class LabelOut(BaseModel):
    name: str
    count: int


class LabelList(BaseModel):
    results: list[LabelOut]


@router.get("/labels", response_model=LabelList)
def list_labels() -> LabelList:
    with db.get_conn() as conn:
        rows = labels_repo.list_with_counts(conn)
    return LabelList(results=[LabelOut.model_validate(r) for r in rows])


@router.get("/labels/{name}", response_model=PersonList)
def people_with_label(name: str) -> PersonList:
    with db.get_conn() as conn:
        candidates = {r["resource_name"]: r["name"] for r in labels_repo.find_by_name(conn, name)}
        try:
            rn = label_rules.match_name(candidates, name)
        except label_rules.AmbiguousLabel as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        if rn is None:
            raise HTTPException(status_code=404, detail=f"no label {name!r}")
        return PersonList(results=[to_out(r) for r in people.with_label(conn, rn)])
```

- [ ] **Step 5: Register the router in `api/main.py`**

```python
from api.routers import imessage, labels, linkedin, people, search, whatsapp
...
app.include_router(labels.router)
```

- [ ] **Step 6: Run to verify they pass**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS (whole suite green again).

- [ ] **Step 7: Full checks, then commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
git add services/person_edit.py services/person_create.py services/contact_fields.py \
  tests/test_person_edit.py tests/test_person_create.py tests/test_contact_fields.py \
  api/ tests/test_api.py
git commit -m "labels: add/remove on PATCH, labels on create, GET /labels routes"
```

---

### Task 6: Skills, CLAUDE.md, and the spec's reserved-name wording

**Files:**
- Modify: `.claude/skills/editing-person/SKILL.md`, `.claude/skills/creating-person/SKILL.md`, `.claude/skills/fetching-person/SKILL.md`, `.claude/skills/searching-people/SKILL.md`, `.claude/skills/people-architecture/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-10-02-multiple-labels-design.md` §4.3, §5.2

**Interfaces:** none (docs).

- [ ] **Step 1: Find every stale reference**

Run: `grep -rn "relationship_label\|relationship label" CLAUDE.md .claude/skills/`
Expected: the hits listed in the File Structure table. Every one is updated in the steps below; none may remain except where a sentence explains that `labels` replaced it.

- [ ] **Step 2: `editing-person`**

- Frontmatter description: replace "tag alice as a colleague", "set the relationship label for that contact" with "tag alice as an investor", "add/remove a label", "put bob on the climbing list".
- Replace each `-d '{"relationship_label": "colleague"}'` example with `-d '{"labels": {"add": ["colleague"]}}'`, and add one remove example: `-d '{"labels": {"remove": ["prospect"]}}'`.
- Replace the `relationship_label` bullet with: **`labels`** — `{"add": [...], "remove": [...]}`, either optional. Adds create a label that doesn't exist yet; adding a label someone already has, or removing one they don't, is a no-op. Matching ignores case. Other labels are never touched. `400` for a blank name, a name in both lists, `Inbox`, or a Google built-in group name (`Starred`, `My Contacts`) that no label of yours shares; `409` when a name matches two labels that differ only in case — use the exact spelling.
- Output template: `labels: <comma-separated, or "none">`.
- The "not valid keys inside `contact`" sentence: `memberships` is owned by `labels`.

- [ ] **Step 3: `creating-person`**

Replace `"relationship_label": "..."` with `"labels": ["..."]` in the body shape and the worked `curl`; update the "`notes` and `relationship_label` are" sentence to "`notes` and `labels` are".

- [ ] **Step 4: `fetching-person`**

Field list: `relationship_label` → `labels` (a list). Output template line: `<labels, comma-separated, if any> · first seen …`. The `notes` paragraph: "`labels` are the Google contact's labels (contact groups)".

- [ ] **Step 5: `searching-people`**

- Description: add "who's tagged X", "list everyone on my X list", "what labels do I have".
- New section **Listing by label**:
  ```bash
  TOKEN=$(gcloud auth print-identity-token)
  curl -s -H "Authorization: Bearer $TOKEN" https://people-api.drolet.cloud/labels
  curl -s -H "Authorization: Bearer $TOKEN" "https://people-api.drolet.cloud/labels/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "Book Club")"
  ```
  `GET /labels` lists every label with its member count; `GET /labels/{name}` returns everyone with that label — all of them, most recent interaction first, regardless of eligibility. `404` unknown label, `409` ambiguous case.
- Example response and output template: `relationship_label` → `labels`.

- [ ] **Step 6: `people-architecture` and `querying-people-db`**

- `people-architecture`: the column list drops `relationship_label`; the source-of-truth row becomes `labels` (see Step 7's wording); add `GET /labels`, `GET /labels/{name}` to the route list; `PATCH` body bullets use `labels`.
- `querying-people-db`: add rows for `contact_groups` (`resource_name` PK, `name`, `updated_at` — user-defined labels only, replaced every sync) and `people_labels` (`person_id`, `group_resource_name`, both FK `ON DELETE CASCADE`). Note `people.relationship_label` is unused and pending removal. Add an example:
  ```sql
  SELECT p.id, p.email, p.display_name
  FROM people p JOIN people_labels pl ON pl.person_id = p.id
  JOIN contact_groups g ON g.resource_name = pl.group_resource_name
  WHERE lower(g.name) = lower('climbing');
  ```

- [ ] **Step 7: `CLAUDE.md`**

- Stack table, Database row: add `contact_groups`, `people_labels` to the table list.
- Source-of-truth table: replace the `relationship_label` row with:
  `| labels (`contact_groups`, `people_labels`) | Google **contact groups** (user-defined; system groups and `GOOGLE_CONTACT_GROUP` excluded) | Google → DB — `contact_groups` replaced from `contactGroups.list` on every sync, so renames/deletes need no per-contact work; a person's memberships rewritten on every apply. `PATCH` adds/removes membership (`{"labels": {"add", "remove"}}`) without touching other labels. |`
- Code layout: add `repo/labels.py` (contact_groups/people_labels), `services/labels.py` (pure label rules: user groups, membership rns, name matching, reserved names), `api/routers/labels.py` (`GET /labels`, `GET /labels/{name}`).
- Piece 3 paragraph: "If `notes`/`labels` was given".
- Editable-contact-fields paragraph: "they're owned by the dedicated `notes` and `labels` fields".
- Add a short **Labels (2026-10-02 design)** subsection after "Editable contact fields" pointing at the spec, and noting `people.relationship_label` is unused and dropped in a follow-up.

- [ ] **Step 8: Spec wording**

In the spec:
- §4.3, replace the first paragraph's description of `labels.person_labels(person, user_groups)` with: `apply_person` passes `labels.membership_rns(person)` — every group rn on the contact, unfiltered — to `repo/labels.py::set_contact_labels(conn, resource_name, rns)`, which replaces the person's rows by joining against `contact_groups`. The join is the filter: system groups, `Inbox`, and any group newer than the last refresh are dropped, never an FK error.
- §5.2, replace the "reserved name" bullet with: the reserved group (`GOOGLE_CONTACT_GROUP`), always; or a name that matches no user label but does match a Google system group (`name` or `formattedName`). A user label sharing a system group's name ("Family") is an ordinary label.
- §3 table: `services/labels.py` row lists `membership_rns`, `normalize_change`, `match_name`, `resolve`; `repo/labels.py` row lists `set_contact_labels(conn, resource_name, group_rns)`.

- [ ] **Step 9: Verify and commit**

Run: `grep -rn "relationship_label" CLAUDE.md .claude/skills/`
Expected: only lines that say `labels` replaced it / the column is pending removal.

```bash
git add CLAUDE.md .claude/skills docs/superpowers/specs/2026-10-02-multiple-labels-design.md
git commit -m "docs: labels in skills, CLAUDE.md, and spec"
```

---

### Task 7: Local end-to-end verification, migration, and PR

**Files:** none (verification and shipping).

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Apply the schema to the real DB (additive, safe before merge)**

```bash
scripts/fetch-env.sh
.venv/bin/python scripts/migrate_db.py
```
Expected: `Migration complete`. The old deployed code is unaffected (nothing it reads changed).

- [ ] **Step 2: Exercise labels end to end against a throwaway contact**

Use the `verifying-pr-locally` skill to start the local API against the real DB. Then create a throwaway person — this also exercises `POST /people` with `labels`. `+1 555-0100-0199` numbers are reserved as fictional.

```bash
TOKEN=$(gcloud auth print-identity-token)
BASE=http://localhost:8080
ID=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$BASE/people" \
  -d '{"contact": {"names": [{"givenName": "E2E Labels Test"}], "phoneNumbers": [{"value": "+15550100999"}]},
       "labels": ["e2e-label-a"]}' | tee /dev/stderr | jq -r .id)
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$BASE/people/$ID" -d '{"labels": {"add": ["e2e-label-b"]}}' | jq .labels
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/labels" | jq '.results[] | select(.name|startswith("e2e"))'
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/labels/E2E-LABEL-A" | jq '[.results[].id]'
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$BASE/people/$ID" -d '{"labels": {"remove": ["e2e-label-a", "never-existed"]}}' | jq .labels
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' "$BASE/people/$ID" -d '{"labels": {"add": ["Inbox"]}}'
```
Expected, in order: a `201` body with `"labels": ["e2e-label-a"]`; `["e2e-label-a", "e2e-label-b"]`; both e2e labels with `count: 1`; `[$ID]`; `["e2e-label-b"]`; `400`.

In the Google Contacts UI, confirm "E2E Labels Test" carries `e2e-label-b` only. Then delete both `e2e-*` labels and the contact there. Run `curl -s -X POST -H "Authorization: Bearer $TOKEN" "$BASE/people/$ID/sync"` — it refreshes `contact_groups` — and confirm `GET /labels` lists no `e2e-*` label. The `people` row is left with `google_deleted_at` set after the next nightly sync, like any contact deleted in Google.

Post the results to the PR per the `verifying-pr-locally` skill once the PR exists (Step 4).

- [ ] **Step 3: Full checks**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -q
```
Expected: all pass.

- [ ] **Step 4: Open the PR**

Use the `/pr-open` skill (CLAUDE.md: never hand-roll `git push` + `gh pr create`). The description must include the post-merge rollout below as a checklist.

- [ ] **Step 5: Post-merge rollout (after the PR merges and both deploy workflows succeed)**

```bash
.venv/bin/python scripts/clear_sync_token.py
URL=$(gcloud functions describe people-sync --region us-central1 --format='value(serviceConfig.uri)')
curl -s -X POST -H "Authorization: Bearer $(gcloud secrets versions access latest --secret=people-sync-token)" "$URL"
```
Expected: counts with `updated` near the contact total (one full pass).

Backfill check (spec §8 step 4) via `querying-people-db` — must return zero rows:

```sql
SELECT p.id, p.relationship_label
FROM people p
WHERE p.relationship_label IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM people_labels pl JOIN contact_groups g ON g.resource_name = pl.group_resource_name
    WHERE pl.person_id = p.id AND lower(g.name) = p.relationship_label
  );
```

UI-propagation check (spec §8 step 5): in the Contacts UI, add a label named `e2e-ui-check` to any contact that has a `people` row. Re-run `people-sync` (command above), then confirm `GET /labels/e2e-ui-check` lists that person. Delete the `e2e-ui-check` label in the UI afterwards; the next sync removes it from `contact_groups`.

- [ ] **Step 6: Follow-up PR — drop the column**

Only after Step 5 is clean. On a new branch, append to `repo/schema.sql`:

```sql
-- Replaced by contact_groups/people_labels (multiple-labels design §8 step 6).
ALTER TABLE people DROP COLUMN IF EXISTS relationship_label;
```
Remove the "pending removal" notes from `CLAUDE.md` and `querying-people-db`; run `migrate_db.py` after merge; open with `/pr-open`.
