---
name: testing-people-handlers
description: Use when locally testing the people handlers (email_classified, email_sent, sync) against the real production DB without deploying a Cloud Function, or smoke-testing changes to services/ingest.py, services/google_contacts_sync.py, services/hubspot_mirror.py, or handlers/.
metadata:
  type: project
---

# Testing People Handlers Locally

## Unit tests (no credentials needed)

```bash
.venv/bin/pytest tests/ -q
```

`tests/test_handlers.py` covers `handlers/email_classified.py` and
`handlers/email_sent.py` end-to-end with the DB, Google, and HubSpot clients
mocked. Start there for pure logic changes.

## Running a handler against the real (production) DB

`handlers.email_classified.handle(event)` and `handlers.email_sent.handle(event)`
take a plain dict shaped like the events on `email-events` — build one and
call the handler directly. This writes real rows to the `people` Cloud SQL
database and, for a newly-eligible person, creates a **real Google Contact**
(that path has no feature flag — see the gotcha below).

```bash
scripts/fetch-env.sh   # once — populates .env from Secret Manager + tfvars
```

```python
from dotenv import load_dotenv
load_dotenv()
import os
os.environ["HUBSPOT_WRITES_ENABLED"] = "false"   # safety: no real HubSpot create/evict

from handlers import email_classified, email_sent

email_classified.handle({
    "event": "email_classified",
    "message_id": "test-msg-1",
    "category": "respond",
    "importance": "P2",
    "confidence": 0.9,
    "subject": "Test subject",
    "sender": "person@example.com",
    "sender_display": "Test Person",
    "to": ["ben@example.com"],
    "cc": [],
    "received_at": "2026-09-03T12:00:00+00:00",
    "tags": [],
    "reasoning": "",
    "body": "Hello",
    "body_html": None,
    "web_link": None,
})

email_sent.handle({
    "event": "email_sent",
    "graph_message_id": "test-sent-1",
    "conversation_id": None,
    "sent_at": "2026-09-03T12:05:00+00:00",
    "from": "ben@example.com",
    "to": ["person@example.com"],
    "cc": [],
    "subject": "Re: Test subject",
})
```

Field shapes are `models/events.py::EmailClassifiedEvent` /
`EmailSentEvent`; both handlers only require the keys they read (`sender`,
`sender_display`, `category`, `received_at` for `email_classified`;
`to`, `cc`, `sent_at`, `graph_message_id` for `email_sent`) — the rest can be
empty/None as shown above.

## Running the sync handler

```python
from dotenv import load_dotenv
load_dotenv()
from handlers import sync
print(sync.run())   # {"google": {...}, "hubspot": {...}}
```

This runs a real Google Contacts incremental sync (or full list, first run)
and, if `HUBSPOT_WRITES_ENABLED=true`, the full HubSpot reconcile
(adopt/heal/enforce/fill — see `people-architecture`).

## Gotchas

- **`HUBSPOT_WRITES_ENABLED=false` only gates HubSpot.** `services/google_contacts_sync.py::ensure_contact`
  has no equivalent flag — an eligible test person always gets a real Google
  Contact created in the `GOOGLE_CONTACT_GROUP` group (default `"Inbox"`). To
  test counters/eligibility without any external write, use a `sender` that
  matches the automated filter (e.g. `test@noreply.example.com`) so
  `eligible` never flips true.
- Counters are **not idempotent** — running the same payload twice bumps
  `message_count`/`my_response_count` again. That's expected (spec §6.1); it
  mirrors how Pub/Sub redelivery behaves in production.
- `category="ignore"` on `email_classified` should leave a first-seen sender
  ineligible (`eligibility.inbound_eligible` returns `False`) — a useful
  no-op check before assuming eligibility logic is broken.
- Delete test Google Contacts / HubSpot contacts afterwards if you don't want
  them lingering — there's no cleanup script.
- `sync.run()` advances the stored `sync_token` in `sync_state` — a second
  call picks up from where the first left off, not a fresh full list.
