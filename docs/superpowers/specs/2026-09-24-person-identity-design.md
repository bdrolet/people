# Person Identity — Design

**Date:** 2026-09-24
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

`people.email` is the primary key. That makes a person without an email
address unrepresentable — yet **553 of Ben's 959 Google contacts have no email
address, and 466 of those have a phone number**. The nightly sync skips every
one of them (`services/google_contacts_sync.py::apply_person`, `if not email:
return`), so more than half the address book is invisible to the service, and
the 590 unmatched iMessage handles have nowhere to land.

This design replaces the email primary key with a surrogate `BIGSERIAL id` and
makes `email` a nullable unique attribute, so a person can be identified by an
email address, a phone number, or both. It is **identity only**: no new rows
are created, no sync behavior changes, and every existing endpoint keeps its
current result.

Ben's constraint, stated during design and encoded here as a database
constraint: *every contact has an email or a phone number; a contact with
neither is a data error to fix, not a state to support.*

This is **piece 1 of 3**:

1. **Identity** (this spec) — the key change, behavior-preserving.
2. **Adopt email-less Google contacts** — stop skipping them in the sync and
   backfill the 553 already in the address book.
3. **`POST /people`** — create a contact deliberately, with an email, a phone,
   or both.

Extends `docs/superpowers/specs/2026-09-03-people-service-extraction-design.md`
(§4.3 source of truth) and the iMessage and contact-field designs, which both
link to `people` by email today.

## 2. Goals and non-goals

**Goals**

1. Identify a person by a surrogate id, with `email` nullable and unique.
2. Let a person be addressed by email, E.164 phone, or id through one route.
3. Keep `GET /people/{email}` byte-for-byte compatible — inbox consumes it in
   production.
4. Move `imessage_handles` and `linkedin_connections` onto `person_id` without
   losing a single existing link.
5. Make "email or phone, never neither" a constraint the database enforces.

**Non-goals**

- Creating any `people` row that does not exist today. The sync still skips
  email-less Google contacts; adopting them is piece 2.
- `POST /people`; that is piece 3.
- Changing eligibility, the event handlers, or the HubSpot mirror's behavior
  for people who have an email.
- Mirroring phone-only people to HubSpot — impossible by construction (§5.2).
- Making person ids durable external references (§4.3).

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `repo/schema.sql` | repo | The migration (§4.1), guarded so `migrate_db.py` stays idempotent. |
| `services/identity.py` | services | **New, pure.** `classify(ident)` → `("email" \| "phone" \| "id", value)`. No I/O. |
| `repo/people.py` | repo | `_COLUMNS` gains `id`; `get_by_id`, `get_by_phone`; `eligible_not_in_hubspot` gains the email guard. |
| `repo/imessage.py`, `repo/linkedin.py` | repo | Write `person_id`; reads join `people` to keep serving `person_email`. |
| `services/imessage_export.py`, `services/linkedin_export.py` | services | Matching resolves to `person_id`. |
| `api/routers/people.py` | api | `{ident}` resolution, 409 on ambiguity, `id` on responses. |
| `api/routers/imessage.py`, `linkedin.py` | api | `person_id` alongside the existing `person_email`. |

## 4. Data model

### 4.1 Migration

Table sizes at design time: `people` 511, `imessage_handles` 824,
`linkedin_connections` 1,885. The whole migration is one sub-second
transaction; no staged rollout is needed.

**Order matters and is not obvious.** The child foreign keys reference
`people(email)`, which is backed by the primary-key index, so
`DROP CONSTRAINT people_pkey` fails with a dependency error while they exist.
The children must therefore be moved off `person_email` **before** the primary
key is swapped, and their new foreign keys added **after** `id` becomes the
primary key (a foreign key needs a unique or primary-key target).

```sql
-- 1. Surrogate key column, populated for every existing row.
ALTER TABLE people ADD COLUMN IF NOT EXISTS id BIGSERIAL;

-- 2. Children gain person_id and are backfilled through the old text link.
--    No FK yet: people.id is not unique until step 3.
ALTER TABLE imessage_handles     ADD COLUMN IF NOT EXISTS person_id BIGINT;
ALTER TABLE linkedin_connections ADD COLUMN IF NOT EXISTS person_id BIGINT;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'imessage_handles' AND column_name = 'person_email') THEN
        UPDATE imessage_handles h SET person_id = p.id
          FROM people p WHERE h.person_email = p.email AND h.person_id IS NULL;
        ALTER TABLE imessage_handles DROP COLUMN person_email;   -- drops its FK too
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'linkedin_connections' AND column_name = 'person_email') THEN
        UPDATE linkedin_connections c SET person_id = p.id
          FROM people p WHERE c.person_email = p.email AND c.person_id IS NULL;
        ALTER TABLE linkedin_connections DROP COLUMN person_email;
    END IF;
END $$;

-- 3. Now nothing depends on the email PK index: swap the key.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'people'::regclass AND i.indisprimary AND a.attname = 'email'
    ) THEN
        ALTER TABLE people DROP CONSTRAINT people_pkey;
        ALTER TABLE people ADD PRIMARY KEY (id);
        ALTER TABLE people ALTER COLUMN email DROP NOT NULL;
        ALTER TABLE people ADD CONSTRAINT people_email_key UNIQUE (email);
    END IF;
END $$;

-- 4. With people.id a primary key, the children can reference it.
DO $$ BEGIN
    ALTER TABLE imessage_handles ADD CONSTRAINT imessage_handles_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE linkedin_connections ADD CONSTRAINT linkedin_connections_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 5. Every person must be reachable by something.
DO $$ BEGIN
    ALTER TABLE people ADD CONSTRAINT people_has_an_identifier
        CHECK (email IS NOT NULL OR cardinality(phone_numbers) > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS imessage_handles_person_id_idx     ON imessage_handles (person_id);
CREATE INDEX IF NOT EXISTS linkedin_connections_person_id_idx ON linkedin_connections (person_id);
```

Dropping `person_email` drops its foreign key with it. 75 handle links and 5
connection links exist today, all pointing at emails that resolve, so the
backfill is exact; both importers recompute their links on every run regardless.

Because `migrate_db.py` applies the whole file, a partially-applied migration
(a crash between steps) resumes correctly on the next run: every step tests the
state it is about to change. The implementation must verify this by applying
the file twice, and by applying it to a database stopped mid-way.

`ON CONFLICT (email)` in `upsert_inbound` / `upsert_outbound` continues to work
against the unique constraint exactly as it did against the primary key.
Postgres permits many NULLs under a unique constraint, so phone-only rows do
not collide.

### 4.2 The identifier constraint

`people_has_an_identifier` encodes the stated rule. All 511 current rows
satisfy it (every one has an email), so it validates on creation. Any future
path that would write a person with neither identifier fails loudly at the
database rather than creating an unreachable row.

### 4.3 Person ids are internal

Ids are not stable across a rebuild of `people` from Google Contacts plus a
re-import. That scenario is close to theoretical — the Cloud SQL instance has
automated daily backups, 7 retained, so recovery is a restore, which preserves
ids — but the rule stands: **a person id is never a durable external
reference.** The email address remains the human-facing handle, and the two
child tables' `person_id` values are recomputed by their importers.

(Noted while checking, outside this spec's scope: point-in-time recovery is
disabled on that instance, so a restore loses up to 24 hours, and the
`message_count` / `my_response_count` counters rebuild only via
`scripts/import_contacts.py`.)

## 5. Behavior

### 5.1 Resolving `{ident}`

`services/identity.py::classify` decides, in this order:

1. Contains `@` → **email**, normalized with `services.eligibility.normalize`.
2. Parses through `services.imessage_export.normalize_handle` → **phone**,
   E.164. That function rejects anything under 7 digits, so short numeric
   values cannot be misread as phone numbers.
3. All digits → **id**.
4. Otherwise → 404 (nothing can match it).

`+` must be percent-encoded in the path (`%2B15035550123`), as with the
existing iMessage handle routes.

**Ambiguity is an error, not a guess.** A phone number on several people —
a household landline — resolves to more than one row and returns **409** with
the candidate ids. Picking one arbitrarily risks editing the wrong person.

### 5.2 Phone-only people and the HubSpot mirror

`clients/hubspot.py` searches and creates contacts by email address, so a
phone-only person cannot be mirrored. `repo/people.py::eligible_not_in_hubspot`
gains `AND email IS NOT NULL`.

Today this is a no-op — every row has an email — but it must land **in this
piece**, before piece 2 adopts 553 email-less contacts. Without it, the nightly
reconcile would try to create HubSpot contacts it cannot key, and burn the
bounded cap on failures.

Eligibility itself is untouched: it stays email-driven and monotonic, and a
phone-only person simply never satisfies it.

### 5.3 API surface

- `GET /people/{ident}`, `PATCH /people/{ident}`, `POST /people/{ident}/sync`
  accept all three identifier kinds. **`GET /people/{email}` behaves exactly as
  before** — inbox's production path.
- Responses gain `id: int`.
- **`PersonOut.email` becomes `str | None`.** This is the one change that is
  not purely additive: a person may now have no email. Inbox looks people up
  *by* email, so it can only receive rows whose email is populated; a null is
  reachable only through id or phone lookup, which inbox never performs. Called
  out explicitly rather than discovered later.
- iMessage and LinkedIn payloads gain `person_id` and keep `person_email`,
  derived by joining `people` (now nullable).

## 6. Testing

- `tests/test_identity.py` — the three classification paths plus `42`, a
  5-digit short code, an empty string, an unencoded `+`, and a value that is
  neither.
- `tests/test_schema.py` (real Postgres) — migration idempotent across two
  applications; `people_has_an_identifier` rejects a row with neither
  identifier and accepts phone-only; `person_id` foreign keys null out on a
  person delete; `ON CONFLICT (email)` still upserts.
- `tests/test_repo_people.py` — `get_by_id` / `get_by_phone` SQL;
  `eligible_not_in_hubspot` carries the email guard.
- `tests/test_repo_imessage.py`, `tests/test_repo_linkedin.py` — writes use
  `person_id`; reads still serve `person_email` via the join.
- `tests/test_imessage_export.py`, `tests/test_linkedin_export.py` — matching
  resolves to `person_id`.
- `tests/test_api.py` — each identifier kind resolves; ambiguity returns 409
  with candidate ids; `id` present on responses; and an explicit regression
  test that `GET /people/{email}` is unchanged.

## 7. Rollout

1. `scripts/migrate_db.py` from the branch (guarded, idempotent, sub-second).
2. Merge; CI deploys `people-api` and the Cloud Functions.
3. Re-run `scripts/import_imessage.py` and `scripts/import_linkedin.py` so the
   links repopulate through `person_id`. The backfill already made them
   correct; the re-runs prove the importers write the new shape.
4. Confirm `GET /people/{email}` still answers for a known person, and that a
   phone lookup answers for one of the 75 linked iMessage handles.

No Terraform, secrets, or inbox changes.

## 8. Decisions

| Decision | Choice | Why |
|---|---|---|
| Key type | `BIGSERIAL` | Matches the house style (`linkedin_*`, `imessage_imports`); 8-byte child keys; no need for external id generation. |
| Not UUID | — | Buys distributed generation we do not need, costs readability in every debug query. |
| Not `resourceName` | — | Null for people with no Google contact; a key cannot be null. |
| Not "email else phone" | — | Mutable: a phone-only person gaining an email would change identity, which is the fragility being removed. |
| Addressing | One route, any identifier | Keeps inbox and every skill working with readable emails; one route to document. |
| Ambiguity | 409 with candidates | A wrong edit is worse than a failed request. |
| `email` nullable unique | — | Lets phone-only people exist while every existing `ON CONFLICT (email)` upsert keeps working. |
| Scope | Identity only; no new rows | Keeps a migration that touches the primary key reviewable and provably behavior-preserving. |
