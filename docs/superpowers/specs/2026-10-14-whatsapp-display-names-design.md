# WhatsApp Display Names and LID Phone Resolution — Design

**Date:** 2026-10-14
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`
**Amends:** `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md` §5.3, §6.5, §6.6

## 1. Summary

Most WhatsApp handles in the `people` DB carry a garbled `display_name`, a
base64 string like `IAA=` or `CJaiyNAG…IAA=`, instead of a person's name.
Measured on 2026-10-14: **537 of 645 `whatsapp_handles` rows** have one.
Garbled names make `GET /whatsapp/handles?q=` and any by-hand roster work
useless for group participants.

The cause is a wrong assumption in the snapshot design (§6.6): it treats
`ZWAMESSAGE.ZPUSHNAME` as "the sender's self-chosen display name". On this
store it is not a name at all. Every one of its 10,705 non-null values is a
base64-encoded protobuf (decoded, it holds a varint timestamp and sometimes a
message id; no string field carries a name). The importer falls back to it for
every handle seen only as a group sender, which is almost all of them.

The real names live elsewhere, in two places the importer never reads:

- `ChatStorage.sqlite` → `ZWAPROFILEPUSHNAME (ZJID, ZPUSHNAME)`: the push name
  each contact chose, keyed by JID. 1,244 rows (311 `@lid`, 933 phone JIDs).
- `ContactsV2.sqlite` (same container) → `ZWAADDRESSBOOKCONTACT`: WhatsApp's
  copy of the phone's address book, with `ZFULLNAME`, `ZPHONENUMBER`,
  `ZWHATSAPPID` (phone JID) **and `ZLID`**. 498 rows, every one carrying both a
  LID and a phone number.

The second table also fixes a structural gap: the snapshot design says a LID
handle "cannot be phone-matched, ever" (§5.5). That's true of the message store
on its own, but the address book maps LID → phone for every contact in it. Using
it links **103 more group-member identities** to exactly one `people` row by
phone. Those 103 currently link to nothing.

This design:

1. Stops reading `ZWAMESSAGE.ZPUSHNAME` as a name.
2. Resolves display names from `ZWAPROFILEPUSHNAME` and the address book.
3. Resolves LID handles to a phone number through the address book, and
   phone-matches them.
4. Clears the garbled names already stored.

Nothing about the snapshot's boundaries changes. The import still writes only
`whatsapp_*` tables. It never writes `people`, Google Contacts or HubSpot.

## 2. Goals and non-goals

**Goals**

1. No `whatsapp_handles.display_name` is ever a protobuf blob again.
2. Every handle with a recoverable name gets one.
3. LID handles and members whose LID appears in the address book are matched by
   phone, with the same uniqueness rule as any phone match.
4. Existing garbled rows are cleaned by one `--full` run, with no hand SQL.

**Non-goals**

- Decoding the `ZWAMESSAGE.ZPUSHNAME` protobuf. It carries no name, so there's
  nothing to gain.
- Creating `people` rows or Google Contacts from WhatsApp data. WhatsApp stays
  a non-source for `people` fields (CLAUDE.md source-of-truth table). §7 covers
  this.
- Changing `handle` keys. A LID handle stays `lid:<id>` even when its phone is
  known, so existing rows, member rows and API URLs keep working.
- Name-matching more broadly. §6.5's narrow LID-only, exact, unique name match
  is unchanged and still keyed on `ZPARTNERNAME` only.

## 3. Measured facts (2026-10-14)

Probe run read-only against copies of both files, as `clients/whatsapp_local.py`
already does.

| Fact | Count |
|---|---|
| `whatsapp_handles` rows | 645 |
| … with a base64-blob `display_name` | 537 (507 LID, 30 phone) |
| `ZWAMESSAGE.ZPUSHNAME` non-null | 10,705 |
| … base64-shaped (`[A-Za-z0-9+/]+={0,2}`, length % 4 = 0) | 10,705 (all) |
| `ZWAGROUPMEMBER.ZCONTACTNAME` non-empty | 0 |
| `ZWAPROFILEPUSHNAME` rows | 1,244 (311 LID, 933 phone) |
| `ZWAADDRESSBOOKCONTACT` rows | 498, all with `ZLID`, `ZWHATSAPPID`, `ZPHONENUMBER`, `ZFULLNAME` |
| Garbled handles recoverable from either table | 152 of 537 |
| Distinct group-member identities | 6,018 (3,424 LID) |
| … LID members with an address-book phone | 107 |
| … LID members with a profile push name | 269 |
| LID members newly phone-matchable to exactly one `people` row | 103 |

The other 385 garbled handles have no name anywhere on this Mac. They go to
`NULL`, which is the honest answer and what `GET /whatsapp/handles` already
renders.

Two corrections to the snapshot design's §5 facts:

- §6.6: "`ZWAMESSAGE.ZPUSHNAME` (present on all 10,329 rows — the sender's
  self-chosen display name)" is **false**. It's present, but it's a protobuf
  blob, not a name.
- §5.5: "These chats **cannot be phone-matched**, ever" is true of
  `ChatStorage.sqlite` alone, and false once `ContactsV2.sqlite` is read.

## 4. Components

| Path | Change |
|---|---|
| `clients/whatsapp_local.py` | Also copy `ContactsV2.sqlite` (+ `-wal`/`-shm`) into the same temp dir. Read `ZWAPROFILEPUSHNAME` and `ZWAADDRESSBOOKCONTACT`. Drop `ZPUSHNAME` from `MESSAGE_SQL` and `REQUIRED_COLUMNS`. `RawStore` gains `push_names: dict[jid, str]` and `address_book: list[dict]`. |
| `services/whatsapp_export.py` | New pure `resolve_display_name(jid, partner_name, push_names, book_by_jid)`. New pure `lid_phone(handle, book_by_lid)`. `build_batch` uses both. `match_handles` phone-matches a LID handle through `lid_phone`. |
| `models/whatsapp.py` | `WhatsAppHandle` gains `phone: str \| None`. |
| `repo/schema.sql` | `ALTER TABLE whatsapp_handles ADD COLUMN IF NOT EXISTS phone TEXT;` |
| `repo/whatsapp.py` | Write `phone`. `display_name` conflict rule becomes mode-dependent (§5.4). |
| `scripts/import_whatsapp.py` | Summary line gains `lid-phone-matched` and `names: partner N, address book N, profile N, member N, none N`. `upsert_handles` gets the run mode. |
| `api/routers/whatsapp.py` | `phone` added to the handle response (additive). |
| `.claude/skills/importing-whatsapp`, `querying-people-db` | Document `phone`, the new match method, and the corrected name sources. |
| `tests/` | See §8. |

## 5. Design

### 5.1 Reading the address book

`ContactsV2.sqlite` sits beside `ChatStorage.sqlite` in the same group
container, needs the same (no) Full Disk Access, and is live in the same way.
So it's copied with its sidecars into the same `_copied` temp directory, and
removed in the same `finally`. A missing `ContactsV2.sqlite` isn't an error:
the import logs a count-only warning and proceeds with an empty address book.
Name and LID resolution then degrade to `ZWAPROFILEPUSHNAME` alone. Missing
tables or columns inside it are a `SchemaError`, the same as for the message
store.

```sql
-- ChatStorage.sqlite
SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME
WHERE ZPUSHNAME IS NOT NULL AND trim(ZPUSHNAME) <> '';

-- ContactsV2.sqlite
SELECT ZLID, ZWHATSAPPID, ZFULLNAME FROM ZWAADDRESSBOOKCONTACT;
```

Both are small, at about 1,250 and 500 rows, and carry no watermark. They're
read in full on every run, incremental included, as `ZWAGROUPMEMBER` already is.

### 5.2 Display-name precedence

For a handle with raw JID `j`, first non-empty wins:

1. `ZWACHATSESSION.ZPARTNERNAME`, for 1:1 sessions. Unchanged.
2. `ZWAADDRESSBOOKCONTACT.ZFULLNAME` where `ZLID = j` or `ZWHATSAPPID = j`.
   This is the name as Ben saved it, so it's the most useful for him.
3. `ZWAPROFILEPUSHNAME.ZPUSHNAME` where `ZJID = j`. This is the other party's
   self-chosen name.
4. `ZWAGROUPMEMBER.ZCONTACTNAME`. Kept for stores where it's populated (0 here).
5. `NULL`.

`ZWAMESSAGE.ZPUSHNAME` is never used. As a guard against a future schema reuse,
`resolve_display_name` also skips any candidate that is base64-shaped *with
padding*: `^[A-Za-z0-9+/]+={1,2}$` and length divisible by 4. No real name ends
in `=`, so the guard never drops one ("Mike" is valid base64 but unpadded, and
passes). It misses unpadded blobs (626 of the 10,705 on this store), which is
acceptable for a guard on a field the importer no longer reads. A test pins
"Mike" through and `IAA=` out.

Group members keep no `display_name` column today (`whatsapp_chat_members` has
none). That stays as it is.

### 5.3 LID → phone

`lid_phone(handle, book_by_lid)` returns an E.164 string for a `lid:<id>`
handle whose `<id>@lid` is a `ZLID` in the address book. It takes the
`ZWHATSAPPID` local part (digits) and runs it through the existing
`normalize_phone_local` path (via `jid_to_handle`), the same normalization any phone JID gets (§6.4,
including the MX/AR rule). It does not use the `ZPHONENUMBER` display string,
which carries locale formatting. A phone handle's `phone` is its handle.

In `match_handles`:

1. A phone handle is matched by phone, unchanged.
2. A LID handle with a `lid_phone` is matched by that phone, under the same
   exactly-one-row rule, with `match_method = 'lid_phone'`.
3. A LID handle without one falls back to the existing narrow `'name'` match.
   That match is never tried when step 2 found a phone but no unique row; a
   known phone that doesn't match means "no match".

The same function serves `whatsapp_chat_members.person_id`, so the 103 members
link on the next run.

`repo/whatsapp.py::rematch_stored_handles` (the SQL re-match for stored handles
an incremental batch didn't carry) matches on `COALESCE(phone, handle)`. It now
covers a LID handle that has a stored `phone`, and labels that link
`'lid_phone'`. A LID handle without a `phone` stays excluded, as today, because
its only possible link is the name match the SQL can't recompute. `match_method` values become `'phone' | 'lid_phone' |
'name' | NULL`, and a reader can trust `lid_phone` as much as `phone`. The link
is no less certain, it just has one more hop, through Ben's own address book.

### 5.4 Cleaning the stored names

`repo/whatsapp.py` currently writes
`display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)`.
With that rule a garbled name survives forever whenever the new resolution is
`NULL`, which is the case for 385 of the 537.

New rule:

- **`--full` run:** `display_name = EXCLUDED.display_name`. A full run sees
  every handle's complete evidence, so its answer, `NULL` included, is
  authoritative.
- **Incremental run:** keep `COALESCE`. An incremental batch may not see a
  handle's 1:1 session at all, so a missing name there means "not seen", not
  "has none".

Rollout's `--full` run therefore cleans every existing row, and no migration
SQL is needed. `whatsapp_handles` rows for handles absent from the full batch
are untouched, as today. None exist in practice, since a full batch reads every
handle with history.

### 5.5 Output

Counts only (§6.8 of the snapshot design; the repo is public):

```
handles 645 (phone-matched 72, name-matched 0, lid-phone-matched N, unmatched …)
names: partner N, address book N, profile N, member N, none N
members 9,257 rows / 6,018 identities (phone-matched …, lid-phone-matched N)
```

## 6. Privacy

`ContactsV2.sqlite` is Ben's own address book. It's read from a temp copy that
is deleted in `finally`, and only the fields above are read. No address-book
row becomes a stored row of its own. The address book only fills in
`display_name`/`phone` on handles that already earn a row under §4.1 of the
snapshot design, and drives `person_id` matching. Phones and names never reach
logs or the summary (§5.5). `people-api` gains `phone` on handle responses,
which it already effectively serves as the `handle` for phone handles.

## 7. Does WhatsApp feed `people`? (unchanged, stated for the record)

No. The import stores phone numbers (the E.164 `handle`, and now `phone` for
resolved LIDs) and display names **only** in `whatsapp_*`. It never creates a
`people` row or a Google Contact, and never writes a `people` field. A WhatsApp
contact who isn't already in Google Contacts stays visible only through
`GET /whatsapp/handles`. Turning WhatsApp identities into contacts would be a
separate design. It would need its own duplicate rules against Google, and an
answer for the 3,000+ LID-only community-group members who have no phone at
all.

## 8. Tests

- `resolve_display_name` precedence: partner over address book over profile
  over member over `None`.
- A `ZWAMESSAGE.ZPUSHNAME`-style blob (`IAA=`, `CJaiyNAGIAA=`) is never
  returned. `Mike`, `Ana`, `Jo` and a CJK name are returned unchanged.
- `lid_phone`: hit, miss, and an address-book `ZWHATSAPPID` that won't
  normalize (→ `None`, counted).
- `match_handles`: a LID handle with a resolved phone matching one row
  (`lid_phone`); matching two rows (no link, no name fallback); with no
  resolved phone and a unique partner name (`name`, unchanged).
- `repo/whatsapp.py`: `--full` overwrites a stored garbled name with `NULL`; an
  incremental run with `NULL` keeps the stored name.
- `clients/whatsapp_local.py`: a store without `ContactsV2.sqlite` imports with
  an empty address book. A `ContactsV2.sqlite` missing `ZLID` raises
  `SchemaError`.
- Synthetic fixtures only. No real names, numbers or JIDs (public repo).

## 9. Rollout

1. Merge. The schema change is additive (`ADD COLUMN IF NOT EXISTS`); apply it
   with `scripts/migrate_db.py`.
2. `python scripts/import_whatsapp.py --full --dry-run` and check the summary
   against §3: about 152 names recovered, 103 new member links, and 0
   base64-blob names.
3. `python scripts/import_whatsapp.py --full`.
4. Verify:
   ```sql
   SELECT count(*) FROM whatsapp_handles
   WHERE display_name ~ '^[A-Za-z0-9+/]{4,}={1,2}$';   -- expect 0
   SELECT match_method, count(*) FROM whatsapp_chat_members m
   JOIN whatsapp_handles h USING (handle) GROUP BY 1;
   ```
5. Routine incremental runs need nothing new.

## 10. Decisions

| Decision | Why |
|---|---|
| Address-book name ranks above profile push name | It's the name Ben chose, so it matches how he searches. The push name is whatever the other person typed. |
| Keep `lid:` handle keys; add `phone` beside them | Re-keying would orphan member rows and break API URLs, for no gain. |
| New `match_method = 'lid_phone'` | Readers can tell the extra hop apart, and §6.5's "distrust `name`" guidance stays precise. |
| `--full` overwrites `display_name`; incremental coalesces | It cleans the existing rows with no migration, and doesn't let a partial batch erase good names. |
| Missing `ContactsV2.sqlite` degrades, doesn't fail | Names are a nicety; the import's counters and messages shouldn't depend on it. |
