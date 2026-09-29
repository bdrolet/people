---
name: importing-whatsapp
description: Use when the user wants to load, refresh, or re-import their WhatsApp history into the people database, run scripts/import_whatsapp.py, or check how old the WhatsApp snapshot is.
---

# Importing a WhatsApp Snapshot

`scripts/import_whatsapp.py` reads WhatsApp's local store on Ben's Mac
(`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`)
and incrementally upserts chats, handles, group membership and messages into the
`whatsapp_chats`, `whatsapp_handles`, `whatsapp_chat_members` and
`whatsapp_messages` tables, then appends a row to `whatsapp_imports`. It never
writes to `people`, Google Contacts or HubSpot — WhatsApp activity never affects
counters, eligibility or HubSpot ranking. Spec:
`docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md`.

## No Full Disk Access needed

Unlike `importing-imessage`, this needs no special permission: the WhatsApp group
container is readable by the user. Do not send Ben to System Settings.

The app is usually running, so the script copies the store **and its `-wal`/`-shm`
sidecars** to a temp directory and reads the copy, then deletes it. If a run dies
hard, check for a leftover `/tmp/whatsapp-import-*` directory — it holds the full
plaintext history.

## First run: full, dry-run, then for real

```bash
cd ~/src/people
scripts/fetch-env.sh            # if .env is missing or stale
.venv/bin/python scripts/import_whatsapp.py --full --dry-run
.venv/bin/python scripts/import_whatsapp.py --full
```

Routine runs after that are incremental and take seconds:

```bash
.venv/bin/python scripts/import_whatsapp.py
```

`--full` re-reads every message and reconciles deletions; the default reads from
the `Z_PK` watermark plus a 14-day re-scan window. `--db PATH` points at a copy or
a fixture.

## Reading the output

```
chats 201 (direct 102, group 99, skipped 7, duplicate jid 1)
messages 10,245 upserted, 0 deleted, 83 senderless dropped, 1 duplicate id
handles 621 (phone-matched 69, name-matched 0, unmatched 552, unnormalized 1)
members 10,090 rows / 6,767 identities across 99 groups (matched 444)
watermark 10329   mode full
```

(Illustrative magnitudes, from the design's measured baseline — the shape of a
real run's output, not a live number.) The members line reports `matched`, not
`phone-matched` — a member row carries no `match_method`, so that count folds in
name matches too. The handles line does split `phone-matched` from
`name-matched`, because a handle row does carry a `match_method`.

Counts only, never names or numbers — the repo is public. The oddities are all
expected and documented:

- **`senderless dropped`** — group messages WhatsApp cannot attribute to a sender.
  A message attributed to nobody inflates counts and cannot be ranked, so it is
  dropped rather than stored with a null sender.
- **`duplicate id`** — `ZSTANZAID` is not unique (one known pair in the store);
  the pair collapses into one row and is counted so it is never a silent surprise.
- **`unnormalized`** — a chat whose number will not parse.
- **`skipped`** — status/broadcast sessions, which carry no conversation.

## Three things to know before trusting the numbers

1. **LID chats can never be phone-matched.** WhatsApp is migrating to Linked IDs,
   which deliberately contain no phone number. Their handle is stored as
   `lid:<id>` and they can only be linked by an exact, unique display-name match
   (`match_method = 'name'`) — the one link type worth distrusting. Expect LID's
   share of the store to grow over time.
2. **`group_message_count` means something different here than in iMessage.** On
   a `whatsapp_handles` row it counts messages that handle **sent** in groups;
   `imessage_handles.group_message_count` counts *every* message in a group the
   handle belongs to, because `chat.db` cannot attribute group senders and
   WhatsApp can. Never compare the two directly.
3. **The numbers are a floor, not a total.** The desktop store holds only what
   the Mac app has synced, which this import cannot reconcile against the phone's
   full history.

## Group membership is not a contact list

`whatsapp_chat_members` holds the full roster, but most member rows sit in very
large community groups where co-membership says nothing about a relationship.
That is why:

- `whatsapp_handles` only carries identities with real interaction (a 1:1 chat, or
  at least one group message sent) — a small fraction of the member-row count.
- `whatsapp_chats.member_count` is stored, so a five-person family group is
  distinguishable from a community group.
- `group_count` on a handle is **not** a closeness signal. Check the group's
  `member_count` before reading anything into it.

## What is never served

Message text is stored in Postgres and is **never** returned by `people-api` — no
endpoint, no field, no `include=` parameter. A direct DB query
(`querying-people-db`) is the only reader. Media files are never read at all:
only `has_media` and a coarse `media_kind` are stored.

## Checking how old the snapshot is

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -s -H "Authorization: Bearer $TOKEN" https://people-api.drolet.cloud/whatsapp/imports/latest
```

Or in SQL: `SELECT * FROM whatsapp_imports ORDER BY id DESC LIMIT 5;`
