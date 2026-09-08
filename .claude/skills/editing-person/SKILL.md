---
name: editing-person
description: >
  Use when the user wants to update a person's notes or relationship label —
  "add a note about X", "tag alice as a colleague", "set the relationship
  label for that contact". Does not create people — a person becomes a
  contact automatically by emailing or being emailed; use searching-people
  to check whether someone already exists first.
metadata:
  depends-on: "fetching-person, searching-people"
---

# Editing a Person

`PATCH /people/{email}` writes to Google Contacts **first** (it is the
source of truth for `notes`/`relationship_label` — spec §4.3), then refreshes
the `people` DB row from Google and returns it. It never creates a new
person — see "No creation" below.

## Base URL and token

```bash
BASE=https://people-api.drolet.cloud
TOKEN=$(gcloud secrets versions access latest --secret people-api-token --project bens-project-462804)
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

## Errors

- `404` — unknown email. Check with **searching-people** first.
- `409` — the person has no linked Google contact yet (not eligible, or
  eligible but people hasn't created/linked one). There's nothing to write
  through to. If the person should be eligible, wait for the next
  `email_classified`/`email_sent` event or nightly sync to link them, then
  retry.

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
