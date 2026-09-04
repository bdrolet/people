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
BASE=$(cd ~/src/people/terraform && terraform output -raw people_api_url)
```

No custom domain is mapped yet — `people_api_url` is a `run.app` URL.

## Auth token

```bash
TOKEN=$(gcloud secrets versions access latest --secret people-api-token --project bens-project-462804)
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

`q` matches a case-insensitive email substring or trigram similarity on
`display_name`; results are ordered by `last_interaction` (the more recent
of `last_seen`/`last_contacted`) descending. `limit` defaults to 20, max 100.

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
  "in_google_contacts": true, "in_hubspot": true
}
```

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
