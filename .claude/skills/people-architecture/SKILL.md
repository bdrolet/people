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
counters, eligibility, or HubSpot ranking). Same story for WhatsApp: only the
local `scripts/import_whatsapp.py` reads its local store, and it is not a
source of truth for any `people` field either.

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

scripts/import_whatsapp.py (local, manual, no Full Disk Access needed) ──ChatStorage.sqlite──▶ whatsapp_* tables (incremental upsert)

inbox-process, Claude Code skills ──Google ID token (Cloud Run IAM)──▶ people-api (Cloud Run)
                                                        GET/PATCH /people/{ident}, POST /people, POST /people/{ident}/sync, POST /search, GET /people,
                                                        GET /labels, GET /labels/{name},
                                                        GET /linkedin/connections[/{slug}], GET /linkedin/imports/latest,
                                                        GET /imessage/handles[/{handle}], GET /imessage/imports/latest,
                                                        GET /whatsapp/handles[/{handle}], GET /whatsapp/chats, GET /whatsapp/imports/latest
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
last_contacted, message_count, my_response_count, notes,
eligible, automated, google_resource_name, google_etag, google_deleted_at,
hubspot_contact_id, hubspot_synced_at, phone_numbers, company, job_title,
google_fields, updated_at; `CHECK` constraint `people_has_an_identifier`:
`email IS NOT NULL OR cardinality(phone_numbers) > 0`) and `sync_state`
(key/sync_token/last_run_at/last_status — one row, `key='google_contacts'`),
plus `contact_groups` (user-defined labels, replaced every sync) and
`people_labels` (person ↔ label membership). `people.relationship_label` is
unused, pending removal; `PersonOut.relationship_label` is a transitional
read-only copy (first label, lowercased) kept for inbox until it reads `labels`.

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

WhatsApp snapshot tables (five, no cloud component, no Graph/HubSpot/Google
involvement) — `whatsapp_handles` (PK `handle`, E.164 or `lid:<id>` for a
Linked ID that carries no phone number, soft link `person_id` →
`people.id` FK `ON DELETE SET NULL`, per-handle 1:1 and group stats — note
`group_message_count` here counts messages the handle **sent** in groups,
unlike `imessage_handles.group_message_count`, which counts every message in
a group the handle belongs to because `chat.db` cannot attribute group
senders), `whatsapp_chats`, `whatsapp_chat_members` (the full group roster —
`handle` is deliberately not a foreign key to `whatsapp_handles`, since most
members have no row there), `whatsapp_messages` (text lives only here —
never served by `people-api`) — are incrementally upserted by each manual
run of `scripts/import_whatsapp.py`; `whatsapp_imports` is its append-only
audit. Nothing flows from WhatsApp to Google Contacts, HubSpot, or
`people`'s counters/eligibility. A `match_method` of `name` is the one link
worth distrusting, since it's the only way a `lid:` handle ever links (no
phone number to match on).

The `imessage_*` and `whatsapp_*` child tables link by `person_id` (a
foreign key to `people.id`); the API still serves `person_email` alongside
it by joining `people`, so `searching-people`'s read shape is unchanged. See
`querying-people-db` for the join.

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
would burn the bounded cap trying to create contacts it can't key. This is
why an **adopted** contact (below) can never appear in HubSpot: it exists
*because* it has no email.

This is piece 1 of a 3-piece design (`docs/superpowers/specs/2026-09-24-person-identity-design.md`):
identity only, no new rows. Piece 2 (`docs/superpowers/specs/2026-09-28-adopt-emailless-contacts-design.md`,
shipped) is **Adoption**, below. Piece 3
(`docs/superpowers/specs/2026-09-28-post-people-design.md`, shipped) is
**`POST /people`**, below Adoption — creating a person by hand, and the
email-promotion gap that adoption alone left behind.

### Adoption (piece 2, shipped 2026-09-28)

The nightly sync no longer skips a Google contact that has no email
address. `apply_person` **adopts** it instead, provided it has at least one
phone number that normalizes to E.164 — the same rule
`services/imessage_export.py::normalize_handle` uses for iMessage matching
(`services/contact_fields.py::derive`'s `phone_numbers` list is what gets
checked). The row lands via `people.create_from_google(conn, None, ...)`:
`email IS NULL`, `phone_numbers` populated, `company`/`job_title`/
`google_fields` derived as for any synced contact, `eligible = TRUE`,
`automated = FALSE`, and the message counters (`message_count`,
`my_response_count`) at their default `0` — no mail has ever been seen from
these people.

A contact with **neither** an email nor a usable phone (a short code does
not count as usable) is **skipped** — nothing is written, and it's counted
in `run_sync`'s `skipped` total, which flows into the nightly log line and
the `POST /sync` response body. Names are deliberately not logged or
persisted anywhere (the repo is public); the count is the whole report, and
it should trend to zero as Ben fixes the contacts in Google.

Measured 2026-09-28, before adoption shipped: 968 Google contacts, 553 with
no email, 449 adoptable, 104 skipped (87 with neither identifier, 17 with a
phone that doesn't parse).

Once adopted, a contact is found by `google_resource_name` on every later
sync (`people.get_by_google_resource`) and refreshes exactly like any other
linked contact — the "linked" `apply_person` branch calls
`people.update_from_google`, which always updates `phone_numbers`/
`company`/`job_title`/`google_fields`. **As of piece 3 below, it also
promotes `email`:** if the Google contact has since gained an address (e.g.
via `PATCH /people/{ident}`'s `contact.emailAddresses`, see
`editing-person`) and no *other* `people` row already holds it
(`repo/people.py::email_owner` guards this), that address is written onto
this row, moving it off `email IS NULL` for good. If the address *is*
already claimed by a different row, promotion is skipped — writing it would
violate the `people_email_key` unique constraint and abort the whole
sync — and the row stays `email IS NULL`, keyed by phone/id. That claimed
case is exactly what `querying-people-db`'s duplicate-detection query
watches for (piece-2 design §5.4): it finds an adopted row whose Google
contact carries an email that already belongs to a different `people` row.
Nothing merges them automatically — see "Creating a person by hand" below
for the mechanism (`apply_person`'s promotion step) in full.

Adoption is otherwise automatic and incremental — a contact added by phone
tomorrow adopts on the next nightly `people-sync` — but the incremental
Google sync token means a run only revisits contacts that changed since the
last one, so reaching the backlog of 553 already-existing contacts needed
one full pass after this shipped: `scripts/clear_sync_token.py` (local
only) clears the stored token in `sync_state` and prints what it cleared,
so the next `run_sync` takes the full-listing path instead of an
incremental one.

### Creating a person by hand (piece 3, shipped 2026-09-28)

`POST /people` (`services/person_create.py`) creates a Google Contact and
its `people` row together, from a `contact` map of Google People API field
names plus the same top-level `notes`/`labels` fields `PATCH`
uses (`labels` is a list of names, validated before the Google contact is
created — a bad one is a `400`/`409` and creates nothing). It reuses the sync's own row-creation path (`apply_person`), so a
hand-created person is indistinguishable from one the sync adopted —
including derived `phone_numbers`/`company`/`job_title`/`google_fields`.
Validation runs before any write: the `contact_fields` allowlist, then at
least one identifier (an email, or a phone that normalizes to E.164 — a
short code doesn't count), else `400`. A duplicate — matched by email or
phone on either the `people` side or the Google side — is refused with
`409 {"error": "person exists", "candidates": [<id>, ...]}` rather than
creating a second row; `candidates` is empty when the only match is a
Google contact the sync hasn't adopted yet, since there's no `people.id` to
give — run a sync, then `PATCH`. See **creating-person** for the
caller-facing detail.

This piece also closes the email-promotion gap Adoption left open: an
adopted (email-less) row's `email` column was write-once (creation/link
only) even after its Google contact gained an address. `apply_person`'s
linked branch now promotes — writes that address onto the row — the first
time it sees one, but only when the address isn't already held by a
*different* `people` row (`repo/people.py::email_owner` guards this;
writing a claimed address would violate `people_email_key` and abort the
entire nightly sync, so the claimed case is left alone and both rows
stand — `querying-people-db`'s duplicate-detection query is what finds
them). `run_sync`'s counts gain a `promoted` kind for this.

**Promotion makes a person mirrorable to HubSpot for the first time.**
`eligible_not_in_hubspot` excludes rows with no email (see "Identity"
above), so a newly-promoted row becomes a candidate for the next
`fill` phase, subject to the existing cap and eviction rules — a new,
if currently rare, way for the mirror's population to grow.

Measured 2026-09-28: 984 people, 449 of them adopted (email-less), 34 phone
numbers already shared across more than one row from duplicate Google
contacts adopted separately before this endpoint existed.

## Source of truth (spec §4.3)

| Field | Truth | Direction |
|---|---|---|
| `display_name` | Google Contacts once linked; event data before that | Google → DB on sync/link. Event data never overwrites a Google-sourced name. |
| `notes` | Google contact **biography** | Both ways: `PATCH /people/{ident}` writes Google first, then refreshes the DB row from it. The event path never writes notes. |
| `labels` (`contact_groups`, `people_labels`) | Google **contact groups** (user-defined; system groups and `GOOGLE_CONTACT_GROUP` excluded) | Google → DB — `contact_groups` replaced from `contactGroups.list` on every sync; a person's memberships rewritten on every apply. `PATCH` adds/removes membership (`{"labels": {"add", "remove"}}`) without touching other labels. |
| counters, timestamps, `eligible`, `automated` | DB | Written only by the event handlers and `scripts/import_contacts.py`. |
| `google_deleted_at` | Google | Set by sync when a linked `resourceName` comes back deleted. Never recreated. |
| `hubspot_contact_id` | DB (people manages) | Set on create/adopt, cleared on evict/heal. |
| `phone_numbers`, `company`, `job_title`, `google_fields` | Google Contacts | Google → DB on link, nightly sync, and after every `PATCH`. Event data never writes them; never pushed to HubSpot (contact-field-edits design §4.1). |
| `whatsapp_*` tables | WhatsApp's local store on Ben's Mac | Store → DB on local import (`scripts/import_whatsapp.py`). Never written back anywhere; WhatsApp is not a source for any `people` field. |

If the DB is lost, everything except the counters rebuilds from Google
Contacts plus a full sync; counters rebuild via `scripts/import_contacts.py`.

**Editing beyond `notes`/`labels`:** `PATCH /people/{ident}`
also takes a `contact` map — arbitrary Google People API fields (phone
numbers, name, organization, birthday, addresses, ...), validated against an
allowlist in `services/contact_fields.py` and written to Google in one
`updateContact` call. `biographies` and `memberships` are rejected inside
`contact` since `notes`/`labels` already own them. Email
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
  email; see "Identity" above. Unless and until one gains an address via
  promotion (see "Creating a person by hand" above), at which point it
  becomes a `fill` candidate like any other eligible row.
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
`google_contacts_sync`, `hubspot_mirror`, `person_edit`, `person_create`
(`POST /people`: validate, refuse duplicates, create in Google, let
`google_contacts_sync.apply_person` create the row), `contact_fields`
(pure — allowlist validation, email add-only rule, `identifiers()` for
duplicate checking, Google-payload derivation), `identity` (pure — classify
`{ident}` as email/phone/id, spec §5.1), `sync_auth`) ·
`handlers/` orchestrate clients + repo + services, called only from
`main.py` (`api/routers/` play the same role for `people-api`, called only
from `api/main.py`) · `models/` pure types, no imports from other layers ·
`main.py` — CF entry points only, always `otel.flush()` in `finally`.
