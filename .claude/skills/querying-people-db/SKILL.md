---
name: querying-people-db
description: >
  Use when the user wants to query, read, or inspect data in the people Cloud SQL
  database — checking people rows, eligibility, Google Contacts/HubSpot linkage,
  or sync state, verifying pipeline output, or debugging what's stored in the DB.
---

# Querying the People DB (GCP Cloud SQL)

**Project:** `bens-project-462804` | **Instance:** `us-central1:inbox` | **DB:** `people`

## Connection setup

Always activate the venv and fetch the password from Secret Manager:

```bash
source ~/src/people/.venv/bin/activate
```

```python
import os, sys, subprocess
sys.path.insert(0, os.path.expanduser('~/src/people'))
os.environ.update({
    'CLOUD_SQL_CONNECTION_NAME': 'bens-project-462804:us-central1:inbox',
    'POSTGRES_USER': 'people',
    'POSTGRES_DB': 'people',
    'POSTGRES_PASSWORD': subprocess.check_output(
        ['gcloud', 'secrets', 'versions', 'access', 'latest',
         '--secret=people-db-password', '--project=bens-project-462804'],
        text=True
    ).strip(),
})
from clients.db import get_conn
```

`get_conn()` uses the Cloud SQL Python Connector with `pg8000` over IAM/ADC — no proxy or firewall rules needed.

## Running queries

```python
with get_conn() as conn:
    rows = conn.execute("SELECT ...", ()).fetchall()
    for r in rows:
        print(r['column_name'])
```

Use `r['column_name']` dict-style access — rows come back as dicts on both
connection paths (Cloud SQL connector and local direct psycopg3).

## Key tables

| Table | Contents |
|---|---|
| `people` | `id` (BIGSERIAL **PK**), `email` (nullable, **unique** — no longer the PK), `display_name`, `first_seen`, `last_seen`, `last_contacted`, `message_count`, `my_response_count`, `notes`, `eligible`, `automated`, `google_resource_name`, `google_etag`, `google_deleted_at`, `hubspot_contact_id`, `hubspot_synced_at`, `phone_numbers` (text[], E.164, GIN), `company`, `job_title` (both from the primary/first `organizations` entry), `google_fields` (JSONB, the full allowlisted Google contact payload, GIN), `updated_at`. `CHECK` constraint `people_has_an_identifier`: `email IS NOT NULL OR cardinality(phone_numbers) > 0` — every row has an email or a phone, never neither. |
| `contact_groups` | user-defined labels only: `resource_name` (PK), `name`, `updated_at` — replaced every sync. `people.relationship_label` is unused and pending removal. |
| `people_labels` | `person_id` (FK → `people.id`), `group_resource_name` (FK → `contact_groups.resource_name`), both `ON DELETE CASCADE` |
| `sync_state` | one row, `key='google_contacts'`: `sync_token`, `last_run_at`, `last_status` |
| `linkedin_connections` | LinkedIn snapshot, PK `profile_url` (`linkedin.com/in/<slug>`): `full_name`, `email`, `company`, `position`, `connected_on`, `person_id` (FK → `people.id`, `ON DELETE SET NULL` — replaced `person_email`; there is no `person_email` column anymore), `match_method`, `message_count`, `my_message_count`, `last_message_at`, `last_my_message_at`, `snapshot_at` |
| `linkedin_messages` | `conversation_id`, `sender_name`, `sender_profile_url`, `recipient_names`, `recipient_profile_urls` (text[]), `sent_at`, `subject`, `content`, `folder`, `from_me` |
| `linkedin_recommendations` | `direction` (`given`/`received`), `full_name`, `company`, `job_title`, `text`, `status`, `created_on`, `profile_url` (null unless the name matched one connection) |
| `linkedin_imports` | append-only audit of `scripts/import_linkedin.py` runs: `snapshot_at`, `source`, row counts, `matched_by_email`, `matched_by_name` |
| `imessage_handles` | iMessage snapshot, PK `handle` (E.164 phone or lowercased email): `display_name`, `google_resource_name`, `person_id` (FK → `people.id`, `ON DELETE SET NULL` — replaced `person_email`; there is no `person_email` column anymore), `match_method` (`email`/`google`/null), `message_count`/`my_message_count` (1:1 chats only), `last_message_at`, `last_my_message_at`, `group_message_count`, `last_group_message_at`, `updated_at` |
| `imessage_chats` | PK `chat_guid`: `display_name` (group name, if set), `is_group`, `participant_handles` (text[], excludes Ben), `last_message_at` |
| `imessage_messages` | PK `guid`: `chat_guid`, `sender_handle` (NULL when `from_me`), `from_me`, `sent_at`, **`text`** (NULL if undecodable or retracted), `service` (`iMessage`/`SMS`/`RCS`), `has_attachments`, `edited_at`, `retracted` — **message text lives only here; `people-api` never serves it, by design (spec §6)** |
| `imessage_imports` | append-only audit of `scripts/import_imessage.py` runs: `ran_at`, `mode` (`incremental`/`full`), `max_rowid` (watermark), row counts, `matched_by_email`, `matched_by_google`, `linked_to_people` |
| `whatsapp_handles` | WhatsApp snapshot, PK `handle` (E.164 phone, or `lid:<id>` for a Linked ID that carries no phone number): `jid` (raw JID last seen), `display_name`, `person_id` (FK → `people.id`, `ON DELETE SET NULL`), `match_method` (`phone`/`name`/null — `name` is the only match method worth distrusting, since it's the sole way a `lid:` handle links), `message_count`/`my_message_count`/`last_message_at`/`last_my_message_at` (1:1 chats only), `group_message_count` (messages this handle **sent** in groups — **not** every message in a group the handle belongs to, which is what `imessage_handles.group_message_count` means; never compare the two directly), `last_group_message_at`, `group_count` (active memberships — **not** a closeness signal; check the group's `member_count` first), `updated_at` |
| `whatsapp_chats` | PK `chat_jid`: `kind` (`direct`/`group`), `subject` (group subject, or the 1:1 partner name), `handle` (direct only), `created_at`, `member_count` (groups only — bimodal: most groups are small, a handful are large communities), `message_count`, `my_message_count`, `last_message_at`, `updated_at` |
| `whatsapp_chat_members` | the full group roster, PK `(chat_jid, handle)`: `person_id` (FK → `people.id`, `ON DELETE SET NULL`), `is_admin`, `is_active`. `handle` is deliberately **not** a foreign key to `whatsapp_handles` — most members have no row there, since membership alone isn't interaction (§4.1). A member row carries no `match_method` of its own |
| `whatsapp_messages` | PK `(chat_jid, stanza_id)`: `sender_handle` (NULL when `from_me`), `from_me`, `sent_at`, **`text`** (the only place WhatsApp message text lives — `people-api` never serves it, by design, spec §7), `message_type` (raw, never used to filter), `has_media`, `media_kind` (`image`/`video`/`audio`/`document`/`vcard`/`other`), `source_pk` (the watermark column) |
| `whatsapp_imports` | append-only audit of `scripts/import_whatsapp.py` runs: `started_at`, `finished_at`, `mode` (`incremental`/`full`), `watermark`, row counts, `senderless_dropped`, `duplicate_stanza_ids`, `sessions_skipped`, `handles_unnormalized`, `matched_by_phone`, `matched_by_name` |

`last_interaction` (`GREATEST(last_seen, last_contacted)`) is derived, not
stored — repeat the expression below rather than looking for a column.

**`people.id` is internal.** It's the join key for `linkedin_connections`
and `imessage_handles`, and what `people-api` returns as `id` and accepts in
`GET/PATCH /people/{ident}`, but it is not a durable external reference —
`people` is rebuildable from Google Contacts plus a full sync, and ids do
not survive a rebuild. Use the email address as the human-facing handle in
anything written outside this DB (a note, a doc, a message); use `id` only
for a join, a phone-only person (no email), or resolving a
`people-api` `409` ambiguous-phone response.

## Common queries

**People with a label:**
```sql
SELECT p.id, p.email, p.display_name
FROM people p JOIN people_labels pl ON pl.person_id = p.id
JOIN contact_groups g ON g.resource_name = pl.group_resource_name
WHERE lower(g.name) = lower('climbing');
```

**Recent people by last interaction:**
```sql
SELECT email, display_name, message_count, my_response_count,
       GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction
FROM people
ORDER BY last_interaction DESC
LIMIT 20
```

**Eligible people not yet in HubSpot** (candidates the nightly `fill` phase
will pick up, most recent first):
```sql
SELECT count(*) FROM people WHERE eligible AND hubspot_contact_id IS NULL;

SELECT email, display_name,
       GREATEST(COALESCE(last_seen, 'epoch'::timestamptz), COALESCE(last_contacted, 'epoch'::timestamptz)) AS last_interaction
FROM people
WHERE eligible AND hubspot_contact_id IS NULL
ORDER BY last_interaction DESC
LIMIT 20;
```

**Count of managed HubSpot contacts** (has a `hubspot_contact_id` — the set
`reconcile()`'s `enforce`/`heal` phases operate on):
```sql
SELECT count(*) FROM people WHERE hubspot_contact_id IS NOT NULL;
```

**People by phone number** (`phone_numbers` is `text[]`, E.164-normalized;
matches the GIN index):
```sql
SELECT id, email, display_name, phone_numbers
FROM people
WHERE '+15550100001' = ANY(phone_numbers);
```

**Phone-only people** (no email — `people-api` never mirrors these to
HubSpot, since `clients/hubspot.py` keys on email. Two kinds land here: a
person the nightly sync **adopted** straight from a Google contact that has
a phone but no email address, and a person `people` created some other way
that just hasn't linked to Google yet):
```sql
SELECT id, display_name, phone_numbers FROM people WHERE email IS NULL;
```

**Adopted contacts, still phone-only** (the subset of the above the nightly
sync created from Google directly — `google_resource_name IS NOT NULL` is
what distinguishes an adoption from an unlinked phone-only row; this is
also the **rollback selector** if adoption ever needs to be undone —
deleting these rows restores the prior state, per the
adopt-emailless-contacts design §6.4). **Only catches rows that haven't
promoted yet** — once a row gains an email (below), it drops out of this
`email IS NULL` filter and reads as an ordinary row; there's no flag
recording that it was ever adopted or created without one:
```sql
SELECT id, display_name, phone_numbers, first_seen
FROM people
WHERE email IS NULL AND google_resource_name IS NOT NULL
ORDER BY first_seen DESC;
```

**Duplicate check after adoption or promotion** (a phone-only row — adopted
by the nightly sync, or created by hand via `POST /people`
(`creating-person`) — whose Google contact carries an email address that
already belongs to a *different* `people` row — the piece-2 design's §5.4
query verbatim). `apply_person`'s promotion step (piece 3) only ever
*reads* this situation, never writes into it: it guards on
`repo/people.py::email_owner` and leaves a claimed address unwritten rather
than aborting the nightly sync (writing it would violate `people_email_key`),
so the two rows stand apart — this query is what finds a promotion that was
refused for exactly that reason. `POST /people` itself can't create a fresh
instance of this case — its own duplicate check (`creating-person`) already
refuses to create a second row for a claimed address — so expect **zero
rows** in steady state; see `people-architecture`'s Adoption and "Creating a
person by hand" sections:
```sql
SELECT a.id AS adopted_id, p.id AS existing_id
FROM people a
JOIN jsonb_array_elements(a.google_fields -> 'emailAddresses') e ON TRUE
JOIN people p ON lower(p.email) = lower(e ->> 'value')
WHERE a.email IS NULL AND p.id <> a.id;
```

**Birthdays this month** (`google_fields->'birthdays'` is the raw Google
People API array, e.g. `[{"date": {"month": 4, "day": 2}}]`; `jsonb_array_elements`
unnests it since a contact can carry more than one):
```sql
SELECT email, display_name, elem->'date'->>'month' AS month, elem->'date'->>'day' AS day
FROM people, jsonb_array_elements(google_fields->'birthdays') AS elem
WHERE (elem->'date'->>'month')::int = EXTRACT(MONTH FROM now())::int
ORDER BY (elem->'date'->>'day')::int;
```

**People at a given company** (`company` is one of the two typed columns
derived from `organizations`; prefer this over `google_fields` for company/
title lookups):
```sql
SELECT email, display_name, company, job_title
FROM people
WHERE company ILIKE '%example corp%';
```

**Linked to Google Contacts vs. not, among eligible people:**
```sql
SELECT
  count(*) FILTER (WHERE google_resource_name IS NOT NULL) AS linked,
  count(*) FILTER (WHERE google_resource_name IS NULL AND google_deleted_at IS NULL) AS unlinked,
  count(*) FILTER (WHERE google_deleted_at IS NOT NULL) AS deleted_in_google
FROM people WHERE eligible;
```

**Eligibility funnel:**
```sql
SELECT automated, eligible, count(*) FROM people GROUP BY automated, eligible ORDER BY count(*) DESC;
```

**Sync health:**
```sql
SELECT key, last_run_at, last_status, (sync_token IS NOT NULL) AS has_token FROM sync_state;
```

**LinkedIn connections Ben talked with and let go quiet** (join `people` for
the email — `linkedin_connections` links by `person_id`, not `person_email`):
```sql
SELECT lc.full_name, lc.company, lc.position, lc.message_count, lc.my_message_count,
       lc.last_message_at, lc.person_id, p.email AS person_email
FROM linkedin_connections lc
LEFT JOIN people p ON p.id = lc.person_id
WHERE lc.my_message_count > 0 AND lc.last_message_at < now() - interval '1 year'
ORDER BY lc.last_message_at DESC
LIMIT 50;
```

**Messages with one connection** (the expression matches the GIN index):
```sql
SELECT sent_at, from_me, sender_name, left(content, 200) AS content
FROM linkedin_messages
WHERE (recipient_profile_urls || ARRAY[sender_profile_url]) @> ARRAY['linkedin.com/in/<slug>']::text[]
ORDER BY sent_at DESC;
```

**Snapshot age and match rates:**
```sql
SELECT * FROM linkedin_imports ORDER BY id DESC LIMIT 5;
```

**iMessage handles Ben talked with and let go quiet** (join `people` for the
email — `imessage_handles` links by `person_id`, not `person_email`):
```sql
SELECT h.handle, h.display_name, h.message_count, h.my_message_count,
       h.last_message_at, h.person_id, p.email AS person_email
FROM imessage_handles h
LEFT JOIN people p ON p.id = h.person_id
WHERE h.my_message_count > 0 AND h.last_message_at < now() - interval '6 months'
ORDER BY h.last_message_at DESC
LIMIT 50;
```

**Message text with one handle** (the only way to read it — `people-api`
never serves it):
```sql
SELECT sent_at, from_me, left(text, 200) AS text
FROM imessage_messages
WHERE chat_guid IN (
  SELECT chat_guid FROM imessage_chats
  WHERE is_group = false AND '+15550100001' = ANY(participant_handles)
)
ORDER BY sent_at DESC
LIMIT 50;
```

**iMessage snapshot age:**
```sql
SELECT * FROM imessage_imports ORDER BY id DESC LIMIT 5;
```

**People with WhatsApp activity but no email address** (§8, identity design):
```sql
SELECT p.id, p.display_name, h.handle, h.message_count, h.last_message_at
FROM whatsapp_handles h
JOIN people p ON p.id = h.person_id
WHERE p.email IS NULL
ORDER BY h.last_message_at DESC NULLS LAST;
```

**Group co-membership that is actually meaningful: small groups only**
(most member rows sit in groups of 200+, where co-membership says nothing
about a relationship — spec §5.7):
```sql
SELECT c.subject, c.member_count, COUNT(*) FILTER (WHERE mem.person_id IS NOT NULL) AS known
FROM whatsapp_chats c
JOIN whatsapp_chat_members mem ON mem.chat_jid = c.chat_jid
WHERE c.kind = 'group' AND c.member_count < 50
GROUP BY c.chat_jid, c.subject, c.member_count
ORDER BY known DESC;
```

**Message text with one WhatsApp chat** (the only way to read it —
`people-api` never serves it):
```sql
SELECT sent_at, from_me, left(text, 200) AS text
FROM whatsapp_messages
WHERE chat_jid = (SELECT chat_jid FROM whatsapp_chats WHERE handle = '+15550100001')
ORDER BY sent_at DESC
LIMIT 50;
```

**WhatsApp snapshot age:**
```sql
SELECT * FROM whatsapp_imports ORDER BY id DESC LIMIT 5;
```
