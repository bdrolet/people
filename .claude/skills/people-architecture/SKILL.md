---
name: people-architecture
description: Use when the user asks how the people service works, how it fits with inbox/HubSpot/Google Contacts, what GCP resources exist, how the Cloud Functions or people-api are triggered, or how any component of the people pipeline fits together.
---

# People: Architecture Reference

## Dividing rule

Inbox owns mail: receiving, classifying, tagging, and the `email_classified` /
`email_sent` feeds. **People owns everyone Ben corresponds with**: the
canonical Google Contact, the derived `people` index, the HubSpot mirror, and
`people-api`. Inbox never talks to Google Contacts or HubSpot again. People's
Cloud Functions never talk to Microsoft Graph — only the local
`scripts/import_contacts.py` does. Likewise, people's Cloud Functions never
read `chat.db` — only the local `scripts/import_imessage.py` does, and
iMessage is not a source of truth for any `people` field (it doesn't affect
counters, eligibility, or HubSpot ranking).

## Event flow

Inbox classifies every email and publishes `email_classified` (and, for mail
Ben sends, `email_sent`) to its `email-events` Pub/Sub topic. People's own
subscription on that topic drives `people-process`; a nightly HTTP call drives
`people-sync`.

```
inbox-process CF ──publish email_classified/email_sent──▶ email-events (Pub/Sub, INBOX-owned)
                                                                  │ people's own subscription
                                                                  ▼
                                                        people-process CF (main.py process)
                                                        ingest → counters + eligibility (durable)
                                                                  │ eligible?
                                                        ┌─────────┴─────────┐
                                                        ▼                   ▼
                                              Google Contacts (create/link)   HubSpot (bounded mirror)
                                                                  │
                                                                  ▼
                                                     Cloud SQL db `people` (people, sync_state, linkedin_*)

Cloud Scheduler people-sync (4 AM ET, before inbox's 5 AM sweep)
        ──POST /sync (Bearer people-sync-token)──▶ people-sync CF (main.py sync)
                                                        Google Contacts incremental sync (sync token)
                                                        → HubSpot reconcile: adopt / heal / enforce / fill

scripts/import_linkedin.py (local, manual) ──LinkedIn data export──▶ linkedin_* tables (snapshot, replaced per import)

scripts/import_imessage.py (local, manual, needs Full Disk Access) ──chat.db──▶ imessage_* tables (incremental upsert)

inbox-process, Claude Code skills ──Google ID token (Cloud Run IAM)──▶ people-api (Cloud Run)
                                                        GET/PATCH /people/{ident}, POST /people/{ident}/sync, POST /search, GET /people,
                                                        GET /linkedin/connections[/{slug}], GET /linkedin/imports/latest,
                                                        GET /imessage/handles[/{handle}], GET /imessage/imports/latest
```

Full design: `docs/superpowers/specs/2026-09-03-people-service-extraction-design.md`;
identity model: `docs/superpowers/specs/2026-09-24-person-identity-design.md`.

## GCP resources (all in `terraform/`, state prefix `people`)

| Resource | Name | Notes |
|---|---|---|
| Pub/Sub topic | `email-events` | **owned by INBOX terraform** (producer owns); data source here |
| CF gen2 | `people-process` | Pub/Sub trigger on `email-events`, entry point `process`, repo-root source |
| CF gen2 | `people-sync` | HTTP, entry point `sync`; public invoker + app-level bearer auth (`people-sync-token`) — Cloud Scheduler `people-sync` at `0 4 * * *` America/New_York |
| Cloud Run | `people-api` | FastAPI (`api/`), Cloud Run IAM (`roles/run.invoker` per caller, see terraform/api.tf); image in Artifact Registry repo `people`; deployed by `.github/workflows/deploy-api.yml` |
| Cloud SQL | database `people`, user `people` on instance `inbox` (`bens-project-462804:us-central1:inbox`) | instance owned by inbox terraform; tables `people`, `sync_state` |
| GCS | `bens-project-462804-people-cf-source` | CF source zip |
| SA | `people-process-cf@`, `people-sync-cf@`, `people-api@` | secretAccessor on owned + shared secrets, `cloudsql.client` |

Shared Secret Manager secrets (`grafana-otlp-endpoint`, `grafana-otlp-token`,
`google-calendar-client-id`, `google-calendar-client-secret`) are data
sources — owned elsewhere (platform state / schedule). `hubspot-token`,
`google-contacts-refresh-token`, `people-db-password`,
`people-sync-token` are owned here. See `adding-people-secret` for the wiring
checklist.

No custom domain is mapped for `people-api` yet — call it via
`terraform output -raw people_api_url` (a `run.app` URL) until one is added.

## Database

`people` DB on `bens-project-462804:us-central1:inbox` (Postgres 16 +
pg_trgm). Tables: `people` (`id` BIGSERIAL **PK**, `email` nullable
**unique** — no longer the PK, display_name, first_seen, last_seen,
last_contacted, message_count, my_response_count, relationship_label, notes,
eligible, automated, google_resource_name, google_etag, google_deleted_at,
hubspot_contact_id, hubspot_synced_at, phone_numbers, company, job_title,
google_fields, updated_at; `CHECK` constraint `people_has_an_identifier`:
`email IS NOT NULL OR cardinality(phone_numbers) > 0`) and `sync_state`
(key/sync_token/last_run_at/last_status — one row, `key='google_contacts'`).

`phone_numbers` (text[], E.164), `company`, and `job_title` are a derived
index over `google_fields` (JSONB, the full allowlisted Google contact
payload) — not a separate source. All four are Google-owned: written on
link, nightly sync, and after every `PATCH /people/{ident}` edit to a
`contact` field (contact-field-edits design §4). See `querying-people-db`
for JSONB query examples.

LinkedIn snapshot tables — `linkedin_connections` (PK `profile_url`, soft link
`person_id` → `people.id` FK `ON DELETE SET NULL`, per-connection
message stats), `linkedin_messages`, `linkedin_recommendations` — are fully
replaced by each manual run of `scripts/import_linkedin.py`;
`linkedin_imports` is its append-only audit. Nothing flows from LinkedIn to
Google Contacts or HubSpot.

iMessage snapshot tables — `imessage_handles` (PK `handle`, soft link
`person_id` → `people.id` FK `ON DELETE SET NULL`, per-handle 1:1 and
group message stats), `imessage_chats`, `imessage_messages` (text lives only
here — never served by `people-api`) — are incrementally upserted by each
manual run of `scripts/import_imessage.py`; `imessage_imports` is its
append-only audit. Nothing flows from iMessage to Google Contacts, HubSpot,
or `people`'s counters/eligibility.

Both child tables link by `person_id` (a foreign key to `people.id`); the
API still serves `person_email` alongside it by joining `people`, so
`searching-people`'s read shape is unchanged. See `querying-people-db` for
the join.

`last_interaction` is derived as `GREATEST(last_seen, last_contacted)`, not
stored. Schema: `repo/schema.sql`, applied via `scripts/migrate_db.py`. Prod
connects through the Cloud SQL Python Connector with pg8000
(`CLOUD_SQL_CONNECTION_NAME` set); local uses direct psycopg3
(`POSTGRES_HOST`). See `querying-people-db`.

## Identity: `id`, `email`, and phone (2026-09-24 design)

`people.id` (`BIGSERIAL`) is the primary key; `email` is nullable and
unique (`phone_numbers`, `text[]`, already existed). A person is addressed
by one route, `GET/PATCH /people/{ident}` and `POST /people/{ident}/sync`,
where `{ident}` is classified by `services/identity.py::classify` in this
order: contains `@` → email; parses as a phone (via
`services.imessage_export.normalize_handle`, E.164, rejects anything under
7 digits) → phone; all digits → id; otherwise → 404. A `+` in a phone must
be percent-encoded in the path (`%2B15550100001`), matching the existing
iMessage handle routes. A phone shared by more than one person (a household
landline) is ambiguity, not a guess: it 409s with the candidate ids
(`{"error": "ambiguous phone", "candidates": [...]}`) and the caller retries
with one of them. Responses carry `id`; `email` may be `null`.
`imessage_handles` and `linkedin_connections` link by `person_id` (see
Database above); the API still serves `person_email` via a join, so reading
skills don't change.

**Ids are internal.** A person id is never a durable external reference —
`people` is rebuildable from Google Contacts plus a full sync, and ids don't
survive that. The email address remains the human-facing handle;
`querying-people-db` and the person skills use `id` only for a join, a
phone-only person, or resolving a 409.

**Phone-only people can't reach HubSpot.** `clients/hubspot.py` searches and
creates contacts by email, so `repo/people.py::eligible_not_in_hubspot`
excludes rows with `email IS NULL` — otherwise the nightly `fill` phase
would burn the bounded cap trying to create contacts it can't key.

This is piece 1 of a 3-piece design (`docs/superpowers/specs/2026-09-24-person-identity-design.md`):
identity only, no new rows. The nightly sync **still skips every Google
contact with no email address** (`services/google_contacts_sync.py::apply_person`)
— adopting those (553 of them, per the design doc) is piece 2, not yet
shipped; `POST /people` (piece 3) doesn't exist either.

## Source of truth (spec §4.3)

| Field | Truth | Direction |
|---|---|---|
| `display_name` | Google Contacts once linked; event data before that | Google → DB on sync/link. Event data never overwrites a Google-sourced name. |
| `notes` | Google contact **biography** | Both ways: `PATCH /people/{ident}` writes Google first, then refreshes the DB row from it. The event path never writes notes. |
| `relationship_label` | Google **contact group** membership | Google → DB — the first non-system, non-`GOOGLE_CONTACT_GROUP` group, lowercased. `PATCH` writes by moving group membership. |
| counters, timestamps, `eligible`, `automated` | DB | Written only by the event handlers and `scripts/import_contacts.py`. |
| `google_deleted_at` | Google | Set by sync when a linked `resourceName` comes back deleted. Never recreated. |
| `hubspot_contact_id` | DB (people manages) | Set on create/adopt, cleared on evict/heal. |
| `phone_numbers`, `company`, `job_title`, `google_fields` | Google Contacts | Google → DB on link, nightly sync, and after every `PATCH`. Event data never writes them; never pushed to HubSpot (contact-field-edits design §4.1). |

If the DB is lost, everything except the counters rebuilds from Google
Contacts plus a full sync; counters rebuild via `scripts/import_contacts.py`.

**Editing beyond `notes`/`relationship_label`:** `PATCH /people/{ident}`
also takes a `contact` map — arbitrary Google People API fields (phone
numbers, name, organization, birthday, addresses, ...), validated against an
allowlist in `services/contact_fields.py` and written to Google in one
`updateContact` call. `biographies` and `memberships` are rejected inside
`contact` since `notes`/`relationship_label` already own them. Email
addresses are add-only — a submission that would drop an existing or keyed
address gets a `409`. See **editing-person** for the caller-facing detail.

**Read mask widened, one resync expected.** Reading these fields back
required widening `PERSON_FIELDS` (the People API read mask) in
`clients/google_contacts.py`. `connections.list` treats a changed
`personFields` as incompatible with an existing sync token, so **the first
nightly `people-sync` run after this shipped performed one full resync**
instead of an incremental one — expected, self-healing
(`_is_expired_sync_token`), not a fault. If a full resync shows up in the
logs again unexpectedly, check whether `PERSON_FIELDS` changed.

## Eligibility (spec §5)

```
automated  = address matches AUTOMATED_SENDER_PATTERN (env override; default
             catches no-reply/noreply/do-not-reply/mailer-daemon/
             notifications@/alerts@/support@/newsletter@)
             or its domain is in AUTOMATED_SENDER_DOMAINS
             or address is in OWN_ADDRESSES
eligible   = not automated
             and (an inbound email with category != "ignore"
                  or Ben sent this person an email)
```

`services/eligibility.py`. Eligibility is monotonic — once true it stays
true, re-evaluated on every event. Only **To** and **Cc** recipients of sent
mail are counted; Bcc is excluded (`services/ingest.py::record_outbound`).

## HubSpot as a bounded mirror (spec §8.1)

- `HUBSPOT_MAX_CONTACTS` (default `1000`) is a **hard** cap — people evicts
  before it creates (`services/hubspot_mirror.py`).
- **Managed** = has a `hubspot_contact_id` in `people`. Unmanaged HubSpot
  contacts count toward the cap but are **never** archived by people.
- Eviction victim is the managed contact with the oldest `last_interaction`.
- Only eligible people are mirrored; losing eligibility never happens, so a
  contact leaves HubSpot only by eviction.
- A phone-only person (no email) is never mirrored — HubSpot is keyed on
  email; see "Identity" above.
- Engagements (`log_email`) are only created for contacts currently in
  HubSpot; an evicted contact's history restarts if it comes back.
- All writes gated by `HUBSPOT_WRITES_ENABLED` (default `false` — see
  "Phase C" in `CLAUDE.md`). **Google Contacts writes are not gated** — an
  eligible person always gets a real Google Contact, cap or no cap.
- Nightly `people-sync` runs `reconcile()`: adopt (unmanaged HubSpot contact
  matching an eligible row), heal (managed row whose HubSpot contact is
  gone), enforce (evict down to cap), fill (create up to cap from the most
  recently interacted-with eligible people not yet mirrored).

## Layer rules

`clients/` I/O only (Google Contacts, HubSpot, Cloud SQL, OTel, local Graph
import) · `repo/` DB read/write on an open connection, never opens its own ·
`services/` one concern per file (`eligibility`, `ingest`,
`google_contacts_sync`, `hubspot_mirror`, `person_edit`, `contact_fields`
(pure — allowlist validation, email add-only rule, Google-payload
derivation), `identity` (pure — classify `{ident}` as email/phone/id,
spec §5.1), `sync_auth`) ·
`handlers/` orchestrate clients + repo + services, called only from
`main.py` (`api/routers/` play the same role for `people-api`, called only
from `api/main.py`) · `models/` pure types, no imports from other layers ·
`main.py` — CF entry points only, always `otel.flush()` in `finally`.
