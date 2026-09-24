---
name: editing-person
description: >
  Use when the user wants to update a person's notes, relationship label, or
  any other Google contact field — phone number, name, company/job title,
  birthday, address — "add a note about X", "tag alice as a colleague", "set
  the relationship label for that contact", "update alice's phone number",
  "add a work address for bob". Does not create people — a person becomes a
  contact automatically by emailing or being emailed; use searching-people
  to check whether someone already exists first.
metadata:
  depends-on: "fetching-person, searching-people"
---

# Editing a Person

`PATCH /people/{email}` writes to Google Contacts **first** (it is the
source of truth for `notes`/`relationship_label` and every other contact
field — spec §4.3, contact-field-edits design), then refreshes the `people`
DB row from Google and returns it. It never creates a new person — see "No
creation" below.

## Base URL and token

```bash
BASE=https://people-api.drolet.cloud
TOKEN=$(gcloud auth print-identity-token)   # Cloud Run IAM; your gcloud login is the credential
```

## Update notes and/or relationship label

```bash
curl -s -X PATCH "$BASE/people/<email>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"notes": "Met at PyCon 2026, works on infra at Acme"}'

curl -s -X PATCH "$BASE/people/<email>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"relationship_label": "colleague"}'
```

Send only the field(s) you're changing — both are optional, independently
settable.

- **`notes`** writes the Google contact's **biography** field, replacing it
  outright.
- **`relationship_label`** writes by adding the contact to a Google contact
  group named by the label (created if it doesn't exist) and removing it
  from whichever other user-managed group currently reads as its label. It
  becomes a **Google Contacts group**, visible and editable in the Google
  Contacts UI too.

**Always show the returned row afterward** — the response is the refreshed
person record (same shape as `fetching-person`), proof the write landed:

```
**<display_name or email>** — updated
relationship_label: <value>
notes: <value>
```

## Update any other contact field

Every other field on the Google contact — phone numbers, name, company/job
title, birthday, addresses, and more — is edited through a `contact` map on
the same PATCH body. Its keys are Google People API field names, each value
a list of objects (a bare object is also accepted for a single-valued field
like `birthdays`). **`notes` and `relationship_label` are not valid keys
inside `contact`** — they keep their own top-level PATCH fields (above) and
are rejected with a `400` if sent inside `contact`, since two writers for
the same field is how they'd drift.

```bash
# Phone number — adds/replaces the contact's phone numbers
curl -s -X PATCH "$BASE/people/<email>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contact": {"phoneNumbers": [{"value": "+15550100001", "type": "mobile"}]}}'

# Name
curl -s -X PATCH "$BASE/people/<email>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contact": {"names": [{"givenName": "Alice", "familyName": "Example"}]}}'

# Company / job title
curl -s -X PATCH "$BASE/people/<email>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contact": {"organizations": [{"name": "Example Corp", "title": "Engineer"}]}}'

# Birthday
curl -s -X PATCH "$BASE/people/<email>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"contact": {"birthdays": [{"date": {"year": 1990, "month": 5, "day": 14}}]}}'
```

A few things worth knowing before sending one of these:

- **An empty list clears the field.** `{"contact": {"phoneNumbers": []}}`
  removes every phone number on the contact. Easy to do by accident if you
  meant to add one number and instead sent a truncated list — always send
  the *complete* desired set for a field, not a delta.
- **Email addresses are add-only.** A submitted `emailAddresses` list must
  still contain every address the contact currently has, plus the address
  this person row is keyed by — otherwise it's a `409`. Removing or changing
  an email address goes through the Google UI, not this endpoint.
- Fields not sent in `contact` are left untouched on the Google contact.
- `contact` accepts exactly this curated set of keys — not "any Google
  field minus a few": `addresses`, `birthdays`, `calendarUrls`,
  `clientData`, `emailAddresses`, `events`, `externalIds`, `genders`,
  `imClients`, `interests`, `locales`, `locations`, `miscKeywords`, `names`,
  `nicknames`, `occupations`, `organizations`, `phoneNumbers`, `relations`,
  `sipAddresses`, `urls`, `userDefined`. `biographies` and `memberships` are
  deliberately excluded from this set — `notes` and `relationship_label`
  already own them as dedicated PATCH fields, and having a second writer for
  the same field is how they'd drift. Anything else — including real Google
  person fields not in this set, like `ageRanges` or `skills` — is rejected
  with a `400` naming the offending key. The list above is copied from
  `services/contact_fields.py::WRITABLE_FIELDS`; treat that constant as the
  authority if the two ever disagree.

## Errors

- `404` — unknown email. Check with **searching-people** first.
- `400` — an unknown/excluded `contact` key (the response names it), or a
  value that isn't an object or list of objects.
- `409` — one of two things: the person has no linked Google contact yet
  (not eligible, or eligible but people hasn't created/linked one — wait for
  the next `email_classified`/`email_sent` event or nightly sync, then
  retry); or a submitted `emailAddresses` would remove/change an existing
  address (send the full existing set, or drop `emailAddresses` from the
  request); or a concurrent edit changed the contact between the read and
  the write (stale etag) — re-fetch with **fetching-person** and retry.

## No creation

There is no "add a person" endpoint. Someone becomes a person automatically
the moment they email Ben or Ben emails them — the event pipeline handles
creation, eligibility, and (once eligible) the Google Contact / HubSpot
mirror. If **searching-people** shows nobody for an address you expected to
find, either no qualifying email has been exchanged yet, or (spec §5) every
message so far was automated/filed `ignore` and nobody has replied.

For adding someone to the **referral outreach** pipeline specifically (a
different, HubSpot-only concern), see the global `adding-referral-contact`
skill instead.
