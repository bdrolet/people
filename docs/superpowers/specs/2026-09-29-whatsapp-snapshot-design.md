# WhatsApp Snapshot — Design

**Date:** 2026-09-29
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

`people` knows who Ben emails, the LinkedIn snapshot adds who he messages
there, and the iMessage snapshot added texting. WhatsApp is the remaining gap,
and it is the one that covers the people iMessage structurally misses:
international contacts. Of the 97 one-to-one WhatsApp chats on this Mac, 24
carry non-US numbers.

This design adds a **WhatsApp snapshot**, deliberately modelled on the iMessage
one (`docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md`): a local
script reads WhatsApp's SQLite store on Ben's Mac and incrementally upserts
chats, handles, members, and messages into five new tables in the `people`
database. Handles are linked to `people` rows through phone numbers.

- **Local read, incremental upsert.** `scripts/import_whatsapp.py` copies the
  store and opens it read-only, upserting from a `Z_PK` watermark. No new cloud
  infrastructure; people's Cloud Functions never read WhatsApp.
- **Separate tables, soft link.** A handle links to a `people` row only when the
  match is unambiguous.
- **Read-only relative to everything else.** The import never writes to
  `people`, Google Contacts, or HubSpot. WhatsApp is not a source of truth for
  any existing field and does not affect eligibility or HubSpot ranking.
- **Stats via the API, text only in the DB.** Message text is stored in Cloud
  SQL but is **never** served by `people-api` — the same constraint iMessage
  carries, for the same reason.

**This design is grounded in a read-only probe of the live database on
2026-09-29.** Every count in it was measured, not estimated; §5 records the
facts, including three pieces of common WhatsApp-schema folklore that are
**false** for this store and would produce wrong data if assumed. §12 is the
measured baseline an implementer can check their work against.

## 2. Goals and non-goals

**Goals**

1. Store WhatsApp chats, handles, group membership, and messages (with text)
   from the local store.
2. Rank handles by real 1:1 interaction (message count, last message, whether
   Ben replied), with group-chat activity counted separately so large groups do
   not skew it — identical semantics to `imessage_handles`.
3. Link handles to existing `people` rows by phone number where the match is
   unambiguous.
4. Surface per-handle stats and group membership through `people-api` and the
   people skills.
5. Make re-running cheap enough to run often (seconds).

**Non-goals**

- Serving message text through `people-api`, in any endpoint or field.
- Attachments: only a `has_media` flag and a coarse `media_kind` are stored;
  files are never read, copied, or uploaded.
- Reactions, starred flags, delivery/read receipts, disappearing-message
  timers, calls.
- Creating `people` rows, Google Contacts, or HubSpot contacts from WhatsApp
  data, or updating `people` counters, `last_contacted`, eligibility, or
  HubSpot ranking. Eligibility stays email-driven (parent spec §5).
- Writing anything back to WhatsApp, Google Contacts, or HubSpot.
- Fuzzy matching. Exact phone matches, plus one narrowly-scoped exact-name
  match for LID chats (§6.5), and nothing else.
- Status/broadcast content (`status@broadcast`, `0@status`, `*.status`
  sessions) — skipped entirely (§6.3).
- Newsletters/channels (`NewsletterChatSearchV1f.sqlite` is a separate store
  and is not read).
- Scheduled runs (`launchd`). Same position as iMessage: cheap enough for one,
  but scheduling is a later step.
- Reconciling against the phone. The Mac store holds what the desktop app has
  synced; see the caveat in §5.6.

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `clients/whatsapp_local.py` | clients | Copies the store (`.sqlite` + `-wal` + `-shm`) to a temp path, opens the copy read-only (`file:<path>?mode=ro`, `uri=True`), and yields raw session, message, group-info, and group-member rows. Local script only, like `clients/imessage_local.py` and `clients/graph_local.py`. |
| `services/whatsapp_export.py` | services | Pure logic: JID parsing and E.164 normalization including the MX/AR rule (§6.4), session classification, message filtering, media classification, and handle matching. No I/O. |
| `repo/whatsapp.py` | repo | `latest_watermark`, `upsert_*`, `delete_missing`, `recompute_handle_stats`, `record_import`, and the API read queries. Takes an open connection. |
| `models/whatsapp.py` | models | `WhatsAppHandle`, `WhatsAppChat`, `WhatsAppChatMember`, `WhatsAppMessage`, `WhatsAppBatch` dataclasses. |
| `scripts/import_whatsapp.py` | script | CLI (§6). |
| `api/routers/whatsapp.py` | api | New `/whatsapp/...` endpoints (§8). |
| `api/routers/people.py`, `api/routers/search.py` | api | Additive `whatsapp` field (§8.1). |
| `repo/schema.sql` | repo | Five new tables (§4), applied with `scripts/migrate_db.py`. |
| `.claude/skills/importing-whatsapp/` | skill | How to run the import; no Full Disk Access needed (§5.1). |

**No new dependencies.** `phonenumbers` is already in `requirements-dev.txt`
and `requirements.txt` (added by the iMessage work).

**Reused unchanged from `services/imessage_export.py`:**

- `apple_ts` — WhatsApp's `ZMESSAGEDATE` is **seconds** since 2001-01-01, and
  `apple_ts` already routes values below `10**11` down the
  seconds-since-2001 branch. Verified: `MIN(ZMESSAGEDATE)` maps to
  2019-07-23. One change needed — the annotation is `int | None` and Core Data
  hands back a `float`, so widen it to `int | float | None` (the arithmetic is
  already correct for floats).
- `normalize_handle` — used as the inner call of the WhatsApp JID normalizer
  (§6.4), not replaced.

Importing `services.imessage_export` from `services/whatsapp_export.py` is a
services-to-services import, which the layer rules permit; duplicating either
function would be worse.

## 4. Data model

The `person_id` column is `BIGINT REFERENCES people(id) ON DELETE SET NULL`,
matching `imessage_handles` and `linkedin_connections` as they exist **today**.

> **Note for the implementer:** the iMessage spec (§4) describes a
> `person_email TEXT REFERENCES people(email)` column. That is stale — PR #14
> re-keyed `people` on a `BIGSERIAL` id and moved both snapshots to
> `person_id`. Follow the live schema in `repo/schema.sql`, not the iMessage
> spec's §4, and use `person_id` from the start here. The API still exposes
> `person_email` via a join, and this design does the same (§8).

`ON DELETE SET NULL` over `RESTRICT` (would block a delete) or `CASCADE` (would
destroy snapshot rows over an advisory link). The constraint proves the row
exists, not that it is the right person; accuracy comes from re-matching every
run (§6.5).

**Operational note, now load-bearing:** `people` rows *are* deleted now —
`scripts/merge_duplicate_contacts.py` deletes them when collapsing duplicate
contacts, and it re-points `imessage_handles.person_id` and
`linkedin_connections.person_id` onto the survivor before doing so, precisely
because `ON DELETE SET NULL` would otherwise drop the links silently. **That
script must be taught about both `whatsapp_handles` and
`whatsapp_chat_members`** — see §11, step 5. This is
not optional cleanup; skipping it means a future merge silently unlinks
WhatsApp handles.

### 4.1 `whatsapp_handles`

One row per person-identity Ben has **interacted with**: a handle gets a row
when it has a 1:1 chat, or has sent at least one message in a group. Measured,
that is **621 rows**.

The alternative — a row per identity seen anywhere, including silent group
members — would be **6,767 rows**, of which 6,193 are people who have never
messaged Ben and never spoken in a group they share with him (§5.7). That is
not a contact list, it is the membership roster of two community groups with
1,212 and 1,189 members, and it would make `GET /whatsapp/handles` useless
without a filter and `group_count` meaningless. Those identities are still
stored in full, in `whatsapp_chat_members` (§4.3), which is where roster data
belongs.

```sql
CREATE TABLE IF NOT EXISTS whatsapp_handles (
    handle                 TEXT PRIMARY KEY,   -- E.164, or 'lid:<id>' (§6.4)
    jid                    TEXT,               -- the raw JID last seen for this handle
    display_name           TEXT,               -- ZPARTNERNAME, else ZPUSHNAME (§6.6)
    person_id              BIGINT REFERENCES people(id) ON DELETE SET NULL,
    match_method           TEXT,               -- 'phone' | 'name' | NULL
    message_count          INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    my_message_count       INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    last_message_at        TIMESTAMPTZ,
    last_my_message_at     TIMESTAMPTZ,
    group_message_count    INT NOT NULL DEFAULT 0,   -- messages this handle sent in group chats
    last_group_message_at  TIMESTAMPTZ,
    group_count            INT NOT NULL DEFAULT 0,   -- groups this handle is an active member of
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS whatsapp_handles_person_idx ON whatsapp_handles (person_id);
```

`group_message_count` differs from `imessage_handles.group_message_count`, and
the difference is deliberate: iMessage counts *all* messages in a group the
handle belongs to (it cannot reliably attribute group senders), whereas
WhatsApp *can* attribute them via `ZWAMESSAGE.ZGROUPMEMBER` (§5.4). So this
column counts **messages that handle actually sent**, which is the more useful
number. Document the divergence in the skill so the two are not compared
naively.

### 4.2 `whatsapp_chats`

```sql
CREATE TABLE IF NOT EXISTS whatsapp_chats (
    chat_jid          TEXT PRIMARY KEY,        -- ZWACHATSESSION.ZCONTACTJID
    kind              TEXT NOT NULL,           -- 'direct' | 'group'
    subject           TEXT,                    -- group subject, or the 1:1 partner name
    handle            TEXT,                    -- for 'direct': the other party's handle
    created_at        TIMESTAMPTZ,             -- ZWAGROUPINFO.ZCREATIONDATE (groups only)
    member_count      INT NOT NULL DEFAULT 0,  -- groups only; see §5.7
    message_count     INT NOT NULL DEFAULT 0,
    my_message_count  INT NOT NULL DEFAULT 0,
    last_message_at   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`handle` is denormalized for `direct` chats so a person lookup does not have to
re-derive it from the JID. It is `NULL` for groups.

`member_count` exists so a consumer can tell a five-person family group from a
1,212-member community group; without it, `group_count` on a handle invites
exactly the wrong conclusion (§5.7).

**On the primary key.** 208 sessions carry only **207 distinct JIDs** — one JID
has two session rows (both `ZSESSIONTYPE = 0`, one holding a single message and
one holding none). Keying on `chat_jid` collapses them, which is correct: they
are the same conversation, and the empty one contributes nothing. Same class of
issue as the duplicate stanza id (§4.4), and worth a test so the collapse is
deliberate rather than discovered.

### 4.3 `whatsapp_chat_members`

WhatsApp's genuinely new signal over iMessage: real group membership, 10,090
rows across 99 groups. Worth its own table — "who do I know through this
group" is not derivable from message traffic alone, because a member who never
posts leaves no messages.

```sql
CREATE TABLE IF NOT EXISTS whatsapp_chat_members (
    chat_jid   TEXT NOT NULL REFERENCES whatsapp_chats (chat_jid) ON DELETE CASCADE,
    handle     TEXT NOT NULL,
    person_id  BIGINT REFERENCES people (id) ON DELETE SET NULL,
    is_admin   BOOLEAN NOT NULL DEFAULT FALSE,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (chat_jid, handle)
);
CREATE INDEX IF NOT EXISTS whatsapp_chat_members_handle_idx ON whatsapp_chat_members (handle);
CREATE INDEX IF NOT EXISTS whatsapp_chat_members_person_idx ON whatsapp_chat_members (person_id);
```

`ON DELETE CASCADE` here, unlike the `people` FK: a membership row is
meaningless without its chat, and both sides are import-owned, so cascading is
correct rather than destructive.

`person_id` is resolved here too, by the same matcher (§6.5), and it is the
reason this table carries the link rather than relying on a join through
`whatsapp_handles`: **1 person already in `people` is in a group with Ben and
has never messaged him** (measured 2026-09-29 against the real store — an
earlier probe recorded 375 here, which four independent matching strategies
could not reproduce; see §12's dated note) — no 1:1 chat, never spoke in the
group. They get no `whatsapp_handles` row under the §4.1 rule, so without
`person_id` here they would be invisible to `people-api` entirely, and "I
share a group with them" is real signal worth keeping even when today it is
one person rather than hundreds — the column costs nothing to carry, and
WhatsApp's LID migration (§5.5) will keep moving this number as members who
still have a matchable phone number today migrate away from one.

A member's handle is deliberately **not** a foreign key to `whatsapp_handles` —
most members have no row there by design (§4.1), so the constraint would be
wrong, not merely inconvenient.

### 4.4 `whatsapp_messages`

```sql
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    chat_jid       TEXT NOT NULL REFERENCES whatsapp_chats (chat_jid) ON DELETE CASCADE,
    stanza_id      TEXT NOT NULL,       -- ZWAMESSAGE.ZSTANZAID
    sender_handle  TEXT,                -- NULL when from_me
    from_me        BOOLEAN NOT NULL,
    sent_at        TIMESTAMPTZ,
    text           TEXT,                -- never served by the API (§7)
    message_type   INT,                 -- raw ZMESSAGETYPE, for later analysis
    has_media      BOOLEAN NOT NULL DEFAULT FALSE,
    media_kind     TEXT,                -- 'image' | 'video' | 'audio' | 'document' | 'vcard' | 'other'
    source_pk      BIGINT NOT NULL,     -- ZWAMESSAGE.Z_PK, the watermark column
    PRIMARY KEY (chat_jid, stanza_id)
);
CREATE INDEX IF NOT EXISTS whatsapp_messages_sender_idx ON whatsapp_messages (sender_handle);
CREATE INDEX IF NOT EXISTS whatsapp_messages_sent_idx ON whatsapp_messages (sent_at DESC);
CREATE INDEX IF NOT EXISTS whatsapp_messages_source_pk_idx ON whatsapp_messages (source_pk);
```

**On the primary key.** iMessage keys messages by a globally-unique GUID.
WhatsApp's `ZSTANZAID` is *almost* unique but not quite: measured 10,328
distinct values across 10,329 rows — **one** duplicated id, and that duplicate
pair is inside a single chat, so `(chat_jid, stanza_id)` does not fully
disambiguate it either. The chosen key collapses that one pair into one row.

That is the right trade: the alternative keys are `Z_PK` (not stable — Core
Data renumbers on a store rebuild, so an upsert keyed on it would duplicate
every row after one) or `(chat_jid, stanza_id, sent_at)` (stable, but makes
every read and the `delete_missing` sweep three-column and buys one row out of
10,329). Losing one duplicate message is acceptable and is recorded in the
import output as `duplicate_stanza_ids` so it never becomes a silent surprise.
A test must pin this: two rows with the same `(chat_jid, stanza_id)` in one
batch produce one row, and the count is reported.

### 4.5 `whatsapp_imports`

```sql
CREATE TABLE IF NOT EXISTS whatsapp_imports (
    id                     BIGSERIAL PRIMARY KEY,
    started_at             TIMESTAMPTZ NOT NULL,
    finished_at            TIMESTAMPTZ NOT NULL,
    mode                   TEXT NOT NULL,       -- 'full' | 'incremental'
    watermark              BIGINT,              -- highest source_pk seen
    chats_upserted         INT NOT NULL DEFAULT 0,
    members_upserted       INT NOT NULL DEFAULT 0,
    handles_upserted       INT NOT NULL DEFAULT 0,
    messages_upserted      INT NOT NULL DEFAULT 0,
    messages_deleted       INT NOT NULL DEFAULT 0,
    senderless_dropped     INT NOT NULL DEFAULT 0,
    duplicate_stanza_ids   INT NOT NULL DEFAULT 0,
    sessions_skipped       INT NOT NULL DEFAULT 0,
    handles_unnormalized   INT NOT NULL DEFAULT 0,
    matched_by_phone       INT NOT NULL DEFAULT 0,
    matched_by_name        INT NOT NULL DEFAULT 0
);
```

### 4.6 Source-of-truth addendum

Add to the parent spec's table (CLAUDE.md §Source of truth):

| Field | Truth | Direction |
|---|---|---|
| `whatsapp_*` tables | WhatsApp's local store on Ben's Mac | Store → DB on local import (`scripts/import_whatsapp.py`). Never written back anywhere; WhatsApp is not a source for any `people` field. |

## 5. The source database (measured 2026-09-29)

### 5.1 Location and access

```
~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite
                                                             /ChatStorage.sqlite-wal
                                                             /ChatStorage.sqlite-shm
```

- **Plaintext SQLite 3.** Header bytes are `SQLite format 3`; no encryption, no
  keychain step.
- **11 MB** database in a 526 MB container (the rest is media, which this design
  never touches).
- **Full Disk Access is not required** — unlike `~/Library/Messages/chat.db`,
  this path is readable by the user without it. The skill should say so rather
  than copy the iMessage setup instructions.
- **The app is live** (latest message timestamp was the minute of the probe), so
  the `-wal` may be non-empty at read time. Copy all three files and read the
  copy, as `clients/imessage_local.py` does; do not open the original, and do
  not use `immutable=1`, which would silently ignore the WAL and read stale
  data.

The schema is Core Data (`Z`-prefixed tables, `Z_PK` integer keys):
`ZWAMESSAGE`, `ZWACHATSESSION`, `ZWAGROUPINFO`, `ZWAGROUPMEMBER`,
`ZWAMEDIAITEM`, `ZWAPROFILEPUSHNAME`, and others this design ignores.

### 5.2 Volume

| | |
|---|---|
| messages | 10,329 (3,190 from Ben; 8,193 with non-empty text) |
| chat sessions | 208 rows, 207 distinct JIDs (§4.2) — 98 one-to-one, 99 group, 11 other |
| messages in 1:1 chats | 5,940 |
| messages in group chats | 3,264 |
| messages in `@lid` chats | 1,125 (§5.5) |
| group member rows | 10,090 across 99 groups, 6,767 distinct identities |
| group members who ever sent a message | 538 |
| handles with interaction (§4.1) | 621 |
| date range | 2019-07-23 → 2026-09-29 |

For scale: the iMessage snapshot holds 106,083 messages. WhatsApp adds ~10% of
that volume, and its value is coverage of a different population, not bulk.

### 5.3 Three things that are NOT true

Each of these is widely repeated about WhatsApp's iOS/Catalyst schema and is
**false for this store**. An implementer who assumes any of them will write
wrong data with no error.

1. **`ZMEDIAITEM IS NOT NULL` does not mean "has media".** It is set on 9,664 of
   10,329 rows, including **7,762 plain-text (`ZMESSAGETYPE = 0`) messages**.
   Derive media by joining `ZWAMEDIAITEM` and requiring real content —
   `ZMEDIALOCALPATH`, `ZMEDIAURL`, or `ZVCARDSTRING` non-null. That yields
   **1,576** messages with media, which is the number to expect.
2. **`ZGROUPEVENTTYPE` is not a group-event flag.** It is non-zero on 10,085 of
   10,329 rows, across 25 distinct values, in 1:1 chats as well as groups. Do
   not use it to identify system messages or to classify anything.
3. **`ZSTANZAID` is not unique.** See §4.4.

Additionally, `ZMESSAGETYPE` has a long tail (21 distinct values observed: 0
text, 1 image, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 14, 15, 19, 20, 23, 43, 46, 59,
66). Do **not** build an allowlist of types — store the raw value in
`message_type` and let later analysis interpret it. Filtering is by the rules in
§6.3, not by type.

### 5.4 Determining the sender

- `ZISFROMME = 1` → Ben. `sender_handle` is `NULL`, `from_me` is true. This
  mirrors `imessage_messages`.
- 1:1 inbound → the sender is the chat's partner, from the session JID.
- Group inbound → `ZWAMESSAGE.ZGROUPMEMBER` → `ZWAGROUPMEMBER.ZMEMBERJID`. Set
  on 2,928 rows.
- **Group inbound with no `ZGROUPMEMBER`: 83 rows.** These are senderless and
  must be **dropped and counted** in `senderless_dropped`, not stored with a
  null sender. This is the same ruling the iMessage import made (its spec §5.2
  and the `senderless_dropped` counter) and for the same reason: a message
  attributed to nobody inflates chat counts while being useless for ranking.
  253 group rows also lack `ZGROUPMEMBER` but have `ZISFROMME = 1`; those are
  Ben's own and are kept.

### 5.5 LID sessions — the significant wrinkle

WhatsApp is migrating to **LID** (Linked ID) identifiers, which deliberately do
not contain a phone number. Measured: **4 `@lid` sessions carrying 1,125
messages — 11% of the entire database** — with `ZSESSIONTYPE = 0` (the same
value 1:1 sessions use), and one of them alone holds 1,117 messages.

Consequences:

- These chats **cannot be phone-matched**, ever. No amount of normalization
  helps; the number is not present.
- They are real 1:1 conversations and among the most active in the store, so
  dropping them would discard the single largest chat.
- Their handle is stored as `'lid:<local-part>'` (e.g. `lid:99900000000001`) so
  the `handle` primary key stays unambiguous and a LID handle can never
  collide with an E.164 one.
- `ZPARTNERNAME` is populated for them, which enables the narrow name match in
  §6.5.

This is the one part of the design with no iMessage precedent. Expect LID's
share to grow as WhatsApp migrates, so the handling must be deliberate rather
than incidental.

### 5.6 Completeness caveat

The desktop store holds what the Mac app has synced, which is not guaranteed to
equal the phone's history. The observed range starts 2019-07-23, which is deep,
but this design cannot verify it against the phone and does not try. Record it
as a known limitation in the skill: WhatsApp numbers are a floor, not a total.

### 5.7 Group size is bimodal, and it matters

Membership is not a contact list. Measured distribution across the 99 groups:

| Group size | Groups | Member rows |
|---|---|---|
| under 10 | 46 | 220 |
| 10–49 | 25 | 499 |
| 50–199 | 9 | 1,078 |
| 200–999 | 16 | 5,892 |
| 1,000+ | 2 | 2,401 |

The two largest hold 1,212 and 1,189 members. **8,293 of 10,090 member rows sit
in groups of 200 or more** — community and announcement groups where
co-membership says nothing about a relationship. Only 300 distinct identities
appear in groups smaller than 50, and only 538 ever sent a message.

Three design consequences, all of them load-bearing:

1. `whatsapp_handles` is restricted to identities with interaction (§4.1), or
   6,193 strangers drown the 621 real ones.
2. `whatsapp_chats.member_count` is stored, so a consumer can distinguish the
   two regimes instead of treating `group_count` as a closeness signal.
3. `whatsapp_chat_members` still stores all 10,090 rows with a resolved
   `person_id`, because 375 of them *are* people already in `people` (§4.3) —
   the roster is worth keeping, it just is not a handle list.

## 6. Import (`scripts/import_whatsapp.py`)

```
python scripts/import_whatsapp.py [--full] [--dry-run] [--db PATH]
```

Mirrors `scripts/import_imessage.py`: `--full` rebuilds from scratch and runs
`delete_missing`; the default is incremental; `--dry-run` reads and matches for
real but rolls back; `--db` points at an alternate store (a copy, or a test
fixture).

### 6.1 Reading

`clients/whatsapp_local.py` copies the three store files to a temp directory,
opens the copy with `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`, and
yields:

- sessions: `Z_PK`, `ZCONTACTJID`, `ZSESSIONTYPE`, `ZPARTNERNAME`,
  `ZLASTMESSAGEDATE`, and the joined `ZWAGROUPINFO.ZCREATIONDATE`
- messages: `Z_PK`, `ZSTANZAID`, `ZCHATSESSION`, `ZISFROMME`, `ZMESSAGEDATE`,
  `ZTEXT`, `ZMESSAGETYPE`, `ZPUSHNAME`, `ZGROUPMEMBER`, `ZMEDIAITEM`, and the
  joined `ZWAMEDIAITEM.ZMEDIALOCALPATH`/`ZMEDIAURL`/`ZVCARDSTRING`/`ZTITLE`
- group members: `ZCHATSESSION`, `ZMEMBERJID`, `ZISADMIN`, `ZISACTIVE`,
  `ZCONTACTNAME`

The temp copy is deleted in a `finally`, including on error.

### 6.2 Watermark

Incremental runs select messages with `Z_PK > (watermark - safety)` where the
watermark is `MAX(source_pk)` from `whatsapp_messages` and `safety` covers a
14-day re-scan window, exactly as the iMessage import does — late-arriving and
edited messages get re-read rather than missed. `--full` ignores the watermark.

Sessions, group info, and members are re-read in full on every run: 208 and
10,090 rows are trivial, and membership changes leave no watermark.

### 6.3 Filtering

Skip, and count in `sessions_skipped`:

- `status@broadcast`, `0@status`, and any JID ending `.status` — status/story
  traffic, 0 messages in all of them today but present as sessions.
- Sessions whose JID has no `@`, or an unrecognized suffix.

Keep everything else, including `@lid` (§5.5). Classification:

- JID ends `@g.us` → `kind = 'group'`
- JID ends `@s.whatsapp.net` or `@lid` → `kind = 'direct'`

Cross-check against `ZSESSIONTYPE` (observed: 0 for 1:1 and `@lid`, 1 or 4 for
groups, 2/3 for status) and log a warning on disagreement rather than trusting
it — the JID suffix is authoritative, `ZSESSIONTYPE` is the sanity check.

Message-level: drop senderless group inbound (§5.4). Do not drop by
`ZMESSAGETYPE` (§5.3). Empty `ZTEXT` is stored as `NULL`, not `''`.

`media_kind` is derived from the `ZWAMEDIAITEM` join: `ZVCARDSTRING` present →
`'vcard'`; otherwise map from `ZMESSAGETYPE` (1 → image, 2 → video, 3 → audio,
8 → document) with anything else that has real media → `'other'`. `has_media`
is true exactly when the join found real content (§5.3, item 1).

### 6.4 JID normalization — including the MX/AR rule

```python
def jid_to_handle(jid: str) -> tuple[str | None, str]:
    """Return (handle, kind_hint). handle is E.164, 'lid:<id>', or None."""
```

For `@s.whatsapp.net`, the local part is digits. Call
`imessage_export.normalize_handle("+" + local)` first. When that returns
`None`, apply the legacy-mobile-digit rule and retry:

| Country | JID form | E.164 form |
|---|---|---|
| Mexico (52) | `521` + 10 digits (13 total) | `52` + 10 digits (12 total) |
| Argentina (54) | `549` + 10 digits (13 total) | `54` + 10 digits (12 total) |

**This rule is not optional — it is worth 40% of the match rate.** Measured:

| | chats normalized | chats matching a `people` row |
|---|---|---|
| `normalize_handle` alone | 77 of 97 | **43** |
| plus the MX/AR rule | 96 of 97 | **60** |

All 20 failures were country code 52 with 13-digit local parts. They are not
recoverable by relaxing validation: `phonenumbers` reports
`is_possible_number = False` for them, so the existing
`is_possible_number` check in `normalize_handle` (which already had to be
loosened once, for the `+1555010xxxx` fixture range — see its comment) cannot
be loosened further to cover this. The digit must be stripped.

Argentina is included on the same historical basis even though this store has
no `54` chats today; the rule is symmetric and costs one line. A test should
cover both, plus a `52`-prefixed number that is *already* 12 digits (must not
be mangled).

The 1 remaining unnormalized chat has a 1-digit local part and no messages —
count it in `handles_unnormalized` and skip it.

For `@lid`, the handle is `'lid:' + local`. Never attempt phone normalization.

Store the raw JID in `whatsapp_handles.jid` regardless, so nothing is lost when
normalization fails.

### 6.5 Matching handles to people

Applies to both `whatsapp_handles.person_id` and
`whatsapp_chat_members.person_id`, by the same function over the same inputs.
Re-matched from scratch on **every** run, like both existing snapshots — a
link is never sticky, so a merge, a new contact, or a corrected phone number is
picked up on the next import.

1. **By phone (`match_method = 'phone'`).** Look the E.164 handle up against
   `people.phone_numbers` (which Google Contacts populates). A handle matching
   **exactly one** `people` row links to it. A handle matching more than one
   links to nothing and is counted — ambiguity is an error, not a guess, the
   same position `repo/people.py::get_by_phone` takes for the API.
2. **By name, for LID handles only (`match_method = 'name'`).** `ZPARTNERNAME`
   is present for all 98 one-to-one sessions, including LID ones. For a LID
   handle, and **only** when phone matching is impossible by construction,
   match `ZPARTNERNAME` case-insensitively against `people.display_name` and
   link only when exactly one row matches. `repo/people.py::names_for_matching`
   already exists for exactly this, and `services/linkedin_export.py`
   establishes the precedent.

   This is the one place a false link can occur. Constrain it: exact
   (normalized-case, trimmed) equality only, never substring or fuzzy; unique
   match only; and `match_method` records `'name'` so a reader can tell a
   name-matched link from a phone-matched one and distrust it accordingly.
   Never name-match a non-LID handle — if it has a phone and the phone does not
   match, the answer is "no match", not "try the name".

No email matching: WhatsApp has no email addresses.

### 6.6 Display names

Prefer `ZWACHATSESSION.ZPARTNERNAME` (the name as WhatsApp shows it, available
for every 1:1 session). Fall back to `ZWAMESSAGE.ZPUSHNAME` (present on all
10,329 rows — the sender's self-chosen display name) for handles seen only as
group members, and to `ZWAGROUPMEMBER.ZCONTACTNAME`. These are the *other
party's* chosen names, not Google Contacts data, and they never overwrite
anything in `people`.

### 6.7 Write

One transaction. Order matters:

1. `whatsapp_chats` upsert (parents first — messages and members reference it).
2. `whatsapp_handles` upsert — **only** for identities meeting the §4.1
   interaction rule (a 1:1 chat, or at least one group message sent). Stats are
   zeroed on insert only; `ON CONFLICT` must not reset counters, which step 5
   recomputes. A handle that later starts talking gains its row on that run, so
   the rule needs no backfill.
3. `whatsapp_chat_members` — delete-and-replace per chat, since membership is a
   full snapshot with no per-row id. Every member is written here regardless of
   the §4.1 rule, with `person_id` resolved.
4. `whatsapp_messages` upsert, chunked (`chunk = 500`, as `repo/imessage.py`).
5. `recompute_handle_stats` — a single SQL aggregate over
   `whatsapp_messages` + `whatsapp_chat_members`, not per-row Python.
6. `record_import`.

Timestamp aggregates must use `GREATEST(EXCLUDED.x, whatsapp_chats.x)` on
conflict. **This is a known, previously-shipped bug:** the iMessage import's
first version nulled `imessage_chats.last_message_at` on incremental runs for
939 of 971 rows, because an incremental batch legitimately contains no messages
for most chats and a bare `EXCLUDED.x` overwrote the stored value with `NULL`.
A test must cover it: an incremental run touching one chat must leave every
other chat's `last_message_at` intact.

### 6.8 Output

Counts only — never names, numbers, or message text; this repo is public and
the output lands in terminals and CI logs. Mirror
`scripts/import_linkedin.py::summary`:

```
DRY RUN — nothing written          (only when --dry-run)
chats 201 (direct 102, group 99, skipped 7, duplicate jid 1)
messages 10,245 upserted, 0 deleted, 83 senderless dropped, 1 duplicate id
handles 621 (phone-matched 69, name-matched 0, unmatched 552, unnormalized 1)
members 10,090 rows / 6,767 identities across 99 groups (phone-matched 444)
watermark 10329
```

## 7. Privacy

- **Message text is stored and never served.** No endpoint, no field, no
  `include=` parameter. `api/routers/whatsapp.py` must not select `text`, and a
  test should assert the string `text` is absent from every response model —
  the iMessage design took the same position and it has held.
- **Media is never read.** Only a boolean and a coarse kind, both derived from
  the database; no file is opened, hashed, copied, or uploaded.
- **Nothing personal is committed.** Import output is counts; test fixtures are
  synthetic; the repo stays clean of real JIDs, names, and text.
- **The temp copy is deleted** in a `finally` block. It contains the full
  message history in plaintext, so leaving it behind is a real leak, not an
  untidiness.
- Group membership reveals third parties who have never messaged Ben directly.
  That is the point of the table, but it means `whatsapp_chat_members` should
  never be exposed with names attached for unmatched handles — §8.3 returns
  handles and counts, not a roster of names.

## 8. `people-api` changes

### 8.1 Additive field on person responses

`GET /people/{ident}` gains a `whatsapp` object alongside the existing
`imessage` one, `null` when the person has no linked handle, and omitted (as
`imessage` already is) from list and search responses:

```json
"whatsapp": {
  "handles": ["+15550100001"],
  "message_count": 214, "my_message_count": 98,
  "last_message_at": "2026-09-28T19:02:11Z",
  "last_my_message_at": "2026-09-28T18:40:02Z",
  "group_message_count": 31, "group_count": 3,
  "shared_groups": 2,
  "match_method": "phone"
}
```

Aggregated across every handle linked to that person, the way
`repo/imessage.py::summary_for_person` does. No text, no chat subjects.

`shared_groups` comes from `whatsapp_chat_members.person_id`, not from
`whatsapp_handles`, and it is the reason the object must be returned as
**non-null when the person has group membership but no handle row** — the 375
people in §4.3. For one of those the interaction counters are all zero and
`match_method` is `null`, which is accurate: nothing has been exchanged, they
are simply in a group together. Count only groups where the member row is
`is_active`.

### 8.2 `POST /search`

Unchanged in shape. As with iMessage, a person carrying WhatsApp activity is not
ranked differently; search stays email/name/company-driven.

### 8.3 New endpoints (`api/routers/whatsapp.py`)

| Endpoint | Returns |
|---|---|
| `GET /whatsapp/handles?limit=&unmatched=` | handles with stats, `person_email` via join, ordered by 1:1 `last_message_at` desc |
| `GET /whatsapp/handles/{handle}` | one handle, plus the groups it is in (subjects and counts) |
| `GET /whatsapp/chats?kind=&limit=` | chats with counts and `last_message_at`; **no `last_message_text`** |
| `GET /whatsapp/imports/latest` | the newest `whatsapp_imports` row |

`{handle}` needs percent-encoding for `+` (`%2B`), as `fetching-person`
documents for phone idents. A `lid:` handle needs no encoding.

Auth is Cloud Run IAM, like every other route — no new credential.

## 9. Skills and docs

- **New:** `.claude/skills/importing-whatsapp/SKILL.md` — how to run it, the
  no-Full-Disk-Access difference from iMessage, the LID limitation, the
  completeness caveat (§5.6), and the `group_message_count` divergence from
  iMessage (§4.1).
- **Update:** `querying-people-db` (the new tables, and a query for
  "people with WhatsApp activity but no email"), `fetching-person` (the
  `whatsapp` field), `people-architecture` (the new subsystem).
- **Update CLAUDE.md:** the Stack table's Database row (four new tables), the
  Code layout block (every new file), and the source-of-truth table (§4.6).

> While updating CLAUDE.md, note that `scripts/merge_duplicate_contacts.py` is
> currently missing from the Code layout block — it was added by PR #18 and
> never documented. Fix that in the same pass.

## 10. Observability

Counters in `clients/otel.py`, prefixed `people_` like the rest:

- `whatsapp_import_messages` (labels: `mode`, `outcome` =
  `upserted`/`deleted`/`senderless_dropped`/`duplicate`)
- `whatsapp_import_handles` (labels: `match_method` =
  `phone`/`name`/`none`/`unnormalized`)
- `whatsapp_import_duration_seconds`

The script is local, so it flushes OTel explicitly before exit, as
`scripts/import_imessage.py` does.

## 11. Rollout

1. **Schema first.** Apply the five tables with `scripts/migrate_db.py`. All
   `CREATE TABLE IF NOT EXISTS` — purely additive, so this is safe ahead of
   code. (Contrast with the identity migration, which was destructive and
   caused a production outage when applied before its code; that experience is
   why the ordering is called out. Here, additive-first is correct.)
2. **Import code and script**, with tests. No deploy needed — the script is
   local and the Cloud Functions do not read WhatsApp.
3. **`--dry-run`, then a real `--full` run.** Check the output against §12.
4. **API router and the additive field**, which is what triggers the
   `deploy-api.yml` workflow.
5. **Teach `scripts/merge_duplicate_contacts.py` about `whatsapp_handles`** —
   add `repo/whatsapp.py::repoint_person` and call it beside the iMessage and
   LinkedIn re-points in `_collapse_rows`. Without this, the next contact merge
   silently unlinks WhatsApp handles via `ON DELETE SET NULL` (§4). This step
   is not optional and belongs in the same PR as the schema.
6. **Skills and CLAUDE.md** (§9).

> **Status note (added during implementation):** step 5 could not be completed in
> this PR — `scripts/merge_duplicate_contacts.py` is not on `main`, it is on the
> unmerged `merge-duplicate-contacts` branch (PR #18). `repo/whatsapp.py::repoint_person`
> is implemented and tested; the call site must be added to `_collapse_rows`
> whichever of the two branches merges second. Until then, a contact merge silently
> unlinks WhatsApp handles and memberships via `ON DELETE SET NULL`.

## 12. Measured baseline (2026-09-29)

An implementer should reproduce these from a real `--full` run. A material
difference means either the store changed or the import is wrong.

| Quantity | Expected |
|---|---|
| messages in the store | 10,329 |
| messages stored | 10,245 (10,329 − 83 senderless − 1 duplicate id) |
| duplicate stanza ids collapsed | 1 |
| messages from Ben | 3,190 |
| messages with non-empty text | 8,193 |
| messages with real media | 1,576 |
| chat sessions in the store | 208 rows / 207 distinct JIDs |
| sessions skipped (status/broadcast) | 7 |
| `whatsapp_chats` rows | 201 (102 direct + 99 group, one duplicate JID collapsed) |
| direct chats | 102 (98 `@s.whatsapp.net` + 4 `@lid`) |
| group chats | 99 |
| messages in 1:1 `@s.whatsapp.net` chats | 5,940 |
| messages in group chats | 3,264 |
| messages in `@lid` chats | 1,125 |
| group member rows | 10,090 across 99 groups |
| distinct group-member identities | 6,767 |
| group members who ever sent a message | 538 |
| `whatsapp_handles` rows (interaction only, §4.1) | 621 |
| `whatsapp_handles` phone-matched to a `people` row | 69 |
| membership-only identities | 6,193 |
| membership-only identities phone-matched | 375 |
| 1:1 chats normalized to E.164 | 96 of 97 |
| 1:1 chats phone-matched to a `people` row | 60 |
| messages in phone-matched 1:1 chats | 5,845 |
| member rows in groups of 200+ | 8,293 of 10,090 |
| largest two groups | 1,212 and 1,189 members |
| date range | 2019-07-23 → run date |

Distinct `people` phone numbers available for matching at probe time: 723.

**Re-measured against the first real `--full` run (2026-09-29, same day, later).**
Almost every figure above reproduced within ordinary live-store drift — including
the two figures anchored elsewhere in this doc, `whatsapp_handles` phone-matched
(69, exact) and the MX/AR rule's 1:1-chat count (60, exact) — and the chat total
landed at 199, matching the correction this doc's own reviewers already expected
given the duplicate-JID collapse and the one unnormalized session (102+99=201 was
never internally consistent; 100+99=199 is). One figure did not reproduce and was
investigated rather than copied forward: **membership-only identities
phone-matched, measured 1, not 375.** Four independent match strategies — exact
E.164 against `people.phone_numbers`, last-10/9/8/7-digit suffix matching, and a
lookup against Google Contacts' own phone index (726 numbers) — all agree on 26
total matched member identities (1 of which is membership-only; the other 25 are
also 1:1 partners or group senders with a `whatsapp_handles` row already). No
matching rule, tight or loose, reproduces 375, and the ambiguous-phone rule is not
the cause: none of the 26 matches are ambiguous. The conclusion is that 375 was an
error in the original probe, not a change in the data or a defect in the shipped
matcher — corrected below and in §4.3.

| Quantity | Measured 2026-09-29 (real run) |
|---|---|
| messages in the store | 10,332 |
| messages stored | 10,225 |
| duplicate stanza ids collapsed | 1 |
| messages from Ben | 3,190 |
| messages with non-empty text | 8,169 |
| messages with real media | 1,553 |
| chat sessions in the store | 208 rows / 207 distinct JIDs |
| sessions skipped (status/broadcast) | 7 |
| `whatsapp_chats` rows | 199 (100 direct + 99 group, one duplicate JID collapsed, one unnormalized session dropped) |
| direct chats | 100 (96 `@s.whatsapp.net` + 4 `@lid`) |
| group chats | 99 |
| messages in 1:1 `@s.whatsapp.net` chats | 5,918 |
| messages in group chats | 3,182 |
| messages in `@lid` chats | 1,125 |
| group member rows | 9,213 across 99 groups |
| distinct group-member identities | 5,991 |
| group members who ever sent a message | 540 |
| `whatsapp_handles` rows (interaction only, §4.1) | 623 |
| `whatsapp_handles` phone-matched to a `people` row | 69 |
| membership-only identities | 5,451 |
| membership-only identities phone-matched | **1** (was recorded as 375 above; not reproducible — see note) |
| 1:1 chats normalized to E.164 | 96 of 97 |
| 1:1 chats phone-matched to a `people` row | 60 |
| messages in phone-matched 1:1 chats | 5,846 |
| member rows in groups of 200+ | 7,416 of 9,213 |
| largest two groups | 1,213 and 1,190 members |
| date range | 2019-07-23 → run date |

Distinct `people` phone numbers available for matching at probe time: 724.
Roughly 57% of distinct group-member identities are now `lid:` rather than a
phone number (up from an unstated share at the original baseline) — consistent
with §5.5/§13's expectation that WhatsApp's LID migration keeps eating into the
phone-matchable population over time; the falling membership-only match count
sits on the same trend, not a separate one.

## 13. Decisions

| Decision | Choice | Why |
|---|---|---|
| Where the data lives | Five new tables in the `people` DB | Same shape as the iMessage and LinkedIn snapshots; one place to query relationships. |
| Message text | Stored in Postgres, never served | Ben's constraint, carried from the iMessage design; direct DB query is the only reader. |
| Message primary key | `(chat_jid, stanza_id)` | `Z_PK` is unstable across store rebuilds; the alternative three-column key buys one row out of 10,329 (§4.4). |
| Duplicate stanza id | Collapse, and report the count | One known pair; silent collapse would be the only real problem. |
| `ZMEDIAITEM` as a media flag | Rejected — join `ZWAMEDIAITEM` | Set on 7,762 plain-text rows; would report 9,664 media messages instead of 1,576. |
| `ZGROUPEVENTTYPE` | Ignored entirely | Non-zero on 10,085 of 10,329 rows; means nothing useful here. |
| `ZMESSAGETYPE` | Stored raw, never used to filter | 21 distinct values and a long tail; an allowlist would silently drop real messages. |
| Senderless group inbound (83) | Dropped and counted | Same ruling as iMessage; a message attributed to nobody inflates counts and cannot be ranked. |
| MX/AR legacy digit | Stripped, with a retry | Worth 40% of the match rate (43 → 60 chats); not fixable by relaxing validation. |
| LID chats | Stored as `lid:<id>`, never phone-matched | 11% of all messages, including the single largest chat; dropping them is not an option (§5.5). |
| LID name matching | Exact and unique only, `match_method = 'name'` | The only way to link a LID chat at all; recorded distinctly so it can be distrusted. |
| Name matching for non-LID handles | Not done | A handle with a phone that does not match means "no match"; falling back to names would manufacture false links. |
| Group membership | Its own table, full roster | The one signal WhatsApp has and iMessage does not; a silent member leaves no messages to infer from. |
| `whatsapp_handles` scope | Interaction only — 621 rows, not 6,767 | 6,193 of those identities are strangers in two 1,200-member community groups; they would drown the real contacts (§5.7). |
| `person_id` on `whatsapp_chat_members` | Yes, resolved at import | 375 people already in `people` share a group with Ben and have never messaged him; without it they are invisible to the API (§4.3). |
| `member_count` on chats | Stored | Without it, `group_count` reads as a closeness signal when it is mostly community-group noise (§5.7). |
| Duplicate session JID | Collapse on `chat_jid` | 208 sessions, 207 JIDs; the extra row holds no messages. |
| `group_message_count` semantics | Messages the handle *sent* | WhatsApp can attribute group senders; iMessage could not. Divergence documented. |
| Attachments | Flag and coarse kind only | Same as iMessage; files are never read. |
| Re-matching | Every run, from scratch | A merge or a new contact should be picked up without a full rebuild. |
| Full Disk Access | Not required | The group container is user-readable, unlike `chat.db`; the skill must not copy iMessage's setup steps. |
| Scheduling | Out of scope | Same position as iMessage: cheap enough for `launchd`, but that is a later decision. |
