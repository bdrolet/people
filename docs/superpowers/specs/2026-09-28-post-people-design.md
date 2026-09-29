# `POST /people` and Email Promotion — Design

**Date:** 2026-09-28
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

A person enters `people` only as a side effect today: Ben emails someone, or a
Google contact appears in the nightly sync. There is no way to say "add this
person" — the request that started this three-part effort.

This piece adds **`POST /people`**: create the Google contact and the `people`
row together, from an email address, a phone number, or both. It reuses the
sync's own creation path, so a hand-added person is indistinguishable from one
the sync adopted.

It also closes the **email-promotion gap** deferred from piece 2. Today
`repo/people.py::update_from_google` never writes the `email` column, so an
adopted phone-only row stays email-less forever — even after the contact gains
an address in Google. Promotion matters most precisely here: giving a phone-only
person an email address is a natural first thing to do after adding them, and
without promotion the index never reflects it.

**This is piece 3 of 3.** Pieces 1 (`#14`, surrogate id) and 2 (`#15`, adoption)
are merged and deployed. **There is no schema change.**

## 2. Goals and non-goals

**Goals**

1. `POST /people` creates a Google contact and its `people` row from a `contact`
   map, with an email, a phone, or both.
2. Refuse to create someone who already exists — on either side — returning the
   existing person's id so the caller can `PATCH` instead (§5.3).
3. Promote an adopted row's `email` when Google has one and the address is
   unclaimed (§6).
4. Never abort a sync: a claimed address is left alone, not written (§6.2).
5. A hand-created person is byte-identical in shape to an adopted one.

**Non-goals**

- Deleting people or Google contacts. Deletion stays in the Google UI.
- Merging duplicates. Detection exists (piece 2 §5.4); merging needs its own
  design and per-case judgment.
- Creating a HubSpot contact directly. The mirror decides that on its own
  schedule; §6.3 notes the one new way a person becomes mirrorable.
- Bulk import. `scripts/import_contacts.py` already covers backfill.
- An `eligible` flag in the request. A Google-sourced contact is eligible, as
  `create_from_google` already sets.
- Changing the event handlers, eligibility, or the HubSpot cap.

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `clients/google_contacts.py` | clients | **New** `create_person(body, group_resource_name) -> dict` — `createContact` with a caller-supplied body. The existing `create_contact` (email-only body) stays for the eligibility path. |
| `services/person_create.py` | services | **New.** `create(conn, *, contact, notes, relationship_label) -> dict`: validate → duplicate check → Google create → row via `apply_person` → optional `notes`/`label`. Raises `Invalid`, `Duplicate`. |
| `services/contact_fields.py` | services | **New** `identifiers(contact) -> tuple[list[str], list[str]]` — the submitted emails and E.164 phones, for validation and the duplicate check. |
| `services/google_contacts_sync.py` | services | `apply_person` promotes (§6); `run_sync` counts `promoted`. |
| `repo/people.py` | repo | **New** `email_owner(conn, email) -> int | None`; `update_from_google` gains an optional `email` written only when not `None`. |
| `api/routers/people.py` | api | `POST /people` → 201, mapping `Invalid`→400, `Duplicate`→409. |
| `.claude/skills/` | skill | `creating-person` (new), plus `editing-person` / `people-architecture` updates. |

## 4. Request and response

```
POST /people
{
  "contact": {"names": [{"givenName": "Alice", "familyName": "Example"}],
              "phoneNumbers": [{"value": "+15550100001", "type": "mobile"}],
              "emailAddresses": [{"value": "alice@example.com"}]},
  "notes": "met at the conference",
  "relationship_label": "colleague"
}
→ 201, a PersonOut body (the same shape GET returns, including `id`)
```

`contact` uses Google People API field names, exactly as `PATCH` does — one
vocabulary for describing a contact. `notes` and `relationship_label` keep their
dedicated fields; they are rejected inside `contact` by the existing allowlist,
because `biographies` and `memberships` have owners.

## 5. Creating

### 5.1 Validation, before any write

1. `contact_fields.validate(contact)` — the existing allowlist and shape check.
2. **At least one identifier**, else `400`: an address in `emailAddresses`, or a
   phone in `phoneNumbers` that `normalize_handle` accepts. This mirrors the
   `people_has_an_identifier` CHECK rather than discovering it at write time,
   and it is the rule Ben stated: a contact with neither is a data error.
3. The duplicate check (§5.3).

Nothing reaches Google until all three pass.

### 5.2 The flow

1. `gc.create_person(body, group)` where `body` is the validated `contact` plus
   membership of `GOOGLE_CONTACT_GROUP` — the same group `ensure_contact` uses,
   resolved the same way (`groups.get(group_name())` else `ensure_group`).
2. `apply_person(conn, created, groups)` creates the row. **This is the whole
   point of the ordering:** row creation stays in one place, so a hand-created
   person and an adopted one are identical by construction, including derived
   `phone_numbers` / `company` / `job_title` / `google_fields`.
   `apply_person` returns a kind string, not a row, so the row is then read with
   `people.get_by_google_resource(conn, created["resourceName"])` — the same
   lookup the sync uses, and the only one that works for a contact with no
   email. If it returns `None`, `apply_person` declined to create (which
   validation should have made impossible): raise `Invalid` rather than return a
   half-made person, so the caller learns the contact exists in Google without a
   row and the next sync will pick it up.
3. If `notes` or `relationship_label` was given,
   `person_edit.update(conn, row["id"], notes=..., relationship_label=...)` —
   reusing the PATCH machinery, including the contact-group move.
4. Return the row.

If Google fails, nothing local is written. If step 2 or 3 fails after Google
succeeded, the contact exists in Google without a row (or without its label) and
the next nightly sync repairs it — the same self-healing the sync already
provides, and the reason `apply_person` is reused rather than reimplemented.

### 5.3 Duplicates: refuse, with the existing id

Checked before any write, over the submitted identifiers:

| Side | Email | Phone |
|---|---|---|
| `people` | `people.get` | `people.get_by_phone` |
| Google | `gc.search_by_email` | `gc.list_phone_index` |

Any hit → `409` with `{"error": "person exists", "candidates": [<ids>]}`.
`candidates` holds `people.id` values; a Google-only match (a contact the sync
has not yet adopted) returns an empty list with the same error, since there is no
id to give — the caller's remedy is to run a sync and then `PATCH`.

Rationale: 34 phone numbers are already shared across rows because duplicate
Google contacts were each adopted separately. A create-anyway endpoint would
manufacture more of exactly the mess currently being cleaned up by hand.

### 5.4 Errors

| Condition | Status |
|---|---|
| Allowlist or shape violation | 400 |
| No usable identifier | 400 |
| Person exists (either side) | 409 with candidates |
| Google 4xx | 400, carrying Google's message |
| Google 5xx | propagates untouched |

## 6. Email promotion

### 6.1 Where and when

In `apply_person`'s linked branch — the path every already-linked contact takes
on every sync:

```python
if linked:
    if linked.get("email") is None and not derived["phone_numbers"]:
        return "skipped"                       # piece 2's guard, unchanged
    promote = None
    if linked.get("email") is None:
        candidate = primary_email(person)
        if candidate and people.email_owner(conn, candidate) is None:
            promote = candidate
    people.update_from_google(conn, rn, email=promote, ...)
    return "promoted" if promote else "updated"
```

`update_from_google`'s new `email` parameter is written **only when not
`None`**, so all existing callers behave exactly as before. That restraint is
deliberate: this function runs for every linked contact on every sync, and the
only intended new write is the promotion itself.

### 6.2 The guard, and why it exists

If the address already belongs to a different row, writing it violates the
`people_email_key` unique constraint and **aborts the entire sync** — the same
failure mode as piece 2's phone-clearing bug, on the same nightly path. So a
claimed address is not written; both rows stand, and piece 2 §5.4's
duplicate-detection query is what finds them.

### 6.3 Consequence: a promoted person becomes mirrorable

`eligible_not_in_hubspot` excludes rows without an email (piece 1 §5.2). A
promoted row has one, so the next reconcile may create a HubSpot contact for
them, subject to the existing `HUBSPOT_MAX_CONTACTS` cap and its eviction rules.

This is correct — they now have an address HubSpot can key on — but it is a new
way for the mirror's population to grow. Expected volume today is near zero;
recorded so that it is not a surprise if a batch of adopted contacts later gains
addresses.

### 6.4 Counting

`run_sync`'s counts gain `promoted`, so a promotion appears in the nightly log
line, the `POST /sync` response, and the `google_sync_changes` metric (which
already labels by kind).

## 7. Testing

- `tests/test_contact_fields.py` — `identifiers` returns submitted emails and
  E.164 phones; a short-code-only phone yields no phone identifier.
- `tests/test_person_create.py` — validation rejects an allowlist violation and
  a contact with no usable identifier, writing nothing; a duplicate on each of
  the four lookup paths raises `Duplicate` with the right candidates and writes
  nothing; the happy path calls `create_person` then `apply_person`, and applies
  `notes` / `relationship_label` only when given; a Google failure leaves no
  local write.
- `tests/test_google_contacts.py` — `create_person` sends the caller's body plus
  the group membership, and does not disturb `create_contact`.
- `tests/test_google_contacts_sync.py` — promotion when the address is
  unclaimed; **no** promotion when claimed, and no abort; `"promoted"` counted;
  a row that already has an email is untouched; piece 2's skip guard still fires.
- `tests/test_repo_people.py` — `email_owner` returns an id or `None`;
  `update_from_google` writes `email` only when passed.
- `tests/test_schema.py` (real Postgres) — promoting to an address held by
  another row raises a unique violation, proving the guard is load-bearing
  rather than decorative.
- `tests/test_api.py` — `POST /people` 201 with the body; 400 for each
  validation failure; 409 with candidates; the `GET`/`PATCH` contract unchanged.

## 8. Rollout

No schema change, so code lands first and nothing needs a data step.

1. Merge; CI deploys `people-api` and both Cloud Functions.
2. `POST /people` a real contact with a phone only, read it back by phone, then
   `PATCH` an email onto it and confirm promotion on the following sync.
3. Trigger one sync and read the `promoted` count. Expect 0 unless a contact
   gained an address in Google since the last run.
4. Delete the test contact in the Google UI afterwards; the next sync marks it
   `google_deleted_at`.

## 9. Decisions

| Decision | Choice | Why |
|---|---|---|
| Row creation | Google first, then `apply_person` | One owner for row shape; a hand-added person equals an adopted one; Google failure writes nothing. |
| Request shape | The same `contact` map as `PATCH` | One vocabulary; the allowlist already protects `notes` / `relationship_label`. |
| Duplicates | 409 with the existing id | 34 phone numbers are already shared from duplicate contacts; creating more is the opposite of the goal. |
| Identifier requirement | Enforced in the API, not just the CHECK | Ben's rule, and a clear 400 beats a constraint violation. |
| Promotion | Only when the address is unclaimed | A claimed address would abort the nightly sync. |
| `update_from_google` email | Written only when explicitly passed | Restraint on a path that runs for every linked contact every night. |
| Deletion | Out of scope | Irreversible; the Google UI is the right place. |
