# Outreach dashboard as code — design

Date: 2026-11-05. Status: draft for Ben's review.

This is the third of three projects. The first two have shipped:

1. **The LinkedIn send queue**: PR #52, spec `2026-11-05-linkedin-send-queue-design.md`. It emits `people.linkedin_*`.
2. **Qualified-lead verdicts**: PR #53. It adds `outreach qualify`, `people.outreach_qualifications` and `people.outreach_qualified_leads`.

## Intent

Ben wants one place that shows whether referral outreach is working. It should be read as **leading and lagging indicators**:

- **Leading:** the effort he controls, visible the same day. Briefs, nudges, and LinkedIn messages approved and sent, against his pace.
- **Lagging:** results, which trail by weeks. The result that counts as success is **an introduction, or engagement with a qualified lead** (Ben's words). Qualification is Ben's yes/no verdict from his first meeting with the lead (project 2).

A third, smaller concern: the LinkedIn sender runs unattended on his Mac. He needs to know when it has stopped or needs him, and to hear about it by **email**.

## Goals

- One Grafana Cloud dashboard, **defined as code in this repo** and deployed from `main` like everything else.
- **Results at the top:** qualified leads and introductions per brief cohort, the conversion chain, lost reasons.
- **Effort in the middle:** briefs and nudges against the pace targets, follow-ups due, warm pool left, LinkedIn approved vs. sent.
- **Sender health at the bottom:** queue depth and age, sends vs. ceiling, outcomes, heartbeat, pauses.
- **Email alerts** for the sender conditions below.

## Non-goals

- The funded-founder campaign. It's a separate funnel (Ben's decision) and gets no panels here.
- Changing what the pipeline records. This project only publishes what's already in the DB, plus the targets.
- Grafana querying Postgres directly. Rejected earlier: Cloud SQL would need a network path from Grafana Cloud.
- A dashboard for the rest of the people service (sync, HubSpot, API).

## Anti-goals: it fails, even if it technically works, when

- **Anything personal lands in this public repo.** That includes Ben's email address, his pace targets or floors, and any person's name. The targets live in the private `OUTREACH_TARGETS` TOML today and must stay out of the dashboard JSON.
- **It shows numbers that disagree with `outreach today` or `outreach report`.** Those CLIs are the source of truth. The dashboard must use the same definitions: cohort = the Monday (New York) of the first brief, "reached a stage" = ever reached it per the event log, qualified = current verdict yes.
- **Panels go blank between runs** because the gauges only exist while a process is reporting. See Constraints.
- **Alerts fire all night or every weekend.** The sender only sends weekdays 8–19 Pacific, so "stuck" conditions must respect that window.

## Constraints

- **Grafana Cloud stack:**
  - Prometheus is `https://prometheus-prod-67-prod-us-west-0.grafana.net/prometheus` (instance `3286064`). See the `querying-grafana-metrics` skill.
  - OTel metric names become Prometheus names with dots turned into underscores, and counters get `_total`. For example, `people.linkedin_sends` becomes `people_linkedin_sends_total`.
- **Gauges are observable and only report while a process runs.**
  - `people.outreach_stage_population`, `people.outreach_follow_ups_due` and `people.outreach_qualified_leads` are refreshed by `services/outreach.py::record_population`. That runs nightly in `people-sync` (deployed) and on every `outreach` CLI run (local).
  - The `people.linkedin_*` gauges are reported by the sender every 2 minutes, but only while the Mac is awake.
  - **Panels must therefore read the latest value over a window**, e.g. `last_over_time(...[26h])` for nightly data, never an instant value.
- **Counters come from short-lived processes** (the CLI, the sender, the Cloud Functions). Use `increase()` / `sum by` over day and week windows, and treat counter resets as normal.
- **The repo is public.** Ben's alert email address goes in a Terraform variable, set in `terraform.tfvars` locally and as a GitHub variable in `deploy.yml`, exactly like `own_addresses` / `outreach_from_addresses`.
- **Deploys:** `.github/workflows/deploy.yml` runs `terraform apply` on pushes touching `terraform/` (among others) via WIF. The Grafana provider needs a Grafana Cloud **service account token** with dashboard and alerting write access. It is a new secret that Ben creates by hand in the Grafana UI. Follow `adding-people-secret` to wire it into Secret Manager and the workflow.
- **Layer rules** (CLAUDE.md) apply to any new publishing code: SQL in `repo/`, logic in `services/`, instruments in `clients/otel.py`.

## Decided (with Ben)

- **Tooling:** Grafana Cloud. The dashboard is written in **Python with the Grafana Foundation SDK** (`grafana-foundation-sdk`), which generates the dashboard JSON. Terraform's **`grafana/grafana` provider** in `people/terraform/` applies it as a `grafana_dashboard`. Alerts are `grafana_rule_group` resources with an email contact point.
- **Rejected alternatives:**
  - Hand-written JSON: hard to review.
  - Grafonnet: a new language.
  - Grafana Git Sync: bypasses Terraform.
- **Success metric:** introductions and engagement with **qualified** leads (yes/no, recorded in project 2).
- **Layout:** lagging on top, leading in the middle, sender health at the bottom.
- **Alerts go to email.**

## Design

### New metrics to publish

The cohort numbers the dashboard needs don't exist as metrics yet. `people.outreach_stage_population` is current-stage only, so a person who replied and then declined no longer counts as replied. Two additions:

1. **`people.outreach_cohort`** (observable gauge), labeled `cohort_week` (ISO week, as today) and `stage`. Stage is one of `briefed`, `replied`, `intro`, `conversation`, `qualified`, `proposal`, `signed`, and the value is the count.
   - It is filled from the same `services/outreach.py::counts()` that `outreach report` uses, so the dashboard and the report can't disagree.
   - It is refreshed in `record_population` (nightly and per CLI run).
   - **All cohorts, including `none`** *(my call)*. `none` is introductions and conversations with no brief week, which `outreach report`'s "All" row counts. No week cap: a year of cohorts is about 370 series, which is cheap. The dashboard's all-time totals must equal `outreach report` run with no flags.
2. **`people.outreach_target`** (observable gauge), labeled `target` (`briefs_per_day`, `briefs_per_week`, and anything else the targets TOML defines), and the value.
   - The targets file exists only on Ben's Mac, so this is published **only by the local `outreach` CLI** (`today`, and any run that loads targets), never by `people-sync`.
   - The dashboard reads it with `last_over_time(...[8d])`. If it's missing, the panel simply shows no target line.
   - The values are Ben's private pace numbers. Publishing them to his own private Grafana stack is fine; committing them is not.

3. **DB-derived gauges replace the event counters for anything that must match the CLIs** *(my call)*.
   - **The problem:** the sender (a new launchd process every run) and the outreach CLI are short-lived. They share one series with no instance id, and each run starts its cumulative counter at 0. So `increase()` over `people_linkedin_sends_total`, `people_linkedin_queued_total`, `people_outreach_nudges_total` or `people_outreach_lost_total` undercounts.
   - **The fix:** publish these as observable gauges computed from the DB in `record_population`, and in the sender's per-run gauge refresh for the LinkedIn ones.
     - `people.outreach_activity`, labeled `kind` (`first_briefs`, `nudges`) and `window` (`today`, `week`), from the same queries `outreach today` uses (`first_briefs_between`, `nudges_between`), in New York dates. That also settles the first-brief-vs-every-brief question.
     - `people.outreach_lost_reasons`, labeled `lost_reason`, from `counts().lost_reasons`.
     - `people.linkedin_outbox`, labeled `status` (`queued`, `sending`, `sent`, `set_aside`, `check_linkedin`, `cancelled`), a row count.
     - `people.linkedin_daily`, labeled `what` (`approved`, `sent`) and `kind`, giving today's and recent days' counts. The builder may choose the exact shape, as long as the "approved vs. sent per day" panel matches the outbox table.
   - **The existing counters stay** (other things may use them), but the dashboard doesn't read them for totals.

### Dashboard: one, three rows

The dashboard is named `Referral outreach`, in a folder `People` *(my call)*. The panel list is the builder's to refine, but each row must answer the question in its heading.

**Row 1 — Is outreach producing results? (lagging)**

- **Qualified leads per brief week** (bars, last 12 cohorts), with **introductions per brief week** beside or overlaid.
- **Conversion chain** for the selected cohort range: briefed → replied → intro → conversation → qualified, as counts and rates. These must equal `outreach report`.
- **Qualified verdicts:** yes / no / pending now (`people_outreach_qualified_leads`).
- **Lost reasons** (`people_outreach_lost_reasons`).

**Row 2 — Am I putting in the effort? (leading)**

- **Briefs:** first briefs today and this week (`people_outreach_activity`), against `people_outreach_target`.
- **Nudges this week** (`people_outreach_activity`) and **follow-ups due** (`people_outreach_follow_ups_due`).
- **Warm pool left:** `to_contact` population.
- **LinkedIn approved vs. sent per day** (`people_linkedin_daily`), split by `kind`.

**Row 3 — Is the LinkedIn sender healthy?**

- **Queue depth** and **oldest queued age**.
- **Sends today and this week vs. the ceiling** (`people_linkedin_sends_today` / `_week` against `people_linkedin_cap_today` / `_week`).
- **Outbox by status** (`people_linkedin_outbox`), plus outcome trends from `people_linkedin_sends_total` as a rough, possibly undercounted signal.
- **Approval-to-send time** (histogram quantiles).
- **Last heartbeat** (age) and **paused** (with reason).

**Template variables:** a cohort-week range *(builder's call)*. No per-person variables. There are no person labels, by design.

### Alerts (email)

A contact point `ben-email`, with the address from a Terraform variable `alert_email` (sensitive, never in the repo). One rule group, `linkedin-sender` *(my call on thresholds; Ben can tune them)*.

**Routing without touching the stack's policy tree** *(my call)*. There's no Grafana Terraform anywhere in `~/src`, so whatever routing the stack has was made in the UI, and `grafana_notification_policy` would replace that whole tree. So **no notification-policy resource**: each rule sends to `ben-email` directly through the rule's `notification_settings` (contact point, group-by, repeat interval). That can't clobber alerts made elsewhere.

| Alert | Condition | Notes |
|---|---|---|
| Sender silent | heartbeat older than 30 min **during the send window** | A sleeping Mac at night or on a weekend must not fire. Use a time-of-day/weekday condition, or a mute timing for outside weekdays 08:00–19:00 America/Los_Angeles. |
| Queue paused | `people_linkedin_sender_paused == 1` for 5 min | Include `reason` in the message. Login and captcha pauses need Ben; `rate_limited` resolves itself, so it's informational. |
| Check LinkedIn | `people_linkedin_outbox{status="check_linkedin"} > 0` | A send of unknown outcome that Ben must confirm by hand. It stays firing until he resolves the row, with a reminder email every 24 h *(my call)*, and a resolved email once the count is back to 0. |
| Queue stuck | oldest queued age > 4 h **during the send window** while the heartbeat is fresh | The sender is alive but not draining. |

Alert messages carry no person names or message text (none exist in labels anyway).

### Code layout

- `dashboards/` (new, top level): `outreach.py` builds the dashboard with the Foundation SDK and writes `dashboards/build/outreach.json`. A `make`-free script entry point: `python -m dashboards.outreach`.
  - **Generated JSON is committed** *(my call)*, so Terraform needs no Python at apply time. CI checks that it's current: rebuild and `git diff --exit-code`.
- `terraform/grafana.tf`: the provider, folder, `grafana_dashboard` (`config_json = file(...)`), contact point and rule group. No notification policy (see Alerts). `terraform/variables.tf` gets `grafana_url` and `alert_email` (sensitive). The token comes from Secret Manager per `adding-people-secret`.
- `grafana-foundation-sdk` goes in `requirements-dev.txt` only. Never `requirements.txt`, which the Cloud Functions install.
- The new gauges go in `clients/otel.py`, `services/outreach.py` (and `services/outreach_targets.py` for the target values), and `scripts/outreach.py`.
- **Docs:** a CLAUDE.md stack row for the dashboard and alerts, and a short section in `querying-grafana-metrics` listing the outreach and LinkedIn metrics.

### Ben's one-time setup

The plan should list these as manual steps:

1. Note the Grafana stack URL (e.g. `https://<stack>.grafana.net`). Create a **service account** with Editor (dashboards and alerting) and generate a token.
2. Add the token to Secret Manager (name per `adding-people-secret`, e.g. `grafana-dashboards-token`).
3. Set `alert_email` and `grafana_url` in `terraform.tfvars`, and as GitHub variables `ALERT_EMAIL` and `GRAFANA_URL`.

## Left to the builder

- Exact panel types, colors, units and layout within each row.
- Whether the briefs panel uses a new first-brief label on the existing counter or a dedicated gauge. It must match `outreach today`.
- How the send-window restriction is expressed: a mute timing or the query itself.
- Whether the provider reads the token through a Secret Manager data source or `TF_VAR_` from a GitHub secret.
- Tests: unit tests for the new gauge rows (pure functions over `counts()`, the today counts, the outbox counts and the targets), and a check that the generated JSON is current. At least one test asserts that the cohort gauge rows sum to the same numbers `outreach_report.render` prints for the same `FunnelCounts`.
