---
name: importing-imessage
description: Use when the user wants to load, refresh, or re-import their iMessage history into the people database, run scripts/import_imessage.py, or check how old the iMessage snapshot is.
---

# Importing an iMessage Snapshot

`scripts/import_imessage.py` reads the Messages database on Ben's Mac
(`~/Library/Messages/chat.db`, opened read-only) and incrementally upserts
chats, handles, and messages into the `imessage_chats`, `imessage_handles`,
and `imessage_messages` tables, then appends a row to `imessage_imports`. It
never writes to `people`, Google Contacts, or HubSpot — iMessage activity
never affects counters, eligibility, or HubSpot ranking. Spec:
`docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md`.

## 1. Grant Full Disk Access (one-time, per terminal)

Reading `chat.db` directly requires Full Disk Access: **System Settings →
Privacy & Security → Full Disk Access** → add the terminal app you run the
script from → **restart the terminal**. Without it the script exits 2 with:

```
grant Full Disk Access to your terminal: System Settings → Privacy & Security → Full Disk Access
```

## 2. First run: full, dry-run, then for real

```bash
cd ~/src/people
scripts/fetch-env.sh            # if .env is missing or stale
.venv/bin/python scripts/import_imessage.py --full --dry-run
.venv/bin/python scripts/import_imessage.py --full
```

`--dry-run` reads `chat.db`, builds the Google Contacts phone index, and
matches every handle, but writes nothing — always run it first. `--full`
reads every message in `chat.db` and also reconciles deletions (rows in the
DB no longer present in `chat.db`); use it for the first import and
periodically after.

## 3. Routine runs

```bash
.venv/bin/python scripts/import_imessage.py
```

A plain (no `--full`) run is incremental: it reads messages with `ROWID`
past the last run's watermark, plus a 14-day re-scan window that catches
edits and unsends made since. Handles, chats, and matches are re-evaluated
in full on every run regardless of mode, so links to Google Contacts and
`people` stay current even on an incremental run. Cheap enough to run often.

`--db <path>` overrides the default `~/Library/Messages/chat.db`.

## 4. Reading the counts

```
messages 12,408 upserted (undecoded 3, retracted 2, deleted 0)
handles 412 (email 18, google 291 [linked to people 64], unmatched 103; short codes dropped 57)
chats 530 (group 44)   watermark 1,284,110 → 1,296,518   mode incremental
```

(Numbers illustrative.)

- **undecoded** — message text that couldn't be decoded from
  `attributedBody`; stored with `text = NULL`, not a failure.
- **email / google [linked to people N] / unmatched** — how each handle
  matched: an email handle equal to a `people.email`, or a phone number
  mapping to exactly one Google contact (further linked to a `people` row
  when that contact has one). A phone number carried by two or more Google
  contacts is deliberately left unmatched.
- **short codes dropped** — numbers that don't parse as real phone numbers;
  their messages, chats, and handles are skipped entirely.
- **deleted** — nonzero only on `--full`: rows removed because `chat.db` no
  longer has them.
- **watermark** — the `ROWID` range this run covered; the next incremental
  run starts just past it.

Any error rolls back the whole run — the watermark does not advance, so a
re-run retries the same range and nothing is double-counted.

## 5. Check snapshot age

```bash
TOKEN=$(gcloud secrets versions access latest --secret people-api-token --project bens-project-462804)
curl -s https://people-api.drolet.cloud/imessage/imports/latest -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`404` = never imported.

## Message text stays out of the API

`imessage_messages.text` is stored in Cloud SQL but is **never** served by
`people-api` — no response model in `api/routers/imessage.py`, and neither
the `imessage` block on `GET /people/{email}` nor `imessage_results` on
`POST /search`, carries a text or content field. Only stats and link
metadata (`message_count`, `my_message_count`, `last_message_at`, …) are
reachable through the API. Reading actual message content requires a direct
DB query — see `querying-people-db`.
