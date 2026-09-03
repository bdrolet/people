---
name: importing-contacts
description: Use when the user wants to backfill, bootstrap, or bulk-import people from email history, run scripts/import_contacts.py, or seed the people database and Google Contacts/HubSpot from a year of inbox/sent mail.
---

# Bulk-Importing People from Email History

Runs `scripts/import_contacts.py` (spec §12) from the repo root. It pages
Inbox and Sent Items via Microsoft Graph for `--days N` (default 365) and
feeds each message through the same `services/ingest.py` functions the
event handlers use, so counters and eligibility come out identically to the
live pipeline.

## Prerequisites

`.env` needs `CLIENT_ID`, `TENANT_ID`, and `OWN_ADDRESSES` — see
`scripts/fetch-env.sh` (populates `.env` from Secret Manager +
`terraform.tfvars`; `CLIENT_ID`/`TENANT_ID` are the shared Azure app,
read-only here — the script is a public client and needs no
`CLIENT_SECRET`).

```bash
scripts/fetch-env.sh
source .venv/bin/activate
```

## Command

```bash
python scripts/import_contacts.py --dry-run          # counts only, no writes — run this first
python scripts/import_contacts.py                     # real run, default 365 days
python scripts/import_contacts.py --days 180           # shorter window
```

- **`--dry-run`** runs the full scan and prints the same summary dict, but
  rolls back the transaction and skips every Google/HubSpot write — always
  run this first to sanity-check the counts before a real run.
- First run prompts a **device code** flow in the browser (Graph auth,
  cached at `~/.people-token-cache.json` — separate from inbox's cache).

## Resuming an interrupted run

The script commits **once per message**, so an interrupted run keeps
everything it already processed — just re-run the same command to pick up
where it left off. Because counters (`message_count`, `my_response_count`)
are not idempotent, re-running from the start without care double-counts
already-processed messages. Use `--reset-counters` for a clean re-run from
scratch:

```bash
python scripts/import_contacts.py --days 365 --reset-counters
```

This zeroes `message_count`/`my_response_count` on every row before
scanning, then recomputes them from the full window — safe on an already-run
table, not just an empty one.

## What to expect

Progress happens silently per message (no line-by-line log); the run ends
with a summary:

```python
{"inbound": 812, "outbound": 340, "newly_eligible": 210, "skipped": 4}
```

`skipped` counts messages missing a `from`/`sent_at`/`received_at`, or
inbound mail from an `OWN_ADDRESSES` address.

## After running

Use `querying-people-db` to spot-check: eligible-not-in-hubspot count, a
sample of recent-by-`last_interaction` rows. If `HUBSPOT_WRITES_ENABLED` was
`true` during the run, spot-check in HubSpot that a few known senders
appear with correct names — otherwise HubSpot has nothing to check yet and
the nightly `people-sync` reconcile fills it later.
