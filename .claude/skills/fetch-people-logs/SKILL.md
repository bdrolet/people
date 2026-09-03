---
name: fetch-people-logs
description: Use when the user wants to fetch, read, tail, or inspect logs from the people-process or people-sync Cloud Functions or the people-api Cloud Run service, check for errors after a deploy, debug a missed event, or investigate why a person wasn't created in Google Contacts or HubSpot.
---

# Fetching People Logs

**Project:** `bens-project-462804` | **Region:** `us-central1` | **Functions:** `people-process`, `people-sync` | **Cloud Run:** `people-api`

## Cloud Functions

```bash
gcloud functions logs read people-process --project=bens-project-462804 --region=us-central1 --limit=50
gcloud functions logs read people-sync --project=bens-project-462804 --region=us-central1 --limit=50
```

**Errors only** (swap the function name):
```bash
gcloud logging read \
  'resource.type="cloud_function" resource.labels.function_name="people-process" severity>=ERROR' \
  --project bens-project-462804 --limit 50 \
  --format='table(timestamp, severity, textPayload)'
```

**Free-text search:**
```bash
gcloud logging read \
  'resource.type="cloud_function" resource.labels.function_name="people-process" textPayload:"<keyword>"' \
  --project bens-project-462804 --limit 50 \
  --format='table(timestamp, textPayload)'
```

## Cloud Run (`people-api`)

```bash
gcloud run services logs read people-api --project=bens-project-462804 --region=us-central1 --limit=50
```

## What to look for

| Pattern | Meaning |
|---|---|
| `email_classified <message_id> from <email> — eligible=True newly=True` | new eligible person — Google Contact + HubSpot create should follow |
| `email_sent <graph_message_id> → N recipients` | outbound counters updated |
| `Google ensure_contact failed for <email>` | Google API call failed — swallowed, counted as `people_external_errors{system="google"}` |
| `HubSpot ensure_contact failed for <email>` / `HubSpot log_email failed for <email>` | HubSpot call failed — swallowed, counted as `people_external_errors{system="hubspot"}` |
| `HubSpot cap reached by unmanaged contacts — not creating` | cap enforcement blocked on unmanaged contacts — needs a manual look at HubSpot, not a bug |
| `Evicted <email> from HubSpot (last interaction ...)` | eviction ran (event-time or nightly reconcile) |
| `Google sync token expired — full resync` | `people-sync` fell back to a full list (HTTP 410) — expected occasionally |
| `sync complete google=... hubspot=...` | `people-sync` finished; the dict shows counts per phase (`updated`/`linked`/`created`/`deleted` for Google, `adopted`/`healed`/`evicted`/`filled` for HubSpot) |
| Any `ERROR` or unhandled traceback | investigate — an exception in `process` re-raises so Pub/Sub redelivers |

## Retention

Logs persist for 30 days in the `_Default` bucket. For older history, query
the `people` table instead (`querying-people-db`) — `updated_at` and
`hubspot_synced_at` tell you when a row last changed.
