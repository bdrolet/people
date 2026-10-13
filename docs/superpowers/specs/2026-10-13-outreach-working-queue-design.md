# Outreach working queue: follow-ups, targets, ranking and a skill

**Status:** design. §1–§6 were approved section by section in conversation on
2026-10-13. Ben approved writing the spec without reviewing §7–§8 section by
section, so treat them as drafted and confirm them at spec review. See §12 for
handoff.
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
- **`adding-referral-contact`** rewritten to feed this pipeline (§8).

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
columns `queue` derives). `services/outreach.py::follow_ups` classifies each
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
Outreach: Tue 2026-10-14                                    ON PACE

Briefs   today 2/5 · week 7/10 expected (25) · need 3 today · plan 7/10 by today (150 by Nov 21)
Nudges   due 3 (1 overdue) · sent this week 4
Queue    38 at to_contact (7.6 workdays)

Brief next
  1. Ana Ruiz        email · imessage       last 2026-09-30   #rank 1
Nudge
  Raj Patel         nudge 1 · briefed 10-06 · due 10-13 (overdue) · email
  Tom Lee           nudge 2 · briefed 09-29 · due 10-13 · switch to imessage
Propose close
  Kim Wu            briefed 09-22 · 2 nudges, no reply → outreach mark 41 declined
```

`--json` emits the same data for the skill.

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

`.claude/skills/working-outreach-queue/SKILL.md` in this repo, linked into
`~/.claude/skills/` by adding it to `scripts/link-skills.sh`'s list. It runs
commands from `~/src/people` with `.venv/bin/python scripts/outreach.py`.
Its metadata depends on `email-builder` (agent), `sending-inbox-email`,
`searching-people`, `creating-person` and `adding-referral-contact`.

### 7.2 Triggers

The skill triggers on requests like "work my outreach", "what's my outreach
today", "am I on track", "brief the next person", "who do I need to nudge",
"reassess the queue", "set my outreach targets", "record that X replied" or
"X introduced me to Y".

### 7.3 Procedures

- **Today:** run `outreach today --json` and present the screen, leading
  with the verdict and the one thing to do next.
- **Brief:**
  1. Take the top ranked person.
  2. Read their context: `queue` row, last messages, notes, and
     `fetching-person` when needed.
  3. If the best channel is email, dispatch `email-builder` with the brief
     and the explicit identity `ben@drolet.ai`. It leaves an Outlook draft;
     Ben sends it, and the `email_sent` event records the brief.
  4. Otherwise, write the text for Ben to send from his phone. After he
     confirms it went, run `mark <person> briefed --channel <c>`.
- **Nudge:** same split. Nudge 1 replies in the brief's thread or channel.
  Nudge 2 uses another channel when one exists, worded as a graceful exit.
  A non-email nudge is recorded with `outreach nudge`.
- **Close:** for each proposed close, show the history and ask. On yes, run
  `mark <person> declined --note no_response`.
- **Record outcomes:**
  - `mark replied --channel` for non-email replies;
  - `intro`, `buyer`, `conversation`, `proposal`, `signed`, `lost`;
  - `lead` for a lead with no introduction.
- **Reassess (§6.4):**
  1. Run `queue --json --limit 500` and `queue --stage candidate --json`.
  2. Propose an order with a one-line reason each, ranking on likelihood of
     knowing buyers (role, company, industry), warmth (recency, two-way
     threads) and channel strength.
  3. Propose skips with reasons.
  4. If the queue is still below the low-water mark, propose candidates to
     promote.
  5. Ask Ben to confirm, then apply with `decide --skip --note`, `decide` and
     `rank --file`, writing the file to a temp path.
- **Targets:** edit `outreach_targets.toml` on request ("4 briefs a day
  from next week"), echoing the diff. A change of plan (dates, total) also
  needs `client_funnel.md` updated first, per that file's header. The skill
  says so and does not edit the funnel document itself.
- **Report:** run `outreach report`, using `OUTREACH_FLOORS`.

### 7.4 Rules the skill states

- Briefs and email nudges go from `ben@drolet.ai`; the default sender
  `ben@drolet.cloud` never counts.
- The skill never sends. Email is left as a draft; other channels are
  Ben's to send.
- Recording a non-email brief or nudge happens only after Ben confirms it
  went out.

## 8. `adding-referral-contact`

The skill moves from `~/.claude/skills/adding-referral-contact` (unversioned)
into this repo's `.claude/skills/` and is linked the same way. The
unversioned copy is removed after the link replaces it. It is rewritten
around the pipeline:

1. Find the person (`searching-people`). If missing, create them
   (`creating-person`).
2. Optionally enrich: the PDL and Apify notes stay as phase-2 placeholders.
3. Run `outreach decide <person>`, and optionally `outreach rank <person>
   <n>`.

The bulk-import entry point keeps pointing at `scripts/import_contacts.py`,
followed by `outreach_seed.py`. The "HubSpot pipeline stage" section is
replaced by a pointer to phase 2's HubSpot mirror.

## 8a. CLI changes at a glance

| Command | Change |
|---|---|
| `outreach today [--targets PATH] [--json]` | **New** (§5) |
| `outreach nudge <person> --channel C [--at] [--from] [--note]` | **New** (§3.3) |
| `outreach rank <person> <n>` / `--file PATH` / `--clear` | **New** (§6.2) |
| `outreach queue` | **Adds `--json`**: emits `services/outreach.py::queue_item` dicts, already JSON-shaped. `--stage candidate` already works. The skill needs both (§7.3). |
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
  - `OUTREACH_TARGETS` / `OUTREACH_FLOORS` resolution.
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

## 11. Rollout

1. Run `scripts/migrate_db.py` against production before merging. The
   handler writes `nudge` rows as soon as it deploys.
2. Create `outreach_targets.toml` in the consulting repo and commit it there.
3. Re-run `scripts/fetch-env.sh`, then `scripts/link-skills.sh`.
4. Check `outreach today` against production.

## 12. Handoff

**State as of 2026-10-13**

- This spec is the only artifact. It is committed on branch
  `outreach-working-queue` in `~/src/people`, not pushed, with no PR.
- No implementation plan exists yet.
- **Next step:** Ben reviews this spec, confirming §7–§8 in particular. Then
  write the plan with `superpowers:writing-plans` and save it to
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

- **Sending identities.** inbox-api's default sender is `ben@drolet.cloud`
  (inbox PR #67, `DEFAULT_FROM_ADDRESS`). Consulting mail must name
  `ben@drolet.ai` explicitly. `OUTREACH_FROM_ADDRESSES=ben@drolet.ai` is the
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
