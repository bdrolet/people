---
name: searching-people
description: >
  Use when searching for people — finding someone by name or email substring,
  or listing people by recent interaction. Use when asked to "find that
  person", "who have I talked to about X", "who have I emailed recently", or
  "list people I've corresponded with". Searches the people-api index, not
  live email.
---

# Searching People

## Base URL

```bash
BASE=https://people-api.drolet.cloud
```

`people-api.drolet.cloud` is the Cloud Run domain mapping (terraform/api.tf); the raw `run.app` URL is still available via `cd ~/src/people/terraform && terraform output -raw people_api_url` if DNS is ever the problem.

## Auth token

```bash
TOKEN=$(gcloud auth print-identity-token)   # Cloud Run IAM; your gcloud login is the credential
```

## Search by name or email

```
POST $BASE/search
```

```bash
curl -s -X POST "$BASE/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"q": "<query>", "limit": 20}' | python3 -m json.tool
```

`q` matches a case-insensitive email substring, trigram similarity on
`display_name`, or a case-insensitive substring of `company` — so "who works
at Acme" is one call; results are ordered by `last_interaction` (the more
recent of `last_seen`/`last_contacted`) descending. `limit` defaults to 20,
max 100.

The response also carries `linkedin_results`: LinkedIn connections whose
name, company, or position match `q` (substring or trigram), ordered by last
LinkedIn message, same `limit`. `results` entries always have `linkedin: null`
— fetch the person for their LinkedIn summary.

It also carries `imessage_results`: iMessage handles whose display name or
handle match `q` (substring or trigram similarity > 0.3), ordered by
`last_message_at` desc nulls last, same `limit`. `results` entries always
have `imessage: null` — fetch the person for their iMessage summary.

## Search LinkedIn connections

```
GET $BASE/linkedin/connections
```

```bash
curl -s "$BASE/linkedin/connections?company=<company>&replied=true&quiet_since=2025-01-01&limit=50" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Filters (all optional, combined with AND): `q` (name substring), `company`,
`position` (case-insensitive substrings), `min_messages` (int),
`replied` (`true` = Ben has sent at least one message), `quiet_since`
(date — last message before it; never-messaged connections are excluded),
`unmatched` (`true` = not linked to a `people` row), `limit` (default 50,
max 500). Ordered by `last_message_at` desc (nulls last), then
`connected_on` desc.

Examples: "former colleagues at X who went quiet" →
`company=X&replied=true&quiet_since=<a year ago>`; "LinkedIn people I've never
emailed" → `unmatched=true&min_messages=1`.

Each result: `profile_url`, `full_name`, `email`, `company`, `position`,
`connected_on`, `person_email` (linked people row, if any), `match_method`
(`email`/`name`/null), `message_count`, `my_message_count`,
`last_message_at`, `last_my_message_at`, `snapshot_at`.

This is a snapshot from Ben's last manual export — check its age with
`GET $BASE/linkedin/imports/latest` before claiming something is current.
Open one with `GET $BASE/linkedin/connections/{slug}` (see **fetching-person**).

## Search iMessage handles

```
GET $BASE/imessage/handles
```

```bash
curl -s "$BASE/imessage/handles?min_messages=10&replied=true&quiet_since=2025-01-01&limit=50" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Filters (all optional, combined with AND): `q` (display name or handle
substring), `min_messages` (int, 1:1 messages only), `replied` (`true` = Ben
has sent at least one message), `quiet_since` (date — `last_message_at`
before it), `unmatched` (`true` = no `google_resource_name` and no
`person_email`), `include_groups` (bool, default `false` — ranks by the
later of `last_message_at`/`last_group_message_at` instead of 1:1 only),
`limit` (default 50, max 500). Ordered by `last_message_at` desc nulls last.

Example: "who have I stopped texting" →
`replied=true&quiet_since=<a year ago>`. Open one with
`GET $BASE/imessage/handles/{handle}` — URL-encode a `+` in a phone handle as
`%2B` (e.g. `%2B15550100001`). Message text is never in this response — see
**querying-people-db** for that.

## List recent people

```
GET $BASE/people?recent=N&eligible_only=true
```

```bash
curl -s "$BASE/people?recent=20&eligible_only=true" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`recent` (default 20, max 200) is the row count, ordered by
`last_interaction` descending. `eligible_only` defaults to `true` — set it
`false` to include people who have never crossed the eligibility bar (spec
§5: an automated sender, or someone Ben has only ever received `ignore`-filed
mail from).

## Result shape

Both endpoints return `{"results": [...]}` of:

```json
{
  "email": "alice@example.com",
  "display_name": "Alice Example",
  "first_seen": "…", "last_seen": "…", "last_contacted": "…",
  "message_count": 12, "my_response_count": 4,
  "relationship_label": "colleague", "notes": "…",
  "eligible": true, "automated": false,
  "in_google_contacts": true, "in_hubspot": true,
  "phone_numbers": ["+15550100001"], "company": "Example Corp", "job_title": "Engineer",
  "contact": null
}
```

`phone_numbers` (E.164), `company`, and `job_title` are populated from the
three typed columns, which list/search queries already have on the row.
`contact` — the full Google field blob — is **always `null` in list and
search results**; it's only filled on a single-person fetch
(**fetching-person**, `GET /people/{email}`), to keep listings one query.

## Presenting results

One line per person — email is the natural handle (no ref system; unlike
Asana GIDs or calendar event ids, an email address is already something a
person can act on directly):

```
[<display_name or email>](mailto:<email>) · last interaction <last_interaction date> · msgs <message_count> / replies <my_response_count>
      <relationship_label, if set> — <notes, truncated to one line, if set>
```

- Mark `in_google_contacts`/`in_hubspot` only if the user asks about linkage.
- Offer to open one with **fetching-person** (`GET /people/{email}`) or
  update it with **editing-person** — both take the email directly, no
  lookup step needed.
