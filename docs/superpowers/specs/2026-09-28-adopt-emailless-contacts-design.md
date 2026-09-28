# Adopt Email-less Google Contacts — Design

**Date:** 2026-09-28
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

Google Contacts holds **968 contacts**; `people` holds **534 rows**. The gap is
that **553 Google contacts have no email address**, and the nightly sync skips
every one of them — a single line in
`services/google_contacts_sync.py::apply_person`:

```python
email = primary_email(person)
if not email:
    return None
```

Those 553 are invisible to `people-api`, the skills, and search. The clearest
symptom: **159 iMessage handles match a Google contact but link to no person**,
because that contact has no row to link to.

Piece 1 (merged, `#14`) made an email-less person representable: `people.id` is
the key, `email` is nullable, and a phone number is a valid identifier. This
piece uses that capability — it deletes the skip, adopts the contacts that can
satisfy the identity constraint, and counts the ones that cannot.

This is **piece 2 of 3**. Piece 3 adds `POST /people`.

**There is no schema change.** The columns already exist.

## 2. Goals and non-goals

**Goals**

1. Adopt every email-less Google contact that has at least one parseable phone
   number (**449** today), as an eligible `people` row.
2. Count and log the contacts that cannot be adopted (**104**: 87 with no phone
   at all, 17 whose phone numbers do not parse), so the number is visible and
   trends to zero as Ben fixes them in Google.
3. Adopt automatically from then on — a contact added by phone tomorrow is
   adopted on the next nightly run, with no manual step.
4. Let those people be read and edited: `GET`/`PATCH /people/{ident}` by phone
   or id must work for a person with no email address.
5. Link the iMessage handles that adoption makes linkable.

**Non-goals**

- `POST /people` (piece 3).
- Merging duplicates (§5.4). Detection is in scope; merging is not.
- Mirroring adopted people to HubSpot — impossible by construction: HubSpot
  keys on email, and `eligible_not_in_hubspot` already excludes rows without
  one (piece 1, §5.2).
- Changing eligibility for anyone who has an email, or changing the event
  handlers, which are untouched.
- Fixing the 104 in Google. That is Ben's data to correct; this piece only
  makes the count visible.
- Writing anything back to Google.

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `services/google_contacts_sync.py` | services | `apply_person` adopts instead of skipping (§5.1); `run_sync` counts `skipped`. |
| `repo/people.py` | repo | `create_from_google` accepts `email: str | None`; `_norm` returns `None` for `None` (§5.2.1). |
| `services/person_edit.py` | services | `update` takes a `person_id` and fetches with `get_by_id` (§5.3) — the piece-1 prerequisite. |
| `api/routers/people.py` | api | Passes `target["id"]` to `person_edit.update`. |
| `scripts/clear_sync_token.py` | script | **New, tiny.** Clears the stored Google sync token so the next run is a full pass (§6.2). |
| `.claude/skills/people-architecture`, `querying-people-db`, `editing-person` | skill | Adoption, the skipped count, and that phone-only people are editable. |

## 4. Current numbers

Measured 2026-09-28, before any change:

| | |
|---|---|
| Google contacts | 968 |
| No email address | 553 |
| …with ≥1 parseable phone → **adoptable** | **449** |
| …with a phone that does not parse | 17 |
| …with neither email nor phone (all have names) | 87 |
| `people` rows | 534 |
| …linked to a Google contact | 397 |
| iMessage handles matched to a Google contact | 222 |
| …but linked to no person | **159** |

## 5. Behavior

### 5.1 Adoption

`apply_person`'s creation path becomes:

```python
email = primary_email(person)
derived = contact_fields.derive(person)
if not email and not derived["phone_numbers"]:
    return "skipped"
if not email:
    people.create_from_google(conn, None, display_name=..., resource_name=rn, ...)
    return "created"
# unchanged from here: existing-row link, or create with an email
```

The update path is already correct: `apply_person` looks a linked person up by
`google_resource_name` (`people.get_by_google_resource`), so once adopted, a
contact refreshes on later runs exactly like any other.

`derived["phone_numbers"]` is `contact_fields.derive`'s E.164 list, so
"parseable" means what `services.imessage_export.normalize_handle` accepts —
the same rule the iMessage import uses. A contact whose only phone is a short
code or unparseable string is therefore skipped, which is why the count is 104
rather than 87.

### 5.2 Adopted rows

- `email` is `NULL`; `phone_numbers` carries the E.164 list; `company`,
  `job_title`, `google_fields` are derived as for any synced contact.
- `eligible = TRUE`, `automated = FALSE` — the values `create_from_google`
  already uses. **Decision:** adopted contacts are real contacts, so they
  appear in `GET /people` and search like anyone else. They cannot reach
  HubSpot, so the mirror and its cap are unaffected.
- `first_seen` is the adoption time. The counters stay 0: no mail has been seen
  from these people, and nothing in this piece changes how counters are
  written.

### 5.2.1 `_norm(None)` must return `None`, not `""`

`repo/people.py::_norm` is `(email or "").strip().lower()`, which turns `None`
into the empty string. That is a trap, and it would not fail on the first
adopted contact — it would fail on the **second**:

- `""` is not NULL, so the `people_has_an_identifier` CHECK sees a non-null
  email and passes, even for a contact with no address.
- `email` is `UNIQUE`, and unlike NULLs, two empty strings **do** collide. The
  first adoption inserts `email = ''`; the second raises a unique violation and
  aborts the sync.

So `_norm` must return `None` for `None` (and for a blank string), and only
normalize real values. A test adopts **two** email-less contacts in one run and
asserts both land with `email IS NULL` — one adoption would not catch this.

### 5.3 Editing a person with no email

`services/person_edit.py::update(conn, email, ...)` becomes
`update(conn, person_id, ...)` and fetches with `people.get_by_id`. Everything
else in it is unchanged: it still writes Google first, still refreshes from
Google afterwards, still raises `NotLinked` when there is no Google contact.

`api/routers/people.py` already resolves any identifier to a row, so it simply
passes `target["id"]`. This removes the redundant re-fetch the piece-1 review
noted, and it is what makes `PATCH /people/%2B15550100001` work.

### 5.4 Duplicates: detected, not merged

If an adopted phone-only contact later gains an email address that already
belongs to a different `people` row, there will be two rows for one person: the
adopted one (matched by `google_resource_name`) and the original (matched by
email). Nothing merges them.

Merging needs its own design — which row wins, what happens to two sets of
counters, which `google_resource_name` survives — and probably a human decision
per case. This piece only makes the situation findable:

```sql
SELECT a.id AS adopted_id, p.id AS existing_id
FROM people a
JOIN jsonb_array_elements(a.google_fields -> 'emailAddresses') e ON TRUE
JOIN people p ON lower(p.email) = lower(e ->> 'value')
WHERE a.email IS NULL AND p.id <> a.id;
```

Expected to return zero rows today. Documented in `querying-people-db`.

### 5.5 The skipped count

`run_sync`'s counts dict gains `skipped`, which flows into the log line the
nightly run already emits and into the `POST /sync` response body. Per Ben's
decision, there is **no** skipped-contacts table and **no** endpoint: a count
is enough, and it should trend to zero as he fixes the contacts in Google.

The names of the 104 are deliberately not persisted or logged — the repo is
public and the logs are not the place for a contact list.

## 6. Rollout

### 6.1 Order: code first, then the token

**Deploy the code before touching any data.** New code against the current data
is a no-op (nothing is adopted until a full sync runs), whereas the reverse
order is what broke production during piece 1: a destructive migration landed
before the code that understood it. There is no schema change here, so this
piece has no such hazard — but the ordering rule stands.

1. Merge; CI deploys `people-api` and both Cloud Functions.
2. `scripts/clear_sync_token.py` — clears the stored token (one row update).
3. Trigger `people-sync` by hand and read its counts.
4. Re-run `scripts/import_imessage.py` to link the newly adoptable handles.

### 6.2 `scripts/clear_sync_token.py`

`repo/sync_state.py` already owns the token. The script clears it and prints
what it cleared, so the next `run_sync` takes the full-listing path that
`_is_expired_sync_token` handling already exercises (the read-mask widening in
the contact-fields feature triggered exactly this path, successfully).

### 6.3 Expected results

| Check | Expected |
|---|---|
| Contacts processed | 968 |
| `created` | ~449 |
| `skipped` | ~104 |
| `updated` | ~397 |
| `people` rows | 534 → ~983 |
| HubSpot contacts created | **0** |
| Rows with no email and no phone | **0** (the CHECK constraint forbids it) |
| Duplicate query (§5.4) | 0 rows |
| iMessage handles linked, after re-import | materially above 76 (159 are candidates) |

If the handle-link count barely moves, adoption did not do what this design
claims: stop and investigate before merging anything further.

### 6.4 Rollback

The code change is additive. If adoption proves wrong, the adopted rows are
exactly `email IS NULL AND google_resource_name IS NOT NULL`; deleting them
restores the prior state, and the `ON DELETE SET NULL` foreign keys clear the
child links automatically.

## 7. Testing

- `tests/test_google_contacts_sync.py` — the three creation branches: an email
  behaves as today; no email with a parseable phone → `created` with
  `email=None`; no email and no parseable phone → `skipped`, nothing written.
  Also: an adopted contact on a later run takes the `updated` path via
  `google_resource_name`, and `run_sync` accumulates `skipped`.
- `tests/test_repo_people.py` — `_norm(None)` and `_norm("  ")` both return
  `None`, never `""` (§5.2.1); `create_from_google` with `email=None` writes a
  NULL email; and **two** email-less contacts adopted in the same run both land
  with `email IS NULL` — the case that would fail if `_norm` returned `""`.
  The two-contact test needs real Postgres, so it belongs in
  `tests/test_schema.py` alongside the other constraint tests; the `_norm` and
  single-row tests stay unit-level.
- `tests/test_person_edit.py` — `update` keyed by `person_id`; `NotFound` when
  the id does not exist; the existing notes / relationship-label / contact-field
  behavior unchanged.
- `tests/test_api.py` — `PATCH` and `GET` against a phone-only person by phone
  and by id; the email path unchanged (inbox's contract).
- `tests/test_schema.py` — unchanged; the constraint it already tests is what
  makes the `skipped` branch necessary.

## 8. Decisions

| Decision | Choice | Why |
|---|---|---|
| Adoption trigger | Remove the skip; clear the token for one full pass | Smallest change that closes the gap permanently; the full-sync path is already exercised. No one-off script to maintain. |
| Unadoptable contacts | Counted and logged, not persisted | Ben's call: a count is enough and should trend to zero. No table, no endpoint, no names in logs. |
| Adopted eligibility | `eligible = TRUE` | They are real contacts and belong in listings and search; they cannot reach HubSpot, so the cap is unaffected. |
| Phone parseability | `normalize_handle`, the same rule the iMessage import uses | One definition of "a phone number we can use", so adoption and matching agree. |
| Duplicates | Detect, do not merge | Merging needs its own design and per-case judgment. |
| `person_edit` | Keyed by `person_id` | Required for a phone-only person to be editable; also removes a redundant re-fetch. |
| Deploy order | Code, then data | The inverse order caused the piece-1 outage. |
