---
name: fetching-person
description: >
  Use when the user wants the full record for a specific person by email
  address, phone number, or person id — relationship label, notes, message
  counts, Google Contacts/HubSpot linkage. Use for "what do we know about X",
  "show me that contact", "pull up alice@example.com". Use after
  searching-people to open a selected result, or directly when you already
  have the email, phone, or id.
metadata:
  depends-on: "searching-people"
---

# Fetching a Person

## Prerequisites

You need an email address, an E.164 phone number, or a person id (the `id`
field on a **searching-people** result). If you only have a name, use
**searching-people** first.

## Base URL and token

```bash
BASE=https://people-api.drolet.cloud
TOKEN=$(gcloud auth print-identity-token)   # Cloud Run IAM; your gcloud login is the credential
```

## Fetch

```
GET $BASE/people/{ident}
```

`{ident}` is an email, an E.164 phone number, or a numeric person id — the
API classifies which one it is (an `@` means email, a parseable phone number
means phone, all-digits means id). Percent-encode a `+` in a phone number as
`%2B` (a literal `+` also works, but percent-encoding is the reliable form
in a shell double-quoted string):

```bash
curl -s "$BASE/people/<email>" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s "$BASE/people/%2B15550100001" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s "$BASE/people/42" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`404` = nobody matches — an unknown email/phone/id, or a person people
hasn't seen mail from or to yet. Check first with **searching-people** if
the exact identifier is uncertain.

`409` = the phone number belongs to more than one person (a household
landline) — the response body's `detail` carries `{"error": "ambiguous
phone", "candidates": [<id>, ...]}`. People never guesses; refetch with one
of the candidate ids (`GET $BASE/people/<id>`) after confirming which person
the user means.

**Response fields:** `id`, `email` (nullable — null for a phone-only
person), `display_name`, `first_seen`, `last_seen`, `last_contacted`,
`message_count` (inbound), `my_response_count` (Ben's replies),
`relationship_label`, `notes`, `eligible`, `automated`, `in_google_contacts`,
`in_hubspot`, `linkedin` (null, or the linked LinkedIn connection's
`profile_url`, `company`, `position`, `connected_on`, `message_count`,
`my_message_count`, `last_message_at`, `last_my_message_at`, `snapshot_at`),
`imessage` (null, or an aggregate over every iMessage handle linked to this
person: `handles`, `message_count`, `my_message_count`, `last_message_at`,
`last_my_message_at`, `group_message_count`, `last_group_message_at`,
`imported_at`), `phone_numbers` (list of E.164 strings), `company`,
`job_title` (both from the contact's primary — or first — organization, null
if none), `contact` (the full allowlisted Google field blob this response
was derived from, e.g. `addresses`, `birthdays`, `urls` — present here since
this is a single-person fetch).

`id` is an internal surrogate key — stable for now, but not a durable
external reference (`people` is rebuildable from Google Contacts, and ids
would not survive that). Prefer the email as the human-facing handle when
presenting or linking to a person; use the id only for a phone-only person
or to resolve a 409's candidates.

**Zero message counts with a real, eligible contact isn't a bug.** The
nightly sync **adopts** a Google contact that has a phone number but no
email address (`people-architecture`'s Adoption section) — these people
show up here with `email: null`, `eligible: true`, and `message_count`/
`my_response_count` both `0`, because no mail has ever been seen from them;
they were pulled in from Google, not from the event pipeline. That's
different from a person `people` has simply never linked to Google — this
one already carries `phone_numbers` and `contact` data.

## Presenting

```
**<display_name or email or "person <id>">** <mailto:<email>, if set>
<relationship_label, if set> · first seen <first_seen> · last interaction <max(last_seen, last_contacted)>
<job_title, if set> at <company, if set>   ← only if either is set
phone: <phone_numbers, comma-joined, if any>
messages: <message_count> received / <my_response_count> replied
notes: <notes, if set — this is the Google contact's biography>
Google Contacts: <yes/no>   HubSpot: <yes/no>
LinkedIn: <company> · <position> · <message_count> msgs / <my_message_count> from Ben · last <last_message_at> (snapshot <snapshot_at date>)   ← only if linkedin is set
iMessage: <message_count> msgs / <my_message_count> from Ben · last <last_message_at> · groups <group_message_count>   ← only if imessage is set
```

`email` may be null (a phone-only person) — fall back to `display_name`,
then a phone number, then `person <id>`, and drop the `mailto:` link.

`notes` is the Google contact's **biography** field — `relationship_label`
comes from the Google contact's group membership (excluding system groups
and the internal "Inbox" group people uses to mark contacts it created). Both
are edited with **editing-person**, which writes through to Google first.

## LinkedIn detail

For conversation history and recommendations, take the slug after `/in/` in
`linkedin.profile_url` (or from a **searching-people** LinkedIn result, which
also covers connections with no `people` row):

```bash
curl -s "$BASE/linkedin/connections/<slug>?messages_limit=100" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Returns the connection fields plus `recommendations` (given/received) and
`conversations` (newest first, each with `messages` newest first). `404` =
not in the current snapshot. Message bodies are private — quote only what the
user asks for.

## iMessage detail

`imessage.handles` lists the phone numbers/Apple ID emails linked to this
person. Fetch one for its link fields and the group chats it's in:

```bash
curl -s "$BASE/imessage/handles/<handle>" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

URL-encode a `+` in a phone handle as `%2B` (e.g. `%2B15550100001`). `404` =
unknown handle. This response, like the `imessage` summary above, never
carries message text — only direct DB queries do (**querying-people-db**).
