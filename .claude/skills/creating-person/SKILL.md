---
name: creating-person
description: >
  Use when the user wants to add a brand-new contact by hand — "add a
  contact", "create a contact", "add X to my contacts", "I met someone",
  "add this person's number/email". Creates a Google Contact and the
  `people` row together. Does not touch an existing person — check first
  with searching-people/fetching-person, and use editing-person to change
  someone who already has a row.
metadata:
  depends-on: "searching-people, fetching-person, editing-person"
---

# Creating a Person

`POST /people` creates a Google Contact and its `people` index row together,
in that order — this is the only way to add someone who has never emailed
or been emailed Ben and isn't already in Google Contacts. Google write
first; if it fails, nothing local is written.

**Check first.** Someone may already exist. Use **searching-people** (name)
or **fetching-person** (known email/phone) before creating — see
"Duplicates" below for what happens if you skip that.

## Base URL and token

```bash
BASE=https://people-api.drolet.cloud
TOKEN=$(gcloud auth print-identity-token)   # Cloud Run IAM; your gcloud login is the credential
```

## Request

```
POST $BASE/people
{
  "contact": {<Google People API field names>},
  "notes": "...",
  "relationship_label": "..."
}
→ 201, a PersonOut body (the same shape GET/PATCH return, including `id`)
```

`contact` uses the same Google field names and the same allowlist as
**editing-person**'s `contact` map (`services/contact_fields.py::WRITABLE_FIELDS`
— `addresses`, `birthdays`, `emailAddresses`, `names`, `organizations`,
`phoneNumbers`, `urls`, and more). `notes` and `relationship_label` are
separate top-level fields, exactly like `PATCH` — **not** keys inside
`contact`. Sending `biographies` or `memberships` inside `contact` is
rejected with a `400` naming the field, since `notes`/`relationship_label`
already own them; this is the mistake most likely to trip up a first call.

## An identifier is required

At least one of:

- an email address in `emailAddresses`, or
- a phone number in `phoneNumbers` that normalizes to E.164.

A short code (e.g. `"262966"`) does not count as a usable phone number —
the same rule adoption uses. Neither present → `400`, checked before
anything is written to Google.

## Examples

Phone only, no notes or label:

```bash
curl -s -X POST "$BASE/people" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contact": {"names": [{"givenName": "Alice", "familyName": "Example"}],
                   "phoneNumbers": [{"value": "+15550100001", "type": "mobile"}]}}'
```

Email plus phone, with notes and a relationship label:

```bash
curl -s -X POST "$BASE/people" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contact": {"names": [{"givenName": "Alice", "familyName": "Example"}],
                   "emailAddresses": [{"value": "alice@example.com"}],
                   "phoneNumbers": [{"value": "+15550100001", "type": "mobile"}]},
       "notes": "met at the conference",
       "relationship_label": "colleague"}'
```

**Always show the returned row afterward** — same presentation as
**fetching-person** — as proof the person was actually created, including
the new `id`.

## Duplicates: refused, not merged

Checked before any Google write, across four lookup paths: `people` by
email, `people` by phone, Google Contacts by email, Google Contacts by
phone. Any hit → `409`:

```json
{"error": "person exists", "candidates": [<id>, ...]}
```

- **`candidates` non-empty** — the match is already a `people` row.
  `PATCH` that id (or email/phone) with **editing-person** instead of
  creating a second one.
- **`candidates` empty** — the match exists only in Google Contacts; the
  nightly sync hasn't adopted it into a `people` row yet, so there's no id
  to give. The remedy is to run a sync first (the nightly `people-sync`
  Cloud Scheduler job, or `handlers.sync.run()` locally — see
  `testing-people-handlers`), then `PATCH` the row it creates.

34 phone numbers are already shared across more than one `people` row from
duplicate Google contacts adopted separately before this endpoint existed —
this duplicate check exists so `POST /people` doesn't manufacture more of
that.

## Errors

- `400` — an allowlist/shape violation in `contact` (same rules as
  **editing-person**'s PATCH), or no usable identifier.
- `409` — person exists (either side) — see "Duplicates" above.
- Anything Google itself rejects (a genuine 4xx from the People API) also
  comes back as `400`, carrying Google's message.

## After creating: phone-only people and later promotion

A phone-only person created here behaves exactly like one the nightly sync
**adopted** (`people-architecture`'s Adoption section) — same derived
`phone_numbers`/`company`/`job_title`, `email IS NULL`, keyed by phone or
`id`. If Google later gains an email address for them (e.g. via
**editing-person**'s `contact.emailAddresses`) and that address isn't
already claimed by a different row, the next nightly sync writes it onto
this row automatically (promotion — see `people-architecture` and
`querying-people-db`). A promoted person also becomes eligible for the
HubSpot mirror for the first time, since the mirror's query excludes
rows with no email — worth knowing if a batch of these later gain addresses.
