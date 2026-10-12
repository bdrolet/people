# Referral Outreach Tooling — Design

**Date:** 2026-10-12
**Status:** Draft, awaiting review
**Repo:** `bdrolet/people`
**Reads with:** the consulting repo's `docs/client_funnel.md` (private; the floors, read points and checkpoints) and `docs/referral_outreach_tool.md` (the original tool sketch this design supersedes).

## 1. Summary

Cycle one of the consulting strategy is a warm-referral test: brief the warm
network by hand, ask each person for one name, and count what happens at
every stage from brief to signed engagement. The funnel is only useful if
every stage is recorded as a count the moment it happens, and the plan only
reaches a readable volume (150 briefs, 250 as the stretch) if the research,
sending, logging and reminders cost minutes a day instead of hours.

This design adds a **referral pipeline** to the people service: a durable
record of each contact's stage, a buyer-side record for every introduction,
automatic stage advances from the email events the service already receives,
a mirror of both into HubSpot for the working view and reminders, a
completed Asana task per stage event so the strategy layer's leads and
tripwires count themselves, and a weekly report of actuals against the
funnel floors.

The words of every message stay Ben's. Nothing here drafts, sequences or
sends on its own.

## 2. Goals and non-goals

**Goals**

1. One place that says, for every warm contact, what stage they are at and
   when they got there. Counts derive from that record, never from memory.
2. Cut the per-brief cost to about two minutes: the context Ben needs to
   write is on screen, the send is one action, the logging is zero actions.
3. Advance stages automatically wherever an event already proves the
   transition: Ben's outgoing mail proves *briefed*, their incoming mail
   proves *replied*.
4. Feed the strategy layer with no double entry: each stage event becomes one
   completed Asana task carrying the stage tag and `serves:consulting`.
5. Report actuals against the floors weekly, stage by stage, with the
   "readable yet" flag from the funnel doc, so a miss is seen the week it
   becomes real.
6. Support a volume of 40 briefs a week without the tooling being the limit.

**Non-goals**

- Drafting message text, suggesting wording, or any sequence or automated
  send. The strategy forbids it and the tool sketch's design principle stands.
- Open or click tracking.
- A web UI. The working surfaces are a HubSpot saved view, a CLI, and
  Claude skills over `people-api`.
- Detecting replies on iMessage, WhatsApp or LinkedIn automatically. Those
  arrive as snapshot imports, not events; they are marked by hand in phase 1
  (§6.3 notes the phase-2 path).
- Revenue reporting. The lag measure is reported by hand through tasks-api as
  today.

## 3. Where state lives, and why

The tool sketch assumed HubSpot as the system of record. This design puts the
record in the `people` database and makes HubSpot a derived mirror, for three
reasons that come from this repo:

- **The repo's rule is durable DB first, best-effort side effects second.**
  `handlers/email_classified.py` writes counters and eligibility in a
  transaction and only then touches Google and HubSpot, and
  `services/hubspot_mirror.py` owns the retry-or-swallow policy. Reply
  detection (§6.3) runs inside that handler and must read the contact's
  stage; a read against HubSpot there would add a network dependency to the
  durable path.
- **The HubSpot mirror is bounded and evicts.** `hubspot_mirror.cap()` is the
  free-tier 1,000 and `_evict_oldest` archives the oldest contact when it is
  full. A stage stored only as a HubSpot property would be archived with the
  contact.
- **Every other reader is already on the DB.** `people-api`, the skills, and
  the import scripts query Postgres. The report (§9) is a query.

HubSpot keeps what it is good at: the saved view Ben works down, the deal
board for buyers, and follow-up task reminders. Everything in HubSpot can be
rebuilt from the DB with one reconcile.

## 4. Stage model

Two pipelines, because referrers and buyers are different people and
different counts.

### 4.1 Referrer pipeline (one row per warm contact)

```
candidate → to_contact → briefed → replied → introduced
                                          ↘ declined
                 ↘ skipped
```

| Stage | Meaning | Set by |
|---|---|---|
| `candidate` | Proposed by the seed script (§5) from the warm-pool bars; not yet Ben's decision | `outreach seed` |
| `to_contact` | Ben has decided to brief this person | CLI, or HubSpot view |
| `briefed` | A note with the one-line offer and the one-name ask has been sent | `email_sent` event (§6.2), or CLI for other channels |
| `replied` | They answered in any channel. A name mentioned without an introduction keeps them here. | `email_classified` event (§6.3), or CLI |
| `introduced` | At least one introduction with the buyer on the thread has been made. Creates an opportunity (§4.2). | CLI (`outreach intro`) |
| `declined` | Replied and said no, or no reply after the follow-up window | CLI |
| `skipped` | Ben decided not to brief them | CLI |

A contact can be `introduced` more than once; the stage is the furthest
reached, and each introduction is its own opportunity row.

### 4.2 Buyer pipeline (one row per introduction)

```
intro → conversation → proposal → signed
     ↘ lost (from any stage, with a reason)
```

| Stage | Meaning | Set by |
|---|---|---|
| `intro` | An introduction was sent with buyer and Ben on the thread. A name Ben must chase is **not** an intro; it is a `lead` flag on the referrer row (§4.3). | `outreach intro` |
| `conversation` | A scheduled call or meeting with the buyer happened and the offer was discussed | `outreach conversation` |
| `proposal` | A written scope and price went to the buyer | `outreach proposal` |
| `signed` | Signed agreement or paid deposit invoice | `outreach signed` |
| `lost` | Ended without signing; `lost_reason` is one of `no_budget`, `no_fit`, `price`, `scope`, `timing`, `no_response`, `other` | `outreach lost` |

The `lost_reason` list is the qualitative read for the proposal and close
stages, which the funnel doc says cannot be read from counts this cycle. The
report (§9) tallies reasons.

### 4.3 Stage events

Every transition is appended to an event log with the stage, the time, the
channel (`email`, `imessage`, `whatsapp`, `linkedin`, `call`, `in_person`),
the source (`event`, `cli`, `hubspot`, `seed`), and later the Asana task gid the
event produced. Counts, cohorts and the report all derive from this log, so
a correction is a new event, not an edit.

Transitions are monotonic within a pipeline: a request to set a stage the row
has already passed is a no-op that returns the current row, which is what
makes the event handlers (§6) safe to replay.

## 5. Schema

Added to `repo/schema.sql`, following the `linkedin_connections` conventions
(`person_id` FK). Pipeline rows cascade with their person, since a pipeline
row without its person is meaningless; only `buyer_id` uses `ON DELETE SET
NULL`, because the opportunity keeps its `buyer_label` and stays meaningful.

```sql
CREATE TABLE IF NOT EXISTS outreach_contacts (
    person_id      BIGINT PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    stage          TEXT NOT NULL CHECK (stage IN
                     ('candidate','to_contact','briefed','replied','introduced','declined','skipped')),
    cohort_week    DATE,                -- Monday of the week they were briefed
    briefed_at     TIMESTAMPTZ,
    replied_at     TIMESTAMPTZ,
    channel        TEXT,                -- channel of the brief
    has_lead       BOOLEAN NOT NULL DEFAULT FALSE,  -- gave a name Ben must chase
    follow_up_on   DATE,                -- next nudge; cleared on reply
    notes          TEXT,
    hubspot_synced_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outreach_opportunities (
    id             BIGSERIAL PRIMARY KEY,
    referrer_id    BIGINT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    buyer_id       BIGINT REFERENCES people(id) ON DELETE SET NULL,  -- null until the buyer has a people row
    buyer_label    TEXT NOT NULL,       -- "Name, Company" as typed at intro time
    stage          TEXT NOT NULL CHECK (stage IN ('intro','conversation','proposal','signed','lost')),
    lost_reason    TEXT CHECK (lost_reason IN
                     ('no_budget','no_fit','price','scope','timing','no_response','other')),
    intro_at       TIMESTAMPTZ NOT NULL,
    conversation_at TIMESTAMPTZ,
    proposal_at    TIMESTAMPTZ,
    signed_at      TIMESTAMPTZ,
    lost_at        TIMESTAMPTZ,
    hubspot_deal_id TEXT UNIQUE,
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outreach_events (
    id             BIGSERIAL PRIMARY KEY,
    pipeline       TEXT NOT NULL CHECK (pipeline IN ('referrer','buyer')),
    person_id      BIGINT REFERENCES people(id) ON DELETE CASCADE,       -- referrer pipeline
    opportunity_id BIGINT REFERENCES outreach_opportunities(id) ON DELETE CASCADE,  -- buyer pipeline
    stage          TEXT NOT NULL,
    at             TIMESTAMPTZ NOT NULL,
    channel        TEXT,
    source         TEXT NOT NULL CHECK (source IN ('event','cli','hubspot','seed')),
    graph_message_id TEXT,              -- the email that proved it, when source = 'event'
    asana_task_gid TEXT,                -- set once the strategy task exists (§8)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((pipeline = 'referrer') = (person_id IS NOT NULL)),
    CHECK ((pipeline = 'buyer') = (opportunity_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS outreach_events_stage_at_idx ON outreach_events (pipeline, stage, at);
CREATE INDEX IF NOT EXISTS outreach_events_unsynced_idx ON outreach_events (id) WHERE asana_task_gid IS NULL;
```

A `people` row can be deleted and rebuilt (the schema comment on
`linkedin_connections` warns about this). Pipeline rows cascade with it, so
the rebuild runbook must export `outreach_*` first. §12 lists it.

## 6. Stage advances from events

`people-process` already receives every `email_sent` and `email_classified`
event. Two small additions to the existing handlers advance the referrer
pipeline. Both run inside the handler's transaction, after the durable
counters and before the best-effort side effects, and both are no-ops when
the person has no `outreach_contacts` row or is past the stage in question.

### 6.1 Service: `services/outreach.py`

Owns the stage machine. Public functions, each taking an open connection:

- `advance(conn, person_id, stage, *, at, channel, source, graph_message_id=None) -> Row | None`
  Monotonic; appends the event; sets `cohort_week` on the first `briefed`;
  clears `follow_up_on` on `replied`; sets `follow_up_on = at + 7 days` on
  `briefed`.
- `open_opportunity(conn, referrer_id, buyer_label, *, at, buyer_id=None) -> Opportunity`
  Also advances the referrer to `introduced`.
- `advance_opportunity(conn, opportunity_id, stage, *, at, lost_reason=None) -> Opportunity`
- `queue(conn, *, limit) -> list[QueueRow]` — §7.
- `counts(conn, *, since, until) -> FunnelCounts` — §9.

### 6.2 `email_sent` proves *briefed*

In `handlers/email_sent.py`, for each recipient whose row is at
`to_contact`: `advance(..., "briefed", at=sent_at, channel="email", source="event",
graph_message_id=event["graph_message_id"])`.

Guard: only when the sending identity is Ben's consulting address
(`OUTREACH_FROM_ADDRESSES`, a new env var, so the public repo holds no
address). A note sent from another identity does not count as a brief.

A contact at `candidate` is not advanced. Deciding to brief someone is Ben's
act (`to_contact`); the email is the proof it happened.

### 6.3 `email_classified` proves *replied*

In `handlers/email_classified.py`, when the sender's row is at `briefed` and
the event's `category` is not `ignore` and the row is not `automated`:
`advance(..., "replied", at=received_at, channel="email", source="event", ...)`.

An out-of-office is classified `ignore` by inbox and does not count. A reply
on a different thread still counts; the stage machine does not care which
thread, only that they wrote back after the brief.

**Other channels.** Texts and LinkedIn messages arrive as snapshot imports
(`import_imessage.py`, `import_linkedin.py`, `import_whatsapp.py`). In
phase 1 Ben marks these by CLI. Phase 2 (§13) can have each import script
advance `briefed` or `replied` when it sees a message from Ben or from the
contact after the stage's timestamp, with `source="event"` and the message's
own channel.

## 7. The working surfaces

### 7.1 Seed: `scripts/outreach_seed.py`

Proposes candidates from the warm-pool bars measured on 2026-10-12 and
inserts them at `candidate`:

- email: eligible, not automated, both sides have written, any interaction in the window;
- LinkedIn: both sides messaged, position not matching recruiter terms, last message in the window;
- iMessage and WhatsApp: 1:1 thread, Ben replied, at least N messages, last message in the window.

Flags: `--window-days` (default 730), `--min-messages` (default 10),
`--dry-run`. `outreach decide` also adds the Google Contacts label
`consulting-outreach` through the existing label path (`PATCH /people`), so
the cohort is visible as a list on the phone, where most texts will be sent.
The label marks membership only; the stage is never a label, because it
would churn on every advance and Google is not the record (§3). Candidates from LinkedIn or a phone-only handle with no `people`
row are listed but not inserted; creating the person is the existing
`creating-person` path. Idempotent: an existing pipeline row is never
downgraded to `candidate`.

### 7.2 Queue: `GET /outreach/queue` and `outreach queue`

Returns contacts at `to_contact` (and, with `--stage`, any stage), oldest
interaction first, each with what Ben needs on screen before writing:

```json
{
  "person": {"id": 42, "email": "...", "display_name": "...", "company": "...", "job_title": "..."},
  "stage": "to_contact",
  "last_interaction": "2025-03-02",
  "channels": {"email": true, "imessage": true, "linkedin": "linkedin.com/in/...", "whatsapp": false},
  "last_messages": {"email": "2025-03-02", "imessage": "2026-07-19", "linkedin": "2024-11-30"},
  "labels": ["former-colleague"],
  "notes": "...",
  "follow_up_on": null
}
```

`channels` is the point: the funnel doc expects texts to beat email on reply
rate, so the queue should make the warmest channel obvious.

The CLI (`scripts/outreach.py`, subcommands below) prints the same as a
table. A HubSpot saved view (§10) shows the mirrored subset for working from
a phone.

### 7.3 Send

Email briefs go through `inbox-api` drafts, which the `sending-inbox-email`
skill already does: create the draft, Ben edits and sends from Outlook or the
skill sends it, inbox publishes `email_sent`, and §6.2 advances the stage.
No Graph code is added to people, which keeps the repo's rule that only local
import scripts touch Graph.

`outreach draft <person>` creates the draft itself and always passes
`from: {address: <consulting address>, shared: false}`, so the sending
identity is never a judgment call at send time. The consulting address is an
alias on the primary mailbox (no shared mailbox is configured), which is why
the sent note lands in the primary Sent Items and inbox's existing
subscription publishes it with `from` set. A brief sent by hand from the
wrong identity is not counted: the contact stays at `to_contact` and shows
up in `outreach queue` as unsent, which is the signal to resend or to mark
it with `--channel email --from <address>` if it was deliberate.

For texts and LinkedIn, Ben sends by hand and runs
`outreach mark <person> briefed --channel imessage`.

### 7.4 CLI: `scripts/outreach.py`

| Command | Effect |
|---|---|
| `outreach queue [--stage S] [--limit N]` | §7.2 |
| `outreach decide <person> [--skip]` | `candidate → to_contact`, or `skipped` |
| `outreach draft <person> [--subject S]` | creates an empty-bodied inbox-api draft to the person from the consulting alias; Ben writes the body in Outlook |
| `outreach mark <person> briefed\|replied\|declined [--channel C] [--at T]` | manual advance |
| `outreach lead <person> "<name, company>"` | sets `has_lead`; does not create an opportunity |
| `outreach intro <referrer> "<buyer name, company>" [--buyer <person>] [--at T]` | opens an opportunity, referrer → `introduced` |
| `outreach conversation\|proposal\|signed <opportunity-id> [--at T]` | buyer advance |
| `outreach lost <opportunity-id> --reason R` | buyer lost |
| `outreach followups` | rows with `follow_up_on <= today` |
| `outreach report [--floors path] [--since D]` | §9 |
| `outreach sync-asana` / `outreach sync-hubspot` | §8, §10, for backfill and repair |

`<person>` resolves the way `people-api` does: email, phone, or id.

## 8. Strategy sync: one completed Asana task per stage event

The strategy layer in tasks counts a lead only on a **completed** task carrying
the lead tag and `serves:consulting`. So every stage event that maps to a lead
or tripwire tag produces exactly one Asana task, created complete.

| Event | Tags | Task name |
|---|---|---|
| referrer `briefed` | `outreach`, `serves:consulting` | `Brief: <display name>` |
| buyer `intro` | `intro`, `serves:consulting` | `Intro: <buyer label> via <referrer>` |
| buyer `conversation` | `conversation`, `serves:consulting` | `Conversation: <buyer label>` |
| buyer `proposal` | `proposal`, `serves:consulting` | `Proposal: <buyer label>` |
| buyer `signed` | `signed-client`, `serves:consulting` | `Signed: <buyer label>` |

Other events (`replied`, `declined`, `lost`) produce no task; they are
counted by the report, not by the strategy layer.

**Mechanism.** `clients/tasks_api.py`, modelled on tasks' `clients/inbox_api.py`:
`POST /tasks` with `name`, `tags`, `context` (a one-line pointer: the stage,
the date, the channel), then `PATCH /tasks/{gid}` with `completed: true`.
Auth is an identity token for the tasks-api audience via the same
`gcp_auth.id_token_for` pattern. The gid is written to
`outreach_events.asana_task_gid`; rows with a null gid are retried by
`outreach sync-asana` and by the nightly `people-sync` run, so a tasks-api
outage never loses a count.

**Where it runs.** Inside the handlers (§6) it is a best-effort side effect
after commit, like the HubSpot mirror. From the CLI it runs inline. It is
never on the durable path.

**Timing matters.** The strategy layer counts by the task's `completed_at`,
which is when Asana saw the completion, not the stage event's `at`. A sync
that lags a week moves the count into the wrong week and can fire or miss a
tripwire. So the sync runs in the handler and the CLI immediately, the
nightly retry is a backstop, and `outreach sync-asana` prints how many events
were synced late and by how much.

**IAM.** The `people-process-cf` and `people-sync-cf` service accounts need
`roles/run.invoker` on `tasks-api`. Today `terraform/api.tf` in the tasks
repo grants invokers only to users (`api_invoker_users`) and the deployer; a
new `api_invoker_service_accounts` list variable with the same `for_each`
shape carries the two people accounts. Ben's user already has it for the
CLI. See §12.1.

**Tags.** `outreach`, `conversation`, `proposal`, `signed-client` exist in
the strategy today; `intro` is new and is created on first use through
tasks-api's tag resolution.

## 9. Report: actuals against the floors

`outreach report` prints, for the whole test and per cohort week:

```
Cohort      Briefed  Replied  Intro  Conv  Prop  Signed   Reply%  Intro%  Conv%
2026-10-13       27       14      3     1     0       0     52%     11%    4%
2026-10-20       31       12      2     0     0       0     39%      6%    0%
...
All             150       60     12     7     2       0     40%      8%   4.7%

Stage          Actual  Floor@n  Readable?  Verdict
Replied            60       60   yes        at floor
Intro              12       12   yes        at floor
Conversation        7        7   yes        at floor
Proposal            2      2.4   no (n<186) --
Signed              0      0.8   no (n<564) --

Lost reasons: no_budget 1, timing 1
Follow-ups due: 9
```

Rates are per briefed contact. `Floor@n` multiplies the floor rate by the
briefed count that has had time to reach the stage (the cohort lag from the
funnel doc: 1 week to reply, 3 to intro, 5 to conversation, 7 to proposal,
11 to sign), so a cohort briefed yesterday does not drag the reply rate down.
`Readable?` applies the funnel doc's thresholds. `Verdict` is `above`, `at`
or `below` the floor, with `below` only when the readable threshold is met.

The floors and thresholds are **not in this repo**. They are read from a
small TOML file passed with `--floors`, which lives beside
`client_funnel.md` in the private consulting repo:

```toml
[floors]           # rate per briefed contact
replied = 0.40
intro = 0.08
conversation = 0.048
proposal = 0.016
signed = 0.0053

[readable_after]   # briefed count before a zero means anything
replied = 6
intro = 36
conversation = 61
proposal = 186
signed = 564

[lag_weeks]
replied = 1
intro = 3
conversation = 5
proposal = 7
signed = 11
```

Without `--floors` the report prints counts and rates only. The same
function backs `GET /outreach/report` so a skill can ask for it.

## 10. HubSpot mirror

Derived, best-effort, rebuildable. Reuses `clients/hubspot.py` and the
mirror's existing policy.

- **Contact property** `referral_stage` (one custom property; free tier
  allows about ten) set on every mirrored contact with a pipeline row.
  Written on each referrer advance; reconciled nightly in
  `services/hubspot_mirror.reconcile`.
- **Saved view** "Outreach queue": `referral_stage = to_contact`, owner Ben,
  sorted by last activity ascending. Created by hand once; nothing in code
  depends on it.
- **Deal pipeline** (the one free pipeline), stages `Intro`, `Conversation`,
  `Proposal`, `Signed`, `Lost`. One deal per opportunity, named with the
  buyer label, associated to the referrer contact and to the buyer contact
  when known. `hubspot_deal_id` on the opportunity row. Written on each
  buyer advance.
- **Follow-up tasks.** A HubSpot task on the contact, due `follow_up_on`,
  created on `briefed`, closed on `replied`. This is what makes the queue
  workable from a phone; Asana is not involved.

A pipeline row whose contact has been evicted from the mirror (§3) is simply
not mirrored; the DB is unaffected and the row is mirrored again if the
contact comes back.

**Direction.** HubSpot never advances a stage on its own in phase 1. If Ben
drags a deal on the board, the next reconcile logs the difference and
`outreach sync-hubspot --pull` applies it as a `source="hubspot"` event. This
keeps one writer per transition.

## 11. Testing

Follows the existing layout under `tests/`.

- `test_outreach.py`: the stage machine. Monotonic advances, replay safety,
  cohort week assignment, `follow_up_on` set and cleared, opportunity open
  and advance, lost reasons, `counts()` against fixture events including
  cohort lag.
- `test_handlers.py` additions: `email_sent` from a consulting address
  advances `to_contact → briefed` and nothing else; from another identity it
  does not; `email_classified` from a `briefed` contact advances to `replied`,
  an `ignore` category does not, an `automated` sender does not, and a
  contact with no pipeline row is untouched.
- `test_tasks_api.py`: payloads for each event type; a failed `POST` leaves
  `asana_task_gid` null and `sync-asana` retries it; a `PATCH` failure after a
  successful `POST` records the gid and retries only the completion.
- `test_outreach_report.py`: the table against known fixtures, floor at `n`
  with lag, `Readable?` thresholds, verdicts, and the no-floors mode.
- `test_hubspot_mirror.py` additions: property write, deal create and stage
  move, follow-up task create and close, and skip when the contact is not
  mirrored.

Handler tests use the existing fixtures and the `testing-people-handlers`
skill's harness. No test calls HubSpot, tasks-api or Postgres over the
network.

## 12. Operations

### 12.1 Changes in other repos

Everything that counts lives in people, but four other places need a small
change or none, checked against their code on 2026-10-12.

| Repo | Change | Why |
|---|---|---|
| **tasks** | `terraform`: `api_invoker_service_accounts` variable and `google_cloud_run_v2_service_iam_member` for the two people function accounts. No application change. | Tag names are resolved and created on first use (`services/tags.py`), a completed task's goal is read from its `serves:` tag when it has no score row (`services/goal_state.py`), and `completed_at` is indexed on `PATCH`. The strategy layer counts §8's tasks with no code change. |
| **inbox** | No application change. Update the `sending-inbox-email` skill's identity table and the `email-builder` agent brief so consulting correspondence is sent from the consulting alias. | The Sent Items subscription already publishes `email_sent` with `from` for mail sent from an alias on the primary mailbox. The identity rule is policy, and the tool enforces it on its own drafts (§7.3). |
| **context** | Already open: PR #4 sizes the referral test at 150 and ties the tripwires to the floors. The `intro` lead and tripwire in that PR assume §8 ships before the first cohort is briefed. | The tags §8 writes are the ones the strategy counts. |
| **consulting** | Done: `docs/client_funnel.md` and `docs/client_funnel_floors.toml`. The report's `--floors` path points there. | Floors stay out of the public repo. |
| **~/.claude/skills** | Phase 2: a `working-outreach-queue` skill linked from this repo's `.claude/skills` via `scripts/link-skills.sh`, and a pointer from `adding-referral-contact` to the new pipeline. | The skills are how Ben works the queue from a Claude session. |

- **Env.** `OUTREACH_FROM_ADDRESSES` (comma-separated; which sending
  identities count as a brief; `outreach draft` sends from the first),
  `TASKS_API_URL`, `INBOX_API_URL`. Both via Secret Manager and
  terraform like the existing secrets; the `adding-people-secret` skill is the
  path.
- **Migration.** `scripts/migrate_db.py` applies the new tables; it is
  additive and idempotent like the rest of `schema.sql`.
- **Rebuild runbook.** Before any `DELETE FROM people`, export the three
  `outreach_*` tables; they cascade.
- **Metrics.** See §12.2.
### 12.2 Telemetry

The service already exports OpenTelemetry metrics to Grafana Cloud through
`clients/otel.py`, and the original tool sketch named Grafana as the
reporting surface. The pipeline emits enough to chart the funnel and the
work behind it there, with these instruments added in the same style as
`people.events_received`:

| Instrument | Kind | Attributes | Emitted when |
|---|---|---|---|
| `outreach.stage_events` | counter | `pipeline`, `stage`, `channel`, `source` | every appended event (§4.3); this is the activity feed |
| `outreach.stage_population` | observable gauge | `pipeline`, `stage` | rows per stage, read from the DB on each `people-sync` run and on every `outreach` CLI invocation, so the funnel shape is a gauge, not a sum of counters |
| `outreach.follow_ups_due` | observable gauge | | rows with `follow_up_on <= today` |
| `outreach.lost` | counter | `lost_reason` | buyer `lost` |
| `outreach.sync` | counter | `target` (`asana`, `hubspot`), `outcome` (`ok`, `retry`, `failed`), `late` (bucketed: `0d`, `1d`, `2-7d`, `8d+`) | each sync attempt; `late` is `now - event.at` |
| `outreach.enrichment` | counter | `provider`, `outcome` | phase 3 lookups, so the PDL free quota is visible |
| `outreach.report_runs` | counter | `verdict_below` (count of stages below floor) | each `outreach report` run; the only place a floor verdict reaches Grafana, as a count, never a rate |

Rules:

- **Names and addresses never become attributes.** Attribute values are
  drawn from the closed enums above. Cardinality stays in the tens.
- **Grafana is the trend view, the DB report is the truth.** Counters count
  at emit time, so a late sync lands in the wrong day on a chart; the report
  (§9) computes cohorts from `outreach_events.at`. Dashboards show activity
  and shape; decisions at the read dates use the report.
- **Cohort week is an attribute only on the gauge**, where cardinality is one
  value per week of the test (`cohort_week` as `YYYY-Www`). That is what makes
  a per-cohort funnel panel possible without a per-person series.
- The CLI calls `otel.setup_telemetry("people-outreach-local")` and `flush()`
  on exit, the same as the import scripts, so local actions reach the same
  dashboards as the functions.

One Grafana dashboard, "Consulting funnel", built by hand from these: stage
population by week, stage events per week by channel, follow-ups due, lost
reasons, sync lateness. Dashboards are not in this repo today and this does
not change that.

- **Privacy.** The repo is public. No names, addresses, message text, floors
  or actuals are committed. Fixtures use the existing synthetic people. The
  floors file lives in the consulting repo. Task names in Asana carry
  contact names, which is fine: Asana is private.

## 13. Phases

**Phase 1, the counting layer (first).** Schema, `services/outreach.py`,
the two handler additions, `clients/tasks_api.py` and the Asana sync, the
CLI with `queue`, `decide`, `mark`, `lead`, `intro`, buyer advances,
`followups` and `report`, and the seed script. This is enough to brief at
volume and have every count land in the strategy layer. Target: shipped
before the first cohort is briefed, so week one's replies are counted from
the start.

**Phase 2, the working surfaces.** HubSpot mirror (property, deals,
follow-up tasks), `GET /outreach/queue` and `GET /outreach/report` on
people-api, and a `working-outreach-queue` skill. Snapshot-import stage
advances for iMessage, WhatsApp and LinkedIn. Calendar matching: a past
event whose attendees include an opportunity's buyer, or whose title starts
with `Intro call:`, is proposed as a `conversation` advance by
`outreach followups`; Ben confirms. Calendar events are never tagged for
counting, since the strategy layer counts only tasks.

**Phase 3, enrichment.** People Data Labs on `decide` (100 free lookups a
month covers the test), Apify on demand before writing. Both already
sketched in the tool doc and the `adding-referral-contact` skill; they slot
into the queue payload as `enrichment`.

Each phase is its own plan and PR set.

## 14. Open questions

1. **Which identities count as a brief.** `ben@drolet.ai` certainly; does
   mail from `drolet.cloud` or the personal address count when the note is a
   brief? The env var makes it a config choice, but the choice has to be
   made before §6.2 ships.
2. **One row per person, or per person per cycle.** This design keys
   `outreach_contacts` on `person_id`, which is right for cycle one. A second
   referral cycle in 2027 would need a `cycle` column and a composite key.
   Not built now; noted so the migration is expected.
3. **Buyer as a `people` row.** A buyer who has never emailed Ben has no
   `people` row until the intro thread arrives, which is why `buyer_id` is
   nullable and `buyer_label` is required. Linking them after the fact is a
   `PATCH` on the opportunity; whether to automate that from the intro
   thread's recipients is a phase-2 question.
