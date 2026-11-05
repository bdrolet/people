# LinkedIn send queue — design

Date: 2026-11-05. Status: draft for Ben's review.

This is the first of three related projects, built in this order:

1. **LinkedIn send queue** (this document).
2. **Recording qualified leads**: Ben marks a lead qualified, a judgment that has to be recorded because nothing in the data can infer it. Separate design.
3. **Outreach metrics and a Grafana dashboard as code**: leading and lagging indicators. Separate design. This document only defines the metrics the queue emits so that project can use them.

## Intent

Ben drafts LinkedIn messages (briefs, nudges, replies) in Claude sessions and approves the exact text. Today an approved message is sent right away through the `linkedin` MCP server. Two problems follow from that:

- **Bursts.** Approving several messages in a row sends them back to back. LinkedIn restricts accounts that send automated bursts, and that is the main risk to Ben's account.
- **No durable record.** Once the session closes, nothing records that a message was approved and waiting, or what happened to it.

The queue fixes both. An approved message is saved to the people DB, and a background sender on Ben's Mac delivers it at a safe pace, even after the session that approved it has closed. Every LinkedIn send, queued or immediate, gets a row, so the record and the metrics cover everything.

## Goals

- An approved LinkedIn message is stored durably with its exact text, recipient, the pipeline person it's for (if any), and when it was approved.
- A local sender delivers queued messages one at a time, spaced out and under a daily and weekly ceiling, during working hours, without a Claude session open.
- Any LinkedIn message can be queued. Some can be marked **send now**, which skips the wait and spacing but is still recorded and still counts against the ceiling.
- A send for an outreach person records the matching pipeline event (brief or nudge), exactly as an email brief does today.
- Ben can list, cancel or edit what's waiting, and see why anything was set aside.
- The sender emits metrics for the later dashboard project.

## Non-goals

- Drafting. Writing the text stays with `staging-linkedin-message` and Ben's approval in chat.
- Connection requests and InMail. A recipient who isn't a 1st-degree connection is set aside for Ben to decide.
- Reading replies. Detecting a reply stays with `checking-linkedin-messages` and `outreach mark … replied`.
- Running anywhere but Ben's Mac. The MCP's signed-in browser session lives in `~/.linkedin-mcp/`. Nothing deployed sends LinkedIn messages.
- The Grafana dashboard itself, and qualified-lead recording (projects 2 and 3).

## Anti-goals: it fails, even if it technically works, when

- **A message goes out twice.** A send whose outcome is unknown is never retried automatically.
- **A message goes out that Ben didn't approve**, or with different text from what he approved. The stored text is what gets sent, byte for byte. Editing a queued message means a new approval.
- **The sender keeps hammering after LinkedIn pushes back**, or after a captcha or an expired login.
- **Immediate sends quietly push past the ceiling.**
- **Message text leaves Ben's machine** through metrics, logs or Asana. The repo is public, and message text belongs only in the DB (and in the contact doc, which is already the agreed exception).

## Constraints

- **The MCP has no pacing of its own and LinkedIn publishes no limit.** Version 4.29.0 detects throttling and returns `rate_limited` with `retry_after` (seconds).
- **`send_message` is the only send tool in 4.29.0.** `reply_to_conversation` does not exist in that version, although `.claude/settings.*` and the staging skill still name it. `send_message(linkedin_username, message, confirm_send, profile_urn=None)` targets a profile, and its docs warn it "may create a separate DM" instead of replying in an existing thread. The server runs `@latest`, so tools can appear or change on any launch.
- **`send_message` reports outcomes through** `sent`, `retry_safe`, `status` and `thread_id` (present only when `sent`). Status values seen in the source: `confirmation_required` (dry run), `enter_to_send_enabled`, `composer_occupied`, `composer_unavailable`, `compose_interact_failed`, `recipient_resolution_failed`, `message_unavailable`, `send_unavailable`, `send_unconfirmed`, and `outcome_unknown` (transport: `sent` is absent).
- **The message text is validated.** LF/CRLF/CR end lines, leading and trailing whitespace is trimmed, and tabs, other control characters, DEL, U+0085, U+2028 and U+2029 are refused.
- **The MCP shares one browser** with Ben's Claude sessions. `BrowserBusyError` ("browser is busy") means another session is using it. That is contention, not a failure.
- **Layer rules** (CLAUDE.md): `repo/` holds the SQL, `services/` the logic, `clients/` the I/O, and `scripts/` the CLIs. People's Cloud Functions never send LinkedIn messages. The queue tables are written by local scripts only.
- **Account:** Ben has no LinkedIn Premium or Sales Navigator.

## Decided (with Ben)

- **Where the sender runs:** in the background on Ben's Mac, independent of any Claude session.
- **The ceiling:** **50 sends a day and 250 a week**, configurable. Vendor guidance for free accounts runs 40–100 a day to 1st-degree connections, and Ben asked for more than the 20 a day first proposed.
- **Pace:** at least **3–8 minutes apart (randomized)**, **weekdays, 8 AM–7 PM Pacific** only.
- **Ramp-up:** for the first two weeks the daily ceiling rises linearly **from 30 to 50**, starting from the first queued send. The weekly ceiling scales with it.
- **Every LinkedIn message is queueable**, and any of them may be marked **send now**:
  - It skips the wait and spacing (it is sent in the approving session, see below).
  - It is still recorded and counts against the daily and weekly ceiling.
  - Once the ceiling is reached, a send-now message is **refused, not sent**, and Ben is told. He can queue it instead.
- **Failure handling:**
  - **`rate_limited`:** pauses the **whole queue** until `retry_after` plus a margin has passed.
  - **Login expired, captcha or verification, `enter_to_send_enabled`:** pause the **whole queue** until Ben clears it (`outreach linkedin resume`).
  - **One message's problem** (`message_unavailable`, which in 4.29.0 is the not-1st-degree / no Message action case, plus `composer_occupied` and `recipient_resolution_failed`): that message is **set aside** with the reason, and the queue moves on.
  - **Anything else pauses the whole queue** *(my call)*. This covers `composer_unavailable`, `compose_interact_failed`, `send_unavailable`, raised errors (`AUTH_REQUIRED`, `ACCOUNT_RESTRICTED`, `BROWSER_UNAVAILABLE`, owner/peer browser errors other than busy), and any status a future version adds. These can mean a LinkedIn UI change that would hit every message, so the safe default is to stop. Three set-asides in a row also pause the queue, for the same reason.
  - **`send_unconfirmed`, `outcome_unknown`, or any result with `retry_safe: false` and `sent` not true:** the message is marked **check LinkedIn**. It is never retried. Ben (or a session using `checking-linkedin-messages`) confirms whether it went out, and records it as sent or puts it back in the queue.
- **Pipeline events:** a confirmed send to an outreach person records a brief or a nudge with `channel = linkedin`, the same way `advance_from_sent` does for email: brief a `to_contact` row, nudge a `briefed` one, and leave any other stage alone.
- **Dashboard plan (project 3):** Grafana Cloud, defined as code with Terraform's `grafana/grafana` provider and the Python Grafana Foundation SDK. This project only emits the metrics listed below.

## Design

### Data: `linkedin_outbox`

One row per approved LinkedIn message, in `repo/schema.sql`. Columns, as a sketch; the builder may rename them:

| Column | Notes |
|---|---|
| `id` | BIGSERIAL PK |
| `person_id` | FK → `people.id`, nullable. A message to someone with no `people` row is allowed. `ON DELETE SET NULL`, and `scripts/merge_duplicate_contacts.py` must re-point it before deleting a loser row, like the WhatsApp and iMessage re-points. |
| `linkedin_username` | The recipient slug passed to `send_message`. Required. |
| `profile_urn` | Optional, passed through for the MCP's recipient check. |
| `thread_id` | Optional. The existing thread this message answers, kept for the record and for a future reply tool. |
| `message` | The exact approved text. Validated against the MCP's rules when it is queued, so a bad message fails at approval, not at 9 PM. |
| `kind` | `brief`, `nudge`, `reply`, or `other`, as Ben's intent when queuing. The pipeline event is decided at send time from the person's stage (see Pipeline events), so `kind` is descriptive only. |
| `send_now` | Boolean. |
| `status` | `queued` → `sending` → `sent`, plus `set_aside`, `check_linkedin`, `cancelled`. |
| `reason` | The MCP status or error code behind `set_aside`, `check_linkedin` or a pause. |
| `approved_at`, `not_before` | `not_before` is optional, for "send this tomorrow morning". |
| `attempted_at`, `sent_at` | |
| `sent_thread_id` | The `thread_id` returned on `sent`. |
| `outreach_event_id` | FK → `outreach_events.id`, set when a brief or nudge was recorded. |
| `session_hint` | Optional free text: which agent-deck session queued it. |
| `created_at`, `updated_at` | |

Queue-wide state lives in one small `linkedin_sender_state` row (or a `sync_state`-style key):

- `paused_until`, for the `rate_limited` backoff.
- `paused_reason`, for pauses that need Ben.
- `last_sent_at`.
- `next_gap_seconds`, the randomized gap drawn after each send.
- `ramp_started_on`.

**Message text is personal data.** It stays in this table, is never logged, and is never put in a metric label or an Asana task. That matches the existing rule for iMessage, WhatsApp and LinkedIn text.

### The sender

`scripts/linkedin_sender.py`, run by a launchd agent (`~/Library/LaunchAgents/`, installed by `scripts/link-skills.sh` or a sibling install script) every **2 minutes**. Each run sends **at most one message**:

1. Exit quietly outside the send window, or while `paused_until` or `paused_reason` is set. A `rate_limited` pause clears itself once its time has passed.
2. Exit if `now < last_sent_at + next_gap_seconds`.
3. Exit if today's or this week's sends (every `sent` row, queued or send-now) have reached the current ceiling, including the ramp.
4. Claim the oldest eligible `queued` row (`not_before` passed). Lock it, mark it `sending`, set `attempted_at`, and commit **before** calling the MCP. A crash after this point then leaves a `sending` row, which the next run turns into `check_linkedin` rather than resending.
5. Start the MCP over stdio with the Python `mcp` client. First call `get_session_status`, which opens no browser, and pause the queue with reason `login` if the session isn't usable. A background run must never start a login or pop a browser on Ben's screen. Then call `send_message(..., confirm_send=True)` with the stored text.

   **The sender pins the MCP version** *(my call)*. It reads `LINKEDIN_MCP_VERSION` (initially `4.29.0`) and runs `uvx mcp-server-linkedin==<version>`. The status-to-action mapping is written against that version, and an unattended sender must not pick up a reshaped result on its own. Claude sessions stay on `@latest` through `.mcp.json`. Bumping the pin is a hand step: read the new version's `send_message` result contract, update the mapping if needed, and change the env var.
6. Map the result onto the rules above and record it. On `sent`, record the pipeline event and set `outreach_event_id`, all in the same transaction as the status change.
7. On `BrowserBusyError`, put the row back to `queued` and exit. The next run retries. This is safe because nothing was submitted (`retry_safe` is true).
8. Draw the next gap and emit metrics.

**Why one message per run, on a 2-minute timer:** no long-lived process to babysit, a crash costs at most one message's state, and sleep or wake on the Mac needs no handling. Missed windows just resume.

**The day and week for the ceiling** are counted in America/Los_Angeles, matching the send window.

**A `sending` row older than 15 minutes** (a crashed run) becomes `check_linkedin` *(my call)*.

### Replies

Ben decided any LinkedIn message can be queued, replies included. 4.29.0 can only send through `send_message`, which targets a profile. For a 1st-degree connection, a profile's Message action normally opens the existing 1:1 conversation, but the MCP doesn't promise that. So *(my calls)*:

- A reply is sent with `send_message` like any other message, and the row keeps the `thread_id` it was meant for.
- When the send comes back `sent` with a `thread_id` that differs from the row's `thread_id`, the row is still `sent` (it did go out), but it is flagged `thread_mismatch` and shows in `status` and `today`, so Ben knows a separate DM may have been opened.
- InMail or recruiter threads (no Message action on the profile) come back `message_unavailable` and are set aside, like any non-connection.
- If a future MCP version adds a thread-targeted send tool, the pin bump is where the sender switches to it for rows with a `thread_id`.

### Send now

A session that has Ben's approval for a send-now message:

1. Calls `outreach linkedin queue --send-now …`.
2. The CLI checks the ceiling. If the ceiling is reached, it refuses with a clear message and writes no row.
3. Otherwise it writes the row as `sending` and the session calls `mcp__linkedin__send_message` itself, in front of Ben.
4. The session reports the result with `outreach linkedin result <id> --status … [--thread-id …]`, which applies the same rules as the sender.

Send-now messages ignore the spacing gap and the send window, but they do update `last_sent_at`, so the next queued send still waits its gap.

### Sends Ben makes himself

A message Ben sends by hand through the clipboard path also gets a row, *(my call)* written by `outreach linkedin record …` with status `sent`, `mode = manual`. It counts against the ceiling and updates `last_sent_at`, because the ceiling protects the account, and the account doesn't care who pressed Send. It records the pipeline event through the same path as a queued send, replacing the skill's old step 6 (`outreach mark`/`nudge`) for LinkedIn.

**My call:** the session sends send-now messages itself rather than triggering the sender, because Ben is watching and the result belongs in that conversation.

### Pipeline events

A shared function in `services/outreach.py`, beside `advance_from_sent`, takes a person and a send time and returns the event it wrote: a brief for `to_contact` (via `advance` with `channel='linkedin'`), a nudge for `briefed` (via `nudge`), or nothing for any other stage. `advance_from_sent` and the new path should share the stage decision instead of copying it.

Events need a `source` for queued sends. `outreach_events.source` currently allows `event`, `cli`, `hubspot` and `seed`. Add **`linkedin_outbox`**, or reuse `cli`; the builder decides, and the choice should keep the existing Asana sync and `outreach report` counting LinkedIn briefs exactly as they count email briefs.

**Dedupe:** the `outreach_event_id` on the outbox row is the guard. A row that already has one never writes another.

### CLI

Subcommands under `scripts/outreach.py linkedin` (the builder may make this its own script if that reads better):

- `queue --person <ident> | --username <slug> --kind … [--send-now] [--not-before …] [--thread-id …] --message-file <path>`: validates the text and the ceiling (for send-now), writes the row, and prints its id and expected send time.
- `list [--status …] [--json]`: what's waiting, set aside or needing a check, with the estimated send time for each queued row. It never prints full message text by default; `--show-text` prints it.
- `cancel <id>`, and `edit <id> --message-file <path>`. Edit is allowed only while `queued`. It resets `approved_at` and requires the caller to have Ben's new approval.
- `result <id> --status sent|set_aside|check_linkedin [--thread-id …] [--reason …]`: for send-now messages, and for resolving a `check_linkedin` row by hand.
- `record --person <ident> | --username <slug> --kind … --message-file <path> [--at …]`: a message Ben sent himself.
- `requeue <id>`: puts a `set_aside` or `check_linkedin` row back to `queued`. For `check_linkedin` rows, only after confirming on LinkedIn that the message did not go out.
- `pause [--reason …]`, `resume`, and `status`: queue state, today's and this week's count against the ceiling, the next send time, and the last error.

`outreach today` gains one line: queued count, the oldest queued age, and any pause or `check_linkedin` rows. These are things Ben has to act on.

**Pace counts a LinkedIn brief once it's sent, not when it's approved** (Ben's decision). `today_counts` keeps counting `briefed` events and is unchanged. The queue line is what shows approved work that hasn't gone out yet, so a batch approved late in the day reads as "N queued", not as being behind pace.

### Skill changes

- **`staging-linkedin-message`:**
  - After Ben's yes, the default is `outreach linkedin queue …`, which reports the expected send time. **Send now** (Ben says "send it now", or it is a live reply) uses the send-now flow above.
  - Step 6 (record it) becomes `outreach linkedin record …` for messages Ben sends himself, and goes away for queued and send-now messages, which record their own event.
  - Drop the `reply_to_conversation` references. Use `send_message` and keep the `thread_id` on the row.
- **`checking-linkedin-messages`:** gains how to resolve a `check_linkedin` row: read the thread, then `result … --status sent` or `requeue`.
- **`working-outreach-queue`:** mention the queue line in `today`.
- **`.claude/settings.json` and `settings.local.json`:** `reply_to_conversation` no longer exists. Leave the allow and ask entries alone, since they are harmless, but the CLAUDE.md line should say `send_message` is the only send tool.
- **CLAUDE.md:** add the table to the table list, plus a stack-table row for the sender (launchd label, interval, ceiling env vars), and note that only local scripts write `linkedin_outbox`.

### Configuration

The following env vars (in `.env`, defaults in code) are not personal, so the defaults may live in the repo:

- `LINKEDIN_DAILY_CAP=50`
- `LINKEDIN_WEEKLY_CAP=250`
- `LINKEDIN_RAMP_START=30`
- `LINKEDIN_RAMP_DAYS=14`
- `LINKEDIN_GAP_MIN_SECONDS=180`
- `LINKEDIN_GAP_MAX_SECONDS=480`
- `LINKEDIN_SEND_WINDOW="Mon-Fri 08:00-19:00 America/Los_Angeles"` (format is the builder's call)

### Metrics (for project 3)

Instruments in `clients/otel.py`, service name `people-linkedin-sender`, all under `people.linkedin_*`:

- `people.linkedin_sends` (counter): label `outcome` (`sent`, `set_aside`, `check_linkedin`, `rate_limited`, `paused`, `refused_ceiling`, `browser_busy`), label `mode` (`queued`, `send_now` or `manual`), and label `kind`.
- `people.linkedin_queued` (counter): messages approved into the queue, by `kind` and `mode`.
- `people.linkedin_queue_depth` (gauge): `queued` rows.
- `people.linkedin_queue_oldest_age_seconds` (gauge).
- `people.linkedin_sends_today` and `people.linkedin_sends_week` (gauges), alongside `people.linkedin_cap_today` and `people.linkedin_cap_week` (gauges, ramp-aware), so the dashboard can draw the ceiling line.
- `people.linkedin_approval_to_send_seconds` (histogram).
- `people.linkedin_sender_heartbeat` (gauge, the Unix time of the last run). This one is emitted even on runs that send nothing, so a sleeping Mac shows up as a stale heartbeat.
- `people.linkedin_sender_paused` (gauge, 0 or 1), with a `reason` label.

No person, username or text appears in any label. Each run calls `otel.flush()` before exiting.

## Left to the builder

- Exact table, column, status and CLI names.
- Whether the Python `mcp` client goes in `requirements-dev.txt` or a separate local-only requirements file. It must **not** land in `requirements.txt`, which the Cloud Functions install.
- The `source` value for outbox events, subject to the counting rule above.
- The launchd plist details and how it is installed and uninstalled.
- The test split. Pure logic (window, gap, ceiling with ramp, result → status mapping) belongs in a pure service with unit tests. MCP calls are faked in tests.
