# People Service Extraction — Design

**Date:** 2026-09-03
**Status:** Approved design, awaiting implementation plan
**Repos:** `bdrolet/inbox` (this repo, producer side) and a new **public** repo `bdrolet/people` at `~/src/people`

## 1. Summary

Inbox currently does two things with the people behind its mail: it upserts every
sender into HubSpot and logs each inbound email as a HubSpot engagement
(`clients/hubspot.py`, called from `handlers/pipeline.py`), and it keeps a
`senders` table of per-sender message counts that feeds the classification
prompt (`repo/senders.py`, `services/classification.py::build_prompt`). A
one-off `scripts/import_contacts.py` bulk-loads a year of correspondents into
HubSpot.

This design moves all of it into a new `people` service, following the same
extraction pattern as `tasks` (Asana) and `schedule` (Google Calendar):

- **Google Contacts is the source of truth** for who a person is (name, notes,
  relationship label).
- A **`people` Postgres database** on the shared Cloud SQL instance is a
  rebuildable, derived index: fast email lookup plus per-person counters that
  Google Contacts has no fields for.
- **HubSpot stays, as a bounded mirror**: at most `HUBSPOT_MAX_CONTACTS`
  (default 1000) contacts, always the most recently interacted-with.
- A **`people-api`** Cloud Run service serves person lookups to inbox (sender
  context at classify time), to Claude Code skills, and to future consumers.
- Inbox gains a **Sent Items** Graph subscription and publishes a new
  `email_sent` event so people can count Ben's replies — the field
  (`my_response_count`) the prompt has read since v1 but nothing has ever
  written.

**Dividing rule.** Inbox owns mail: receiving, classifying, tagging, and the
`email_classified` / `email_sent` feeds. People owns everyone Ben corresponds
with: the canonical Google Contact, the derived index, the HubSpot mirror, and
`people-api`. Inbox never talks to Google Contacts or HubSpot again. People's
Cloud Functions never talk to Microsoft Graph (only the local import script
does).

## 2. Goals and non-goals

**Goals**

1. Remove every HubSpot and sender-tracking concern from inbox.
2. Populate `my_response_count` and `relationship_label` for real, so sender
   context in the classification prompt becomes meaningful.
3. Give Ben one place (Google Contacts) to curate people, and have that
   curation flow back into classification and HubSpot automatically.
4. Keep HubSpot under its contact cap without manual pruning.
5. Expose people data over an authenticated HTTP API with the usual
   searching/fetching/editing skills.

**Non-goals (explicitly deferred)**

- People Data Labs role/company enrichment and Apify LinkedIn lookups sketched
  in the global `adding-referral-contact` skill. The spec leaves room (a
  `people` row per person, a Google Contact to enrich) but designs none of it.
- HubSpot deals / pipeline stages.
- Cross-channel identity (SMS, voicemail). Rows are keyed by email address.
- Any change to inbox's classification categories, tagging, or sweep.

## 3. Components

| | |
|---|---|
| **GCP project** | `bens-project-462804`, `us-central1` |
| **Events CF** | `people-process` — Pub/Sub trigger on the inbox-owned `email-events` topic (data source); entry point `process` in `main.py`; handles `email_classified` and `email_sent`, ignores others |
| **Sync CF** | `people-sync` — HTTP trigger, entry point `sync` in `main.py` (same source zip); Cloud Scheduler `people-sync` at `0 4 * * *` America/New_York (before inbox's 5 AM sweep); bearer auth via `people-sync-token` |
| **API** | `people-api` — Cloud Run FastAPI service (`api/`); bearer auth via `people-api-token`; image in Artifact Registry repo `people`; `people-api.drolet.cloud` |
| **Database** | `people` DB + `people` user on Cloud SQL instance `inbox` (`bens-project-462804:us-central1:inbox`, data source — instance owned by inbox terraform); tables `people`, `sync_state`; schema in `repo/schema.sql` |
| **Google Contacts** | People API v1 via `clients/google_contacts.py` — OAuth refresh-token creds, scope `https://www.googleapis.com/auth/contacts`; same Google account and OAuth client as schedule |
| **HubSpot** | `clients/hubspot.py` (ported from inbox) — contacts search/create/update/archive, email engagement create |
| **Observability** | OTel → Grafana Cloud OTLP; metrics prefixed `people_` |
| **Infra** | `terraform/` — GCS backend `bens-project-462804-tf-state`, state prefix `people` |

Repo is **public**. Nothing personal is committed: the HubSpot owner id (today
hardcoded as `"93744502"` in inbox's client), the automated-sender skip
domains, the own-address list, the cap, and every token are env vars or
secrets. `context/` (if any) is gitignored like tasks.

Layer rules are the standard ones: `clients/` I/O only; `repo/` DB read/write
taking an open connection; `services/` one concern per file; `handlers/`
orchestrate; `models/` pure types; `main.py` CF entry points only, always
`otel.flush()` in `finally`; `api/routers/` thin transport.

## 4. Data model

### 4.1 `people` table

```sql
CREATE TABLE IF NOT EXISTS people (
    email                 TEXT PRIMARY KEY,          -- lowercased, trimmed
    display_name          TEXT,                      -- best-known name (Google wins over event data)
    first_seen            TIMESTAMPTZ NOT NULL,
    last_seen             TIMESTAMPTZ,               -- last inbound email from this person
    last_contacted        TIMESTAMPTZ,               -- last email Ben sent to this person
    message_count         INT  NOT NULL DEFAULT 0,   -- inbound emails
    my_response_count     INT  NOT NULL DEFAULT 0,   -- emails Ben sent them
    relationship_label    TEXT,                      -- derived from Google contact group membership
    notes                 TEXT,                      -- derived from Google contact biography
    eligible              BOOLEAN NOT NULL DEFAULT FALSE,
    automated             BOOLEAN NOT NULL DEFAULT FALSE,  -- matched the automated-sender filter
    google_resource_name  TEXT UNIQUE,               -- people/c123…
    google_etag           TEXT,
    google_deleted_at     TIMESTAMPTZ,               -- set when Ben deletes the contact in Google; never recreate
    hubspot_contact_id    TEXT UNIQUE,
    hubspot_synced_at     TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS people_last_interaction_idx
    ON people (GREATEST(COALESCE(last_seen, 'epoch'), COALESCE(last_contacted, 'epoch')) DESC);
CREATE INDEX IF NOT EXISTS people_display_name_trgm_idx ON people USING gin (display_name gin_trgm_ops);
```

`last_interaction` is a derived value, `GREATEST(last_seen, last_contacted)`,
used for HubSpot ranking and the "recent" listing. `pg_trgm` is enabled in the
`people` database for name search.

### 4.2 `sync_state` table

```sql
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,   -- 'google_contacts'
    sync_token  TEXT,
    last_run_at TIMESTAMPTZ,
    last_status TEXT
);
```

### 4.3 Source-of-truth rules

| Field | Truth | Direction |
|---|---|---|
| `display_name` | Google Contacts once a contact exists; event data before that | Google → DB on sync. Event data never overwrites a name that came from Google. |
| `notes` | Google contact **biography** | Both: `PATCH /people/{email}` writes to Google, then sync (or the same request) updates DB. Event path never writes notes. |
| `relationship_label` | Google **contact group** membership | Google → DB. The first non-system group the contact belongs to, by name, lowercased. `PATCH` writes by adding the contact to the group named by the label (creating the group if absent) and removing it from other user groups people manages. |
| counters, timestamps, eligibility, automated | DB | Written only by the event path and the import script. |
| `google_deleted_at` | Google | Set by sync when a previously linked `resourceName` comes back deleted. **Never recreate** a contact once this is set. |
| `hubspot_contact_id` | DB (people manages) | Set on create, cleared on evict. |

If the DB is lost, everything except the counters is rebuildable from Google
Contacts plus a full sync; counters rebuild via the import script.

## 5. Eligibility

Every sender and recipient people sees gets a row and counters. **Eligibility**
decides whether a person deserves a Google Contact (and therefore a HubSpot
slot):

```
automated  = address matches AUTOMATED_SENDER_PATTERN
             or its domain is in AUTOMATED_SENDER_DOMAINS
             or address is one of OWN_ADDRESSES
eligible   = not automated
             and (saw an inbound email with category != "ignore"
                  or Ben sent this person an email)
```

- `AUTOMATED_SENDER_PATTERN` default is the regex from inbox's import script
  (`no-reply`, `noreply`, `do-not-reply`, `mailer-daemon`, `notifications@`,
  `alerts@`, `support@`, `newsletter@`, case-insensitive). Overridable by env.
- `AUTOMATED_SENDER_DOMAINS` and `OWN_ADDRESSES` are env vars (comma-separated);
  the values are personal and live in terraform vars, not code.
- Eligibility is monotonic: once true it stays true. An `ignore` email from an
  already-eligible person still bumps counters.
- Eligibility is re-evaluated on every event, so a person first seen on an
  `ignore` email becomes eligible the moment a later email is classified
  otherwise or Ben replies.

Only **To** and **Cc** recipients of sent mail are counted. Bcc is excluded.

## 6. Event handling (`people-process`)

### 6.1 `email_classified` (existing inbox event, unchanged)

```
sender = event.sender.lower()
row = people.upsert_inbound(conn, sender, event.sender_display, event.received_at)
      -- message_count += 1, last_seen = max(last_seen, received_at),
      -- first_seen if new, display_name only if currently NULL
automated = filter(sender)
eligible  = row.eligible or (not automated and event.category != "ignore")
people.set_flags(conn, sender, automated=automated, eligible=eligible)
conn.commit()                           -- counters are durable before any external call

if eligible and row.google_resource_name is None and row.google_deleted_at is None:
    ensure_google_contact(row)          -- §7
if eligible:
    ensure_hubspot_contact(row)         -- §8, respects cap
if row.hubspot_contact_id:
    hubspot.log_email(row.hubspot_contact_id, event)   -- engagement, as inbox does today
```

Every external call is wrapped: failures are logged, counted
(`people_external_errors{system=google|hubspot}`), and never fail the event.
Pub/Sub redelivery therefore happens only after a DB failure. Counters are
**not** idempotent under redelivery: Pub/Sub is at-least-once, so a rare double
count is possible. This is accepted — it is the same trade-off inbox made for
`senders`, and counters are classification context, not billing. The nightly
reconcile does not correct counts.

### 6.2 `email_sent` (new inbox event, §10.3)

```
for addr in event.to + event.cc:
    addr = addr.lower()
    if addr in OWN_ADDRESSES: continue
    row = people.upsert_outbound(conn, addr, display_from_event, event.sent_at)
          -- my_response_count += 1, last_contacted = max(...), first_seen if new
    automated = filter(addr)
    eligible = not automated              -- Ben wrote to them: eligible
    people.set_flags(conn, addr, automated=automated, eligible=row.eligible or eligible)
conn.commit()
for each newly-eligible row: ensure_google_contact, ensure_hubspot_contact
```

`email_sent` carries no body, so no HubSpot engagement is logged for outbound
mail in v1 (HubSpot's own Outlook/BCC integration covers that if Ben wants it).

### 6.3 Anything else

`logger.info("Ignoring event type %r", kind)` and return, exactly as schedule
does. `label_applied` is ignored.

## 7. Google Contacts

### 7.1 Auth

Reuses schedule's OAuth client: `google-calendar-client-id` and
`google-calendar-client-secret` are referenced as **data sources** (schedule's
terraform owns them). People owns a new secret `google-contacts-refresh-token`,
minted once locally by `scripts/get_google_contacts_token.py` (adapted from
schedule's `get_google_calendar_token.py`) with scope
`https://www.googleapis.com/auth/contacts`. Same account schedule mirrors
calendar into. Credentials are built without passing scopes, for the reason
noted in schedule's `google_calendar.py`.

### 7.2 `ensure_google_contact(row)`

1. `people.searchContacts(query=email, readMask=names,emailAddresses,memberships,biographies)`.
   The People API requires a warm-up call with an empty query before search;
   the client does that once per process.
2. If a match with that email exists, link it: store `resourceName`, `etag`,
   and pull `display_name`, `notes`, `relationship_label` from it (Google wins).
3. Otherwise `people.createContact` with `names` (best-effort first/last split
   of `display_name`, or unstructured if no display name) and one
   `emailAddresses` entry; add it to the contact group named by
   `GOOGLE_CONTACT_GROUP` (default `"Inbox"`, created if absent) so people's
   contacts are distinguishable in the Google UI.
4. Store `resourceName` + `etag`.

Name changes observed later in events never overwrite a linked contact's name.

### 7.3 Nightly sync (`people-sync`)

`GET people/me/connections?syncToken=…&personFields=names,emailAddresses,memberships,biographies,metadata`,
paging until exhausted, then persist the new `nextSyncToken`. If the token is
expired (HTTP 410), do a full list with `requestSyncToken=true`.

For each returned person:

- Linked (its `resourceName` is in `people`): update `display_name`, `notes`,
  `relationship_label`, `etag`. If `metadata.deleted`, set `google_deleted_at`
  and clear `resourceName`.
- Unlinked but has an email address that matches a `people` row with no
  `resourceName` and no `google_deleted_at`: link it (Ben created the contact
  by hand).
- Unlinked and unknown email: **create a `people` row** for it (first_seen =
  now, counters 0, eligible = true, automated = false). Ben's hand-made
  contacts are people too, and this lets `people-api` search return them.

Contact groups are listed once per sync (`contactGroups.list`) to resolve
membership resource names to names. System groups (`myContacts`, `starred`,
etc.) are ignored for `relationship_label`; the `GOOGLE_CONTACT_GROUP` group is
ignored too, so "Inbox" never shows up as a relationship.

The first run has no token and performs the full list; that is the bootstrap.

## 8. HubSpot as a bounded mirror

### 8.1 Rules

- `HUBSPOT_MAX_CONTACTS` (default `1000`) is treated as a **hard** limit: people
  evicts before it creates.
- **Managed** contacts are those with a `hubspot_contact_id` in `people`.
  Contacts in HubSpot that people did not create (**unmanaged**) count toward
  the cap but are **never** archived by people.
- Ranking is by `last_interaction` descending. The eviction victim is the
  managed contact with the oldest `last_interaction`.
- Only **eligible** people are mirrored. Losing eligibility is impossible (§5),
  so a contact only leaves HubSpot by eviction.
- Engagements (`log_email`) are created only for contacts currently in HubSpot.
  Evicted contacts stop accumulating engagements; if they come back, history
  restarts from that point.
- Writes are gated by `HUBSPOT_WRITES_ENABLED` (default `false` until Phase C,
  §11), so people can run alongside inbox's existing HubSpot path without
  double-writing.

### 8.2 `ensure_hubspot_contact(row)` (event time)

```
if not HUBSPOT_WRITES_ENABLED or row.hubspot_contact_id: return
total = hubspot.count_contacts()                # cached in-process for 60 s
if total >= CAP:
    victim = people.oldest_managed(conn)
    if victim is None:
        warn "HubSpot cap reached by unmanaged contacts; not creating"; return
    hubspot.archive(victim.hubspot_contact_id); people.clear_hubspot(conn, victim.email)
existing = hubspot.find_by_email(row.email)      # handles contacts created before people
id = existing.id if existing else hubspot.create(row)     # properties §8.4
people.set_hubspot(conn, row.email, id)
```

`count_contacts` uses the search API's `total` with no filters. Event-time
enforcement only needs to be approximately right; the nightly reconcile is the
authority.

### 8.3 Nightly reconcile (part of `people-sync`)

1. Page all HubSpot contacts (`id`, `email`, `last_email_date`).
2. Adopt: a HubSpot contact whose email matches an eligible `people` row with no
   `hubspot_contact_id` becomes managed (this is how the contacts inbox created
   over the past months become managed on the first run).
3. Heal: a `people` row whose `hubspot_contact_id` no longer exists is cleared.
4. Enforce: while `total > CAP`, archive the managed contact with the oldest
   `last_interaction`. Log how many were archived; emit
   `people_hubspot_evictions`.
5. Fill: while `total < CAP`, create the most recently interacted-with eligible
   person not yet in HubSpot. Stops when nobody is left.

The **first** reconcile is the one that brings HubSpot down to the cap. It may
archive many contacts. HubSpot archives are recoverable for 90 days. Phase C
runs it manually and reviews the count before Cloud Scheduler takes over.

### 8.4 HubSpot properties

On create: `email`, `firstname`/`lastname` (best-effort split), `lifecyclestage=lead`,
`hs_lead_status=NEW`, `hubspot_owner_id=$HUBSPOT_OWNER_ID` (env, no default),
`last_email_date` (custom property inbox already uses; midnight-UTC ms).
On each inbound email for a mirrored contact: update `last_email_date` and
create the `INCOMING_EMAIL` engagement with subject, text/HTML body, and
`from` header, exactly as inbox's `log_email` does today.

## 9. `people-api`

FastAPI on Cloud Run, same skeleton as tasks-api (`api/main.py`, `api/auth.py`
with `PEOPLE_API_TOKEN`, no-op locally, 503 fail-closed on Cloud Run without a
token, `/health`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/people/{email}` | Full row (§4.1 minus internal ids). 404 if unknown. This is inbox's classify-time call. |
| `POST` | `/search` | `{ "q": str, "limit": int=20 }` — case-insensitive match on email substring or trigram similarity on `display_name`; ordered by `last_interaction` desc. |
| `GET` | `/people?recent=N` | Top-N by `last_interaction`; `eligible_only` query flag, default true. |
| `PATCH` | `/people/{email}` | `{ "notes"?: str, "relationship_label"?: str }`. Writes through to Google Contacts (biography / group membership), then updates the DB row. 409 if the person has no Google contact and is not eligible; 404 if unknown. |
| `POST` | `/people/{email}/sync` | Re-pull this one person from Google (used by skills after a manual edit in the Google UI). |

Response shape for a person:

```json
{
  "email": "alice@example.com",
  "display_name": "Alice Example",
  "first_seen": "…", "last_seen": "…", "last_contacted": "…",
  "message_count": 12, "my_response_count": 4,
  "relationship_label": "colleague", "notes": "…",
  "eligible": true, "automated": false,
  "in_google_contacts": true, "in_hubspot": true
}
```

Inbox's sender-context consumer needs only `message_count`,
`my_response_count`, `relationship_label`, `notes` — the four keys
`build_prompt` already reads.

## 10. Inbox changes

### 10.1 Remove HubSpot

- Delete `clients/hubspot.py`, the HubSpot block in `handlers/pipeline.py`,
  `scripts/import_contacts.py`, `.claude/skills/importing-hubspot-contacts/`.
- Drop `hubspot-api-client` from `requirements.txt`.
- Terraform: remove `hubspot_token` variable, the `"hubspot-token"` entry in
  `secrets.tf`, `process_cf_hubspot` IAM binding, the `HUBSPOT_TOKEN`
  secret env. The **secret itself must not be destroyed**: people imports it
  into its state first, then inbox runs `terraform state rm` on the secret and
  its version (same procedure as the Google Calendar secrets moving to schedule,
  `docs/superpowers/plans/2026-08-17-remove-google-calendar.md`).
- `.github/workflows/deploy.yml`: drop `TF_VAR_hubspot_token`; the GitHub secret
  `TF_VAR_HUBSPOT_TOKEN` is deleted from inbox and created in people.
- CLAUDE.md secrets table and `docs/inbox-architecture.md` updated.

### 10.2 Replace `senders` with a people-api lookup

- Delete `repo/senders.py`; remove `senders.upsert`/`senders.get` from
  `handlers/pipeline.py`, `scripts/bootstrap_labels.py`,
  `scripts/backfill_embeddings.py`.
- New `clients/people_api.py::get_person(email) -> dict | None`: `GET
  {PEOPLE_API_URL}/people/{email}` with bearer `PEOPLE_API_TOKEN`, **timeout 2 s**,
  returns `None` on 404, any exception, or unset URL. Records
  `inbox_people_lookup{outcome=hit|miss|error}` and a duration histogram.
- `handlers/pipeline.py`: `sender_ctx = people_api.get_person(msg["sender"])`
  in place of the two `senders` calls. `build_prompt`'s contract (a dict with
  the four keys, or `None`) is unchanged; its tests stay green.
- Terraform: `PEOPLE_API_URL` env var (variable), `PEOPLE_API_TOKEN` from the
  people-owned `people-api-token` secret via a **data source** (people's apply
  must precede inbox's, as inbox's precedes tasks'). IAM: `process_cf` SA gets
  `secretAccessor` on it — granted in **people's** terraform (secret owner
  grants), mirroring how inbox grants schedule's SAs on `msal-token-cache`.
- `repo/schema.sql`: remove the `senders` DDL. The live table is dropped
  manually only after Phase B is verified (§11).

### 10.3 Sent Items subscription and `email_sent` event

**Subscriptions.** Two Graph subscriptions, both on the existing webhook URL:

| Folder | Resource | clientState | Secret |
|---|---|---|---|
| Inbox | `me/mailFolders/inbox/messages` | `inbox-webhook` (unchanged) | `graph-subscription-id` (unchanged) |
| Sent | `me/mailFolders/sentitems/messages` | `inbox-webhook-sent` | `graph-sent-subscription-id` (new, seeded once, `ignore_changes` like the first) |

`functions/renew/main.py` is generalized to iterate a list of
`(resource, client_state, secret_name)` tuples; `_register_subscription`'s
idempotency check matches on `resource` too (it already does). `clients/graph_subscriptions.py::register`
takes `resource` and `client_state` parameters.

**Webhook.** `WEBHOOK_CLIENT_STATE` (inbox) and new `WEBHOOK_CLIENT_STATE_SENT`.
A notification whose `clientState` matches either is published to
`inbox-messages` with a Pub/Sub attribute `folder=inbox|sentitems`. Anything
else is still rejected. Existing behaviour for `folder=inbox` is byte-identical
(the attribute is additive; the processor treats a missing attribute as
`inbox`, so in-flight messages during deploy are safe).

**Processor.** `main.py::process` reads `attrs.get("folder", "inbox")`. For
`sentitems` it calls `handlers/sent.py::run(notification, context)`:

```
fetch message via Graph (immutable id)       -- reuse services/ingestion.fetch
if None: warn, return
if GCP and "[LOCAL-TEST]" in subject: return  -- same guard as pipeline
publish email_sent (below)
```

No DB write, no embedding, no classification, no tagging. Duplicates are
possible (Graph can notify twice); people's counters accept that (§6.1).

**Event** (`services/email_events.py::email_sent_payload`):

```json
{
  "event": "email_sent",
  "graph_message_id": "<immutable id>",
  "conversation_id": "<Graph conversationId or null>",
  "sent_at": "2026-09-03T14:05:00+00:00",
  "from": "ben@drolet.cloud",
  "to": ["alice@example.com"],
  "cc": [],
  "subject": "Re: …"
}
```

No body, no `message_id` (nothing is stored, so there is no inbox UUID). Tasks
and schedule already log-and-ignore unknown event kinds, verified in both
repos' `main.py`.

Metrics: `inbox_emails_sent_published` counter; stage duration with
`stage=sent`.

### 10.4 Docs and skills

- CLAUDE.md: new "People ownership" paragraph next to the tasks/schedule ones;
  `senders` removed from the schema list (four tables); secrets table updated;
  `email_sent` added to the domain-events row.
- Remove the `senders` mention from `docs/inbox-architecture.md`; note the
  Sent Items subscription in the Graph subscription section.
- Global skill `~/.claude/skills/adding-referral-contact/SKILL.md`: project root
  becomes `~/src/people`, entry points become people's import script and
  `people-api`; the enrichment section stays as future work.

## 11. Cutover

Three phases, hard stops between them (the pattern from the tasks extraction).

**Phase A — people live, read-mostly.** Create the repo (`setting-up-service-repo`),
implement everything in §3–§9, apply people's terraform (imports `hubspot-token`,
creates the DB, secrets, CFs, API), mint the Google refresh token, run the
first Google sync (full list → `people` rows for existing contacts).
`HUBSPOT_WRITES_ENABLED=false`. Deploy. People now builds its index from live
`email_classified` events and creates Google Contacts for newly eligible
people. Inbox is untouched and still writes HubSpot.

*Gate A:* a fresh inbound email produces a `people` row and (if eligible) a
Google Contact; `GET /people/{email}` returns it.

**Phase B — inbox cutover.** One inbox PR: §10.1–§10.4. Terraform plan/apply via
the skills; inbox runs `terraform state rm` for `hubspot-token` **after**
confirming people's state owns it. Register the Sent Items subscription (the
renew CF does this on its next run; trigger it manually).

*Gate B:* send a test email; an `email_sent` event arrives, `my_response_count`
increments; classification log shows sender context fetched from people-api;
no HubSpot writes from inbox. Then `DROP TABLE senders` in the inbox DB.

**Phase C — HubSpot mirror on.** Set `HUBSPOT_WRITES_ENABLED=true`, run
`people-sync` manually, review the eviction count in the logs, then let the
scheduler own it.

Rollback for any phase is the inverse: inbox's HubSpot code is one revert away
until the `senders` table is dropped; people can be disabled by removing its
subscription.

## 12. Bulk import (`scripts/import_contacts.py` in people)

Local-only. Authenticates to Graph with a device-code MSAL flow against a
people-owned cache file `~/.people-token-cache.json` (public client:
`CLIENT_ID` + `TENANT_ID` from `.env`, no client secret — as schedule's Graph
client does). Pages Inbox and Sent Items for `--days N` (default 365), and
feeds each message through the **same** `services/ingest.py` functions the
event handlers use, so eligibility and counters are computed identically. Runs
against the production DB by default (Cloud SQL connector, same env as the
CFs), with `--dry-run` to print counts only. External writes obey the same
flags as the CFs.

Because counters are not idempotent, the script is meant to be run once
against an empty `people` table (or with `--reset-counters`, which zeroes
counters first and recomputes them from the scan window).

## 13. Skills

People repo (`.claude/skills/`), copied and adapted from tasks/schedule:
`people-architecture`, `deploy-people`, `fetch-people-logs`,
`querying-people-db`, `adding-people-secret`, `adding-observability`,
`querying-grafana-metrics`, `testing-people-handlers`, `verifying-pr-locally`,
`importing-contacts`.

Global (`~/.claude/skills/`), following the tasks/schedule pairs:
`searching-people` (`POST /search`, `GET /people?recent=`), `fetching-person`
(`GET /people/{email}`), `editing-person` (`PATCH`, then `/sync`). Tokens are
read from `~/src/people/terraform/terraform.tfvars` like the other API skills.

## 14. Observability

Metrics (`people_` prefix): `events_received{event}`, `people_upserts{direction=inbound|outbound}`,
`eligibility_changes`, `google_contacts_created`, `google_sync_changes{kind=updated|linked|created|deleted}`,
`hubspot_contacts_created`, `hubspot_evictions`, `hubspot_engagements_logged`,
`external_errors{system}`, `api_requests{route,status}`, `errors{handler}`.
One span per event in `people-process`; one per sync run with child spans for
the Google and HubSpot halves.

Inbox adds `inbox_people_lookup{outcome}`, a lookup duration histogram, and
`inbox_emails_sent_published`.

## 15. Testing

**People.** `pytest` with the Google, HubSpot, and DB clients stubbed:

- Table-driven tests for the eligibility function (§5) and the automated filter.
- Handler tests: `email_classified` → counters, flags, Google create only when
  newly eligible, HubSpot engagement only when mirrored; `email_sent` → per
  recipient counters, own address skipped, Bcc ignored; unknown event ignored.
- Cap tests: create at cap evicts oldest managed; unmanaged-only cap logs and
  skips; reconcile adopt/heal/enforce/fill each in isolation.
- Sync tests: linked update, hand-made link, unknown create, deletion sets
  `google_deleted_at`, 410 → full resync, relationship label from groups
  ignoring system and `Inbox` groups.
- API tests with the DB stubbed: 404s, search ordering, PATCH write-through
  order (Google before DB), fail-closed auth on Cloud Run.

**Inbox.** Tests for: webhook stamps `folder` per clientState and still rejects
unknown clientState; `handlers/sent.py` payload shape and the `[LOCAL-TEST]`
guard; `clients/people_api.get_person` returns `None` on 404/timeout/exception
and the pipeline classifies without sender context; `build_prompt` unchanged.

**Live.** Gates in §11 using `testing-inbox-pipeline`, `tracing-inbox-email`,
`fetch-inbox-logs`, and people's `testing-people-handlers`.

## 16. Open decisions resolved in this spec

| Question | Decision |
|---|---|
| Scope | HubSpot lift-and-shift **plus** `people-api`; enrichment deferred |
| `senders` table | Moves to people; inbox reads sender context from `people-api`, fail-open |
| Source of truth | Google Contacts; `people` DB is a derived index |
| HubSpot | Kept as a mirror capped at `HUBSPOT_MAX_CONTACTS` (1000), most-recent-first; cap treated as hard |
| Google auth | Schedule's OAuth client, new people-owned refresh token with the contacts scope |
| Who becomes a contact | Non-automated senders on non-`ignore` mail, or anyone Ben writes to |
| Replies | Inbox subscribes to Sent Items and publishes `email_sent` |
| Sent-mail plumbing | Second subscription, same webhook and topic, `folder` attribute, processor branch |
| Repo visibility | Public; all personal values in env/secrets |
