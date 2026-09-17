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
| `people` | `email` (PK), `display_name`, `first_seen`, `last_seen`, `last_contacted`, `message_count`, `my_response_count`, `relationship_label`, `notes`, `eligible`, `automated`, `google_resource_name`, `google_etag`, `google_deleted_at`, `hubspot_contact_id`, `hubspot_synced_at`, `updated_at` |
| `sync_state` | one row, `key='google_contacts'`: `sync_token`, `last_run_at`, `last_status` |
| `linkedin_connections` | LinkedIn snapshot, PK `profile_url` (`linkedin.com/in/<slug>`): `full_name`, `email`, `company`, `position`, `connected_on`, `person_email` (soft link to `people.email`), `match_method`, `message_count`, `my_message_count`, `last_message_at`, `last_my_message_at`, `snapshot_at` |
| `linkedin_messages` | `conversation_id`, `sender_name`, `sender_profile_url`, `recipient_names`, `recipient_profile_urls` (text[]), `sent_at`, `subject`, `content`, `folder`, `from_me` |
| `linkedin_recommendations` | `direction` (`given`/`received`), `full_name`, `company`, `job_title`, `text`, `status`, `created_on`, `profile_url` (null unless the name matched one connection) |
| `linkedin_imports` | append-only audit of `scripts/import_linkedin.py` runs: `snapshot_at`, `source`, row counts, `matched_by_email`, `matched_by_name` |

`last_interaction` (`GREATEST(last_seen, last_contacted)`) is derived, not
stored — repeat the expression below rather than looking for a column.

## Common queries

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

**LinkedIn connections Ben talked with and let go quiet:**
```sql
SELECT full_name, company, position, message_count, my_message_count, last_message_at, person_email
FROM linkedin_connections
WHERE my_message_count > 0 AND last_message_at < now() - interval '1 year'
ORDER BY last_message_at DESC
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
