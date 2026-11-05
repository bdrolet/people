# LinkedIn Send Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every approved LinkedIn message is stored in `linkedin_outbox`, and a launchd-run sender on Ben's Mac delivers queued ones one at a time, spaced and under a ramped daily/weekly ceiling, recording the outreach brief or nudge on a confirmed send.

**Architecture:** Pure rules (window, gap, ceiling with ramp, message validation, MCP result → outcome) live in `services/linkedin_pacing.py` and `services/linkedin_result.py`. `repo/linkedin_outbox.py` holds every SQL statement. `services/linkedin_outbox.py` is the only writer of the outbox tables and owns `run_once` (one sender tick), `queue`, `apply_result`, `record_manual` and the rest; it takes an injected sender so tests fake the MCP. `clients/linkedin_mcp.py` is the stdio MCP client, used only by `scripts/linkedin_sender.py` (the launchd entry). The CLI is `outreach linkedin …`, implemented in `scripts/outreach_linkedin.py` and registered by `scripts/outreach.py`.

**Tech Stack:** Python 3.13, psycopg3 / pg8000, `mcp` Python client (stdio, dev/local requirements only), OTel, launchd.

**Spec:** `docs/superpowers/specs/2026-11-05-linkedin-send-queue-design.md`

## Global Constraints

- Ceiling defaults: `LINKEDIN_DAILY_CAP=50`, `LINKEDIN_WEEKLY_CAP=250`, `LINKEDIN_RAMP_START=30`, `LINKEDIN_RAMP_DAYS=14`.
- Gap defaults: `LINKEDIN_GAP_MIN_SECONDS=180`, `LINKEDIN_GAP_MAX_SECONDS=480`.
- Window default: `LINKEDIN_SEND_WINDOW="Mon-Fri 08:00-19:00 America/Los_Angeles"`; ceiling day and week are counted in America/Los_Angeles (week = Monday–Sunday).
- Pinned MCP: `LINKEDIN_MCP_VERSION` default `4.29.0`; the sender runs `uvx mcp-server-linkedin==<version>`. `.mcp.json` stays on `@latest`.
- A `sending` row older than 15 minutes becomes `check_linkedin`. Three set-asides in a row pause the queue.
- `rate_limited` pauses until `retry_after` + 300 s margin (3600 s when `retry_after` is missing).
- Message text never appears in logs, metrics labels, Asana or stdout except `list --show-text`. No person, username or text in any metric label.
- `mcp` goes in `requirements-dev.txt`, never `requirements.txt`.
- Never call the real LinkedIn MCP with `confirm_send=true` in tests or verification.
- Layer rules: SQL only in `repo/`, decisions in `services/`, I/O in `clients/`, CLIs in `scripts/`. Cloud Functions never import the outbox code.

## Decisions taken here (the design's "my call" / "left to the builder")

- **Names.** Tables `linkedin_outbox`, `linkedin_sender_state` (one row, `id = 1`). Statuses `queued`, `sending`, `sent`, `set_aside`, `check_linkedin`, `cancelled`. Column `mode` ∈ `queued`, `send_now`, `manual`. Column `thread_mismatch BOOLEAN`.
- **Event source:** new value `linkedin_outbox` added to `models.outreach.SOURCES` and to the `outreach_events.source` CHECK (re-created once by a `DO $$` block, like the `nudge` stage migration). Asana sync and `outreach report` never filter on `source`, so LinkedIn briefs count exactly as email briefs do.
- **What counts against the ceiling:** rows with status `sent`, `sending` or `check_linkedin`, by `COALESCE(sent_at, attempted_at)`. An unknown outcome may have gone out, so it counts.
- **Ramp start:** `ramp_started_on` is set (LA date) the first time any outbox row is written. Daily cap on ramp day `d` (0-based) = `start + (cap - start) * min(d, ramp_days) // ramp_days`; weekly cap = `weekly * daily_cap_today // daily`. Before any row exists, `d = 0`.
- **Raised MCP errors** (`isError` result with `_meta["linkedin.dev/error"].code`). Codes that fail before anything is submitted put the row back or set it aside; every other code may have hit mid-send, so the row goes to `check_linkedin` (never resent automatically) and the queue pauses:

  | code | row | queue |
  |---|---|---|
  | `browser_busy` | `queued` | — |
  | `auth_required` | `queued` | hold `login` |
  | `browser_unavailable` | `queued` | hold `browser_unavailable` |
  | `invalid_argument`, `not_found` | `set_aside` | — (counts toward 3-in-a-row) |
  | `rate_limited` | `check_linkedin` | until `retry_after`+300 s |
  | `outcome_unknown` | `check_linkedin` | — |
  | anything else (`account_restricted`, `network`, `page_unreadable`, `internal`, unknown) | `check_linkedin` | hold `<code>` |

  A transport exception from the client (no answer at all) is `outcome_unknown`.
- **Dict results:** `sent is True` → `sent` (thread mismatch flagged when both thread ids are present and differ). Else `retry_safe` not `True` or `status == "outcome_unknown"` → `check_linkedin`. Else `message_unavailable`, `composer_occupied`, `recipient_resolution_failed`, `invalid_message` → `set_aside`. Else (`enter_to_send_enabled`, `confirmation_required`, `composer_unavailable`, `compose_interact_failed`, `send_unavailable`, any new status) → row `queued`, queue hold `<status>`.
- **Send-now result:** `outreach linkedin result <id> --mcp-result <json-or-@file>` runs the same mapping (including pauses); `--status sent|set_aside|check_linkedin` is the manual resolution.
- **Session check:** `get_session_status` → usable iff `result["session"]["stored"] is True`; otherwise hold `login` without calling `send_message`.
- **Launchd:** label `cloud.drolet.people.linkedin-sender`, `StartInterval` 120, installed by `scripts/install-linkedin-sender.sh` (`--uninstall` removes it). Logs to `~/Library/Logs/people-linkedin-sender.log` (no message text is ever logged).
- **CLI home:** `scripts/outreach_linkedin.py` (commands + parser registration), so `scripts/outreach.py` stays readable.

## Review Focus

1. **Crash between claim and result** — a run killed after marking `sending` must never cause a resend: the next run turns a >15-min `sending` row into `check_linkedin`. (Task 4: `test_stale_sending_becomes_check_linkedin`.)
2. **Send-now right at the ceiling** — 49 sent + 1 in-flight `sending` must refuse the next send-now with no row written. (Task 4: `test_send_now_refused_at_ceiling_counts_inflight`.)
3. **Week/day boundaries in Pacific time** — a send at 23:30 PT Sunday counts in the old week, not UTC's Monday. (Task 1: `test_week_bounds_are_pacific`.)
4. **Re-applying a result to a row that already has an event** — `result --status sent` on a row with `outreach_event_id` must not write a second event. (Task 4: `test_apply_sent_twice_writes_one_event`.)
5. **Paused queue with expired `paused_until`** clears itself; a `paused_reason` hold does not. (Task 4: `test_rate_limit_pause_clears_itself`.)

---

### Task 1: Pure pacing rules

**Files:**
- Create: `models/linkedin_outbox.py`, `services/linkedin_pacing.py`
- Test: `tests/test_linkedin_pacing.py`

**Interfaces — Produces:**
- `models/linkedin_outbox.py`: `STATUSES`, `KINDS = ("brief","nudge","reply","other")`, `MODES = ("queued","send_now","manual")`, `COUNTED_STATUSES = ("sent","sending","check_linkedin")`; `@dataclass(frozen=True) SendConfig(daily_cap=50, weekly_cap=250, ramp_start=30, ramp_days=14, gap_min=180, gap_max=480, window=Window(...), mcp_version="4.29.0")`; `@dataclass(frozen=True) Window(days: frozenset[int], start: time, end: time, tz: ZoneInfo)`.
- `services/linkedin_pacing.py`:
  - `load_config(env: Mapping[str,str]) -> SendConfig` (raises `ValueError` on a malformed value)
  - `parse_window(text: str) -> Window` — `"Mon-Fri 08:00-19:00 America/Los_Angeles"`; days as a range or comma list.
  - `in_window(now: datetime, w: Window) -> bool` — start inclusive, end exclusive.
  - `next_window_start(now: datetime, w: Window) -> datetime`
  - `draw_gap(cfg: SendConfig, rng: random.Random) -> int`
  - `caps(cfg: SendConfig, *, today: date, ramp_started_on: date | None) -> tuple[int, int]`
  - `day_bounds(now, tz) -> tuple[datetime, datetime]`, `week_bounds(now, tz) -> tuple[datetime, datetime]`
  - `normalize_message(text: str) -> str` (raises `InvalidMessage(ValueError)`), matching the MCP's `refuse_an_invalid_message` + `normalize_message_text`.

- [ ] **Step 1: Write the failing tests** (`tests/test_linkedin_pacing.py`):
  - `test_window_weekday_hours` — Tue 07:59 PT false, 08:00 true, 18:59 true, 19:00 false; Sat 12:00 false.
  - `test_window_uses_its_own_timezone` — 15:30 UTC on a Monday in November (07:30 PST) false; 16:00 UTC true.
  - `test_next_window_start_skips_weekend` — Fri 19:30 PT → Mon 08:00 PT.
  - `test_gap_within_bounds` — 200 draws with `random.Random(1)` all in [180, 480] and not all equal.
  - `test_caps_ramp` — no start → (30, 150); day 0 → (30,150); day 7 → (40, 200); day 14 and 30 → (50, 250).
  - `test_week_bounds_are_pacific` — `2026-11-09T07:30Z` (Sun 23:30 PST) → week starting Mon 2026-11-02 00:00 PT.
  - `test_normalize_message` — CRLF/CR → LF, whitespace-only line → empty, outer whitespace trimmed, interior kept; `\t`, `\x00`, `\x7f`, `\u0085`, ` `, ` `, `""`, `"  \n "` raise `InvalidMessage`.
  - `test_load_config_defaults_and_overrides` — empty env → defaults above; `LINKEDIN_DAILY_CAP=60` → 60; `LINKEDIN_GAP_MIN_SECONDS=500` with max 480 → `ValueError`.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_linkedin_pacing.py -q` → fails (import error).
- [ ] **Step 3:** Implement both modules.
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5:** Commit `LinkedIn queue: pure pacing rules`.

### Task 2: MCP result → outcome

**Files:**
- Create: `services/linkedin_result.py`
- Test: `tests/test_linkedin_result.py`

**Interfaces — Produces:**
- `@dataclass(frozen=True) Outcome(row_status: str, reason: str | None, metric: str, pause: str | None = None, pause_seconds: int | None = None, thread_id: str | None = None, thread_mismatch: bool = False)` — `row_status` ∈ `sent|set_aside|check_linkedin|queued`; `pause` ∈ `None|"until"|"hold"`; `metric` is the `people.linkedin_sends` outcome label (`sent`, `set_aside`, `check_linkedin`, `rate_limited`, `paused`, `browser_busy`).
- `map_result(result: dict, *, row_thread_id: str | None) -> Outcome` — `result` is the client's normalized shape: either the tool's dict, or `{"error": {"code": str, "retry_after": int | None}}`.
- `RATE_LIMIT_MARGIN = 300`, `RATE_LIMIT_DEFAULT = 3600`.

- [ ] **Step 1: Failing tests**, one per row of the decision tables above, plus:
  - `test_sent_with_matching_thread` / `test_sent_with_other_thread_flags_mismatch` / `test_sent_without_row_thread_is_not_mismatch`.
  - `test_send_unconfirmed_is_check_linkedin` (`sent: False, retry_safe: False`), `test_missing_sent_is_check_linkedin` (`status: outcome_unknown`, no `sent`).
  - `test_unknown_future_status_pauses_and_keeps_row` — `{"status":"brand_new","sent":False,"retry_safe":True}` → `queued`, `hold`, reason `brand_new`.
  - `test_rate_limited_error_pauses_until` — `retry_after=120` → `pause_seconds == 420`, row `check_linkedin`; missing → 3900.
  - `test_browser_busy_requeues_without_pause` → `queued`, `pause is None`, metric `browser_busy`.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Pass. **Step 5:** Commit `LinkedIn queue: MCP result mapping`.

### Task 3: Schema, repo, shared pipeline decision

**Files:**
- Modify: `repo/schema.sql` (append), `models/outreach.py` (`SOURCES` += `"linkedin_outbox"`), `services/outreach.py` (new `record_send`, `advance_from_sent` uses it), `scripts/merge_duplicate_contacts.py` (`_collapse_rows` re-points)
- Create: `repo/linkedin_outbox.py`
- Test: `tests/test_outreach.py` (record_send), `tests/test_linkedin_outbox_db.py` (TEST_DATABASE_URL-gated, like `tests/test_outreach_db.py`), `tests/test_merge_duplicate_contacts.py`

**Interfaces — Produces:**
- `services/outreach.record_send(conn, person_id: int, *, at: datetime, channel: str, source: str, graph_message_id: str | None = None) -> tuple[str, int] | None` — the single stage decision: `to_contact` → `advance(..., "briefed", channel=channel, source=source)`; `briefed` → `nudge(...)`; else `None`. `advance_from_sent` becomes `record_send(conn, pid, at=at, channel="email", source="event", graph_message_id=...)` (its existing tests keep passing; `InvalidTransition` still swallowed).
- `repo/linkedin_outbox.py` (all take an open conn): `insert(conn, **cols) -> dict`, `get(conn, id) -> dict | None`, `lock(conn, id) -> dict | None`, `update(conn, id, changes: dict) -> dict`, `claim_next(conn, now) -> dict | None` (`FOR UPDATE SKIP LOCKED`, oldest `approved_at` with `not_before` null or ≤ now), `list_rows(conn, statuses: tuple[str,...] | None) -> list[dict]`, `count_counted_between(conn, since, until) -> int`, `stale_sending(conn, older_than: datetime) -> list[dict]`, `queue_stats(conn) -> dict` (`queued`, `oldest_approved_at`, `check_linkedin`, `set_aside`, `thread_mismatch` recent), `get_state(conn) -> dict` (inserts the singleton if missing), `lock_state(conn) -> dict`, `update_state(conn, changes) -> dict`, `repoint_person(conn, from_id, to_id) -> int`.
- Schema: `linkedin_outbox` columns per the design table plus `mode`, `thread_mismatch`; CHECKs on `kind`, `mode`, `status`; index on `(status, approved_at)`. `linkedin_sender_state(id SMALLINT PK CHECK (id = 1), paused_until, paused_reason, last_sent_at, next_gap_seconds, ramp_started_on DATE, consecutive_set_asides SMALLINT NOT NULL DEFAULT 0, last_error, updated_at)`.

- [ ] **Step 1: Failing tests:**
  - `tests/test_outreach.py`: `test_record_send_briefs_to_contact_on_linkedin` (event channel `linkedin`, source `linkedin_outbox`), `test_record_send_nudges_briefed`, `test_record_send_ignores_other_stages` — using the module's existing fake-repo style.
  - `tests/test_linkedin_outbox_db.py`: `test_schema_is_still_idempotent`; `test_outbox_rejects_unknown_status`; `test_events_accept_linkedin_outbox_source`; `test_claim_next_skips_future_not_before_and_takes_oldest`; `test_counted_between_includes_sending_and_check`; `test_person_delete_sets_null`; `test_repoint_person`.
  - `tests/test_merge_duplicate_contacts.py`: the collapse test asserts `linkedin_outbox_repo.repoint_person` is called before delete (follow the file's existing WhatsApp assertion).
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Pass (DB tests with a throwaway Postgres: `docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=x postgres:16`, `TEST_DATABASE_URL=postgresql://postgres:x@localhost:55432/postgres`). **Step 5:** Commit `LinkedIn queue: schema, repo, shared send → pipeline decision`.

### Task 4: Outbox service (the only writer) and sender tick

**Files:**
- Create: `services/linkedin_outbox.py`
- Modify: `clients/otel.py` (instruments)
- Test: `tests/test_linkedin_outbox.py` (fake repo in memory + fake sender)

**Interfaces — Consumes:** Tasks 1–3. **Produces:**
- `class Sender(Protocol)`: `session_usable() -> bool`, `send(username: str, message: str, profile_urn: str | None) -> dict` (normalized result; may raise — treated as `outcome_unknown`).
- Errors: `CeilingReached(Exception)` (carries counts), `NotEditable`, `OutboxNotFound`.
- `queue(conn, *, person_id: int | None, username: str, message: str, kind: str, send_now: bool, now: datetime, cfg: SendConfig, not_before: datetime | None = None, thread_id: str | None = None, profile_urn: str | None = None, session_hint: str | None = None) -> dict` — normalizes text; send-now checks the ceiling (raises `CeilingReached`, writes nothing) and writes the row as `sending` with `attempted_at = now`; sets `ramp_started_on` if unset.
- `apply_outcome(conn, row_id: int, outcome: Outcome, *, now: datetime, cfg: SendConfig, rng: random.Random) -> dict` — writes the row status; on `sent`: `sent_at`, `sent_thread_id`, `thread_mismatch`, `last_sent_at = now`, new `next_gap_seconds`, reset `consecutive_set_asides`, and, if `person_id` and no `outreach_event_id`, `outreach.record_send(..., channel="linkedin", source="linkedin_outbox")` and store the event id; on `set_aside`: increment, pause hold `set_aside_streak` at 3; applies `pause`.
- `apply_result(conn, row_id, result: dict, *, now, cfg, rng) -> Outcome` — `map_result` then `apply_outcome` (send-now `--mcp-result`).
- `resolve(conn, row_id, status: str, *, now, cfg, rng, thread_id=None, reason=None) -> dict` — manual `result --status`; only from `sending`/`check_linkedin`.
- `record_manual(conn, *, person_id, username, message, kind, at, now, cfg, rng) -> dict` — inserts `mode='manual'`, `status='sent'`, then the `sent` side effects.
- `cancel(conn, row_id) -> dict`, `edit(conn, row_id, message, *, now) -> dict` (queued only; resets `approved_at`), `requeue(conn, row_id) -> dict` (from `set_aside`/`check_linkedin`).
- `pause(conn, reason)`, `resume(conn)` (clears `paused_until`, `paused_reason`, `consecutive_set_asides`).
- `status(conn, *, now, cfg) -> dict` — state, `sent_today`, `sent_week`, `cap_today`, `cap_week`, `next_send_at`, queue stats.
- `estimate_send_times(conn, rows, *, now, cfg) -> dict[int, datetime | None]` — queued rows in claim order, each after the previous by `gap_min`, pushed to window starts; `None` when paused on hold.
- `run_once(conn, sender_factory: Callable[[], Sender], *, now, cfg, rng) -> str` — returns what happened (`stale`, `outside_window`, `paused`, `gap`, `ceiling`, `empty`, `login`, or the outcome metric). Order: sweep stale `sending` → clear expired `paused_until` → window → pause → gap → ceiling → claim (commit) → `sender_factory()` → `session_usable()` (no: row back to `queued`, hold `login`) → `send` → `apply_outcome` → commit. Emits gauges every call.

- [ ] **Step 1: Failing tests** (`tests/test_linkedin_outbox.py`), with a `FakeSender` recording calls:
  - `test_queue_normalizes_and_starts_ramp`; `test_queue_rejects_invalid_text_and_writes_nothing`.
  - `test_send_now_refused_at_ceiling_counts_inflight` (30 cap on day 0: 29 sent + 1 sending → `CeilingReached`, no insert).
  - `test_run_once_sends_one_and_records_brief` — one queued row for a `to_contact` person → `FakeSender.send` called once with the stored text, row `sent`, `outreach_event_id` set, `last_sent_at` set, gap in bounds, `commit` called before `send`.
  - `test_run_once_outside_window_sends_nothing`; `test_run_once_respects_gap`; `test_run_once_respects_ceiling`.
  - `test_run_once_login_not_usable_holds_queue_and_keeps_row` — `send` never called.
  - `test_browser_busy_requeues`; `test_rate_limit_pause_clears_itself` (paused_until in the past → next run proceeds; `paused_reason` stays put).
  - `test_three_set_asides_pause`.
  - `test_sender_exception_is_check_linkedin`.
  - `test_stale_sending_becomes_check_linkedin` (16 min old → `check_linkedin`, reason `stale_sending`; 10 min old untouched).
  - `test_apply_sent_twice_writes_one_event`.
  - `test_record_manual_counts_and_records_event`.
  - `test_edit_only_while_queued_resets_approval`; `test_requeue_only_from_set_aside_or_check`.
  - `test_metrics_carry_no_identity` — every recorded attribute key ⊆ `{outcome, mode, kind, reason}`.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement; add to `clients/otel.py`: counters `people.linkedin_sends`, `people.linkedin_queued`; histogram `people.linkedin_approval_to_send_seconds`; observable gauges `people.linkedin_queue_depth`, `people.linkedin_queue_oldest_age_seconds`, `people.linkedin_sends_today`, `people.linkedin_sends_week`, `people.linkedin_cap_today`, `people.linkedin_cap_week`, `people.linkedin_sender_heartbeat`, `people.linkedin_sender_paused` (label `reason`), fed by `otel.set_linkedin_gauges(values: dict[str, float], paused_reason: str | None)`. **Step 4:** Pass. **Step 5:** Commit `LinkedIn queue: outbox service and sender tick`.

### Task 5: MCP client, sender script, launchd installer

**Files:**
- Create: `clients/linkedin_mcp.py`, `scripts/linkedin_sender.py`, `scripts/install-linkedin-sender.sh`
- Modify: `requirements-dev.txt` (`mcp>=1.9`)
- Test: `tests/test_linkedin_mcp.py`, `tests/test_linkedin_sender.py`

**Interfaces — Produces:**
- `clients/linkedin_mcp.py`: `normalize_call_result(result) -> dict` (pure: `isError` → `{"error": {"code", "retry_after"}}` from `meta["linkedin.dev/error"]`, else `structuredContent` or JSON from the first text block); `class McpSender` (implements `Sender`) opened by `open_sender(version: str) -> contextlib.AbstractContextManager[McpSender]` that runs `uvx mcp-server-linkedin==<version>` over stdio. Calls are synchronous wrappers around one asyncio loop.
- `scripts/linkedin_sender.py`: `main_with(get_conn, *, now=None, sender_factory=None, env=os.environ) -> int` — `setup_telemetry("people-linkedin-sender")`, `run_once`, logs only the returned word, `otel.flush()` in `finally`.

- [ ] **Step 1: Failing tests:** `test_normalize_error_result_with_retry_after`, `test_normalize_structured_result`, `test_normalize_text_json_result`; `tests/test_linkedin_sender.py::test_main_runs_once_and_flushes` (fake conn, fake sender factory, asserts flush called and no text in captured logs).
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. Installer writes `~/Library/LaunchAgents/cloud.drolet.people.linkedin-sender.plist` (`ProgramArguments`: `<repo>/.venv/bin/python <repo>/scripts/linkedin_sender.py`, `WorkingDirectory` repo, `StartInterval` 120, `EnvironmentVariables.PATH` including `/opt/homebrew/bin`, stdout/stderr to `~/Library/Logs/people-linkedin-sender.log`) and runs `launchctl bootstrap gui/$UID <plist>`; `--uninstall` boots it out and deletes the plist; `--print` writes the plist to stdout only. **Step 4:** Pass; `bash -n` the installer; `scripts/install-linkedin-sender.sh --print | plutil -lint -`. **Step 5:** Commit `LinkedIn queue: MCP client, sender, launchd installer`.

### Task 6: CLI and `outreach today` line

**Files:**
- Create: `scripts/outreach_linkedin.py`
- Modify: `scripts/outreach.py` (register `linkedin`, extend `USER_ERRORS`, pass the LinkedIn summary into `today`), `models/outreach.py` (`TodayCounts.linkedin: dict | None = None`), `services/outreach_today.py` (`Today.linkedin`, one `LinkedIn` line after `Queue`, included in JSON)
- Test: `tests/test_outreach_linkedin_cli.py`, `tests/test_outreach_today.py`

**Interfaces — Produces:** `register(sub: argparse._SubParsersAction) -> None` and `cmd_linkedin(conn, args, out, now)`, dispatching `queue`, `list`, `cancel`, `edit`, `result`, `record`, `requeue`, `pause`, `resume`, `status` with the flags in the design (`queue`/`record` take `--person <ident>` or `--username <slug>`; a `--person` with a `linkedin_url` supplies the username; `--message-file -` reads stdin). `services.linkedin_outbox.today_summary(conn, *, now, cfg) -> dict` (`queued`, `oldest_age_hours`, `paused`, `check_linkedin`, `set_aside`, `thread_mismatch`).

- [ ] **Step 1: Failing tests:** `test_queue_prints_id_and_expected_time`; `test_queue_send_now_at_ceiling_errors_and_exits_1`; `test_queue_person_without_linkedin_url_needs_username`; `test_list_hides_text_unless_show_text`; `test_result_mcp_result_json`; `test_record_manual`; `test_status_prints_counts_against_cap`; `test_today_renders_linkedin_line` (only when anything is queued/paused/needs a check).
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Pass. **Step 5:** Commit `LinkedIn queue: outreach linkedin CLI and today line`.

### Task 7: Docs, skills, checks, migrate, PR

- [ ] Update `.claude/skills/staging-linkedin-message/SKILL.md` (queue by default, send-now flow, `record` replaces step 6 for hand sends, drop `reply_to_conversation`), `.claude/skills/checking-linkedin-messages/SKILL.md` (resolve `check_linkedin`), `.claude/skills/working-outreach-queue/SKILL.md` (the today line), `CLAUDE.md` (tables, stack row for the sender, `send_message` is the only send tool, only local scripts write `linkedin_outbox`, code layout entries), `scripts/fetch-env.sh` untouched (defaults live in code).
- [ ] Full checks: `.venv/bin/pytest tests/ -q`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`.
- [ ] `.venv/bin/python scripts/migrate_db.py` (only after tests pass).
- [ ] Open the PR with `/pr-open`. Do not install the launchd agent.
