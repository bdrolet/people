---
name: fetching-person
description: >
  Use when the user wants the full record for a specific person by email
  address — relationship label, notes, message counts, Google Contacts/HubSpot
  linkage. Use for "what do we know about X", "show me that contact", "pull
  up alice@example.com". Use after searching-people to open a selected result,
  or directly when you already have the email address.
metadata:
  depends-on: "searching-people"
---

# Fetching a Person

## Prerequisites

You need an email address. If you only have a name, use **searching-people**
first.

## Base URL and token

```bash
BASE=https://people-api.drolet.cloud
TOKEN=$(gcloud secrets versions access latest --secret people-api-token --project bens-project-462804)
```

## Fetch

```
GET $BASE/people/{email}
```

```bash
curl -s "$BASE/people/<email>" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`404` = unknown email — nobody, or a person people hasn't seen mail from or
to yet. Not every sender gets a row check first with **searching-people** if
the exact address is uncertain.

**Response fields:** `email`, `display_name`, `first_seen`, `last_seen`,
`last_contacted`, `message_count` (inbound), `my_response_count` (Ben's
replies), `relationship_label`, `notes`, `eligible`, `automated`,
`in_google_contacts`, `in_hubspot`.

## Presenting

```
**<display_name or email>** <mailto:<email>>
<relationship_label, if set> · first seen <first_seen> · last interaction <max(last_seen, last_contacted)>
messages: <message_count> received / <my_response_count> replied
notes: <notes, if set — this is the Google contact's biography>
Google Contacts: <yes/no>   HubSpot: <yes/no>
```

`notes` is the Google contact's **biography** field — `relationship_label`
comes from the Google contact's group membership (excluding system groups
and the internal "Inbox" group people uses to mark contacts it created). Both
are edited with **editing-person**, which writes through to Google first.
