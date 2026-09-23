# iMessage Snapshot — Design

**Date:** 2026-09-21
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

The `people` index knows who Ben emails, and the LinkedIn snapshot adds who he
messages on LinkedIn. Neither sees texting, which is where many of his closest
relationships actually happen.

This design adds an **iMessage snapshot**: a local script reads the Messages
database on Ben's Mac (`~/Library/Messages/chat.db`) and incrementally upserts
chats, handles, and messages into four new tables in the `people` database.
Handles (phone numbers and Apple ID emails) are linked to people through
Google Contacts phone numbers.

- **Local read, incremental upsert.** `scripts/import_imessage.py` opens
  `chat.db` read-only and upserts by message GUID from a `ROWID` watermark. No
  new cloud infrastructure; people's Cloud Functions never read iMessage.
- **Separate tables, soft link.** Handles are keyed by phone number or email,
  not `people.email`. A handle links to a `people` row only when the match is
  unambiguous.
- **Read-only relative to everything else.** The import never writes to
  `people`, Google Contacts, or HubSpot. iMessage is not a source of truth for
  any existing field and does not affect eligibility or HubSpot ranking.
- **Stats via the API, text only in the DB.** Message text is stored in Cloud
  SQL but is **never** served by `people-api`; it is readable only by direct
  DB queries.

## 2. Goals and non-goals

**Goals**

1. Store iMessage/SMS/RCS chats, handles, and messages (with text) from
   `chat.db`.
2. Rank handles by real 1:1 interaction (message count, last message, whether
   Ben replied), with group-chat activity counted separately so large groups
   do not skew it.
3. Link handles to Google Contacts and, through them, to existing `people`
   rows where the match is unambiguous.
4. Surface per-handle stats and metadata through `people-api` and the people
   skills.
5. Make re-running cheap enough to run often (seconds, not minutes).

**Non-goals**

- Serving message text through `people-api`, in any endpoint or field.
- WhatsApp (a later design).
- Attachments: only a `has_attachments` flag is stored; files are never read
  or uploaded.
- Reactions (tapbacks); they are skipped.
- Creating `people` rows, Google Contacts, or HubSpot contacts from iMessage
  data, or updating `people` counters, `last_contacted`, eligibility, or
  HubSpot ranking. Eligibility stays email-driven (parent spec §5).
- Writing phone numbers or names back to Google Contacts.
- Fuzzy or manual linking. Unambiguous matches only.
- Scheduled runs (`launchd`). The script is designed to be cheap enough for
  one, but scheduling is a later step.

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `clients/imessage_local.py` | clients | Opens `chat.db` read-only (`file:<path>?mode=ro`, `uri=True`) and yields raw message, chat, chat-participant, and handle rows. Used only by the local script, like `clients/graph_local.py`. |
| `clients/google_contacts.py` | clients | New `list_phone_index()`: pages `people.connections.list` with `personFields=names,phoneNumbers,metadata` and **no sync token**, returning every contact's resource name, display name, and raw phone numbers. `PERSON_FIELDS` and `list_connections` are unchanged. |
| `services/imessage_export.py` | services | Pure logic: Apple timestamp conversion, `attributedBody` decoding, handle normalization, short-code and reaction filtering, 1:1 vs group classification, and handle matching. No I/O. |
| `repo/imessage.py` | repo | `latest_watermark`, `upsert_batch`, `delete_missing` (for `--full`), `recompute_handle_stats`, `record_import`, and the API read queries. Takes an open connection. |
| `models/imessage.py` | models | `IMessageHandle`, `IMessageChat`, `IMessageMessage`, `IMessageBatch` dataclasses. |
| `scripts/import_imessage.py` | script | CLI (§5). |
| `api/routers/imessage.py` | api | New `/imessage/...` endpoints (§7). |
| `api/routers/people.py`, `api/routers/search.py` | api | Additive `imessage` fields (§7). |
| `repo/schema.sql` | repo | Four new tables (§4), applied with `scripts/migrate_db.py`. |
| `.claude/skills/importing-imessage/` | skill | Full Disk Access setup and how to run the import. |

New dependency: `phonenumbers` (E.164 normalization).

## 4. Data model

`person_email` is `REFERENCES people(email) ON DELETE SET NULL`. Nothing in the
service deletes `people` rows today (a contact deleted in Google gets
`google_deleted_at` set, parent spec §4.3), so the constraint costs nothing in
normal operation and catches an import writing an address that is not in
`people` — a normalization mismatch or a stale match. When a row is deleted,
the link is cleared and the next import refills it.

`ON DELETE SET NULL` is chosen over `RESTRICT` (which would block a delete) and
`CASCADE` (which would destroy snapshot rows over an advisory link). The
constraint proves the address exists, not that it is the right person; matching
accuracy still comes from re-matching every run (§5.4).

**Operational note:** with the constraint in place, a full `people` rebuild must
use `DELETE FROM people`, which clears links, not `TRUNCATE`, which fails on a
referenced table and, with `CASCADE`, would truncate the snapshot tables too.

**Migration:** `linkedin_connections.person_email` gets the same constraint, so
the two snapshots behave alike. `repo/schema.sql` adds it idempotently:

```sql
DO $$ BEGIN
    ALTER TABLE linkedin_connections
        ADD CONSTRAINT linkedin_connections_person_email_fkey
        FOREIGN KEY (person_email) REFERENCES people(email) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
```

The table is small (about 1,300 rows), so the validation scan and lock are
brief. Existing values were matched from `people`, so the constraint should
validate as-is; if any row fails, `scripts/migrate_db.py` reports it and the fix
is to re-run `import_linkedin.py`, which re-matches from scratch.

### 4.1 `imessage_handles`

```sql
CREATE TABLE IF NOT EXISTS imessage_handles (
    handle                 TEXT PRIMARY KEY,   -- E.164 phone or lowercased email, see §5.3
    display_name           TEXT,               -- from the matched Google Contact
    google_resource_name   TEXT,
    person_email           TEXT REFERENCES people(email) ON DELETE SET NULL,
    match_method           TEXT,               -- 'email' | 'google' | NULL
    message_count          INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    my_message_count       INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    last_message_at        TIMESTAMPTZ,
    last_my_message_at     TIMESTAMPTZ,
    group_message_count    INT NOT NULL DEFAULT 0,   -- all messages in group chats this handle is in
    last_group_message_at  TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS imessage_handles_person_email_idx ON imessage_handles (person_email);
CREATE INDEX IF NOT EXISTS imessage_handles_google_idx ON imessage_handles (google_resource_name);
CREATE INDEX IF NOT EXISTS imessage_handles_name_trgm_idx ON imessage_handles USING gin (display_name gin_trgm_ops);
```

Stats are per handle and are always recomputed from `imessage_messages` (§5.5),
never incremented. `message_count` counts every message in 1:1 chats with the
handle; `my_message_count` counts Ben's. "Ben replied" is
`my_message_count > 0`. A person with several handles (a phone number and an
Apple ID email) is aggregated at read time by `person_email`, or by
`google_resource_name` when there is no `people` row.

### 4.2 `imessage_chats`

```sql
CREATE TABLE IF NOT EXISTS imessage_chats (
    chat_guid            TEXT PRIMARY KEY,
    display_name         TEXT,               -- group name, if set
    is_group             BOOLEAN NOT NULL,
    participant_handles  TEXT[] NOT NULL DEFAULT '{}',   -- normalized, excluding Ben
    last_message_at      TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS imessage_chats_participants_idx ON imessage_chats USING gin (participant_handles);
```

`is_group` is true when the chat has more than one participant handle.

### 4.3 `imessage_messages`

```sql
CREATE TABLE IF NOT EXISTS imessage_messages (
    guid             TEXT PRIMARY KEY,     -- chat.db message.guid; upsert key
    chat_guid        TEXT NOT NULL,
    sender_handle    TEXT,                 -- normalized; NULL when from_me
    from_me          BOOLEAN NOT NULL,
    sent_at          TIMESTAMPTZ NOT NULL,
    text             TEXT,                 -- NULL if undecodable or retracted; never served by people-api
    service          TEXT,                 -- 'iMessage' | 'SMS' | 'RCS'
    has_attachments  BOOLEAN NOT NULL DEFAULT FALSE,
    edited_at        TIMESTAMPTZ,
    retracted        BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS imessage_messages_chat_idx ON imessage_messages (chat_guid, sent_at);
CREATE INDEX IF NOT EXISTS imessage_messages_sender_idx ON imessage_messages (sender_handle);
```

A message in several chats in `chat_message_join` (rare) is stored once, under
its lowest `chat_id`. Retracted (unsent) messages keep their row with
`text = NULL` so counts stay stable.

### 4.4 `imessage_imports`

```sql
CREATE TABLE IF NOT EXISTS imessage_imports (
    id                  BIGSERIAL PRIMARY KEY,
    ran_at              TIMESTAMPTZ NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('incremental', 'full')),
    max_rowid           BIGINT NOT NULL,     -- chat.db message.ROWID watermark for the next run
    messages_upserted   INT NOT NULL,
    messages_deleted    INT NOT NULL,        -- --full only
    undecoded           INT NOT NULL,
    handles             INT NOT NULL,
    matched_by_email    INT NOT NULL,
    matched_by_google   INT NOT NULL,
    linked_to_people    INT NOT NULL,
    chats               INT NOT NULL
);
```

Append-only audit of runs, and the source of the watermark. Not replaced.

### 4.5 Source-of-truth addendum

Adds one row to parent spec §4.3:

| Field | Truth | Direction |
|---|---|---|
| `imessage_*` tables | `chat.db` on Ben's Mac | chat.db → DB on local import (`scripts/import_imessage.py`). Never written back anywhere; iMessage is not a source for any `people` field. |

## 5. Import (`scripts/import_imessage.py`)

```
.venv/bin/python scripts/import_imessage.py [--full] [--dry-run] [--db ~/Library/Messages/chat.db]
```

Runs locally against whatever DB `clients/db.py` resolves from `.env`, like
`scripts/import_linkedin.py`. `--db` defaults to `~/Library/Messages/chat.db`.
Requires the terminal (or Python) to have **Full Disk Access**.

### 5.1 Reading `chat.db`

Tables read: `message`, `handle`, `chat`, `chat_handle_join`,
`chat_message_join`. Columns used from `message`: `ROWID`, `guid`, `text`,
`attributedBody`, `handle_id`, `is_from_me`, `date`, `date_edited`,
`date_retracted` (if present), `service`, `cache_has_attachments`,
`associated_message_type`. A missing required column fails the run at the first
read, naming the column. Optional columns (`date_edited`, `date_retracted`) are
treated as NULL when absent, because Apple adds columns across macOS versions.
Column names must be confirmed against Ben's real `chat.db` before
implementation; the fixture (§10) is built from that shape.

Selection:

- **Incremental (default):** messages with `ROWID > watermark` **or** `date`
  within the last 14 days. The re-scan window picks up edits and unsends made
  after the previous run. The watermark is `max_rowid` from the latest
  `imessage_imports` row; 0 if there is none.
- **`--full`:** all messages.

The `handle`, `chat`, and `chat_handle_join` tables are small and are read in
full every run, so every handle is re-matched (§5.4) and chat metadata stays
current even in an incremental run.

### 5.2 Decoding and filtering

- **Timestamps:** `date` is nanoseconds since 2001-01-01 UTC on current macOS
  (seconds on very old rows: values below 10^11 are treated as seconds).
  Converted to UTC `datetime`. `0` means NULL for `date_edited` /
  `date_retracted`.
- **Text:** `text` if non-NULL; otherwise decode `attributedBody` (an
  `NSAttributedString` in Apple `typedstream` format) by extracting the first
  `NSString` payload. A blob that does not decode stores `text = NULL` and is
  counted as `undecoded`; it does not fail the run.
- **Reactions:** rows with `associated_message_type` in 2000–3999 (tapbacks and
  their removals) are skipped.
- **Retracted:** `date_retracted` set → `retracted = true`, `text = NULL`.
- **Service:** stored as-is (`iMessage`, `SMS`, `RCS`); all are imported.

### 5.3 Handle normalization

- Contains `@` → lowercased, trimmed email (via `services.eligibility.normalize`).
- Otherwise parsed with `phonenumbers` (default region `US`) and formatted E.164.
- **Short codes** (numbers that do not parse as a valid number, or have fewer
  than 7 digits after stripping) are dropped: their messages, chats, and handles
  are not imported. Counted in the output as `short codes dropped`.

Ben's own handles never appear as a participant: `chat.db` records his sends as
`is_from_me`, and `chat_handle_join` lists only other participants.

### 5.4 Matching handles

Built once per run: a Google phone index from `list_phone_index()`, mapping
each E.164-normalized phone number to the set of Google contacts carrying it,
and `people` rows' `email` and `google_resource_name`
(`people_repo` read). Evaluated in order; first hit wins:

1. **email** — an email handle equals a `people.email`. `person_email` set;
   `display_name` from that `people` row.
2. **google** — a phone handle maps to **exactly one** Google contact.
   `google_resource_name` and `display_name` set from it; `person_email` set if
   a `people` row has that `google_resource_name`.

Otherwise all link fields stay NULL. A number on two or more Google contacts is
not matched. Matching runs over **every** handle each run (not just those in
the selected messages), so links refresh as Google Contacts and `people` change.

### 5.5 Write

One transaction:

```
upsert imessage_chats      (ON CONFLICT (chat_guid) DO UPDATE)
upsert imessage_messages   (ON CONFLICT (guid) DO UPDATE)  -- batched executemany
[--full] load all chat.db guids / chat guids into TEMP tables, then
         DELETE FROM imessage_messages m WHERE NOT EXISTS (SELECT 1 FROM tmp_guids t WHERE t.guid = m.guid)
         DELETE FROM imessage_chats    c WHERE NOT EXISTS (SELECT 1 FROM tmp_chat_guids t WHERE t.chat_guid = c.chat_guid)
upsert imessage_handles    link fields for every handle
recompute_handle_stats     -- one UPDATE ... FROM (aggregate over imessage_messages ⋈ imessage_chats)
INSERT INTO imessage_imports ...
COMMIT
```

`people` rows are read inside the same transaction as the handle upsert, so the
`person_email` foreign key (§4) cannot fail on a row deleted mid-run; if it does
fail, the run rolls back rather than writing a dangling link.

Any error rolls back; the watermark does not advance, so the next run retries
the same range. `--dry-run` reads `chat.db`, builds the Google index, matches,
and prints the counts it would write, without writing.

A Google API error aborts before the transaction opens; no handle is written
with a partial match set.

### 5.6 Output

Counts only — never names, numbers, or message content:

```
messages 12,408 upserted (new 11,902, refreshed 506, undecoded 3, retracted 2)
handles 412 (email 18, google 291 [linked to people 64], unmatched 103; short codes dropped 57)
chats 530 (group 44)   watermark 1,284,110 → 1,296,518   mode incremental
```

(Numbers illustrative.) Missing Full Disk Access (`sqlite3.OperationalError:
authorization denied` / `unable to open`) exits 2 with:
`grant Full Disk Access to your terminal: System Settings → Privacy & Security → Full Disk Access`.

## 6. Privacy

- **Message text is never served by `people-api`.** No response model in
  `api/` has a text or content field for iMessage; a test enforces this (§10).
  Text is readable only by direct DB queries (`querying-people-db`, `psql`).
- `chat.db` is opened read-only and never copied into the repo. Add
  `chat.db*` and `imessage-export*/` to `.gitignore`.
- Test fixtures use a synthetic SQLite DB with `+1 555 01xx` numbers and
  `example.com` addresses only.
- Message text lives in the same private Cloud SQL database that already holds
  email-derived personal data and the LinkedIn snapshot. No logs or script
  output print message content, names, or numbers.

## 7. `people-api` changes

All iMessage responses carry stats and metadata only — **no message text**.

### 7.1 Additive field on existing person responses

`PersonOut` gains `imessage: IMessageSummary | null`, aggregated over all
handles with `person_email = email`:

```json
"imessage": {
  "handles": ["+15035550123", "alice@example.com"],
  "message_count": 212, "my_message_count": 98,
  "last_message_at": "…", "last_my_message_at": "…",
  "group_message_count": 40, "last_group_message_at": "…",
  "imported_at": "…"
}
```

Filled on `GET /people/{email}` and `PATCH /people/{email}` (one extra
lookup). List responses (`GET /people`, `POST /search` `results`) return
`imessage: null`, like `linkedin`.

### 7.2 `POST /search`

Response gains `imessage_results: list[IMessageHandleOut]` — handles whose
`display_name` or `handle` matches `q` (ILIKE substring or trigram similarity
> 0.3), ordered by `last_message_at` desc nulls last, same `limit`.

### 7.3 New endpoints (`api/routers/imessage.py`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/imessage/handles` | Filters: `q` (display name or handle substring), `min_messages` (int, 1:1), `replied` (bool), `quiet_since` (date: `last_message_at` before it), `unmatched` (bool: no `google_resource_name` and no `person_email`), `include_groups` (bool, default false: rank by `GREATEST(last_message_at, last_group_message_at)`), `limit` (default 50, max 500). Ordered by `last_message_at` desc nulls last. |
| `GET` | `/imessage/handles/{handle}` | One handle's stats and link fields, plus the group chats it is in: `chat_guid`, `display_name`, participant count, message count, `last_message_at`. No message text. 404 if unknown. `{handle}` is URL-encoded (`%2B1503…`). |
| `GET` | `/imessage/imports/latest` | Latest `imessage_imports` row. 404 if never imported. |

`IMessageHandleOut` is `handle`, `display_name`, `google_resource_name`,
`person_email`, `match_method`, and the six stats fields.

## 8. Skills and docs

- New `.claude/skills/importing-imessage/SKILL.md`: grant Full Disk Access,
  first run `--full --dry-run` then `--full`, then incremental runs; how to read
  the counts; how to check freshness via `/imessage/imports/latest`.
- `searching-people`: `imessage_results` and `/imessage/handles` filters
  (e.g. "who have I stopped texting").
- `fetching-person`: show the `imessage` block.
- `querying-people-db`: new tables, including that message text is only
  reachable here, and example queries.
- `people-architecture`, `CLAUDE.md`: code layout, source-of-truth row, local
  import command.

## 9. Observability

Local script, like `import_linkedin.py`: no metrics; prints counts and writes an
`imessage_imports` row. New API routes are covered by the existing
request-metrics middleware in `api/main.py`.

## 10. Testing

- `tests/fixtures/imessage.py` — builds a synthetic `chat.db` in a temp dir with
  the real table/column shape: a 1:1 chat, a group chat, `text`-only and
  `attributedBody`-only messages, an undecodable blob, a tapback, an edited and
  a retracted message, a short-code sender, an email handle, and an old
  seconds-based `date`.
- `tests/test_imessage_export.py` — timestamp conversion (ns and s),
  `attributedBody` decoding and the undecodable case, E.164 and email
  normalization, short-code and reaction filtering, 1:1 vs group, email match,
  unique Google phone match, ambiguous number (two contacts) → no match, Google
  match linked and not linked to `people`.
- `tests/test_imessage_local.py` — read-only open, incremental selection
  (watermark + 14-day window), missing optional column → NULL, missing required
  column fails naming it.
- `tests/test_repo_imessage.py` — `FakeConn` pattern: upsert SQL, `--full`
  deletes, stats recompute, import row; filter SQL/params for
  `/imessage/handles`.
- `tests/test_schema.py` (new, skipped without a local Postgres) — against a
  scratch database created from `repo/schema.sql`: inserting a handle with an
  unknown `person_email` raises, deleting a `people` row nulls the links in
  both `imessage_handles` and `linkedin_connections`, and applying the schema
  twice is a no-op (the `DO $$` block is idempotent).
- `tests/test_import_imessage.py` — dry-run writes nothing; error leaves the
  watermark unchanged; authorization-denied exits 2 with the Full Disk Access
  message.
- `tests/test_api.py` — `imessage` present/absent on `GET /people/{email}`,
  `imessage_results` on search, handle filters, detail 404, imports/latest,
  and a guard that no `/imessage` response or `IMessage*Out` model exposes a
  `text`/`content` field.
- Local verification (`verifying-pr-locally`): `--full --dry-run` then
  `--full` against Ben's real `chat.db`, then an incremental run (expect a
  small refreshed count, no new rows), and spot-check a few known contacts via
  the API.

## 11. Rollout

1. Grant Full Disk Access to the terminal used for the import.
2. Run `scripts/migrate_db.py` from the branch before merging. The new tables
   are `IF NOT EXISTS` and the `linkedin_connections` foreign key is added in an
   idempotent `DO $$` block (§4); both are safe against the running service. If
   the constraint fails to validate, re-run `import_linkedin.py` and repeat.
3. Merge; CI deploys `people-api`.
4. `import_imessage.py --full --dry-run`, then `--full`, then incremental runs.

No Terraform, secrets, Cloud Functions, or inbox changes.

## 12. Decisions

| Decision | Choice | Why |
|---|---|---|
| Scope | Chats, handles, messages with text; no attachments, no reactions | Full context kept for direct DB queries; attachments and tapbacks add bulk without signal. |
| Text exposure | Stored in Cloud SQL, never served by `people-api` | Ben wants stats via the API only; text stays reachable by direct query. |
| Identity | Separate tables keyed by normalized handle, linked via Google Contacts phone numbers | Phone numbers live in Google Contacts; keeps `people`, eligibility, and HubSpot untouched. |
| `person_email` integrity | FK to `people(email)` `ON DELETE SET NULL`, backfilled onto `linkedin_connections` | `people` rows are not deleted in practice, so it is free and catches a bad link at write time; a rebuild uses `DELETE`, not `TRUNCATE`. |
| Refresh | Incremental upsert by GUID from a ROWID watermark + 14-day re-scan; `--full` reconciles deletions | `chat.db` changes daily; full replace would rewrite all history each run. |
| Group chats | Stored; separate `group_message_count` stats | Large groups would otherwise inflate every member's 1:1 ranking. |
| Matching | Email handle exact, then phone on exactly one Google contact | Wrong links are worse than missing ones. |
| Google read | Separate `list_phone_index()` without sync token | Leaves the nightly sync's token and `PERSON_FIELDS` untouched. |
| Effect on `people` | None | iMessage is not a source of truth for any `people` field. |
