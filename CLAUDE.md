# people

People service: Google Contacts is the source of truth for who a person is;
a `people` Postgres index and a bounded HubSpot mirror are derived from it;
`people-api` serves person lookups to inbox and to Claude Code skills.
Inbox publishes `email_classified` (every processed email) and `email_sent`
(mail Ben sends) domain events to its `email-events` Pub/Sub topic; this repo
owns everyone Ben corresponds with.

**Dividing rule.** Inbox owns mail: receiving, classifying, tagging, and the
`email_classified` / `email_sent` feeds. **People owns everyone Ben
corresponds with**: the canonical Google Contact, the derived index, the
HubSpot mirror, and `people-api`. Inbox never talks to Google Contacts or
HubSpot again. People's Cloud Functions never talk to Microsoft Graph — only
the local `scripts/import_contacts.py` does.

Full design:
`docs/superpowers/specs/2026-09-03-people-service-extraction-design.md`.

## Development workflow

When implementing new code in this repo, open a pull request rather than committing to `main`:

1. Create a feature branch off `main` before making changes (never commit code changes directly to `main`).
2. Once the change is implemented and verified, open the PR using the `/pr-open` skill — it pulls the base branch, creates the feature branch, commits, pushes, and writes a rich PR description. Don't hand-roll `git push` + `gh pr create` for this repo.
3. Do this proactively when work is complete — don't wait to be asked. (Still hold off if the change is incomplete, exploratory, or the user signalled they're mid-iteration.)

This overrides the default "commit or push only when asked" behavior for code changes in this repo.

## Stack

| | |
|---|---|
| **GCP project** | `bens-project-462804`, `us-central1` |
| **Events CF** | `people-process` — Pub/Sub trigger on the inbox-owned `email-events` topic (data source), entry point `process` in `main.py`; handles `email_classified` and `email_sent`, ignores everything else |
| **Sync CF** | `people-sync` — HTTP trigger, entry point `sync`; POST with `Authorization: Bearer <people-sync-token>`; Cloud Scheduler `people-sync` at `0 4 * * *` America/New_York (before inbox's 5 AM sweep) — Google Contacts incremental sync, then HubSpot reconcile |
| **API** | `people-api` — Cloud Run FastAPI service (`api/`); bearer auth via `people-api-token`; image in Artifact Registry repo `people`, deployed by `.github/workflows/deploy-api.yml`; no custom domain mapped yet — reach it via `terraform output -raw people_api_url` |
| **Database** | `people` DB + `people` user on Cloud SQL instance `inbox` (`bens-project-462804:us-central1:inbox`, data source — instance owned by inbox terraform); tables `people`, `sync_state`; schema in `repo/schema.sql` |
| **Google Contacts** | People API v1 via `clients/google_contacts.py` — OAuth refresh-token creds, scope `https://www.googleapis.com/auth/contacts`; reuses schedule's OAuth client (`google-calendar-client-id`/`-secret`, data sources), a people-owned refresh token (`google-contacts-refresh-token`) |
| **HubSpot** | `clients/hubspot.py` (ported from inbox) — contacts search/create/update/archive, email engagement create; bounded mirror, see §HubSpot below |
| **Local Graph import** | `clients/graph_local.py` — device-code MSAL auth for `scripts/import_contacts.py` only; people's Cloud Functions never call Graph |
| **Observability** | OTel → Grafana Cloud OTLP; metrics prefixed `people_` |
| **Infra** | `terraform/` — GCS backend `bens-project-462804-tf-state`, state prefix `people` |

Repo is **public**. Nothing personal is committed: the HubSpot owner id, the
automated-sender domains, the own-address list, the contact cap, and every
token are env vars or Secret Manager secrets, never hardcoded.

## Code layout

```
main.py                     CF entry points: process (Pub/Sub → email_classified/email_sent)
                             and sync (HTTP, bearer-auth, nightly)
models/
  events.py                 EmailClassifiedEvent / EmailSentEvent TypedDicts — mirror inbox's payload
  types.py                  IngestResult dataclass
clients/
  db.py                     Cloud SQL connector (pg8000) / local psycopg3
  google_contacts.py        People API v1 — search, create, groups, biography
  hubspot.py                contacts search/create/update/archive, engagements
  graph_local.py            device-code MSAL for scripts/import_contacts.py only
  otel.py                   OTel setup + counters (people.* instruments)
repo/
  schema.sql                people, sync_state tables
  people.py                 all reads/writes on people — takes an open connection
  sync_state.py             google_contacts sync token + status
services/
  eligibility.py            is_automated / inbound_eligible (spec §5) — pure functions over env
  ingest.py                 record_inbound / record_outbound — counters + eligibility, shared by
                             handlers and scripts/import_contacts.py
  google_contacts_sync.py   ensure_contact (link/create), run_sync (nightly), sync_one (PATCH refresh)
  hubspot_mirror.py         ensure_contact, log_email, reconcile (adopt/heal/enforce/fill)
  person_edit.py            PATCH: write Google first, then refresh DB
  sync_auth.py              bearer check for POST /sync
handlers/
  email_classified.py       ingest → eligible? → Google + HubSpot side effects → log_email
  email_sent.py             per-recipient ingest (To+Cc, Bcc excluded) → side effects
  sync.py                   nightly: google_contacts_sync.run_sync, then hubspot_mirror.reconcile
api/
  main.py                   FastAPI app, /health, OTel request-metrics middleware
  auth.py                   bearer verify_token() — no-op locally, 503 fail-closed on Cloud Run
  routers/
    people.py                GET /people?recent=, GET/PATCH/POST-sync /people/{email}
    search.py                 POST /search
scripts/
  import_contacts.py        bulk backfill (spec §12) — --dry-run, --reset-counters
  get_google_contacts_token.py  mint the Google refresh token (contacts scope)
  migrate_db.py             apply repo/schema.sql
  fetch-env.sh               populate .env from Secret Manager + terraform.tfvars
  test-api-local.py          smoke test people-api
  link-skills.sh             symlink searching-people/fetching-person/editing-person into ~/.claude/skills/
terraform/                  main, variables, secrets, cloudsql, pubsub, cloud_functions, iam, api, scheduler
tests/                      one test module per unit
.github/workflows/          ci.yml, deploy.yml (Functions), deploy-api.yml (Cloud Run)
.claude/skills/             people-architecture, deploy-people, fetch-people-logs, querying-people-db,
                             adding-people-secret, adding-observability, querying-grafana-metrics,
                             testing-people-handlers, verifying-pr-locally, importing-contacts,
                             searching-people, fetching-person, editing-person
```

## Event schema

`models/events.py` mirrors what inbox publishes on `email-events`
(`services/email_events.py` in the inbox repo). Fields people actually reads:

- **`email_classified`**: `sender`, `sender_display`, `category`,
  `received_at`, `subject`, `body`, `body_html` — `sender`/`sender_display`/
  `received_at`/`category` drive `services/ingest.py::record_inbound`;
  `subject`/`body`/`body_html` are used only for the HubSpot engagement
  (`hubspot_mirror.log_email`), never persisted in `people`.
- **`email_sent`**: `to`, `cc`, `sent_at` — `to`+`cc` (Bcc excluded) drive
  `services/ingest.py::record_outbound`; the handler never reads `from`.
  Each recipient is instead filtered through `OWN_ADDRESSES` by
  `record_outbound`, so a self-addressed recipient is skipped, not the
  whole message.

Every other field (`message_id`, `importance`, `confidence`, `tags`,
`reasoning`, `web_link`, `graph_message_id`, `conversation_id`, `from`, `subject` on
`email_sent`) is carried in the TypedDict for shape-compatibility but unused
today. Unknown event kinds are logged and ignored (`main.py::process`),
exactly as tasks and schedule do.

### Source of truth (spec §4.3)

| Field | Truth | Direction |
|---|---|---|
| `display_name` | Google Contacts once linked; event data before that | Google → DB on link/sync. Event data never overwrites a Google-sourced name. |
| `notes` | Google contact **biography** | Both ways — `PATCH /people/{email}` writes Google first, then refreshes the DB row from it. The event path never writes notes. |
| `relationship_label` | Google **contact group** membership | Google → DB — first non-system, non-`GOOGLE_CONTACT_GROUP` group, lowercased. `PATCH` writes by moving group membership (adds the target group, removes the prior one). |
| counters (`message_count`, `my_response_count`), timestamps, `eligible`, `automated` | DB | Written only by the event handlers and `scripts/import_contacts.py`. |
| `google_deleted_at` | Google | Set by `people-sync` when a linked `resourceName` comes back deleted. Never recreated. |
| `hubspot_contact_id` | DB (people manages) | Set on create/adopt, cleared on evict/heal. |

If the DB is lost, everything except the counters rebuilds from Google
Contacts plus a full sync; counters rebuild via `scripts/import_contacts.py`.

### Eligibility (spec §5)

```
automated  = address matches AUTOMATED_SENDER_PATTERN (env override; default
             catches no-reply/noreply/do-not-reply/mailer-daemon/
             notifications@/alerts@/support@/newsletter@, case-insensitive)
             or its domain is in AUTOMATED_SENDER_DOMAINS
             or address is in OWN_ADDRESSES
eligible   = not automated
             and (an inbound email with category != "ignore"
                  or Ben sent this person an email)
```

`services/eligibility.py`. Eligibility is monotonic — once true it stays
true, and is re-evaluated on every event, so a person first seen on an
`ignore` email becomes eligible the moment a later email is classified
otherwise, or Ben replies. Only **To** and **Cc** recipients of sent mail
are counted; **Bcc is excluded**.

### HubSpot as a bounded mirror (spec §8.1)

- `HUBSPOT_MAX_CONTACTS` (default `1000`) is a **hard** limit — people
  evicts before it creates (`services/hubspot_mirror.py`).
- **Managed** contacts have a `hubspot_contact_id` in `people`. Contacts in
  HubSpot that people did not create (**unmanaged**) count toward the cap but
  are **never** archived by people.
- Ranking is by `last_interaction` (`GREATEST(last_seen, last_contacted)`)
  descending; the eviction victim is the managed contact with the oldest
  `last_interaction`.
- Only **eligible** people are mirrored. Losing eligibility is impossible, so
  a contact only leaves HubSpot by eviction.
- Engagements (`log_email`) are created only for contacts currently in
  HubSpot. Evicted contacts stop accumulating engagements; if they come
  back, history restarts from that point.
- All writes are gated by `HUBSPOT_WRITES_ENABLED` (default `false`) — see
  "Phase C" below. **Google Contacts writes have no such gate** — an
  eligible person always gets a real Google Contact created, independent of
  the HubSpot cap or `HUBSPOT_WRITES_ENABLED`.
- Nightly `people-sync` runs `reconcile()`: adopt (an unmanaged HubSpot
  contact matching an eligible row with no `hubspot_contact_id`), heal (a
  managed row whose HubSpot contact no longer exists), enforce (evict down to
  cap), fill (create up to cap from the most recently interacted-with
  eligible people not yet mirrored).

## Layer rules

- `clients/` — I/O only (Google Contacts, HubSpot, Cloud SQL, OTel, local Graph import)
- `repo/` — DB read/write only; takes an open connection, never opens its own
- `services/` — business logic, one concern per file
- `handlers/` — orchestrate clients + repo + services; called only from `main.py`
  (`api/routers/` play the same role for `people-api` — thin transport,
  called only from `api/main.py`)
- `models/` — pure types, no imports from other layers
- `main.py` — CF entry points only; always `otel.flush()` in `finally`

## Secrets

Owned here: `hubspot-token` (imported from inbox terraform — the value
carries forward, only ownership moves), `google-contacts-refresh-token`
(minted locally by `scripts/get_google_contacts_token.py`, added by hand —
never in `terraform.tfvars` or state), `people-db-password`,
`people-api-token`, `people-sync-token` (all three `random_password`-
generated by Terraform, not passed in as variables). Read any of them with
`gcloud secrets versions access latest --secret=<name>`.

Shared (data sources, owned elsewhere): `grafana-otlp-endpoint`,
`grafana-otlp-token` (platform state, `~/src/infra`); `google-calendar-client-id`,
`google-calendar-client-secret` (owned by schedule terraform — people reuses
schedule's OAuth client, with its own refresh token and the `contacts`
scope instead of `calendar`).

`people-api-token` is also granted (secret-owner side, in `terraform/iam.tf`)
to inbox's `inbox-process-cf@` service account, so inbox's classify-time
lookup can call `people-api` without a shared credential living in inbox's
own terraform.

See `adding-people-secret` for the full wiring checklist when adding a new one.

## Local dev

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
scripts/fetch-env.sh                 # .env from Secret Manager + terraform.tfvars
.venv/bin/pytest tests/ -q           # unit tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```

Handler smoke tests against the real DB: see `testing-people-handlers`.
Local API: `(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080)`,
then `.venv/bin/python scripts/test-api-local.py`.

## Deployment

Push to `main` touching `main.py`, `clients/`, `models/`, `services/`,
`handlers/`, `repo/`, `requirements.txt`, or `terraform/` auto-deploys
`people-process`/`people-sync` via `.github/workflows/deploy.yml` (WIF →
`terraform apply`).

Push to `main` touching `api/`, `clients/`, `repo/`, `services/`, `models/`,
`Dockerfile`, or `requirements.txt` auto-deploys `people-api` via
`.github/workflows/deploy-api.yml` (build + push image, `gcloud run deploy`,
read-only post-deploy smoke). The first deploy needs a manual runbook before
either workflow can run unattended — see "First-time setup" in `README.md`.

Manual/local verification: `/deploy-people` and `/verifying-pr-locally` skills.

**Phase C.** `HUBSPOT_WRITES_ENABLED` (Terraform var
`hubspot_writes_enabled`) defaults to `false` — people builds its index and
creates Google Contacts, but writes nothing to HubSpot, until the inbox-side
extraction (removing inbox's own HubSpot code and `senders` table, per the
design's §11 Phase B) ships and is verified. Flipping it to `true` is a
one-line `terraform.tfvars` change followed by a manual `people-sync` run
(review the eviction count in the logs before letting the scheduler own it
unattended) — the design's cutover phases are in §11 of the spec.
