---
name: verifying-pr-locally
version: 1.0.0
description: >
  Use when verifying that people changes actually work by running them locally —
  typically after a PR is open and before merging, but at any time on request.
  Use when asked to "test things locally", "make sure this works", "verify the
  branch", or "run an E2E check" on people code. Covers people-api (uvicorn +
  real DB) and the event handlers; posts results to the open PR when one exists.
metadata:
  depends-on: "verify, running-ci-checks, testing-people-handlers, pr-post"
---

# Verifying a PR Locally

Verification is runtime observation, not a test-suite rerun. Invoke the `verify`
skill first for the discipline (surface, probes, evidence); this skill supplies
the people-specific handles.

## 1. Green the static CI first

CI runs **more than `ruff check`** — `ruff format --check` and `mypy` bite most
often. Run all of it before any runtime work (**REQUIRED:** use
`running-ci-checks` for the discipline):

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
.venv/bin/pytest tests/ -q
```

## 2. Build the runtime plan from the diff

```bash
git diff main...HEAD --stat
```

| Changed | Surface | Handle |
|---|---|---|
| `api/`, `services/`, `repo/` reached by the API | HTTP on local uvicorn | §3 |
| event handling (`handlers/`, `main.py` `process`/`sync`, `services/ingest\|eligibility\|google_contacts_sync\|hubspot_mirror\|person_edit`) | invoke the handler directly against the real DB — `testing-people-handlers` | — |
| `terraform/`, `.github/workflows/` | no local surface — say so; suggest `terraform-plan` / post-merge log check | — |

Plan = happy-path check per changed behavior **plus adversarial probes** the
diff points at (automated sender, already-eligible sender, cap already full,
Google 404 on a stale `resourceName`, malformed event dict, auth on/off).
Write it down before running.

## 3. Local API handle

```bash
scripts/fetch-env.sh   # only if .env is missing (Secret Manager + terraform.tfvars)
export DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib   # macOS: libpq is keg-unlinked
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8125 --log-level warning)
```
(cleanup with `pkill -f "uvicorn api.main:app --port 812"`)

- **Background WITHOUT `nohup`.** `nohup` is SIP-protected, so macOS strips
  `DYLD_LIBRARY_PATH` before it execs uvicorn → psycopg can't find libpq and
  the app fails at import. Use the harness's own backgrounding (or `setsid`)
  with `DYLD_LIBRARY_PATH` exported in the same shell.
- Smoke handle: `.venv/bin/python scripts/test-api-local.py --base http://localhost:8125`
  (health → `GET /people?recent=1`).
- `GET /people/{email}`, `POST /search`, `GET /people?recent=` all read the
  real `people` DB via `CLOUD_SQL_CONNECTION_NAME` in `.env` — no writes.
- **`PATCH /people/{email}` writes through to Google Contacts** (biography or
  group membership) before updating the DB — any change touching
  `services/person_edit.py` needs a real PATCH against a real linked contact
  to prove it, not just the mocked unit test.
- **Auth is a no-op locally** — `.env` from `fetch-env.sh` sets
  `PEOPLE_API_TOKEN`, so requests need `Authorization: Bearer $PEOPLE_API_TOKEN`;
  unsetting the env var before starting uvicorn disables the check entirely
  (off Cloud Run, `verify_token` allows all when the token is unset — it only
  fails closed with 503 when `K_SERVICE` is set).
- Health path is `/health` (**not** `/healthz` — GFE reserves that on Cloud Run).
- Live API for comparison: `terraform output -raw people_api_url` (no custom
  domain is mapped yet).

## 4. Local event-handler handle

```bash
(set -a; source .env; set +a; .venv/bin/python -c "
from handlers import email_classified
email_classified.handle({...})
")
```

See `testing-people-handlers` for payload shapes and the gotcha that Google
Contacts writes have no `HUBSPOT_WRITES_ENABLED`-style off switch — use an
automated-looking sender if you don't want a real Google Contact created.

## 5. Execute, fix, re-run

Run every planned check; capture real response bodies / status codes / row
values as evidence. On failure: fix the code, commit to the PR branch
(explicit `git add <files>` only — the tree often carries unrelated dirty
files), re-run the failed check plus its neighbors, re-green §1, and
`git push`.

## 6. Report

Compose a verdict (PASS/FAIL) + a table of checks (happy path and probes
separately) with observed results, plus notes for anything that made you
pause.

If an open PR exists (`gh pr view --json state,number`): post the report as a
PR comment via the **pr-post** skill (read `~/.claude/skills/pr-post/SKILL.md`
and apply its steps, including the security scrub — this repo is public, so
double-check nothing personal — a real email address, a real name — ends up
in the posted comment). If no PR exists, report inline and say posting was
skipped.

Always kill the uvicorn server when done.
