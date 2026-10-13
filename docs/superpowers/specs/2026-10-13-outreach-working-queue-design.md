# Outreach working queue: follow-ups, targets, ranking and a skill

**Status:** implemented (see `docs/superpowers/plans/2026-10-13-outreach-working-queue.md`). The design's §1–§6 were approved section by section in conversation on
2026-10-13. §7–§8 were reviewed the same day and revised: `adding-referral-contact`
is folded into `working-outreach-queue`, nudge items carry the brief's thread,
and the outcome commands match the CLI. See §12 for handoff.
**Builds on:** `2026-10-12-referral-outreach-tooling-design.md` (phase 1, shipped as PR #23).
Section references of the form "phase-1 §6.2" point there.

## 1. Purpose

Phase 1 records the pipeline and proves briefs and replies from email. It
does not tell Ben what to do today, whether he is keeping pace with the
plan, or who to chase. This design adds that, as a dedicated queue that
does not compete with the rest of his task list:

- **Follow-ups** become recorded facts with a fixed cadence (§3).
- **Targets**: a fixed daily and weekly brief target. The nudge target is
  everyone due (§4).
- **`outreach today`**: one screen that says what to do today and whether Ben
  is on track (§5).
- **A saved queue order**, plus an on-demand reassessment (§6).
- **A `working-outreach-queue` skill**, the working surface over the CLI (§7).
- **`adding-referral-contact`** retired, and its job folded into that skill (§8).

The plan this serves is in `~/src/consulting/docs/client_funnel.md`: 150
briefed in six weeks, 25 a week, 2026-10-13 to 2026-11-21.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Logic lives in tested CLI and service code; the skill runs commands and supplies judgment | One source of truth for every count; the skill holds only judgment |
| D2 | Follow-ups are proven by sent email, with `outreach nudge` for other channels | Same evidence rule as briefs (phase-1 §6.2) |
| D3 | Two nudges, at brief + 7 and brief + 14 days; at brief + 21, propose closing | Research summary in §3.1 |
| D4 | The second nudge switches channel when another exists, and is worded as a graceful exit | `client_funnel.md` "What a miss points at": a reply miss means change channel |
| D5 | The brief target is fixed per workday and per week. The nudge target is everyone due | Briefs are a pace Ben chooses; nudges are a backlog |
| D6 | Targets live in the private consulting repo, next to the floors file | The repo is public |
| D7 | No Asana planning tasks. The existing result tasks stay unchanged; nudges create none | Outreach gets its own queue (`outreach today`), not the general task list; nudges are effort, not funnel results |
| D8 | No scheduled queue review. Reassessment runs on demand and is triggered by a low-queue warning | Pruning happens naturally while picking who to brief; only the order needs deliberate reassessment |
| D9 | Closing is always Ben's call | Same as phase 1: the event path never moves to `declined` |

## 3. Follow-ups

### 3.1 Cadence (D3)

Cold-outreach data (vendor studies, directional only) agrees that follow-ups
produce 40–60% of replies and that returns fade after two or three of them.
Advice for referral requests to people you know says: first nudge after 5–7
business days, at most two, the second a polite exit. Ben's contacts are
warm, so long cold sequences would damage the relationships that are the
whole asset.

| From the brief | Due | Action |
|---|---|---|
| +7 days | `follow_up_on` | Nudge 1: short and light, same channel |
| +14 days | `follow_up_on` | Nudge 2: different channel if available, graceful exit |
| +21 days | `follow_up_on` | Propose close: `outreach mark <person> declined` |

Every date is counted from `briefed_at`, not from the last nudge, so a late
nudge does not push the schedule back. A reply clears `follow_up_on`, as in
phase 1.

### 3.2 Data

```sql
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS nudge_count    SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS last_nudged_at TIMESTAMPTZ;
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS rank           INTEGER;   -- §6
ALTER TABLE outreach_events   ADD COLUMN IF NOT EXISTS note           TEXT;      -- §6.3
```

`outreach_events.stage` gains the value `nudge`. The CHECK constraint is
replaced idempotently in `repo/schema.sql`, using the same `DO $$` pattern
the identity migration uses. A `nudge` row is an event, not a stage: the
contact stays at `briefed`. The CLI's `stage` choices, `REFERRER_STAGES` and
`STAGE_ORDER` do not include it. A new `models/outreach.py` constant,
`ACTIVITY_KINDS = ("nudge",)`, names it.

`nudge` events are excluded from:
- the Asana sync. Its pending-events query in `repo/outreach.py` already
  filters on the countable `pipeline:stage` list, which does not include
  `referrer:nudge`;
- `outreach report`'s cohort counts, which select named stages;
- the stage-population gauge.

A new counter, `people.outreach_nudges` (labels `channel`, `source`, `n`
for nudge number 1/2/3+), records them.

### 3.3 Recording a nudge

New in `services/outreach.py`, which remains the only writer:

```python
def nudge(conn, person_id: int, *, at: datetime, channel: str, source: str,
          graph_message_id: str | None = None, note: str | None = None) -> int | None:
```

- The contact row is locked. It must be at `briefed`, and `at` must be at
  least 1 day after `briefed_at`. Otherwise the call returns `None`: the
  event path ignores it silently, and the CLI reports `invalid`. The 1-day
  gap keeps a same-day correction or "forgot the attachment" email from
  counting.
- **Replay safety:** when `graph_message_id` is given and a `nudge` event
  for that person already carries it, the call returns `None`.
- It appends the `nudge` event, increments `nudge_count`, sets
  `last_nudged_at`, and sets `follow_up_on = briefed_at::date + 7 *
  (min(nudge_count, 2) + 1)` (New York date). The result is +14 after
  nudge 1 and +21 after nudge 2 or later.
- A third or later nudge is recorded, and the date stays at +21.

**Email path** (`handlers/email_sent.py`). `_advance_briefed` becomes
`_advance_outreach`. For a message from an outreach address, each To
recipient with a pipeline row is handled by stage:
- `to_contact`: `advance_from_event(... "briefed")`, as today;
- `briefed`: `nudge(... channel="email", source="event", graph_message_id=...)`;
- any other stage: ignored.

Both write in the same durable transaction. Only `briefed` ids go on to the
Asana sync.

**CLI path:** `outreach nudge <person> --channel imessage|whatsapp|linkedin|call|in_person|email [--at ...] [--note ...]`.
An email nudge goes through the CLI only when it was sent from an address
the handler cannot see. Reuse `mark`'s `--from` check.

### 3.4 Follow-ups list

`repo/outreach.py::follow_ups` returns, besides the current columns,
`nudge_count`, `last_nudged_at`, and the channels available (the same
columns `queue` derives). It also returns the brief itself, from the
person's first `briefed` event: `briefed_at`, its `channel` and its
`graph_message_id` (null for a non-email brief), plus the `channel` of the
latest `nudge` event. Without these, nudge 1 cannot reply in the brief's
thread (§7.3). `services/outreach.py::follow_ups` classifies each
row:
- `nudge 1` when `nudge_count = 0`;
- `nudge 2` when `= 1`, suggesting a channel other than the brief's channel
  or nudge 1's channel when one exists;
- `close` when `>= 2`.

`outreach followups` prints that classification.

## 4. Targets

### 4.1 File

`~/src/consulting/docs/outreach_targets.toml`, private (D6):

```toml
# Targets for `outreach today` (people repo). Derived from client_funnel.md
# "Cadence and checkpoints"; change that document first, then this file.
[briefs]
per_day  = 5
per_week = 25
start    = 2026-10-13
end      = 2026-11-21
total    = 150

[queue]
low_water_days = 5        # warn when to_contact holds fewer than low_water_days * per_day

[workdays]
days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
```

`services/outreach_targets.py` is pure, like `outreach_report.py`. It
provides `load_targets(path) -> Targets`, and the load raises `TargetsError`
on a missing table or key, a non-positive number, `start > end`, or an
unknown weekday name.

### 4.2 Locating the file

`outreach today` and `report` resolve their files in this order:
1. `--targets` / `--floors`;
2. the env vars `OUTREACH_TARGETS` / `OUTREACH_FLOORS`;
3. otherwise an error naming both options.

`scripts/fetch-env.sh` writes both env vars into `.env`, pointing at
`$HOME/src/consulting/docs/...`, when those files exist. The public repo
holds only the variable names.

## 5. `outreach today`

`services/outreach_today.py` is pure: `build(counts, targets, *, now) ->
Today` and `render(Today) -> str`. Each count is one `repo/outreach.py`
query, all in New York dates:

| Count | Source |
|---|---|
| briefs today, this week (Mon–Sun), since `start` | `outreach_events` with `stage='briefed'`, one row per person (first brief) |
| nudges sent this week | `outreach_events` with `stage='nudge'` |
| nudges due today, of which overdue | `follow_up_on <= today` / `< today`, `stage='briefed'`, `nudge_count < 2` |
| proposed closes | `stage='briefed'`, `nudge_count >= 2`, `follow_up_on <= today` |
| queue depth | `stage='to_contact'` |
| next to brief | `queue(limit=per_day)` in rank order (§6) |

### 5.1 Verdicts

For the current week, let `w_before` be the workdays before today, and `w`
be `w_before` plus 1 when today is a workday. Each brief line has two marks:
- **by yesterday:** `per_day * w_before`, or for the plan,
  `per_day * workdays(start..yesterday)`;
- **by tonight:** `per_day * w`.

Both are capped at `per_week`, or at `total` for the plan.

| Line | Met | On pace | Behind |
|---|---|---|---|
| Briefs, today | `today >= per_day` | `today < per_day` (the day is not over) | n/a |
| Briefs, week | `week >= by tonight` | `by yesterday <= week < by tonight` | `week < by yesterday` |
| Plan | same rule with the plan's marks | same | same |
| Nudges | nothing due | due only today | anything overdue (`follow_up_on < today`) |

- **Overall:**
  - `ON TRACK` when every line is met.
  - `BEHIND` when any line is behind.
  - Otherwise `ON PACE`, so a morning is never called behind for work the
    day still has time for.
- **Catch-up:** when the week line is not met, the screen shows `need N
  today`, where `N = by tonight - week`.
- **Non-workdays:** they set no brief target. The today line reads `no
  target (weekend)`, and both marks stay at their last workday's values.
- **Plan window:** before `start` or after `end`, the plan line says so and
  takes no part in the verdict.
- **Low queue:** `Queue low: N at to_contact (X workdays). Reassess or
  refill.` when `queue_depth < low_water_days * per_day`. It is a warning
  only, not part of the verdict.

### 5.2 Output

```
Outreach: Wed 2026-10-14                                    BEHIND

Briefs   today 2/5 · week 7/10 expected (25) · need 3 today · plan 7/10 by today (150 by Nov 21)
Nudges   due 2 (1 overdue) · sent this week 4
Queue    38 at to_contact (7.6 workdays)

Brief next
  1. Ana Ruiz        email · imessage       last 2026-09-30   #rank 1
Nudge
  Raj Patel         nudge 1 · briefed 10-06 · due 10-13 (overdue) · email
  Tom Lee           nudge 2 · briefed 09-29 · due 10-13 · switch to imessage
Propose close
  Kim Wu            briefed 09-22 · 2 nudges, no reply → outreach mark 41 declined
```

2026-10-14 is a Wednesday; Monday 10-12 is before the plan's start, so the week's marks count from Tuesday. An overdue nudge makes the verdict `BEHIND`. Nudges due counts nudge items; proposed closes are listed separately.

`--json` emits the same data for the skill. Each nudge and propose-close
item carries `brief: {channel, at, graph_message_id}`,
`last_nudge_channel`, `nudge_count`, `kind` (`nudge 1` / `nudge 2` /
`close`) and `suggested_channel`, all from §3.4. Each brief-next item is a
`queue_item` (§8a).

## 6. Queue order and reassessment

### 6.1 Rank

`outreach_contacts.rank` (§3.2). `repo/outreach.py::queue` orders by
`rank NULLS LAST`, then by the current order. Any move out of `to_contact`
clears `rank`, inside the same `transition` write.

### 6.2 `outreach rank`

- `outreach rank <person> <n>`: sets one rank.
- `outreach rank --file <path>`: one identifier per line (email, E.164
  phone or id, as `person_lookup` resolves). Blank lines and lines starting
  with `#` are ignored.
  - Assigns ranks 1..N in file order.
  - Clears the rank of every other `to_contact` row.
  - Runs in one transaction, and refuses the whole file if any line fails
    to resolve or is not at `to_contact`, listing every bad line.
- `outreach rank --clear`: clears all ranks.

### 6.3 Skip notes

`outreach decide --skip --note "<reason>"` stores the reason on the
`skipped` event's `note`. `note` is also accepted by `nudge` and by `mark
... declined`. It is free text, never sent anywhere.

### 6.4 Reassessment

Reassessment is a skill procedure (§7.4), not a command. It uses `queue
--json`, `rank --file`, `decide` and `decide --skip --note`.

## 7. Skill: `working-outreach-queue`

### 7.1 Placement

`.claude/skills/working-outreach-queue/SKILL.md` lives in this repo and is
linked into `~/.claude/skills/` by adding it to the list in
`scripts/link-skills.sh`. It runs commands from `~/src/people` with
`.venv/bin/python scripts/outreach.py`. Its `depends-on` metadata lists the
skills `sending-inbox-email`, `searching-people`, `fetching-person` and
`creating-person`. The body names the `email-builder` agent it dispatches,
since `depends-on` lists skills, not agents.

It is the only outreach skill. `adding-referral-contact` is retired into it
(§8).

### 7.2 Triggers

The description scopes every trigger to **referral outreach**, so it does
not compete with `logging-job-search` ("heard back from Acme") or
`prioritizing-tasks` ("what should I work on"). Examples:
- "work my outreach", "what's my outreach today", "am I on track with
  outreach";
- "brief the next person", "who do I need to nudge";
- "reassess the outreach queue", "set my outreach targets";
- "add X to my outreach list", "X could refer me";
- "record that X replied to my outreach", "X introduced me to Y".

### 7.3 Procedures

- **Today:** run `outreach today --json` and present the screen. Lead with
  the verdict and the one next action.
- **Brief:**
  1. Take the top person in `today`'s brief-next list (rank order, §6.1).
  2. Read their context: the `queue_item` (channels, last messages,
     notes, `outreach_notes`), plus `fetching-person` when it's needed.
  3. If the best channel is email, dispatch `email-builder` with the brief
     and the explicit consulting identity. It leaves an Outlook draft
     and Ben sends it. The `email_sent` event records the brief, so the
     skill records nothing.
  4. Otherwise, write the text for Ben to send from his phone. After he
     confirms it went out, run `mark <person> briefed --channel <c>`.
  5. **Verify:** once Ben says an email brief is sent, re-run `today
     --json`. The brief count should rise and the person should leave
     brief-next. If neither happens, the mail most likely went from
     the default sender and does not count. Say so, and do not record it by
     hand unless Ben confirms it went from the consulting identity
     (`mark ... --from <consulting identity>`).
- **Nudge:** the same email/other split, driven by the nudge items in
  `today --json`.
  - **Nudge 1:** stays in the brief's channel. For an email brief,
    dispatch `email-builder` as a reply to `brief.graph_message_id` from
    the consulting identity, so it lands in the same thread.
  - **Nudge 2:** uses `suggested_channel` when one is set. It is worded as
    a graceful exit.
  - A non-email nudge is recorded with `outreach nudge <person> --channel
    <c>` after Ben confirms it went out. Email nudges record themselves
    (§3.3). Verify them the same way as briefs, against the nudges-sent
    count.
- **Close:** for each propose-close item, show the brief and nudge history
  and ask. On yes, run `mark <person> declined --note "no reply after 2
  nudges"`. The note is free text, not a `LOST_REASONS` value. The skill
  never closes anyone without asking (D9).
- **Add a person** (from §8):
  1. Find them with `searching-people`. If they are missing, create them
     with `creating-person`, which needs an email or an E.164 phone.
  2. Run `outreach decide <person>`. This moves them to `to_contact` and
     adds the outreach label.
  3. If Ben gives a position ("put her near the top"), run `outreach rank
     <person> <n>`.
  4. For many people at once, point to `scripts/import_contacts.py` and
     then `scripts/outreach_seed.py --dry-run`, which Ben runs.
- **Record outcomes:**
  - Email replies are recorded automatically by `email_classified`, so the
    skill records nothing for them. Use `mark <person> replied --channel
    <c>` only for replies outside email. For a late reply after a decline,
    add `--force`.
  - `lead <person> "<name, company>"`: a lead with no introduction yet.
  - `intro <referrer> "<buyer name, company>" [--buyer <person>]`: an
    introduction. It opens an opportunity and prints its id.
  - `conversation|proposal|signed <opportunity-id>`: buyer stages.
  - `lost <opportunity-id> --reason
    no_budget|no_fit|price|scope|timing|no_response|other`.
- **Reassess (§6.4):**
  1. Run `queue --json --limit 500` and `queue --stage candidate --json`.
  2. Propose an order with a one-line reason for each person. Rank on how
     likely they are to know buyers (role, company, industry), on warmth
     (recency, two-way threads) and on channel strength. Show the current
     `rank` beside each one.
  3. Propose skips, with reasons.
  4. If the queue is still below the low-water mark, propose candidates to
     promote.
  5. Ask Ben to confirm. Then apply with `decide --skip --note`, `decide`
     and `rank --file`, writing the file to a temp path.
- **Targets:** on request ("4 briefs a day from next week"), edit
  `~/src/consulting/docs/outreach_targets.toml`, echo the diff, and commit
  it in the consulting repo with a one-line message. Do not push. A change
  of plan (dates or total) needs `client_funnel.md` updated first, per
  that file's header. The skill says so and does not edit the funnel
  document itself.
- **Report:** run `outreach report`, which reads `OUTREACH_FLOORS`.

### 7.4 Rules the skill states

- Briefs and email nudges go from the consulting identity (the first entry of
  `OUTREACH_FROM_ADDRESSES`). Mail from the default sender (inbox-api's
  `DEFAULT_FROM_ADDRESS`) never counts.
- The skill never sends. Email is left as a draft, and Ben sends other
  channels himself.
- A non-email brief, nudge or reply is recorded only after Ben confirms it
  happened. Anything email proves is never recorded by hand.
- Closing is always Ben's call.

## 8. `adding-referral-contact` (retired)

The unversioned `~/.claude/skills/adding-referral-contact` describes a
HubSpot pipeline stage that was never built, plus PDL/Apify enrichment that
was not built either. Adding someone to the pipeline is now three CLI steps,
so it becomes the **Add a person** procedure in `working-outreach-queue`
(§7.3), and its triggers move to that skill's description (§7.2).

- Delete `~/.claude/skills/adding-referral-contact` (§11). It is
  unversioned, so the delete is local, and nothing in this repo links it.
- PDL role/company enrichment and Apify LinkedIn-post lookup move to §10
  Out of scope.

## 8a. CLI changes at a glance

| Command | Change |
|---|---|
| `outreach today [--targets PATH] [--json]` | **New** (§5) |
| `outreach nudge <person> --channel C [--at] [--from] [--note]` | **New** (§3.3) |
| `outreach rank <person> <n>` / `--file PATH` / `--clear` | **New** (§6.2) |
| `outreach queue` | **Adds `--json`**: emits `services/outreach.py::queue_item` dicts, already JSON-shaped. `queue_item` gains `rank` (§6.1). `--stage candidate` already works. The skill needs both (§7.3). |
| `outreach followups` | Prints the nudge 1 / nudge 2 / close classification and the suggested channel (§3.4) |
| `outreach decide --skip` | **Adds `--note`** (§6.3) |
| `outreach mark <person> declined` | **Adds `--note`** (§6.3). `declined` is already in `MARK_STAGES`. |
| `outreach report` | `--floors` falls back to `OUTREACH_FLOORS` (§4.2) |

## 9. Testing

- `tests/test_outreach.py`, for `nudge`:
  - the 1-day gap;
  - replay by `graph_message_id`;
  - the due-date arithmetic for nudges 1, 2 and 3;
  - wrong stage returns `None`;
  - `rank` cleared on leaving `to_contact`.
- `tests/test_email_sent.py`: one email briefs a `to_contact` recipient and
  nudges a `briefed` one; Cc is ignored; Asana sync is called with brief ids
  only.
- `tests/test_outreach_targets.py`: loading, and every `TargetsError` case.
- `tests/test_outreach_today.py`:
  - verdict table cases: Monday morning, mid-week behind, weekend, before
    `start`, after `end`;
  - the low-queue line;
  - the catch-up number.
- `tests/test_outreach_cli.py`:
  - `nudge`, `rank` (single, `--file` with a bad line refusing the whole
    file, `--clear`), `today` (text and `--json`), `decide --skip --note`;
  - `OUTREACH_TARGETS` / `OUTREACH_FLOORS` resolution;
  - `today --json` nudge items carry `brief.graph_message_id` and
    `brief.channel`; `queue --json` carries `rank`.
- Real-Postgres tests (CI):
  - the migration runs twice cleanly;
  - the `nudge` CHECK value;
  - `queue` ordering by rank;
  - the `today` counts across a New York midnight.

## 10. Out of scope

- Asana planning tasks of any kind (D7).
- Automatic closing (D9).
- Snapshot-import evidence for non-email briefs and nudges, from iMessage,
  WhatsApp or LinkedIn (phase-1 phase 2).
- people-api routes for `today` and `queue` (phase 2). The skill uses the
  CLI.
- Re-looking at `skipped` and `declined` people beyond what `--force`
  already allows. The stored notes make it possible later.
- Contact enrichment: People Data Labs role/company lookup and Apify
  recent-LinkedIn-post lookup, carried over unbuilt from the retired
  `adding-referral-contact` (§8).
- A `--note` on `decide` (to_contact). Context on why someone was added
  goes in `outreach_notes` or the Google notes, not on the event.

## 11. Rollout

1. Run `scripts/migrate_db.py` against production before merging. The
   handler writes `nudge` rows as soon as it deploys.
2. Create `outreach_targets.toml` in the consulting repo and commit it there.
3. Delete the unversioned `~/.claude/skills/adding-referral-contact` (§8).
4. Re-run `scripts/fetch-env.sh`, then `scripts/link-skills.sh`.
5. Check `outreach today` against production.

## 12. Handoff

**State as of 2026-10-13**

- This spec is the only artifact. It is committed on branch
  `outreach-working-queue` in `~/src/people`, pushed to origin, with no PR yet.
- §7–§8 were reviewed on 2026-10-13 and revised:
  - `adding-referral-contact` is retired into `working-outreach-queue`;
  - nudge items carry the brief's thread id;
  - the outcome commands match the CLI;
  - email-proven events are never recorded by hand;
  - target edits are committed in the consulting repo, not pushed;
  - PDL and Apify enrichment are out of scope.
- The plan is `docs/superpowers/plans/2026-10-13-outreach-working-queue.md`.
- **Next step:** Ben approves this spec as a whole. Then write the plan with
  `superpowers:writing-plans` and save it to
  `docs/superpowers/plans/2026-10-13-outreach-working-queue.md`. Phase 1 was
  executed subagent-driven; ask Ben which execution method to use for this
  one.

**Repo rules** (from `CLAUDE.md`)

- The repo is public. Addresses, targets and floors live in env vars,
  uncommitted tfvars, or the private `~/src/consulting` repo.
- Code goes through a feature branch and a PR opened with `/pr-open`,
  proactively.
- Run `scripts/migrate_db.py` against production before merging any schema
  change (§11).

**Current production context**

- **Sending identities.** inbox-api's default sender is `DEFAULT_FROM_ADDRESS`
  (inbox PR #67). Consulting mail must name the consulting identity
  explicitly. The first entry of `OUTREACH_FROM_ADDRESSES` (kept out of the
  repo) is the
  only identity whose sent mail proves a brief, and after this design, a
  nudge. `outreach draft` already sends from it (people PR #24). Exchange's
  `SendFromAliasEnabled` was turned on 2026-10-13; verified by test sends.
- **Pipeline.** The outreach tables are live and empty. The seed
  (`scripts/outreach_seed.py`, dry run: 122 candidates) has not been run yet;
  Ben runs it.
- **Asana.** Phase 1's result tasks (one completed task per countable stage
  event, tagged `outreach` + `serves:consulting`) stay as they are.

**Phase-1 items parked from review, not part of this work**

- 4xx Asana failures retry forever.
- The `LIMIT 200` retry can starve newer events.
- `merge_person` drops the loser's notes and `has_lead`.

## 13. Sources for §3.1

These are vendor studies and advice pieces, directional only.

- Follow-up share of replies and diminishing returns:
  - <https://belkins.io/blog/sales-follow-up-statistics>
  - <https://woodpecker.co/blog/cold-email-statistics/>
  - <https://instantly.ai/blog/follow-up-emails-vs-no-followup/>
  - <https://www.lemlist.com/blog/how-many-cold-email-follow-ups>
  - <https://whali.co.uk/blog/how-many-follow-ups-to-send>
  - <https://optif.ai/learn/questions/follow-up-email-response-rate/>
  - <https://growthlist.co/cold-email-follow-up-timing/>
- Referral-request etiquette:
  - <https://www.salesforge.ai/blog/follow-up-after-no-response>
  - <https://rolewyn.com/blog/how-to-follow-up-referral-request>
  - <https://www.scienceofpeople.com/how-to-ask-for-referrals/>
  - <https://refer.me/blog/how-to-write-the-perfect-referral-request-message>
