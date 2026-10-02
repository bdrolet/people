# Multiple Labels per Person — Design

**Date:** 2026-10-02
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

Ben wants to tag people and keep lists of them — "investors", "climbing",
"holiday cards". Google Contacts already does this: **labels** in the Contacts
UI, **contact groups** in the People API. A contact can carry any number of
them.

`people` reads labels today, but only as a single value.
`services/google_contacts_sync.py::relationship_label` returns the **first**
user-defined group on a contact and drops the rest, and
`services/person_edit.py::_set_label` *moves* a person from one group to
another. A contact labelled both "climbing" and "investor" in the Contacts UI
shows up in `people` as one or the other, depending on membership order.

This design replaces `relationship_label` with **`labels`** — every
user-defined group a contact belongs to — end to end: a derived index in the
DB, add/remove semantics on `PATCH`, a `labels` list on `POST /people`, and two
new read routes for "what labels exist" and "who has this label".

Google stays the source of truth. The DB only ever reflects what Google holds.

## 2. Goals and non-goals

**Goals**

1. Read every user-defined label on a contact into the index — not just the
   first.
2. Add and remove labels on a person without disturbing their other labels.
3. List the labels that exist, with member counts; list everyone carrying a
   given label.
4. Pick up a label renamed or deleted in the Contacts UI on the next sync,
   with no per-contact work.
5. Remove `relationship_label` entirely — one concept, not two.

**Non-goals**

- Matching labels in `POST /search`. Listing by label (§6.2) covers the "who is
  tagged X" question; free-text search over label names can follow if wanted.
- Renaming or deleting a label through the API. Both stay in the Contacts UI;
  the sync picks them up (§4.2).
- Labels on people who have no Google contact. A label *is* a Google group
  membership; an unlinked row has nowhere to hold one (§5.3).
- Pushing labels to HubSpot. HubSpot never received `relationship_label`, and
  this changes nothing there.
- Exposing system groups (`myContacts`, `starred`, Google's built-in
  `friends`/`family`/`coworkers`) or people's own `GOOGLE_CONTACT_GROUP`
  (`Inbox`) group as labels. They stay hidden, as today.

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `repo/schema.sql` | repo | **New** tables `contact_groups`, `people_labels` (§4.1). Additive only; `people.relationship_label` is left in place, unused, and dropped in a follow-up (§8). |
| `repo/labels.py` | repo | **New.** `replace_groups(conn, groups)` — make `contact_groups` exactly match Google's user-defined groups; `set_person_labels(conn, person_id, group_rns)` — replace one person's memberships; `list_with_counts(conn)`; `find_by_name(conn, name) -> list[dict]` (case-insensitive). |
| `repo/people.py` | repo | `_COLUMNS` gains a `labels` array (§6.1). `relationship_label` leaves `_COLUMNS` and every write signature. **New** `with_label(conn, group_rn) -> list[dict]`. |
| `clients/google_contacts.py` | clients | **New** `list_groups_by_rn() -> dict[str, dict]` keyed by `resourceName`, each value `{name, formattedName, groupType}`, so a user group named "Family" can't collide with the built-in "Family" system group (`list_groups()`'s name-keyed dict lets one overwrite the other). Label code uses only the new function; `list_groups()`, `ensure_group` and `modify_group_members` are unchanged. |
| `services/labels.py` | services | **New**, pure. `user_groups(groups_by_rn) -> dict[str, str]` (rn → name, system and `GOOGLE_CONTACT_GROUP` filtered out); `person_labels(person, user_groups) -> list[str]` (the contact's user-group rns); `validate_change(add, remove) -> tuple[list[str], list[str]]` (§5.2). |
| `services/google_contacts_sync.py` | services | `relationship_label()` is deleted. `run_sync` and `sync_one` refresh `contact_groups` before applying persons (§4.2); `apply_person` writes `people_labels` for the row it touched (§4.3). |
| `services/person_edit.py` | services | `relationship_label=` becomes `labels: dict | None` (`{"add": [...], "remove": [...]}`); `_set_label` becomes `_apply_labels` (§5.1). |
| `services/person_create.py` | services | `relationship_label=` becomes `labels: list[str] | None`, applied after creation as `person_edit.update(..., labels={"add": labels})`. |
| `services/contact_fields.py` | services | `OWNED_ELSEWHERE["memberships"]` names `labels` instead of `relationship_label`. |
| `api/routers/people.py` | api | `PersonOut.relationship_label` → `labels: list[str]`; `PersonPatch.labels`, `PersonCreate.labels` (§5). |
| `api/routers/labels.py` | api | **New.** `GET /labels`, `GET /labels/{name}` (§6.2). Registered in `api/main.py`. |

## 4. Data model and sync

### 4.1 Schema

```sql
-- Google contact groups (labels), user-defined only — system groups and
-- GOOGLE_CONTACT_GROUP are never stored. Replaced wholesale from
-- contactGroups.list on every sync, so a rename or delete in the Contacts UI
-- lands without revisiting any contact. (multiple-labels design §4)
CREATE TABLE IF NOT EXISTS contact_groups (
    resource_name TEXT PRIMARY KEY,           -- contactGroups/abc123
    name          TEXT NOT NULL,              -- as Google spells it
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contact_groups_lower_name_idx ON contact_groups (lower(name));

CREATE TABLE IF NOT EXISTS people_labels (
    person_id           BIGINT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    group_resource_name TEXT   NOT NULL REFERENCES contact_groups(resource_name) ON DELETE CASCADE,
    PRIMARY KEY (person_id, group_resource_name)
);
CREATE INDEX IF NOT EXISTS people_labels_group_idx ON people_labels (group_resource_name);
```

Names live in one place (`contact_groups`) and are joined at read time, which
is what makes renames free. Both FKs cascade: deleting a person
(`scripts/merge_duplicate_contacts.py` deletes losers) or a group drops the
membership rows with it.

The lower-name index is **not unique**. Google allows names that differ only in
case; §5.2 says what happens when a name is ambiguous.

### 4.2 Refreshing groups

`run_sync` already calls `gc.list_groups()` once per run. It now calls
`gc.list_groups_by_rn()` instead, then `labels_repo.replace_groups(conn,
labels.user_groups(...))` **before** applying any person:

- new groups are inserted, renamed ones get the new `name` and `updated_at`;
- groups no longer present are deleted, cascading their `people_labels` rows.

`sync_one` (the post-PATCH refresh and `POST /people/{ident}/sync`) does the
same, so a label created by a `PATCH` exists in `contact_groups` before the
person's membership row references it.

Why this matters: renaming a label does not change any contact's etag, so the
incremental sync never revisits its members. Storing the name per-person would
leave every member showing the old name until something else about them
changed. Refreshing the group table every run costs nothing — the list call
already happens.

### 4.3 Per-person memberships

`apply_person` computes `labels.person_labels(person, user_groups)` — every
membership whose `contactGroupResourceName` is a user group — and, once it
knows the row's `id`, calls `labels_repo.set_person_labels(conn, id, rns)`,
which deletes that person's rows and inserts the new set. This runs on every
branch that writes a row: link, create/adopt, the already-linked update, and
promotion.

A contact Google reports as **deleted** (`metadata.deleted`) gets its
`people_labels` cleared along with `mark_google_deleted`: the row stays, the
contact is gone, and so are its labels.

Membership changes made in the Contacts UI reach `people` the way every other
contact edit does — through the incremental sync revisiting the changed
contact. The current single-label design already relies on this; rollout
verifies it explicitly (§8 step 5).

## 5. Writing labels

### 5.1 `PATCH /people/{ident}`

```json
{"labels": {"add": ["investor", "Climbing"], "remove": ["prospect"]}}
```

Either list may be omitted or empty. `labels` sits alongside `notes` and
`contact` and may be combined with them in one request.

`person_edit.update(..., labels=...)`:

1. `validate_change` (§5.2) — **before** any write, as `contact` validation is
   today, so a rejected request changes nothing.
2. Fetch user groups (`list_groups_by_rn` → `user_groups`). Resolve each name
   case-insensitively.
3. For each **add**: an existing group → its rn; no group by that name →
   `gc.ensure_group(name)` creates it with the name exactly as given. Skip any
   group the person is already in (from `live` memberships).
4. For each **remove**: resolve the group; if it doesn't exist or the person
   isn't in it, skip — removing a label someone doesn't have is a no-op, not an
   error.
5. One `gc.modify_group_members(rn, [person_rn], [])` per added group, one
   `gc.modify_group_members(rn, [], [person_rn])` per removed group.
6. The existing `finally: sync_one` refresh pulls groups and memberships back
   from Google, so the response reflects what Google actually holds — including
   after a partial failure.

Ordering inside `update` is unchanged otherwise: the `updateContact` field
write (notes, contact) happens first, then label changes, then the refresh.

### 5.2 Validation (400)

`validate_change(add, remove)` raises `Invalid` for:

- a blank or whitespace-only name (names are stripped; surrounding whitespace
  is not significant);
- the same name, case-insensitively, in both `add` and `remove`;
- a **reserved** name: `GOOGLE_CONTACT_GROUP` (`Inbox`), or the name of any
  system group (`myContacts`, `starred`, and Google's built-in
  `friends`/`family`/`coworkers` by both `name` and `formattedName`). Reserved
  names are checked in step 2, against the live group list, since the set of
  system groups is Google's to define.

Duplicates within one list are collapsed, not rejected.

**Ambiguous names.** If two user groups differ only in case and a request names
one case-insensitively, an exact-case match wins; with no exact match the
request is a `409` naming the candidates. This can only arise from labels made
in the Contacts UI.

### 5.3 Errors

| Condition | Status |
|---|---|
| Validation (§5.2) | `400` |
| Person not found | `404` (unchanged) |
| Person has no Google contact (`NotLinked`) | `409`, same as `notes` today |
| Ambiguous label name | `409` (`Conflict`) |
| Google 4xx on group create/modify | `400`, message verbatim (`Invalid`) |
| Google 5xx | `500`, after the `sync_one` refresh |

### 5.4 `POST /people`

`PersonCreate.relationship_label` becomes `labels: list[str] | None`. After the
contact and row are created, `person_create.create` calls
`person_edit.update(conn, row["id"], notes=..., labels={"add": labels})` — the
same post-create step `notes`/`relationship_label` use today.

### 5.5 `contact.memberships`

Still rejected inside the `contact` map with a `400`; the message now names
`labels` as the field that owns it.

## 6. Reading labels

### 6.1 On every person

`PersonOut.relationship_label: str | None` is replaced by
`labels: list[str]` — names as Google spells them, sorted case-insensitively,
`[]` when none. It's present on every response that carries a person: single
fetch, `GET /people?recent=`, `POST /search`, `PATCH`, `POST /people`.

`repo/people.py::_COLUMNS` gains:

```sql
ARRAY(
    SELECT g.name FROM people_labels pl
    JOIN contact_groups g ON g.resource_name = pl.group_resource_name
    WHERE pl.person_id = people.id
    ORDER BY lower(g.name)
) AS labels
```

so every existing read picks it up without a per-call change.

### 6.2 New routes — `api/routers/labels.py`

**`GET /labels`** — every user-defined label, with member counts, including
labels with no members:

```json
{"results": [{"name": "Climbing", "count": 12}, {"name": "investor", "count": 31}]}
```

Sorted by name, case-insensitively.

**`GET /labels/{name}`** — everyone carrying that label, as a `PersonList`
(same shape as `GET /people?recent=`), ordered by last interaction descending.
Case-insensitive name match (§5.2's exact-case rule breaks ties; an ambiguous
name is a `409`). **No limit and no eligibility filter**: a label is a
deliberate list, and a caller asking for "everyone tagged investor" wants all
of them. An unknown label is a `404`.

Both are served from the DB alone — no Google calls on the read path.

## 7. Testing

- **`tests/test_labels.py`** (pure): `user_groups` drops system groups and
  `GOOGLE_CONTACT_GROUP`; `person_labels` returns *every* user group, not the
  first; `validate_change` covers blank, add/remove overlap, in-list duplicates.
- **`tests/test_repo_labels.py`**: `replace_groups` inserts, renames, and
  deletes (asserting the cascade removes memberships); `set_person_labels`
  replaces rather than appends; `_COLUMNS`' `labels` array is sorted and
  reflects a rename without touching `people_labels`.
- **`tests/test_google_contacts_sync.py`**: a contact in two user groups plus
  `Inbox` plus `myContacts` yields two labels; `run_sync` refreshes groups
  before applying persons; a deleted contact's labels are cleared. Existing
  `relationship_label` tests are rewritten against `labels`.
- **`tests/test_person_edit.py`**: add creates a missing group; add of a label
  already held is skipped; remove of an unheld or unknown label is a no-op;
  reserved and ambiguous names reject before any Google write; other groups
  are never touched.
- **`tests/test_person_create.py`**: `labels` passes through as
  `{"add": labels}`.
- **`tests/test_api.py`**: PATCH/POST body shapes and 400s; `PersonOut.labels`;
  `GET /labels`, `GET /labels/{name}` including 404.
- **Local E2E** (`verifying-pr-locally`) against the real DB: add two labels to
  a test contact, read them back, list by label, remove one, confirm in the
  Contacts UI.

## 8. Rollout

1. **Before merge**, from the branch: `scripts/migrate_db.py`. The DDL is
   additive, so the old code (which still reads `relationship_label`) is
   unaffected.
2. Merge. `deploy.yml` and `deploy-api.yml` ship the Functions and the API.
3. `scripts/clear_sync_token.py`, then trigger `people-sync` once
   (`POST` with the bearer token). The full pass fills `contact_groups` and
   `people_labels` for every contact; an incremental pass would only reach
   contacts changed since the last run.
4. **Verify the backfill**: every row with a non-null `relationship_label`
   has a `people_labels` row whose group name lowercases to it. Any exception
   is investigated before step 6.
5. **Verify UI edits propagate**: add a label to a contact in the Contacts UI,
   run `people-sync`, confirm it appears.
6. **Follow-up PR**: `ALTER TABLE people DROP COLUMN IF EXISTS
   relationship_label;` in `repo/schema.sql`, then `migrate_db.py`. It can't
   ship in this PR: `migrate_db.py` executes the whole file, and dropping the
   column before the new code is live breaks the old code's `_COLUMNS`.

The first full sync re-reads every contact; expect `updated` counts near the
contact total for that one run.

**Docs and skills** updated in the same PR: `editing-person` (add/remove,
replacing every `relationship_label` example), `creating-person`,
`fetching-person`, `searching-people` (gains "list everyone tagged X" via
`GET /labels/{name}` and "what labels do I have" via `GET /labels`),
`people-architecture`, `querying-people-db` (the two tables), and `CLAUDE.md`
(source-of-truth table, code layout, schema list).

## 9. Decisions

| Decision | Choice | Why |
|---|---|---|
| Fate of `relationship_label` | Replaced by `labels` | No callers outside this repo and its skills; two overlapping concepts would need a rule for which group is "primary". |
| Storage | `contact_groups` + `people_labels`, joined at read | A rename changes no contact etag, so a per-person name array would go stale; the group list is already fetched every sync. |
| Storage, rejected: `labels TEXT[]` on `people` | — | Simplest, but stale on rename (above). |
| Storage, rejected: read live from Google | — | One API call per lookup; "list everyone tagged X" would hit rate limits, and every other read is served from the index. |
| Write shape | `{"add": [...], "remove": [...]}` | Tagging must never wipe someone's other labels; a full-set replace makes every caller read-modify-write. |
| Unknown label on add | Create the group | Tagging someone with a new label is the common case; requiring a separate create step adds nothing. |
| Unknown/unheld label on remove | No-op | The end state the caller asked for already holds. |
| Name matching | Case-insensitive, display case preserved | Matches today's lowercased-compare behaviour without throwing away how Ben spelled the label. |
| `GET /labels/{name}` limits | None; no eligibility filter | A label is a deliberate list; partial results would be wrong. |
| Column drop | Follow-up PR | `migrate_db.py` runs the whole schema file; an in-PR drop breaks the old code before the new code deploys. |
