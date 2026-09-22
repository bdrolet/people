---
name: importing-linkedin
description: Use when the user wants to load, refresh, or re-import their LinkedIn data export (connections, messages, recommendations) into the people database, run scripts/import_linkedin.py, or check how old the LinkedIn snapshot is.
---

# Importing a LinkedIn Snapshot

`scripts/import_linkedin.py` loads LinkedIn's official data export into the
`linkedin_connections`, `linkedin_messages`, and `linkedin_recommendations`
tables, replacing the previous snapshot in one transaction, and appends a row
to `linkedin_imports`. It never writes to `people`, Google Contacts, or
HubSpot. Spec: `docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md`.

## 1. Request the export (Ben, by hand)

LinkedIn → **Settings → Data privacy → Get a copy of your data** → "Want
something in particular?" → tick **Connections**, **Messages**,
**Recommendations** → **Request archive**. LinkedIn emails a download link
(usually minutes, up to 24h). Download and unzip it — the script also accepts
the `.zip` directly.

The export is personal data. Keep it outside the repo, or in a path matching
`*LinkedInDataExport*` / `linkedin-export*/` (both gitignored). Never commit it.

## 2. Dry run, then import

```bash
cd ~/src/people
scripts/fetch-env.sh            # if .env is missing or stale
.venv/bin/python scripts/import_linkedin.py ~/Downloads/Basic_LinkedInDataExport_MM-DD-YYYY --dry-run
.venv/bin/python scripts/import_linkedin.py ~/Downloads/Basic_LinkedInDataExport_MM-DD-YYYY
```

`--dry-run` parses everything and reads `people` for matching, but writes
nothing. Always run it first.

If the `me=` URL in the output is not Ben's profile, re-run with
`--me https://www.linkedin.com/in/<ben's-slug>` — it is inferred as the
participant in the most conversations and is never hardcoded (public repo).

## 3. Reading the counts

```
connections 1,284 (email-matched 37, name-matched 112, unmatched 1,135)
messages 4,902 in 611 conversations (me=linkedin.com/in/…; by-url 4,610, by-name 180, unmatched 112, skipped 0)
recommendations given 6 (linked 5), received 9 (linked 8)
snapshot_at 2026-09-16T18:04:11Z
```

- **email-matched / name-matched** — connections soft-linked to a `people`
  row. Name matches require the normalized name to be unique among both
  people and connections; ambiguous names stay unmatched on purpose.
- **by-url / by-name / unmatched messages** — how each message was tied to a
  connection. Unmatched is normal for InMail and recruiters (non-connections).
  A large by-name or unmatched share suggests LinkedIn changed URL formats.
- **skipped** — rows with no conversation id or an unparseable `DATE`. Non-zero
  means the date format changed: fix `parse_timestamp` in
  `services/linkedin_export.py`.
- **missing (treated as empty)** — an optional file was not in the export
  (usually a box left unticked when requesting it).

An `export error: ...` exit means a required file or column is missing — the
DB was not touched. LinkedIn renames columns without notice; update the column
sets at the top of `services/linkedin_export.py` and the fixtures in
`tests/fixtures/linkedin/`.

## 4. Check snapshot age

```bash
TOKEN=$(gcloud auth print-identity-token)   # Cloud Run IAM; your gcloud login is the credential
curl -s https://people-api.drolet.cloud/linkedin/imports/latest -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`404` = never imported.

Links from connections to people go stale as new people arrive by email;
re-running the import re-matches.
