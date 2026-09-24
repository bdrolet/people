# Editable Google Contact Fields — Design

**Date:** 2026-09-24
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

`PATCH /people/{email}` writes exactly two things to Google Contacts today:
the biography (`notes`) and contact-group membership (`relationship_label`).
Every other field on a contact — name, phone numbers, organization, birthday,
addresses — can only be edited in the Google UI.

This design opens the rest of the contact card to the API. A single generic
`contact` map on the existing PATCH body is validated against an allowlist and
written to Google in one `updateContact` call; the DB row is then refreshed
from Google, as it is today. Read-back is served from four new columns on
`people`: three typed ones for the fields worth querying, and one JSONB column
for everything else.

- **Google stays the source of truth.** Write Google first, refresh the DB from
  it. No new writer, no two-way merge.
- **One input path.** The typed columns are derived on refresh, never written
  directly by a caller.
- **Email addresses are add-only** (§5.3), because `people.email` is the
  primary key and the Google linkage key.

Extends the parent design
`docs/superpowers/specs/2026-09-03-people-service-extraction-design.md` (§4.3
source of truth, §9 PATCH).

## 2. Goals and non-goals

**Goals**

1. Edit any field Google supports on a linked contact, through `people-api`.
2. Read those fields back through the API, without a live Google call per read.
3. Query on the two that matter for existing work: phone numbers and company.
4. Keep the existing `notes` / `relationship_label` behavior byte-for-byte.

**Non-goals**

- Editing a contact that `people` has not linked (`409`, as today).
- Creating or deleting Google contacts through the API. Creation stays with
  the eligibility path; deletion stays in the Google UI.
- Removing or changing an existing email address (§5.3).
- Photos, and anything Google does not accept in `updatePersonFields`.
- Re-validating Google's own field schemas (§5.4).
- Writing any of these fields to HubSpot.
- Changing how the iMessage import matches handles. The new
  `phone_numbers` column makes a DB-side match possible later; that is a
  follow-up, deliberately out of scope here (§10).

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `clients/google_contacts.py` | clients | Widen `PERSON_FIELDS`; add `WRITABLE_FIELDS`; add `update_fields(resource_name, etag, fields)` — one `updateContact` with a computed `updatePersonFields` mask. `update_biography` is folded into it. |
| `services/contact_fields.py` | services | **New, pure.** `validate(contact)` → allowlist and shape check; `check_email_addition(existing, submitted, keyed_email)`; `derive(person)` → the four DB values from a Google person payload. No I/O. |
| `services/person_edit.py` | services | Orchestrates: fetch live person → validate → one write → refresh. Unchanged contract for `notes` / `relationship_label`. |
| `services/google_contacts_sync.py` | services | Uses `contact_fields.derive` so link, nightly sync, and post-edit refresh derive identically. |
| `repo/people.py` | repo | `update_from_google` / `create_from_google` persist the four new values. |
| `repo/schema.sql` | repo | Four columns and three indexes (§4). |
| `api/routers/people.py` | api | `PersonPatch.contact`; `PersonOut` gains four fields. |
| `api/routers/search.py` | api | `company` joins the trigram match. |
| `.claude/skills/editing-person/` | skill | How to edit the new fields. |

## 4. Data model

```sql
ALTER TABLE people
  ADD COLUMN IF NOT EXISTS phone_numbers TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS company       TEXT,
  ADD COLUMN IF NOT EXISTS job_title     TEXT,
  ADD COLUMN IF NOT EXISTS google_fields JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS people_phone_numbers_idx ON people USING gin (phone_numbers);
CREATE INDEX IF NOT EXISTS people_google_fields_idx ON people USING gin (google_fields);
CREATE INDEX IF NOT EXISTS people_company_trgm_idx  ON people USING gin (company gin_trgm_ops);
```

`google_fields` holds every allowlisted field exactly as Google returns it,
including `phoneNumbers` and `organizations` with their type labels. The three
typed columns are a **derived index** over that payload, not a replacement:

- `phone_numbers` — every `phoneNumbers[].value`, E.164-normalized with
  `services.imessage_export.normalize_handle`, unparseable ones dropped,
  order preserved, duplicates removed. Normalizing with the same function the
  iMessage import uses is what makes a future DB-side match possible (§10).
- `company` / `job_title` — `name` and `title` of the `organizations` entry
  flagged `metadata.primary`, else the first entry. NULL when absent.

### 4.1 Source-of-truth addendum

Adds one row to the parent spec §4.3:

| Field | Truth | Direction |
|---|---|---|
| `phone_numbers`, `company`, `job_title`, `google_fields` | Google Contacts | Google → DB on link, nightly sync, and after every PATCH. Event data never writes them; they are never pushed to HubSpot. |

### 4.2 Read mask and the sync token

`PERSON_FIELDS` widens from
`names,emailAddresses,memberships,biographies,metadata` to additionally cover
the readable allowlist (§5.1). `connections.list` treats a changed
`personFields` as incompatible with an existing `syncToken`, so **the first
nightly sync after deploy performs a full resync.** That path already exists
and self-heals (`_is_expired_sync_token`, commit `3af8246`); the run is
expected, not a regression. It is called out here so the logs read correctly.

## 5. Writing

### 5.1 Allowlist

`WRITABLE_FIELDS` in `clients/google_contacts.py`:

```
addresses, birthdays, calendarUrls, clientData, emailAddresses, events,
externalIds, genders, imClients, interests, locales, locations, miscKeywords,
names, nicknames, occupations, organizations, phoneNumbers, relations,
sipAddresses, urls, userDefined
```

Deliberately excluded, rejected with `400` naming the key:

| Key | Why |
|---|---|
| `biographies` | Owned by `notes`. Two writers for one field is how they drift. |
| `memberships` | Owned by `relationship_label`, which moves group membership deliberately (parent spec §4.3). |
| `metadata`, `photos` | Not writable through `updateContact`. |
| anything else | Unknown to the People API; rejecting early beats a confusing Google error. |

The read mask (§4.2) is `WRITABLE_FIELDS` plus `memberships,biographies,metadata`.

This list must be **verified against the live People API at implementation
time** — the set `updateContact` accepts in `updatePersonFields` has changed
across API revisions. Any entry Google rejects is dropped from the constant and
the removal noted in the PR, rather than shipped and discovered by a caller.

### 5.2 Shape validation

`contact` must be a JSON object. Each value must be a list of objects, except
`genders`/`birthdays`-style singletons, which Google also accepts as a list —
so the rule is simply **list of objects**, and a bare object is wrapped into a
one-element list before sending. Anything else is `400`.

Validation stops there. Google validates its own field schemas, and its error
message is surfaced verbatim (§5.5); re-implementing those rules here would be
a second source of truth that goes stale.

### 5.3 Email addresses are add-only

If `emailAddresses` is submitted, then, comparing values normalized with
`services.eligibility.normalize`:

1. Every address the contact currently has must still be present, and
2. the address the `people` row is keyed by must be present.

Otherwise `409`, with a message saying removals and changes go through the
Google UI. Rationale: `people.email` is the primary key and the key by which
contacts are linked (`search_by_email`); allowing a removal or rewrite here
would orphan the row from its contact.

### 5.4 One write

`person_edit.update`:

1. `row = people.get(...)` → `404` if absent, `409` if `google_resource_name`
   is NULL (unchanged).
2. `live = gc.get_person(rn)` — fresh etag and memberships (unchanged).
3. Validate `contact` (§5.2), then the email rule against `live` (§5.3).
4. **One** `gc.update_fields(rn, etag, fields)` where `fields` is the submitted
   map plus `biographies` when `notes` was given, and `updatePersonFields` is
   the comma-joined keys of `fields`. Skipped entirely when nothing but
   `relationship_label` was sent.
5. `relationship_label` → `_set_label(...)`, unchanged, a separate API.
6. `finally:` refresh the row via `gsync.sync_one`, unchanged — a partial write
   still lands in the DB as whatever Google now holds.

Replacing the two-call (`update_biography`, then fields) shape with one call
means the fields land together or not at all.

### 5.5 Errors

| Condition | Status | Body |
|---|---|---|
| Unknown / excluded key | 400 | names the offending keys |
| Bad shape | 400 | names the key and what was expected |
| Email removal or change | 409 | "removals and changes go through the Google UI" |
| Stale etag (Google `FAILED_PRECONDITION`) | 409 | "contact changed meanwhile; retry" |
| Other Google 4xx | 400 | Google's own message, verbatim |
| No Google contact linked | 409 | unchanged |
| Unknown person | 404 | unchanged |

## 6. `people-api` changes

### 6.1 `PersonPatch`

```json
{"notes": "...", "relationship_label": "...", "contact": { "<googleField>": [ ... ] }}
```

All three optional; `contact` defaults to `None` (absent), which is distinct
from `{}` (present but empty → no field write).

### 6.2 `PersonOut`

Gains `phone_numbers: list[str]`, `company: str | None`,
`job_title: str | None`, `contact: dict | None`. Detail and PATCH responses
fill all four. List responses (`GET /people`, `POST /search` `results`) fill
the three typed columns — they are already on the row — and return
`contact: null`, keeping listings one query. Additive, so inbox's
classify-time consumer is unaffected.

### 6.3 `POST /search`

`company` joins the existing ILIKE/trigram match on name and email, so "who
works at Acme" is one call. Response shape is unchanged.

## 7. Skills and docs

- `editing-person`: the `contact` map, a worked example per common field
  (phone, name, organization, birthday), the add-only email rule, and that
  `notes` / `relationship_label` keep their own dedicated fields.
- `fetching-person`, `searching-people`: the new response fields.
- `querying-people-db`: the four columns, with example JSONB queries.
- `people-architecture`, `CLAUDE.md`: source-of-truth row, layout entries.

## 8. Observability

No new metrics. The new routes are the existing PATCH route; the API request
middleware already covers it. `person_edit` keeps its existing warning on a
failed post-edit resync.

## 9. Testing

- `tests/test_contact_fields.py` — allowlist accept/reject per excluded key;
  shape validation including the bare-object wrap; email add-only (unchanged
  set passes, addition passes, removal fails, keyed-address removal fails,
  case/whitespace differences do not count as removal); `derive` for
  E.164 normalization, unparseable numbers dropped, primary-vs-first
  organization, missing organization → NULL, empty payload → `{}`.
- `tests/test_person_edit.py` (existing) — one `updateContact` call carrying
  the joined `updatePersonFields`; `notes` still writes a biography; label-only
  edits issue no `updateContact`; stale etag → the 409-mapped exception;
  post-edit resync still runs on failure.
- `tests/test_google_contacts.py` — `update_fields` builds the right mask and
  body; `PERSON_FIELDS` includes the allowlist.
- `tests/test_repo_people.py` — the four values persist through
  `update_from_google` / `create_from_google`.
- `tests/test_schema.py` — new columns and indexes exist; JSONB round-trips.
- `tests/test_api.py` — PATCH accepts `contact`; rejections map to 400/409;
  `PersonOut` carries the four fields; list responses keep `contact: null`;
  search matches on company.
- Live verification (§11).

## 10. Follow-up, not in this spec

`phone_numbers` is normalized with the same function the iMessage import uses,
so a later change could match handles against the DB instead of rebuilding the
Google phone index on every run. Not done here: it would change iMessage
matching semantics, which deserves its own change and its own review.

## 11. Rollout

1. `scripts/migrate_db.py` from the branch (all statements `IF NOT EXISTS`,
   safe against the running service).
2. Merge; CI deploys `people-api`.
3. Live check: `PATCH` a real contact with a phone number and an organization,
   read it back through `GET /people/{email}`, and confirm the same values in
   the Google UI.
4. Expect one full resync on the next nightly run (§4.2).

No Terraform, secrets, Cloud Function, or inbox changes.

## 12. Decisions

| Decision | Choice | Why |
|---|---|---|
| Storage | Typed columns for phone/company/title + JSONB for the rest | Query ergonomics where it matters; arbitrary fields without a migration each time. |
| Input | One generic `contact` map; typed columns derived | One way to set a value; no ambiguity between a typed input and the blob. |
| Allowlist | Explicit, with `biographies`/`memberships` excluded | They already have owners; a second writer would drift. |
| Email addresses | Add-only | `people.email` is the primary key and the linkage key; a removal would orphan the row. |
| Validation depth | Allowlist + shape only | Google owns its schemas; duplicating them would go stale. |
| Write shape | One `updateContact` for all fields incl. biography | Fields land together; fewer round trips. |
| Read mask | Widen `PERSON_FIELDS` | Accepts one full resync, which the sync already self-heals. |
