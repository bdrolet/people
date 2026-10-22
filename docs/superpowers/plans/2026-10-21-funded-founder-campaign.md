# Funded-Founder Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly, hand-run campaign that records digital health companies that just raised, scores them, finds and verifies the technical buyer's email, drafts and fact-checks the Production-Readiness Audit offer, puts the batch on one approval page, sends what Ben ticks, and tracks replies, opt-outs, bounces and one follow-up.

**Architecture:** A second pipeline beside the referral tool, same layering: `repo/campaign.py` is the only SQL, `services/campaign*.py` own the status machine, verification, drafting, fact check, review page, sending, follow-ups and report; `scripts/campaign.py` is the CLI; three event handlers in `handlers/` consume inbox's `email_sent`, `email_classified` and the new `email_bounced`. A Claude Code skill does the research and feeds the CLI. Every status change is an append-only `campaign_events` row, and every count derives from those rows.

**Tech Stack:** Python 3.13, Postgres via `clients/db.py`, `httpx` for Apollo, Hunter and inbox-api, `dnspython` for the deliverability preflight, `claude -p` for the fact check, stdlib `http.server` for the approval page, pytest with the repo's `FakeConn` conventions.

**Spec:** `~/src/consulting/docs/superpowers/specs/2026-10-21-funded-founder-campaign-design.md`. The inbox half (reply recipients, `email_bounced`) is `~/src/inbox/docs/superpowers/plans/2026-10-21-reply-recipients-and-bounce-events.md` and ships first.

## Global Constraints

- Layer rules in `CLAUDE.md`: `clients/` I/O only; `repo/` takes an open connection, never commits; `services/` own one concern and leave committing to the caller; `handlers/` orchestrate; `models/` pure.
- `services/campaign.py` is the only writer of `campaign_*` status data. Handlers and the CLI call it, never `repo/campaign.py`.
- Cloud Functions never talk to Microsoft Graph, Apollo or Hunter. Those clients are CLI-only, like `clients/inbox_api.py`.
- No address is ever drafted unless `verification = 'valid'`. No email ever goes out without `CAMPAIGN_POSTAL_ADDRESS` set and the SPF/DKIM/DMARC preflight passing.
- A company domain is unique in `campaign_companies`; a second add is a no-op that reports the existing row.
- Anyone with a `people` row showing two-way history (`message_count > 0 and my_response_count > 0`), or any row in `outreach_contacts`, is `skipped_warm` and never emailed.
- Sends: at most `per_day` (20) per calendar day across all batches, from `CAMPAIGN_FROM_ADDRESS`, which defaults to the first entry of `OUTREACH_FROM_ADDRESSES`.
- Email copy in §8 of the spec is exact text; the only generated parts are the subject and the opening sentence. The subject never contains "free" and is at most 60 characters.
- Opt-out phrases are matched on every inbound email from a campaign address, including ones inbox filed `ignore`.
- Private numbers (threshold, slots, per-day cap, postal address) live in `~/src/consulting/docs/campaign_targets.toml` and `.env`, never in this public repo.
- Feature branch, `/pr-open`, and `ruff check . && ruff format --check . && pytest -q` green before every commit.

## Review Focus

1. A company added twice in one run (two announcements of the same round) must leave one row and one contact set. Pinned in Task 3.
2. A reply from a colleague at the company (same domain, different address) must mark the contact `replied`, not create a new campaign contact. Pinned in Task 10.
3. A one-word "No" reply that inbox classifies `ignore` must still opt the company out. Pinned in Task 10.
4. Twenty-one approved contacts on one day must send twenty and leave one `approved`, and the next day's `send` must send exactly that one. Pinned in Task 9.
5. A follow-up whose original `graph_message_id` is unknown and whose search finds nothing must still go out as a fresh `Re:` message and record the fallback. Pinned in Task 11.

---

### Task 1: Schema and models

**Files:**
- Modify: `repo/schema.sql` (append after the outreach working-queue block), `models/events.py`
- Create: `models/campaign.py`
- Test: `tests/test_schema.py`, `tests/test_models_campaign.py`

**Interfaces:**
- Produces, in `repo/schema.sql`: tables `campaign_runs`, `campaign_companies`, `campaign_contacts`, `campaign_events` with the columns in spec §7.1, plus `ALTER TABLE people ADD COLUMN IF NOT EXISTS email_bounced_at TIMESTAMPTZ`. CHECK constraints on `round IN ('seed','a','b','other')`, company `status IN ('new','below_bar','warm','active','opted_out','closed')`, contact `status IN ('candidate','unverified','skipped_warm','drafted','approved','skipped','sent','followed_up','replied','opted_out','bounced','booked')`, `verification IN ('valid','accept_all','unknown','invalid')`, `email_source IN ('apollo','hunter','manual')`, event `source IN ('cli','event','page')`. `campaign_events(id, contact_id NULL, company_id NULL, status, at, source, graph_message_id, note, created_at)` with `CHECK (contact_id IS NOT NULL OR company_id IS NOT NULL)`. Unique index on `lower(campaign_companies.domain)`; unique on `(company_id, lower(email))` for contacts. Indexes on `campaign_contacts (status, follow_up_on)` and `campaign_events (status, at)`.
- Produces, in `models/campaign.py`: `ROUNDS`, `COMPANY_STATUSES`, `CONTACT_STATUSES`, `VERIFICATIONS`, `EMAIL_SOURCES`, `EVENT_SOURCES` tuples matching the CHECKs; `OPT_OUT_PHRASES = ("no", "unsubscribe", "remove", "not interested", "no thanks", "please remove me")`; `@dataclass class Advance: row: dict; changed: bool; event_id: int | None`; `@dataclass class RunCounts` with one int per contact status plus `companies_found`, `above_bar`.
- Produces, in `models/events.py`: `EmailBouncedEvent` TypedDict mirroring spec §10.2 exactly.

- [ ] **Step 1: Write the failing tests.** In `tests/test_schema.py` (runs only with `TEST_DATABASE_URL`): apply the schema twice (idempotence), insert a run, a company, a contact and an event, assert the domain unique index rejects `Acme.com` after `acme.com`, assert `people.email_bounced_at` exists. In `tests/test_models_campaign.py`: the tuples in `models/campaign.py` equal the sets named in the CHECK constraints (parse `repo/schema.sql` with a regex per table, the way `tests/test_repo_outreach.py` reads SQL text).

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL=postgresql://localhost/people_schema_test pytest tests/test_schema.py tests/test_models_campaign.py -v` (create the database first with `createdb people_schema_test`; without Postgres, the schema tests skip and the models test must still fail on import).
Expected: FAIL with `ModuleNotFoundError: models.campaign`.

- [ ] **Step 3: Write the schema block, `models/campaign.py` and `EmailBouncedEvent`.** Follow the comment style of the outreach block: say which service is the only writer and that `campaign_events` is append-only.

- [ ] **Step 4: Run the tests to verify they pass**

Run: same command as Step 2.
Expected: PASS (or skip for schema without a database, with the models test passing).

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql models/campaign.py models/events.py tests/test_schema.py tests/test_models_campaign.py
git commit -m "feat(campaign): schema and models for the funded-founder campaign"
```

### Task 2: Repo layer

**Files:**
- Create: `repo/campaign.py`, `tests/test_repo_campaign.py`

**Interfaces:**
- Produces (all take `conn` first, return dict rows or lists, never commit):
  - `insert_run(conn, *, ran_at, lookback_from, lookback_to) -> dict`, `latest_run(conn) -> dict | None`, `update_run_counts(conn, run_id, counts: dict) -> None`
  - `insert_company_if_missing(conn, fields: dict) -> dict | None` (None when the lower-cased domain exists), `get_company(conn, company_id) -> dict | None`, `get_company_by_domain(conn, domain) -> dict | None`, `update_company(conn, company_id, changes: dict) -> dict` (allow-list: `fit_score, fit_notes, status, announcement_excerpt, product_summary, notes`), `companies(conn, *, status=None, run_id=None) -> list[dict]`
  - `insert_contact_if_missing(conn, fields: dict) -> dict | None`, `get_contact(conn, contact_id)`, `get_contact_by_email(conn, email)`, `contacts_by_domain(conn, domain) -> list[dict]`, `update_contact(conn, contact_id, changes: dict) -> dict` (allow-list: every mutable column in §7.1), `contacts(conn, *, status=None, run_id=None, follow_up_due_on: date | None = None) -> list[dict]`, `lock_contact(conn, contact_id) -> dict` (`FOR UPDATE`)
  - `insert_event(conn, *, contact_id, company_id, status, at, source, graph_message_id=None, note=None) -> int`
  - `sent_today(conn, on: date, tz: str) -> int` (count of `sent` events whose `at` falls on `on` in `tz`)
  - `counts_by_week(conn, since: date | None, until: date | None) -> list[dict]` (one row per ISO week, one column per contact status plus `companies_found`, `above_bar`, from `campaign_events` and `campaign_companies`)
  - `set_people_bounced(conn, email: str, at: datetime) -> None`

- [ ] **Step 1: Write the failing tests** with `FakeConn`/`FakeCursor` exactly as in `tests/test_repo_outreach.py`: `update_contact` rejects a column outside the allow-list with `ValueError`; `lock_contact` SQL ends with `FOR UPDATE`; `insert_company_if_missing` uses `ON CONFLICT ... DO NOTHING` and returns None on no row; `sent_today` casts `at AT TIME ZONE %s` to date; `insert_event` returns the `RETURNING id` value.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_repo_campaign.py -v`
Expected: FAIL with `ModuleNotFoundError: repo.campaign`.

- [ ] **Step 3: Implement `repo/campaign.py`** with the `_set_clause` allow-list helper pattern from `repo/outreach.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_repo_campaign.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add repo/campaign.py tests/test_repo_campaign.py
git commit -m "feat(campaign): repo layer"
```

### Task 3: Status machine and warm-contact guard

**Files:**
- Create: `services/campaign.py`, `tests/test_campaign.py`

**Interfaces:**
- Consumes: Task 2's repo functions; `repo.people.get`, `repo.people.get_by_linkedin_url`; `repo.outreach.get_contact` (existing, by person id).
- Produces:
  - `TZ = ZoneInfo("America/New_York")`
  - `_ALLOWED` transitions, verbatim: `candidate → {unverified, skipped_warm, drafted, skipped}`, `unverified → {drafted, skipped}` (after a re-verify), `drafted → {approved, skipped}`, `approved → {sent, drafted, skipped}` (`drafted` only via the stale rule), `sent → {followed_up, replied, opted_out, bounced, booked}`, `followed_up → {replied, opted_out, bounced, booked}`, `replied → {opted_out, booked}`, terminal: `skipped_warm, skipped, opted_out, bounced, booked`.
  - `class InvalidTransition(ValueError)`, `class NotFound(LookupError)`
  - `start_run(conn, *, now: datetime, targets) -> dict`: lookback from the latest run's `ran_at`, or `now - lookback_days_first_run` when none.
  - `add_company(conn, fields: dict, *, run_id: int) -> tuple[dict, bool]`: lower-cases the domain, inserts if missing, returns `(row, created)`; a non-US `hq_country` inserts with `status='below_bar'` and `fit_notes='non-US'`.
  - `score_company(conn, company_id, *, score: int, notes: str, threshold: int) -> dict`: sets `fit_score`, `fit_notes`, and `status` to `active` when `score >= threshold` else `below_bar`; appends a company event.
  - `promote_company(conn, company_id) -> dict`: `below_bar → active` by hand.
  - `add_contact(conn, fields: dict, *, run_id: int) -> tuple[dict, str]`: applies the warm guard before inserting. Returns `(row, "candidate" | "skipped_warm" | "existing")`. Warm when `people.get(email)` has `message_count > 0 and my_response_count > 0`, or `people.get_by_linkedin_url(url)` resolves to a person with a row in `outreach_contacts`, or the email's domain matches any `people` row that is in `outreach_contacts`. A warm contact also sets the company `status='warm'`.
  - `advance(conn, contact_id, status, *, at, source, graph_message_id=None, note=None) -> Advance`: locks the row, no-ops when already at or past the status, raises `InvalidTransition` otherwise, writes the status-specific columns (`sent_at`, `follow_up_on`, `replied_at`), appends an event.
  - `follow_up_date(sent_at: datetime, business_days: int) -> date`: pure; skips Saturday and Sunday in `TZ`.
  - `opt_out_company(conn, company_id, *, at, source, note) -> int`: marks every non-terminal contact at the company `opted_out` and the company `opted_out`; returns how many contacts changed.
  - `is_opt_out(body: str) -> bool`: pure; true when the first non-empty line, stripped of punctuation and lower-cased, equals one of `OPT_OUT_PHRASES` or starts with one of them followed by a space or punctuation.

- [ ] **Step 1: Write the failing tests** (FakeConn with scripted results, the way `tests/test_outreach.py` does): transition table is enforced (`drafted → sent` raises); `advance` on a row already `sent` requesting `sent` returns `changed=False` with no event; `add_company` twice with `acme.com` then `Acme.com` returns `created=False` the second time; `add_contact` with a warm person returns `"skipped_warm"` and sets the company warm; `follow_up_date(Friday 2026-10-23 15:00 ET, 5) == date(2026, 10, 30)`; `is_opt_out("No.\n\nSent from my phone")` is True; `is_opt_out("No problem, send me the intake")` is False; `is_opt_out("Not interested, thanks")` is True.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign.py -v`
Expected: FAIL with `ModuleNotFoundError: services.campaign`.

- [ ] **Step 3: Implement `services/campaign.py`.**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_campaign.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/campaign.py tests/test_campaign.py
git commit -m "feat(campaign): status machine, warm guard and opt-out rules"
```

### Task 4: Targets config

**Files:**
- Create: `services/campaign_targets.py`, `tests/test_campaign_targets.py`, `~/src/consulting/docs/campaign_targets.toml` (committed in the consulting repo)
- Modify: `scripts/fetch-env.sh` (append `CAMPAIGN_TARGETS=` line like `OUTREACH_TARGETS`)

**Interfaces:**
- Produces: `@dataclass(frozen=True) class CampaignTargets: threshold: int; slots: int; slots_through: str; per_day: int; follow_up_business_days: int; lookback_days_first_run: int; stale_after_days: int` and `load_targets(path: Path) -> CampaignTargets` raising `TargetsError` on a missing table or key, mirroring `services/outreach_targets.py`. The TOML is spec §7.3 plus `stale_after_days = 7` under `[sending]`.

- [ ] **Step 1: Write the failing tests**: a complete file loads with the spec's values (`threshold 6, slots 5, slots_through "this fall", per_day 20, follow_up_business_days 5, lookback_days_first_run 30, stale_after_days 7`); a file missing `[offer]` raises `TargetsError` naming the table; `per_day = 0` raises.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign_targets.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement** the loader, write the TOML in the consulting repo with those values, and add the `fetch-env.sh` line.

- [ ] **Step 4: Run the tests to verify they pass**, then `bash scripts/fetch-env.sh && grep CAMPAIGN_TARGETS .env`.
Expected: PASS; the env line points at the consulting file.

- [ ] **Step 5: Commit** (people repo), and commit the TOML in `~/src/consulting` with message `Add campaign_targets.toml for the funded-founder campaign`.

```bash
git add services/campaign_targets.py tests/test_campaign_targets.py scripts/fetch-env.sh
git commit -m "feat(campaign): targets loader and env wiring"
```

### Task 5: Apollo and Hunter clients, verification service

**Files:**
- Create: `clients/apollo.py`, `clients/hunter.py`, `services/campaign_verify.py`, `tests/test_apollo.py`, `tests/test_hunter.py`, `tests/test_campaign_verify.py`
- Modify: `scripts/fetch-env.sh` (`APOLLO_API_KEY`, `HUNTER_API_KEY` from Secret Manager), `terraform/secrets.tf` (declare `apollo-api-key` and `hunter-api-key` secret resources only, versions added by hand per the `adding-people-secret` skill's refresh-token pattern; no CF wiring, these are CLI-only)

**Interfaces:**
- Produces, `clients/apollo.py`: `class ApolloUnavailable(RuntimeError)`; `people_search(domain: str, titles: list[str]) -> list[dict]` (`POST https://api.apollo.io/api/v1/mixed_people/search` with `q_organization_domains`, `person_titles`, header `x-api-key`; returns `[{"name", "title", "linkedin_url", "email", "email_status"}]`); `enrich(linkedin_url: str | None, email: str | None) -> dict | None` (`/api/v1/people/match`). Timeout 15 s. Raises `ApolloUnavailable` when the key is unset.
- Produces, `clients/hunter.py`: `class HunterUnavailable(RuntimeError)`; `domain_pattern(domain: str) -> str | None` (`GET /v2/domain-search`, returns `data.pattern` like `{first}`); `find(domain: str, first_name: str, last_name: str) -> dict | None` (`/v2/email-finder`, returns `{"email", "score"}`); `verify(email: str) -> str` (`/v2/email-verifier`, returns `data.status` mapped: `valid → "valid"`, `accept_all → "accept_all"`, `invalid|disposable|webmail → "invalid"`, anything else `"unknown"`). Timeout 15 s.
- Produces, `services/campaign_verify.py`: `resolve_email(contact: dict, company: dict, *, apollo, hunter) -> tuple[str | None, str, str | None]` returning `(email, verification, source)`: Apollo email first, else Hunter `find`, else `domain_pattern` applied to the name; every candidate goes through `hunter.verify`; no candidate returns `(None, "unknown", None)`. `apollo` and `hunter` are the client modules, injected for tests.

- [ ] **Step 1: Write the failing tests.** Clients: with `httpx.MockTransport`, assert the URL, header and parsed fields, and the status mapping (`webmail → "invalid"`, `unknown → "unknown"`). Service: Apollo email present and Hunter says `valid` → `("cto@acme.com", "valid", "apollo")`; Apollo empty, Hunter finder returns an address that verifies `accept_all` → `(addr, "accept_all", "hunter")`; nothing found → `(None, "unknown", None)`; Apollo email that verifies `invalid` falls through to Hunter rather than returning the invalid address.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_apollo.py tests/test_hunter.py tests/test_campaign_verify.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement** the three modules, the two `fetch-env.sh` lines, and the two secret resources.

- [ ] **Step 4: Run the tests to verify they pass**, then create the two secrets by hand once (`gcloud secrets versions add apollo-api-key --data-file=-` and the Hunter one) after `/terraform-plan` and `/terraform-apply` for the resources.

- [ ] **Step 5: Commit**

```bash
git add clients/apollo.py clients/hunter.py services/campaign_verify.py tests/test_apollo.py tests/test_hunter.py tests/test_campaign_verify.py scripts/fetch-env.sh terraform/secrets.tf
git commit -m "feat(campaign): Apollo and Hunter clients with email verification"
```

### Task 6: Email rendering

**Files:**
- Create: `services/campaign_draft.py`, `tests/test_campaign_draft.py`

**Interfaces:**
- Consumes: `CampaignTargets` (Task 4).
- Produces:
  - `BODY_TEMPLATE`, `FOLLOW_UP_TEMPLATE`, `SIGNATURE` as module constants holding spec §8's text verbatim, with `{first_name}`, `{opening_sentence}`, `{slots}`, `{slots_through}`, `{postal_address}` placeholders.
  - `DEFAULT_SUBJECT = "Production-readiness audit for {company}"`
  - `render_email(*, first_name: str, opening_sentence: str, company_name: str, subject: str | None, targets: CampaignTargets, postal_address: str) -> tuple[str, str]` returning `(subject, body)`; uses `DEFAULT_SUBJECT` when `subject` is None or fails `valid_subject`.
  - `valid_subject(subject: str, company_name: str) -> bool`: at most 60 characters, no `!`, does not contain "free" (case-insensitive), contains the company name.
  - `render_follow_up(*, first_name: str) -> str`.
  - `GENERIC_OPENER = "Congratulations on the round."`

- [ ] **Step 1: Write the failing tests**: the rendered body for `first_name="Sam"`, opener "Congratulations on the Series A last week.", `slots=5`, `slots_through="this fall"`, postal "123 Main St, San Francisco, CA 94110" equals the spec §8 text with those substitutions (assert the full string, built in the test from the spec); `valid_subject("Free audit for Acme", "Acme")` is False; a 61-character subject is False; `render_email(subject=None, ...)` yields `"Production-readiness audit for Acme"`; the follow-up equals spec §8's nudge with "Sam".

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign_draft.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement** with `str.format`-free substitution (`.replace` per placeholder, so a company name containing braces cannot break rendering).

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add services/campaign_draft.py tests/test_campaign_draft.py
git commit -m "feat(campaign): render the audit email and follow-up"
```

### Task 7: Fact check through `claude -p`

**Files:**
- Create: `clients/claude_cli.py`, `services/campaign_factcheck.py`, `tests/test_claude_cli.py`, `tests/test_campaign_factcheck.py`

**Interfaces:**
- Produces, `clients/claude_cli.py`: `Runner = Callable[[list[str], str], subprocess.CompletedProcess]`; `prompt(text: str, *, model: str | None = None, runner: Runner = _run, timeout_s: int = 120) -> str` running `["claude", "-p", "--output-format", "text", *(["--model", model] if model else [])]` with `text` on stdin, returning stdout; raises `ClaudeCliError` on non-zero exit or timeout. No session, no system prompt append: the prompt is the only context.
- Produces, `services/campaign_factcheck.py`: `@dataclass(frozen=True) class FactCheck: ok: bool; unsupported: list[str]; raw: str`; `check(opening_sentence: str, excerpt: str, *, prompt_fn=claude_cli.prompt) -> FactCheck`. The prompt asks for a JSON object `{"supported": bool, "unsupported_claims": [...]}` and nothing else, gives the excerpt and the sentence, and says that a claim is supported only if the excerpt states it. `check` parses the first `{...}` in the reply; unparseable output is `FactCheck(ok=False, unsupported=["unparseable"], raw)`.
  - `PROMPT_VERSION = "2026-10-21"` recorded in the event note when a draft is regenerated.

- [ ] **Step 1: Write the failing tests**: `prompt()` builds the argv and passes the text on stdin; non-zero exit raises `ClaudeCliError`; `check` with a fake `prompt_fn` returning `{"supported": false, "unsupported_claims": ["Series B"]}` gives `ok=False, unsupported=["Series B"]`; fake returning prose with an embedded JSON object still parses; fake returning nothing parseable gives `unsupported == ["unparseable"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_claude_cli.py tests/test_campaign_factcheck.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement** both modules.

- [ ] **Step 4: Run the tests to verify they pass**, then one live call: `python -c "from services import campaign_factcheck as f; print(f.check('Congratulations on the Series B.', 'Acme today announced a $12M Series A led by ...'))"` and confirm `ok=False` with "Series B" in `unsupported`.

- [ ] **Step 5: Commit**

```bash
git add clients/claude_cli.py services/campaign_factcheck.py tests/test_claude_cli.py tests/test_campaign_factcheck.py
git commit -m "feat(campaign): fact-check openers with an isolated claude -p call"
```

### Task 8: Approval page

**Files:**
- Create: `services/campaign_review.py`, `tests/test_campaign_review.py`

**Interfaces:**
- Consumes: Task 3's `advance`, Task 2's `update_contact`.
- Produces:
  - `render_page(*, drafted: list[dict], parked: dict[str, list[dict]], key: str) -> str`: one self-contained HTML document (inline CSS, no external resources). `drafted` rows carry `contact`, `company`; each renders company name, round, product summary, fit score and notes, buyer name and title linked to `linkedin_url`, the verified email, a checkbox named `approve_<contact_id>` checked by default, a `<textarea name="body_<contact_id>">` with the body, and badges "generic opener" when `opener_generic` and "fact check regenerated" when the latest event note says so. `parked` groups `unverified`, `skipped_warm`, `below_bar` under headings with a one-line reason each and no checkbox. One form, `POST /approve?key=<key>`, one button "Send ticked". A row with its checkbox unticked posts as `skip`.
  - `parse_approvals(form: dict[str, list[str]], drafted_ids: list[int]) -> list[tuple[int, str, str | None]]`: `(contact_id, "approved" | "skipped", edited_body_or_None)`; a body that equals the original is None.
  - `serve(*, html: str, key: str, drafted_ids: list[int], on_submit: Callable[[list[tuple[int, str, str | None]]], None], idle_timeout_s: int = 1800, open_browser: bool = True) -> None`: binds `127.0.0.1` on a free port, rejects any request whose `key` query parameter differs (403), serves the page on `GET /`, calls `on_submit` once on a valid `POST /approve`, responds with a short "Sent N, skipped M. You can close this tab." page, and exits; exits without calling `on_submit` after `idle_timeout_s`. Prints the URL with the key.

- [ ] **Step 1: Write the failing tests**: `render_page` output contains one `approve_<id>` checkbox per drafted row and none for parked rows, the badge text when flagged, and no `http://` or `https://` other than LinkedIn links; `parse_approvals` with `approve_7` present and `body_7` unchanged → `(7, "approved", None)`; `approve_8` absent → `(8, "skipped", None)`; edited body → the new text. For `serve`: start it in a thread with `open_browser=False`, GET with the wrong key → 403, POST with the right key → `on_submit` called once and the thread exits.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign_review.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement** with `http.server.ThreadingHTTPServer` and `urllib.parse.parse_qs`; the key is `secrets.token_hex(16)`.

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add services/campaign_review.py tests/test_campaign_review.py
git commit -m "feat(campaign): local approval page"
```

### Task 9: Sending, preflight and the people row

**Files:**
- Create: `services/campaign_send.py`, `tests/test_campaign_send.py`
- Modify: `clients/inbox_api.py` (add `send_draft`, `reply`, `search_sent`), `tests/test_inbox_api.py`, `requirements.txt` (`dnspython>=2.6`)

**Interfaces:**
- Consumes: Tasks 2 to 4; `services.person_create.create(conn, contact=..., labels=["consulting-campaign"])`; inbox-api's `POST /emails/drafts`, `POST /emails/drafts/{id}/send` (returns `id` after the inbox plan), `POST /emails/{id}/reply` with `to`.
- Produces, `clients/inbox_api.py`: `send_draft(draft_id: str, *, from_address: str | None) -> dict`; `reply(message_id: str, *, body: str, to: list[str], from_address: str | None) -> dict`; `search_sent(*, to: str, subject: str) -> list[dict]` against inbox-api's search router (the builder reads `api/routers/search.py` in inbox for the query shape; it returns `graph_message_id`-bearing hits).
- Produces, `services/campaign_send.py`:
  - `class PreflightError(RuntimeError)`
  - `preflight(domain: str, *, postal_address: str | None, inbox_reachable: Callable[[], bool], resolver=dns.resolver) -> None`: raises `PreflightError` naming the first failure: no `v=spf1` TXT at the domain, no TXT at `_dmarc.<domain>` containing `v=DMARC1`, no DKIM TXT at either `selector1._domainkey.<domain>` or `selector2._domainkey.<domain>` (Microsoft 365's selectors; the builder may add `CAMPAIGN_DKIM_SELECTORS` env to widen), empty `postal_address`, or `inbox_reachable()` false.
  - `restage_stale(conn, *, now: datetime, stale_after_days: int) -> int`: `approved` contacts whose approval event is older than `stale_after_days` go back to `drafted` with note `stale`.
  - `send_batch(conn, *, now: datetime, targets: CampaignTargets, from_address: str, postal_address: str, inbox=inbox_api, pause_s: float = 3.0) -> SendResult` with `@dataclass SendResult: sent: int; remaining: int; halted: bool`: takes `approved` contacts oldest first, stops at `targets.per_day - repo.campaign.sent_today(...)`, for each: ensures the `people` row exists (create through `person_create.create` with name, email, company, job title, `linkedin_url`, label `consulting-campaign`; an existing row is linked, not duplicated), creates the draft from `from_address`, sends it, advances to `sent` with `draft_id`, `sent_at=now`, `follow_up_on=follow_up_date(now, targets.follow_up_business_days)` and the returned message id as `graph_message_id` when present, commits after each send, sleeps `pause_s` between sends, and finally writes the run's `counts` (one int per contact status, via `repo.campaign.update_run_counts`). `halted` is True when the run's `bounced` count is above two (checked before each send) and nothing more is sent.

- [ ] **Step 1: Write the failing tests**: preflight passes with all four records present and fails naming `dmarc` when `_dmarc` is missing; `restage_stale` moves an 8-day-old approval back and leaves a 6-day-old one; `send_batch` with 21 approved and `sent_today=0` sends 20 and reports `remaining=1`, and a second call with `sent_today=20` sends 0; a second call next day with `sent_today=0` sends exactly the one left; `person_create.create` is called with the label and not called when `person_id` is already set; a draft that fails to send leaves the contact `approved` and continues with the next; three prior bounces → `halted=True`, zero sent.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign_send.py tests/test_inbox_api.py -v`
Expected: FAIL with `ModuleNotFoundError` and `AttributeError: send_draft`.

- [ ] **Step 3: Implement** the client functions and the service; `pip install dnspython` into `.venv` and pin it in `requirements.txt`.

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add services/campaign_send.py clients/inbox_api.py tests/test_campaign_send.py tests/test_inbox_api.py requirements.txt
git commit -m "feat(campaign): preflight, batch sending and people rows"
```

### Task 10: Event handlers for sent, replied, opted out and bounced

**Files:**
- Create: `handlers/email_bounced.py`
- Modify: `handlers/email_sent.py`, `handlers/email_classified.py`, `main.py` (route `email_bounced`), `tests/test_handlers.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: Task 3's `advance`, `opt_out_company`, `is_opt_out`; Task 2's `get_contact_by_email`, `contacts_by_domain`, `set_people_bounced`; `EmailBouncedEvent`.
- Produces, in `services/campaign.py` (added here, still the only writer):
  - `confirm_sent_from_event(conn, *, to: list[str], from_address: str, graph_message_id: str, at: datetime) -> int`: for each To address with a `sent` or `approved` contact and `from_address` equal to the campaign sender, sets `graph_message_id` when missing (and advances `approved → sent` if the CLI crashed between send and record); returns the count touched.
  - `record_inbound_from_event(conn, *, sender: str, body: str, category: str, graph_message_id: str, at: datetime) -> str | None`: finds a contact by exact email, else any `sent`/`followed_up` contact at the sender's domain; if `is_opt_out(body)` → `opt_out_company(...)` and returns `"opted_out"`; else if `category != "ignore"` → advance to `replied` and return `"replied"`; else returns None. A sender with no campaign contact returns None without writing.
  - `record_bounce_from_event(conn, *, failed_recipients: list[str], graph_message_id: str, at: datetime) -> int`: each matching contact → `bounced`, `set_people_bounced(email, at)`, returns the count.
- Produces, `handlers/email_bounced.py`: `handle(event: dict) -> None` opening a connection, calling `record_bounce_from_event`, committing, logging the count, counting `otel.events_received` with `{"event": "email_bounced"}`.
- `handlers/email_sent.py` calls `confirm_sent_from_event` inside the durable transaction, after `_record_outreach`. `handlers/email_classified.py` calls `record_inbound_from_event` inside the transaction, after the referral `replied` block, and does not gate it on `category` or `automated`.
- `main.py` routes `case "email_bounced": email_bounced.handle(data)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_handlers.py` (monkeypatch the three service functions like the existing `outreach.advance_from_event` stubs) and `tests/test_main.py` (`test_process_routes_bounced`); and in `tests/test_campaign.py` for the three service functions with FakeConn: a colleague at `acme.com` replying to a `sent` contact `cto@acme.com` → `"replied"` on that contact; body "No" with category `ignore` → `"opted_out"` and the company opted out; unknown sender → None and no event; two failed recipients, one known → returns 1 and `set_people_bounced` called once.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_handlers.py tests/test_main.py tests/test_campaign.py -v`
Expected: FAIL on the missing functions and route.

- [ ] **Step 3: Implement** the service functions, the handler, the two handler call sites and the route. Update `main.py`'s docstring and `CLAUDE.md`'s Events CF row to list `email_bounced`.

- [ ] **Step 4: Run the tests to verify they pass**, then `ruff check . && pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add services/campaign.py handlers/email_bounced.py handlers/email_sent.py handlers/email_classified.py main.py tests/test_handlers.py tests/test_main.py tests/test_campaign.py CLAUDE.md
git commit -m "feat(campaign): sent, reply, opt-out and bounce handling from inbox events"
```

### Task 11: Follow-ups

**Files:**
- Create: `services/campaign_followups.py`, `tests/test_campaign_followups.py`

**Interfaces:**
- Consumes: Task 9's `inbox_api.reply`, `inbox_api.search_sent`, `inbox_api.create_draft`/`send_draft`; Task 6's `render_follow_up`; Task 3's `advance`.
- Produces: `run(conn, *, now: datetime, from_address: str, inbox=inbox_api) -> FollowUpResult` with `@dataclass FollowUpResult: sent: int; fallback: int; skipped: int`. Due = `sent` contacts with `follow_up_on <= today (TZ)`. For each: if `graph_message_id` is set → `inbox.reply(graph_message_id, body=render_follow_up(first_name), to=[email], from_address=from_address)`; else `search_sent(to=email, subject=subject)` and use the first hit's id (storing it on the contact); else create and send a fresh draft with subject `"Re: " + subject` and the same body, counting `fallback` and writing note `fallback: fresh Re:` on the event. Advance to `followed_up` after each send, commit per contact. A contact whose reply arrived between selection and send (status no longer `sent` under lock) is `skipped`.

- [ ] **Step 1: Write the failing tests**: due contact with an id → `reply` called with `to=[email]` and the contact advanced; no id, search hit → `reply` called with the hit's id and the contact updated; no id, no hit → `create_draft` with subject `Re: Production-readiness audit for Acme`, `send_draft` called, `fallback == 1`, event note recorded; a contact due tomorrow is not touched.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign_followups.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add services/campaign_followups.py tests/test_campaign_followups.py
git commit -m "feat(campaign): one threaded follow-up after five business days"
```

### Task 12: Report

**Files:**
- Create: `services/campaign_report.py`, `tests/test_campaign_report.py`

**Interfaces:**
- Consumes: Task 2's `counts_by_week`.
- Produces: `report(conn, *, since: date | None, until: date | None) -> list[dict]` and `format_table(rows: list[dict]) -> str`: one line per ISO week, columns in this order: `week, found, above_bar, verified, approved, sent, followed_up, replied, opted_out, bounced, booked`, plus a totals line and a reply rate (`replied / sent`, one decimal, `--` when `sent` is 0).

- [ ] **Step 1: Write the failing tests**: two weeks of fake rows format with the header, aligned columns, a totals line, and `reply rate 12.5%` for 1 reply over 8 sent; zero sent prints `--`.

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add services/campaign_report.py tests/test_campaign_report.py
git commit -m "feat(campaign): weekly report"
```

### Task 13: The CLI

**Files:**
- Create: `scripts/campaign.py`, `tests/test_campaign_cli.py`
- Modify: `.gitignore` (add `.campaign/` for the generated page and run JSON)

**Interfaces:**
- Consumes: every service above; `clients.db.get_conn`; env `CAMPAIGN_TARGETS`, `CAMPAIGN_FROM_ADDRESS` (default first of `OUTREACH_FROM_ADDRESSES`), `CAMPAIGN_POSTAL_ADDRESS`, `INBOX_API_URL`, `APOLLO_API_KEY`, `HUNTER_API_KEY`.
- Produces `python scripts/campaign.py <command>`, same `main_with(argv, get_conn, *, out, err, now) -> int` shape and `USER_ERRORS` handling as `scripts/outreach.py`, with these subcommands and exact behaviours:
  - `run start [--lookback-days N]` → prints the run id and the window; `run show [--json]`.
  - `company add --domain D --name N --round R --announced-on YYYY-MM-DD --source-url U --product "..." --excerpt-file PATH [--amount A] [--country US]` → prints `added <id>` or `exists <id>`.
  - `company score <id> --score S --notes "..."` → prints the resulting status; `company promote <id>`; `company list [--status S] [--json]`.
  - `contact add --company <id> --name "First Last" --title T [--linkedin URL] [--email E]` → runs the warm guard; prints `candidate <id>`, `skipped_warm <id>` or `existing <id>`.
  - `contact verify <id|--all>` → runs `campaign_verify.resolve_email`, stores email, source and verification, advances to `unverified` when not `valid`; prints one line per contact.
  - `contact draft <id> --opener "..." [--subject "..."]` → requires `verification = valid` and an excerpt on the company; runs the fact check; on failure prints the unsupported claims and exits 2 so the skill can retry once with a new opener; `--generic` skips the check and uses `GENERIC_OPENER` with `opener_generic = true`; renders, stores subject and body, advances to `drafted`.
  - `review` → builds the page for every `drafted` contact plus parked rows, serves it, and on submit advances each to `approved` (storing an edited body) or `skipped`, then runs `send` immediately (spec §5.6: one button sends).
  - `send` → `restage_stale`, `preflight`, `send_batch`; prints `sent N, remaining M` and `halted: more than two bounces; run campaign resume` when halted.
  - `resume` → clears the halt for the current run (a run-level `notes` flag) and sends.
  - `followups` → prints `sent N (fallback M, skipped K)`.
  - `bounced <contact-id|email>`, `optout <contact-id|email> [--note]`, `booked <contact-id|email>` → hand advances with `source='cli'`.
  - `report [--since D] [--until D]`.
  - `weekly` → the whole non-research path in order: `followups`, then `review`. Research (`company add`, `score`, `contact add`, `verify`, `draft`) is driven by the skill before `weekly`.

- [ ] **Step 1: Write the failing tests** in the `tests/test_outreach_cli.py` style (fake conn, monkeypatched services, `main_with` with `now`): `company add` twice prints `added` then `exists`; `contact draft` on an `unverified` contact exits 1 with an error naming verification; a failing fact check exits 2 and prints the unsupported claim; `--generic` sets `opener_generic`; `send` without `CAMPAIGN_POSTAL_ADDRESS` exits 1 with the preflight message and sends nothing; `review` calls `serve` and then `send_batch`; `weekly` runs follow-ups before review.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_campaign_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: scripts.campaign`.

- [ ] **Step 3: Implement `scripts/campaign.py`.** Keep each `cmd_*` function under 30 lines by calling the services; the page HTML and the run JSON go under `.campaign/` in the repo root.

- [ ] **Step 4: Run the full suite**

Run: `ruff check . && ruff format --check . && pytest -q`
Expected: all pass.

- [ ] **Step 5: Smoke test against the real database** with `.env` loaded: `run start --lookback-days 30`, `company add` for one real announcement, `company score`, `contact add` for yourself at that company's domain with `--email <your own address>`, `contact verify`, `contact draft --generic`, `review` (untick nothing), and confirm one email arrives in your own inbox from `ben@drolet.ai` with the postal address in the footer. Then `optout <your email>` and confirm the company reads `opted_out`.

- [ ] **Step 6: Commit**

```bash
git add scripts/campaign.py tests/test_campaign_cli.py .gitignore
git commit -m "feat(campaign): campaign CLI"
```

### Task 14: The skill, docs and setup

**Files:**
- Create: `.claude/skills/working-funded-founder-campaign/SKILL.md`
- Modify: `CLAUDE.md` (Stack table row "Funded-founder campaign", code layout entries for the new modules), `README.md` project structure, `scripts/link-skills.sh` if it enumerates skills.

**Interfaces:**
- Produces the weekly research procedure for Claude, as a skill with frontmatter `name: working-funded-founder-campaign`, a `description` that triggers on "run the funded-founder campaign", "find this week's funded digital health companies", "campaign review", "campaign report", "who bounced", and `depends-on: anthropic-skills:chrome-browser, searching-people`. Body sections, in order:
  1. **Setup** (`cd ~/src/people`, `C=".venv/bin/python scripts/campaign.py"`, `scripts/fetch-env.sh` if `CAMPAIGN_TARGETS` is missing).
  2. **Find the rounds**: `run start`; search, in order, Rock Health's weekly funding roundup, Fierce Healthcare funding coverage, MobiHealthNews, Axios Pro Rata, TechCrunch health, then web search for `"digital health" raises ("Series A" OR "Series B" OR seed)` within the window; for each company capture the fields `company add` needs and copy 300 to 1,500 characters of the announcement verbatim into a file under `.campaign/excerpts/<domain>.txt` for `--excerpt-file`. Non-US companies are still added with `--country` so the dedupe remembers them.
  3. **Score** with the spec §5.2 rubric reproduced as a table, reading the site, announcement, careers page and LinkedIn; call `company score` with the points and a one-line note per signal.
  4. **Find the buyer** for `active` companies: Apollo first (`contact add` with what it returns), LinkedIn through Chrome as the fallback; CTO, VP Engineering, Head of Engineering in that order, CEO at seed or when none is listed; at most two per company.
  5. **Verify** with `contact verify --all`.
  6. **Draft**: for each verified contact write one opening sentence that names the round and the product in the announcement's own words, and a subject under 60 characters naming the company; call `contact draft`; on exit 2 rewrite the opener once from the unsupported claims, and on a second failure use `--generic`.
  7. **Review and send**: `weekly`. Tell Ben the URL is open, wait for him to submit, report `sent N, remaining M`, and if `remaining > 0` say to run `campaign send` tomorrow morning.
  8. **Rules**: never invent a fact about a company; never add a contact Ben knows (the guard catches most, but if the people DB shows a conversation, stop and tell Ben); never run `send` or `resume` without Ben saying so after a halt; `bounced`, `optout` and `booked` are recorded only when Ben confirms.
- Setup steps recorded in `CLAUDE.md` under the new row: create the Google contact group `consulting-campaign` once by hand; add `CAMPAIGN_POSTAL_ADDRESS` to `.env` (and the `fetch-env.sh` line reading it from a new `campaign-postal-address` secret); Apollo and Hunter accounts and keys; the inbox plan merged and deployed.

- [ ] **Step 1: Write the skill** per the structure above and update `CLAUDE.md`, `README.md`.

- [ ] **Step 2: Do the one-time setup**: create the contact group, add the postal-address secret and `.env` line, confirm `python scripts/campaign.py send` preflight passes against `drolet.ai` (it prints the three DNS checks).

- [ ] **Step 3: Run the skill end to end for real** with a 30-day lookback on the first run; stop at the approval page and let Ben submit. Record counts from `report` in the PR description.

- [ ] **Step 4: Open the PR** with `/pr-open`. The description lists the new tables, the three handler changes, the CLI, the skill, and the smoke-test results from Task 13 Step 5 and this task's Step 3.
