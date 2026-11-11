# Outreach Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the DB-derived outreach and LinkedIn gauges the dashboard needs, build the `Referral outreach` Grafana dashboard as code, and deploy it plus the `linkedin-sender` email alerts with Terraform.

**Architecture:** Pure row-builders (`services/dashboard_gauges.py`) turn `counts()`, the today queries, the targets and the outbox counts into `(value, labels)` rows; `clients/otel.py` holds them in one keyed store read by observable gauges. `dashboards/outreach.py` (Grafana Foundation SDK) writes `dashboards/build/outreach.json`, which `terraform/grafana.tf` applies with the `grafana/grafana` provider beside a contact point, a mute timing and one rule group.

**Tech Stack:** Python 3.13, OpenTelemetry observable gauges, psycopg/Postgres, `grafana-foundation-sdk` (dev only), Terraform `grafana/grafana` provider.

**Spec:** `docs/superpowers/specs/2026-11-05-outreach-dashboard-design.md`

## Global Constraints

- Nothing personal in the repo: no email address, no pace targets or floors, no person names, in code, JSON or Terraform defaults.
- `grafana-foundation-sdk` goes in `requirements-dev.txt` only, pinned to `1769699998!10.1.0`; never `requirements.txt`.
- No `grafana_notification_policy` resource. Rules route through `notification_settings`.
- `alert_email` (sensitive) and `grafana_url` are Terraform variables, set in `terraform.tfvars` and GitHub variables `ALERT_EMAIL` / `GRAFANA_URL`.
- Layer rules: SQL in `repo/`, logic in `services/`, instruments in `clients/otel.py`.
- Dashboard numbers use the CLI definitions: cohort = New York Monday of the first brief, reached = ever reached per the event log, qualified = current verdict yes.
- Panels read the latest value over a window (`last_over_time`), never a bare instant value.
- Metric names: OTel `people.x` → Prometheus `people_x`; counters gain `_total`.
- Do not `terraform apply`.

## Decisions this plan adds (builder's calls the design left open)

- **Freshest publisher.** Measured in Grafana Cloud 2026-11-05: every `people-sync` run reports under a new random `instance` label, and the CLI under `job="people-outreach-local"`. So `last_over_time(...[26h])` returns one series set per run. A new gauge **`people.outreach_refreshed`** (unix time of the `record_population` run) lets every outreach panel keep only the newest run's series:
  `max by (<labels>) (last_over_time(M[26h]) and on(job, instance) topk(1, last_over_time(people_outreach_refreshed[26h])))`.
  The LinkedIn gauges come from one process with no instance label, so they use plain `last_over_time`.
- **`people.linkedin_daily`** labels: `what` (`approved` | `sent`), `kind` (`brief` | `nudge` | `reply` | `other`), `day` (Pacific ISO date, the last 14 days including today). Approved = rows by `approved_at` day (any status). Sent = `status = 'sent'` rows by `sent_at` day. Zero-filled.
- **Send window in the alerts:** a `grafana_mute_timing` (`linkedin-send-window-closed`: Sat–Sun all day, Mon–Fri 00:00–08:00 and 19:00–24:00, `America/Los_Angeles`) on every rule's `notification_settings`. For "queue stuck", the oldest message's age overnight would fire every morning, so the sender also publishes **`people.linkedin_queue_oldest_window_seconds`**: seconds the oldest queued row has waited *inside* the send window.
- **Token:** Secret Manager secret `grafana-dashboards-token`, read by a `google_secret_manager_secret_version` data source into the provider. No GitHub secret. Because the provider reads it at plan time, Ben creates the secret and its version by hand first; Terraform then adopts the secret with an `import {}` block (Terraform 1.5) and manages it from there. The deployer SA (`inbox-deployer`) has `roles/secretmanager.admin`, which can read it.
- **Datasource:** the dashboard uses a `datasource` template variable (type `prometheus`); rules use variable `prometheus_datasource_uid`, default `grafanacloud-prom`.
- **Cohort variable:** `cohort`, multi-value, include All (`.*`), values `label_values(people_outreach_cohort, cohort_week)`. With All selected the chain equals `outreach report` with no flags, `none` included.

## Review Focus

1. **Two publishers in the 26 h window** (CLI run after the 04:00 sync, or two sync instances): panels must show the newest run only, not the max or the sum. Pinned by `test_latest_selects_newest_run` (Task 4).
2. **A cohort with zero in some stage** must still publish a 0 row, or sums and bar charts drop the week. Pinned by `test_cohort_rows_zero_filled` (Task 1).
3. **`none` cohort** (intros with no brief week) must be in the gauge and in the All total. Pinned by `test_cohort_rows_match_report_render` (Task 1).
4. **Overnight backlog**: a message approved at 19:30 Friday must show ~0 in-window wait at 08:00 Monday. Pinned by `test_window_seconds_skips_nights_and_weekend` (Task 2).
5. **Day boundaries in Pacific vs. UTC** for `linkedin_daily`: a send at 17:30 Pacific is that day's, not tomorrow's UTC day. Pinned by `test_daily_counts_use_pacific_days` (Task 2, DB test).

---

### Task 1: Outreach gauges (cohort, activity, lost reasons, target, refreshed)

**Files:**
- Create: `services/dashboard_gauges.py`, `tests/test_dashboard_gauges.py`
- Modify: `clients/otel.py`, `services/outreach.py` (`record_population`), `scripts/outreach.py` (`cmd_today`), `tests/test_outreach.py`, `tests/test_outreach_cli.py`

**Interfaces:**
- Produces (`services/dashboard_gauges.py`, pure, `Row = tuple[float, dict[str, str]]`):
  - `COHORT_STAGES = ("briefed", "replied", "intro", "conversation", "qualified", "proposal", "signed")`
  - `cohort_rows(counts: FunnelCounts) -> list[Row]` — one row per cohort × stage, labels `cohort_week` (`services.outreach.iso_week`-style string, `"none"` for `None`) and `stage`, zeros included.
  - `activity_rows(*, briefs_today: int, briefs_week: int, nudges_today: int, nudges_week: int) -> list[Row]` — labels `kind` (`first_briefs` | `nudges`), `window` (`today` | `week`).
  - `lost_reason_rows(lost: dict[str, int]) -> list[Row]` — every `LOST_REASONS` value (0 when absent) plus any unknown key, label `lost_reason`.
  - `target_rows(t: Targets) -> list[Row]` — label `target`: `briefs_per_day`, `briefs_per_week`, `briefs_total`, `queue_low_water_days`.
- Produces (`clients/otel.py`): `set_rows(key: str, rows: list[tuple[float, dict[str, str]]]) -> None`; keys and gauges: `outreach_cohort` → `people.outreach_cohort`, `outreach_activity` → `people.outreach_activity`, `outreach_lost_reasons` → `people.outreach_lost_reasons`, `outreach_target` → `people.outreach_target`, `outreach_refreshed` → `people.outreach_refreshed`, plus Task 2's `linkedin_outbox`, `linkedin_daily`, `linkedin_queue_oldest_window_seconds`. A key never set observes nothing.
- `record_population(conn, *, today: date, now: datetime | None = None)` — `now` defaults to `datetime.now(UTC)`; additionally sets cohort (from `counts(conn, today=today)`), lost reasons, activity (`first_briefs_between` / `nudges_between` for today and Monday→today, New York), refreshed (`now.timestamp()`).

- [ ] **Step 1: Write failing tests** in `tests/test_dashboard_gauges.py`:
  - `test_cohort_rows_match_report_render`: build a `FunnelCounts` with two dated cohorts and a `None` cohort; parse `outreach_report.render(counts)`'s table (each cohort line and the `All` line, columns Briefed..Signed); assert for every cohort label and stage the row value equals the printed number, and that the per-stage sum of rows equals the `All` line.
  - `test_cohort_rows_zero_filled`: one cohort with only `briefed=3` yields 7 rows, six of them 0.
  - `test_activity_rows`, `test_lost_reason_rows_fill_known_reasons` (`{"price": 2, "weird": 1}` → `price` 2, `no_fit` 0, `weird` 1), `test_target_rows` (exact labels above, values from the `Targets`).
  - In `tests/test_outreach.py`: `test_record_population_publishes_dashboard_gauges` — monkeypatch `outreach_repo` reads and `otel.set_rows`; assert keys `outreach_cohort`, `outreach_lost_reasons`, `outreach_activity`, `outreach_refreshed` set, refreshed value `== now.timestamp()`, activity week query called with `since=Monday`.
  - In `tests/test_outreach_cli.py`: `today` publishes `outreach_target` rows (monkeypatch `otel.set_rows`).
  - In a new `tests/test_otel.py`: `test_set_rows_observed` — `set_rows("outreach_cohort", rows)` then the key's observe callback returns matching `Observation`s; unset key returns `[]`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_dashboard_gauges.py tests/test_otel.py tests/test_outreach.py tests/test_outreach_cli.py -q` — expect FAIL (missing module / attribute).
- [ ] **Step 3: Implement** the module, `otel.set_rows` + gauge registration in `setup_telemetry` (one `_DASHBOARD_GAUGES: dict[key, (name, description)]` loop like `_LINKEDIN_GAUGES`), `record_population` additions, and `cmd_today` calling `otel.set_rows("outreach_target", dashboard_gauges.target_rows(targets))`.
- [ ] **Step 4: Run** the same tests — PASS.
- [ ] **Step 5: Commit** `Publish outreach cohort, activity, lost-reason and target gauges`.

### Task 2: LinkedIn gauges (outbox by status, daily, in-window wait)

**Files:**
- Modify: `repo/linkedin_outbox.py`, `services/linkedin_pacing.py`, `services/dashboard_gauges.py`, `services/linkedin_outbox.py` (`_emit_gauges`)
- Test: `tests/test_linkedin_pacing.py`, `tests/test_dashboard_gauges.py`, `tests/test_linkedin_outbox.py`, `tests/test_linkedin_outbox_db.py`

**Interfaces:**
- `repo/linkedin_outbox.py`: `status_counts(conn) -> dict[str, int]`; `daily_counts(conn, *, since: datetime, tz: str) -> list[dict]` with keys `what`, `day` (date), `kind`, `n` — approved by `approved_at`, sent by `sent_at` where `status = 'sent'`, both `>= since`, days in `tz`.
- `services/linkedin_pacing.py`: `window_seconds(start: datetime, end: datetime, w: Window) -> float` — seconds of `[start, end)` inside the window; 0 when `end <= start`.
- `services/dashboard_gauges.py`: `outbox_status_rows(counts: dict[str, int]) -> list[Row]` (every `STATUSES`, label `status`); `linkedin_daily_rows(rows: list[dict], *, today: date, days: int = 14) -> list[Row]` (every day × `what` × `KINDS`, zero-filled, `day` ISO string).
- `_emit_gauges` sets `linkedin_outbox`, `linkedin_daily` (since = start of the Pacific day 13 days ago) and `linkedin_queue_oldest_window_seconds` (`window_seconds(oldest_approved_at, now, cfg.window)`, 0 when empty).

- [ ] **Step 1: Write failing tests:** `test_window_seconds_inside_one_day` (09:00→10:30 Pacific Tue = 5400), `test_window_seconds_skips_nights_and_weekend` (Fri 19:30 → Mon 08:10 Pacific = 600), `test_window_seconds_reversed_is_zero`; `test_outbox_status_rows_fill_statuses`; `test_linkedin_daily_rows_zero_filled` (14 days × 2 × 4 = 112 rows, a given row's value); `test_emit_gauges_publishes_outbox_rows` in `tests/test_linkedin_outbox.py` (fake repo, assert the three keys); DB tests `test_status_counts` and `test_daily_counts_use_pacific_days` (a row sent at 2026-11-04T17:30-08:00 lands on day `2026-11-04`).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_linkedin_pacing.py tests/test_dashboard_gauges.py tests/test_linkedin_outbox.py tests/test_linkedin_outbox_db.py -q` — FAIL.
- [ ] **Step 3: Implement** (walk day by day for `window_seconds`, clipping each window day to `[start, end)`).
- [ ] **Step 4: Run** — PASS (DB tests run when `TEST_DATABASE_URL` is set; run them locally against a throwaway Postgres if available).
- [ ] **Step 5: Commit** `Publish LinkedIn outbox, daily and in-window wait gauges`.

### Task 3: Dashboard as code

**Files:**
- Create: `dashboards/__init__.py`, `dashboards/outreach.py`, `dashboards/build/outreach.json`, `tests/test_dashboard.py`
- Modify: `requirements-dev.txt`, `.github/workflows/ci.yml`, `pyproject.toml`/mypy config only if `dashboards/` needs it

**Interfaces:**
- `dashboards/outreach.py`: `latest(expr: str, by: str) -> str` (the freshest-publisher selector above), `build() -> dict` (dashboard JSON model), `main() -> None` writing `dashboards/build/outreach.json` (sorted keys, 2-space indent, trailing newline); `python -m dashboards.outreach`.
- Dashboard: uid `people-outreach`, title `Referral outreach`, variables `datasource`, `cohort`; rows and panels:
  - **Is outreach producing results?** Qualified leads and introductions per brief week (bar chart, last 12 cohorts); Conversion chain (counts briefed→replied→intro→conversation→qualified, and rates); Qualified verdicts (yes/no/pending, `people_outreach_qualified_leads`); Lost reasons.
  - **Am I putting in the effort?** First briefs today vs `briefs_per_day`; this week vs `briefs_per_week` (`people_outreach_target`, `[8d]`); Nudges this week; Follow-ups due; Warm pool left (`to_contact`); LinkedIn approved per day by kind; LinkedIn sent per day by kind.
  - **Is the LinkedIn sender healthy?** Queue depth; Oldest queued age; Sends today vs ceiling; Sends this week vs ceiling; Outbox by status; Send outcomes (`increase(people_linkedin_sends_total[1d])`, titled as a rough signal); Approval to send p50/p90; Last heartbeat age; Paused (reason).

- [ ] **Step 1: Write failing tests** in `tests/test_dashboard.py`: `test_committed_json_is_current` (`build()` serialized == file contents); `test_latest_selects_newest_run` (string contains `topk(1, last_over_time(people_outreach_refreshed[26h]))` and `on(job, instance)`); `test_rows_in_order` (three row titles above, in order); `test_every_new_gauge_is_queried` (each of `people_outreach_cohort`, `people_outreach_activity`, `people_outreach_lost_reasons`, `people_outreach_target`, `people_linkedin_outbox`, `people_linkedin_daily` appears in some target expr); `test_no_personal_data` (no `@`, no `drolet`); `test_targets_use_datasource_variable`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_dashboard.py -q` — FAIL.
- [ ] **Step 3: Implement** with the Foundation SDK builders; add the pinned SDK to `requirements-dev.txt`; generate the JSON; add a CI step after Tests: `python -m dashboards.outreach && git diff --exit-code dashboards/build/`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `Build the Referral outreach dashboard with the Foundation SDK`.

### Task 4: Terraform: provider, folder, dashboard, contact point, alerts

**Files:**
- Create: `terraform/grafana.tf`
- Modify: `terraform/main.tf` (provider `grafana/grafana`), `terraform/variables.tf`, `terraform/secrets.tf`, `terraform/terraform.tfvars.example`, `.github/workflows/deploy.yml`

**Contents:**
- Variables: `grafana_url` (string), `alert_email` (string, sensitive), `prometheus_datasource_uid` (default `grafanacloud-prom`).
- `google_secret_manager_secret.grafana_dashboards_token` (`grafana-dashboards-token`) with an `import {}` block (literal id `projects/bens-project-462804/secrets/grafana-dashboards-token`); data `google_secret_manager_secret_version.grafana_dashboards_token`; `provider "grafana" { url = var.grafana_url, auth = <data>.secret_data }`.
- `grafana_folder.people` (`People`, uid `people`); `grafana_dashboard.outreach` (`config_json = file("${path.module}/../dashboards/build/outreach.json")`, folder uid).
- `grafana_contact_point.ben_email` (`ben-email`, `email { addresses = [var.alert_email] }`, resolve messages on).
- `grafana_mute_timing.linkedin_send_window_closed` as above.
- `grafana_rule_group.linkedin_sender` (`linkedin-sender`, folder `People`, 60 s): each rule queries A (Prometheus, instant), reduces B (last), thresholds C; `notification_settings { contact_point, group_by = ["alertname"], repeat_interval, mute_timings }`:

| Rule | A | C | for | no data | repeat |
|---|---|---|---|---|---|
| LinkedIn sender silent | `time() - max(last_over_time(people_linkedin_sender_heartbeat[7d]))` | > 1800 | 5m | Alerting | 4h |
| LinkedIn queue paused | `max by (reason) (last_over_time(people_linkedin_sender_paused[10m]))` | > 0 | 5m | OK | 4h |
| LinkedIn send needs checking | `max(last_over_time(people_linkedin_outbox{status="check_linkedin"}[26h]))` | > 0 | 0m | OK | 24h |
| LinkedIn queue stuck | in-window wait `> 14400`, `and on()` paused == 0 and sends below both ceilings, all `[10m]` | > 0 | 5m | OK | 4h |

  Annotations carry no names or text; the paused one includes `{{ $labels.reason }}` and says `rate_limited` clears itself.
- `deploy.yml`: `TF_VAR_alert_email: ${{ vars.ALERT_EMAIL }}`, `TF_VAR_grafana_url: ${{ vars.GRAFANA_URL }}`; add `dashboards/build/**` to the trigger paths.

- [ ] **Step 1:** Write the files.
- [ ] **Step 2: Run** `terraform -chdir=terraform fmt -check` and `terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate` — expect success (fix attribute names against the provider schema until it validates).
- [ ] **Step 3: Commit** `Deploy the dashboard and linkedin-sender alerts with Terraform`.

### Task 5: Docs, full checks, PR

**Files:** `CLAUDE.md` (stack row "Outreach dashboard" + code layout `dashboards/`, `services/dashboard_gauges.py`), `.claude/skills/querying-grafana-metrics/SKILL.md` (outreach + LinkedIn metric table, the freshest-publisher selector), `.claude/skills/adding-people-secret/SKILL.md` (note `grafana-dashboards-token`).

- [ ] **Step 1:** Update docs.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/ -q`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`, the dashboard staleness check, terraform fmt/validate — all clean.
- [ ] **Step 3:** Commit, then open the PR with `/pr-open`, listing Ben's one-time setup (service account token → Secret Manager `grafana-dashboards-token`; `grafana_url` + `alert_email` in `terraform.tfvars` and GitHub variables `GRAFANA_URL` / `ALERT_EMAIL`) and saying not to merge before they're done.

## Ben's one-time setup (manual)

1. Note the stack URL (`https://<stack>.grafana.net`). Create a service account with the Editor role (dashboards and alerting) and generate a token.
2. `gcloud secrets create grafana-dashboards-token --replication-policy=automatic --project=bens-project-462804`, then `gcloud secrets versions add grafana-dashboards-token --data-file=- --project=bens-project-462804` (Terraform imports the secret on the next apply).
3. `grafana_url` and `alert_email` in `terraform/terraform.tfvars`; `gh variable set GRAFANA_URL` and `gh variable set ALERT_EMAIL`.
