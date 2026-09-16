# LinkedIn Snapshot — Design

**Date:** 2026-09-16
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`

## 1. Summary

The `people` index only knows people Ben has emailed, and only since the
service went live. Many of the people who matter for consulting referrals —
former colleagues, founders, investors — live on LinkedIn and have never been
emailed from the current mailbox.

This design adds a **LinkedIn snapshot**: Ben requests LinkedIn's official data
export by hand, then runs a local script that loads it into four new tables in
the `people` database. The snapshot is exposed through `people-api` alongside
the existing person records.

- **Manual export, local import.** No scraping, no LinkedIn API, no new cloud
  infrastructure. `scripts/import_linkedin.py <export-dir>` replaces the
  snapshot in one transaction.
- **Separate tables, soft link.** LinkedIn connections are keyed by profile
  URL, not email. A connection links to a `people` row when it can be matched
  confidently; otherwise it stands alone.
- **Read-only relative to everything else.** The import never writes to
  `people`, Google Contacts, or HubSpot. LinkedIn is not a source of truth for
  any existing field.

## 2. Goals and non-goals

**Goals**

1. Store LinkedIn connections, messages (including bodies), and recommendations
   given/received as a queryable snapshot.
2. Rank connections by real interaction (message count, last message, whether
   Ben replied) so "who have I talked to and let go quiet" is one query.
3. Link connections to existing `people` rows where the match is unambiguous.
4. Surface the snapshot through `people-api` and the people skills.

**Non-goals**

- Scraping LinkedIn or calling any LinkedIn API. Refreshes happen when Ben
  re-exports.
- History across snapshots. Each import replaces the previous one.
- Creating `people` rows, Google Contacts, or HubSpot contacts from LinkedIn
  data. Eligibility stays email-driven (parent spec §5).
- Writing LinkedIn company/title/URL into Google Contacts.
- Fuzzy or manual linking of connections to people. Unambiguous matches only.
- Invitations, profile, positions, endorsements, and the rest of the full
  archive.

## 3. Components

| Unit | Layer | Responsibility |
|---|---|---|
| `services/linkedin_export.py` | services | Pure parsing of the export directory into typed records; profile-URL normalization; name normalization; per-connection message stats; person matching. No I/O beyond reading the CSV files it is handed. |
| `repo/linkedin.py` | repo | `replace_snapshot(conn, snapshot)` and the read queries used by the API. Takes an open connection. |
| `models/linkedin.py` | models | `LinkedInConnection`, `LinkedInMessage`, `LinkedInRecommendation`, `LinkedInSnapshot` dataclasses. |
| `scripts/import_linkedin.py` | script | CLI: parse → load people names/emails for matching → `replace_snapshot` → print counts. `--dry-run` prints counts without writing. |
| `api/routers/linkedin.py` | api | New `/linkedin/...` endpoints (§7). |
| `api/routers/people.py`, `api/routers/search.py` | api | Additive `linkedin` fields (§7). |
| `repo/schema.sql` | repo | Four new tables (§4), applied with `scripts/migrate_db.py`. |
| `.claude/skills/importing-linkedin/` | skill | How to request the export and run the import. |

## 4. Data model

All tables are fully replaced on every import. There are no foreign keys to
`people`: the `people` table is rebuildable and the link is advisory.

### 4.1 `linkedin_connections`

```sql
CREATE TABLE IF NOT EXISTS linkedin_connections (
    profile_url         TEXT PRIMARY KEY,   -- normalized, see §5.2
    first_name          TEXT,
    last_name           TEXT,
    full_name           TEXT NOT NULL,      -- "first last", trimmed
    email               TEXT,               -- from export; usually NULL
    company             TEXT,
    position            TEXT,
    connected_on        DATE,
    person_email        TEXT,               -- soft link to people.email
    match_method        TEXT,               -- 'email' | 'name' | NULL
    message_count       INT  NOT NULL DEFAULT 0,
    my_message_count    INT  NOT NULL DEFAULT 0,
    last_message_at     TIMESTAMPTZ,
    last_my_message_at  TIMESTAMPTZ,
    snapshot_at         TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS linkedin_connections_person_email_idx ON linkedin_connections (person_email);
CREATE INDEX IF NOT EXISTS linkedin_connections_name_trgm_idx ON linkedin_connections USING gin (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS linkedin_connections_company_trgm_idx ON linkedin_connections USING gin (company gin_trgm_ops);
CREATE INDEX IF NOT EXISTS linkedin_connections_position_trgm_idx ON linkedin_connections USING gin (position gin_trgm_ops);
```

`message_count` counts every message in conversations where this connection is
a participant; `my_message_count` counts the ones Ben sent. "Ben replied" is
`my_message_count > 0`.

### 4.2 `linkedin_messages`

```sql
CREATE TABLE IF NOT EXISTS linkedin_messages (
    id                      BIGSERIAL PRIMARY KEY,
    conversation_id         TEXT NOT NULL,
    conversation_title      TEXT,
    sender_name             TEXT,
    sender_profile_url      TEXT,           -- normalized
    recipient_names         TEXT,
    recipient_profile_urls  TEXT[] NOT NULL DEFAULT '{}',  -- normalized
    sent_at                 TIMESTAMPTZ NOT NULL,
    subject                 TEXT,
    content                 TEXT,
    folder                  TEXT,
    from_me                 BOOLEAN NOT NULL,
    snapshot_at             TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS linkedin_messages_conversation_idx ON linkedin_messages (conversation_id, sent_at);
CREATE INDEX IF NOT EXISTS linkedin_messages_participants_idx ON linkedin_messages USING gin ((recipient_profile_urls || ARRAY[sender_profile_url]));
```

Messages with people who are not connections (InMail, recruiters) are stored
too; they simply do not contribute stats to any connection row. A surrogate id
is used because the snapshot is replaced wholesale, so no natural key is needed
for idempotency.

### 4.3 `linkedin_recommendations`

```sql
CREATE TABLE IF NOT EXISTS linkedin_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    direction       TEXT NOT NULL CHECK (direction IN ('given', 'received')),
    first_name      TEXT,
    last_name       TEXT,
    full_name       TEXT NOT NULL,
    company         TEXT,
    job_title       TEXT,
    text            TEXT,
    status          TEXT,
    created_on      DATE,
    profile_url     TEXT,          -- linked connection, by unique name match; else NULL
    snapshot_at     TIMESTAMPTZ NOT NULL
);
```

The recommendation CSVs carry no profile URL, so `profile_url` is filled only
when `full_name` matches exactly one connection (§5.3).

### 4.4 `linkedin_imports`

```sql
CREATE TABLE IF NOT EXISTS linkedin_imports (
    id                      BIGSERIAL PRIMARY KEY,
    snapshot_at             TIMESTAMPTZ NOT NULL,
    source                  TEXT NOT NULL,   -- export directory basename
    connections             INT NOT NULL,
    messages                INT NOT NULL,
    recommendations_given   INT NOT NULL,
    recommendations_received INT NOT NULL,
    matched_by_email        INT NOT NULL,
    matched_by_name         INT NOT NULL
);
```

Append-only audit of runs. Not replaced.

### 4.5 Source-of-truth addendum

Adds one row to parent spec §4.3:

| Field | Truth | Direction |
|---|---|---|
| `linkedin_*` tables | LinkedIn data export | Export → DB on manual import. Never written back anywhere. |

## 5. Import (`scripts/import_linkedin.py`)

```
.venv/bin/python scripts/import_linkedin.py ~/Downloads/Basic_LinkedInDataExport_09-16-2026 [--dry-run]
```

Runs locally against whatever DB `clients/db.py` resolves from `.env`, same as
`scripts/import_contacts.py`. Accepts an unzipped export directory (a `.zip`
path is also accepted and read in place with `zipfile`).

### 5.1 Files read

| File | Required | Notes |
|---|---|---|
| `Connections.csv` | yes | Begins with a free-text "Notes:" preamble before the header row. Columns: `First Name, Last Name, URL, Email Address, Company, Position, Connected On` (`Connected On` like `16 Sep 2026`). |
| `messages.csv` | no | Columns include `CONVERSATION ID, CONVERSATION TITLE, FROM, SENDER PROFILE URL, TO, RECIPIENT PROFILE URLS, DATE, SUBJECT, CONTENT, FOLDER` (`DATE` like `2026-09-16 17:03:12 UTC`). |
| `Recommendations_Given.csv` | no | `First Name, Last Name, Company, Job Title, Text, Creation Date, Status`. |
| `Recommendations_Received.csv` | no | Same columns. |

Parsing is **header-driven**: the parser scans for the header row, maps columns
by name, and ignores unknown extra columns, because LinkedIn changes these files
without notice. A missing required column fails the run before any DB write.
A missing optional file is treated as empty and reported. Column names and date
formats above are from LinkedIn's current export and must be confirmed against
Ben's real export before implementation; fixtures are built from that shape.

### 5.2 Normalization

- **Profile URL:** lowercase, strip scheme, `www.`, query string, trailing
  slash → `linkedin.com/in/<slug>`. Applied to connection URLs and every
  sender/recipient URL. Blank URLs become `NULL`.
- **Names:** Unicode NFKD, strip combining marks, lowercase, drop punctuation
  except hyphen/apostrophe, collapse whitespace. Used only for matching; stored
  names keep their original form.
- **Timestamps:** parsed to UTC `datetime`; dates to `date`.

### 5.3 "Me" and per-connection stats

Ben's own profile URL is the participant present in the most conversations in
`messages.csv`. It can be overridden with `--me <profile-url>` (never
hardcoded; the repo is public). `from_me` is `sender_profile_url == me`.

For each connection, over every message where the connection's `profile_url` is
the sender or a recipient: `message_count`, `my_message_count`,
`last_message_at`, `last_my_message_at`.

If message URLs turn out not to use the same slug form as `Connections.csv` for
some rows, those messages fall back to a match on normalized sender/recipient
name against connections whose normalized `full_name` is unique. The importer
reports how many messages matched by URL, by name, and not at all.

### 5.4 Matching connections to people

Evaluated in order; first hit wins:

1. **email** — connection `email` (normalized with `services.eligibility.normalize`)
   equals a `people.email`.
2. **name** — normalized connection `full_name` equals the normalized
   `display_name` of **exactly one** `people` row, and that `people.email` is not
   already claimed by an email match.

Otherwise `person_email` and `match_method` stay `NULL`. Links go stale if
`people` gains rows after an import; re-running the import re-matches.

### 5.5 Write

One transaction:

```
DELETE FROM linkedin_messages; DELETE FROM linkedin_recommendations; DELETE FROM linkedin_connections;
INSERT ... (batched executemany)
INSERT INTO linkedin_imports ...
COMMIT
```

Any error rolls back and leaves the previous snapshot intact. `--dry-run` parses,
matches, and prints the counts it would write, without touching the DB.

Output:

```
connections 1,284 (email-matched 37, name-matched 112, unmatched 1,135)
messages 4,902 in 611 conversations (me=linkedin.com/in/…; by-url 4,610, by-name 180, unmatched 112)
recommendations given 6 (linked 5), received 9 (linked 8)
snapshot_at 2026-09-16T18:04:11Z
```

(Numbers illustrative.)

## 6. Privacy

- Export files are never committed. Add `*LinkedInDataExport*` and
  `linkedin-export*/` to `.gitignore`.
- Test fixtures use invented names and `example.com`-style URLs only.
- Message bodies live in the same private Cloud SQL database that already holds
  email-derived personal data, and are served only behind the existing
  `people-api-token` bearer auth. No logs print message content.

## 7. `people-api` changes

### 7.1 Additive field on existing person responses

`PersonOut` gains `linkedin: LinkedInSummary | null`, populated when a
connection has `person_email = email`:

```json
"linkedin": {
  "profile_url": "linkedin.com/in/alice-example",
  "company": "Example Health", "position": "CTO",
  "connected_on": "2021-04-02",
  "message_count": 14, "my_message_count": 6,
  "last_message_at": "…", "last_my_message_at": "…",
  "snapshot_at": "…"
}
```

Filled on `GET /people/{email}` and `PATCH /people/{email}` responses (one extra
lookup). List responses (`GET /people`, `POST /search` `results`) return
`linkedin: null` to keep them one query; callers fetch the person for detail.
Inbox's classify-time consumer ignores unknown keys, so this is
backward-compatible.

### 7.2 `POST /search`

Response gains `linkedin_results: list[LinkedInConnectionOut]` — connections
whose `full_name`, `company`, or `position` match `q` (ILIKE substring or
trigram similarity > 0.3), ordered by `last_message_at` desc nulls last, same
`limit`. `results` is unchanged.

### 7.3 New endpoints (`api/routers/linkedin.py`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/linkedin/connections` | Filters: `q` (name), `company`, `position` (substring, case-insensitive), `min_messages` (int), `replied` (bool), `quiet_since` (date: `last_message_at` before it), `unmatched` (bool: `person_email IS NULL`), `limit` (default 50, max 500). Ordered by `last_message_at` desc nulls last, then `connected_on` desc. |
| `GET` | `/linkedin/connections/{slug}` | One connection (`slug` is the part after `/in/`), plus its recommendations and its messages grouped by conversation, newest first. `messages_limit` query param (default 100). 404 if unknown. |
| `GET` | `/linkedin/imports/latest` | Latest `linkedin_imports` row, so skills can say how old the snapshot is. 404 if never imported. |

`LinkedInConnectionOut` is `LinkedInSummary` plus `full_name`, `email`,
`person_email`, `match_method`.

## 8. Skills and docs

- New `.claude/skills/importing-linkedin/SKILL.md`: request the export
  (Settings → Data privacy → Get a copy of your data → Connections, Messages,
  Recommendations), unzip, `--dry-run`, then import; how to read the counts.
- `searching-people`: mention `linkedin_results` and `/linkedin/connections`
  filters (e.g. "former colleagues at X who went quiet").
- `fetching-person`: show the `linkedin` block; point to
  `/linkedin/connections/{slug}` for messages.
- `querying-people-db`, `people-architecture`: new tables.
- `CLAUDE.md`: code layout entries, source-of-truth row, local import command.
- `scripts/link-skills.sh`: add `importing-linkedin` only if it should be global
  (it should not — it runs from this repo).

## 9. Observability

The import is a local script, like `import_contacts.py`, and emits no metrics;
it prints counts and writes a `linkedin_imports` row. New API routes are covered
by the existing request-metrics middleware in `api/main.py`.

## 10. Testing

- `tests/fixtures/linkedin/` — small synthetic export: `Connections.csv` with the
  Notes preamble and an extra unknown column; `messages.csv` with a
  multi-recipient conversation, a non-connection sender, and a URL-less row;
  both recommendation files.
- `tests/test_linkedin_export.py` — header detection, missing required column
  fails, missing optional file is empty, URL/name normalization, "me"
  inference and `--me` override, per-connection stats, name-fallback message
  matching, email match, unique-name match, ambiguous-name no-match.
- `tests/test_repo_linkedin.py` — `FakeConn` pattern from
  `tests/test_repo_people.py`: `replace_snapshot` issues deletes, inserts, and the audit row in order;
  filter SQL/params for `/linkedin/connections`.
- `tests/test_import_linkedin.py` — dry-run writes nothing; error rolls back.
- `tests/test_api.py` — `linkedin` field present/absent on `GET /people/{email}`,
  `linkedin_results` on search, connections filters, detail 404, imports/latest.
- Local verification (`verifying-pr-locally`): `--dry-run` then a real import
  against Ben's actual export; spot-check a few known connections via the API.

## 11. Rollout

1. Run `scripts/migrate_db.py` from the branch before merging (all statements
   are `IF NOT EXISTS`, so it is safe against the running service; without the
   tables the new routes would 500).
2. Merge; CI deploys `people-api`.
3. Ben requests the export, runs `import_linkedin.py --dry-run`, then the import.
4. Use `/linkedin/connections` to finish the "people to meet" list.

No Terraform, secrets, Cloud Functions, or inbox changes.

## 12. Decisions

| Decision | Choice | Why |
|---|---|---|
| Identity | Separate tables keyed by profile URL, soft link to `people.email` | Most connections have no email; keeps `people`, eligibility, Google, and HubSpot untouched. |
| Scope | Connections, full messages, recommendations | Ben wants the full conversation context available. |
| Ingestion | Local script from manual export | Refreshes are rare; no upload surface or infra. |
| Refresh semantics | Full replace per import, audit row kept | "Snapshot" is the requirement; history is YAGNI. |
| Matching | Exact email, then unique exact normalized name | Wrong links are worse than missing ones. |
| Surfacing | API + search + skills; no write-back to Google | LinkedIn stays out of the source-of-truth table for existing fields. |
