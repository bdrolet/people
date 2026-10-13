---
name: working-outreach-queue
description: >
  Use when working Ben's referral outreach pipeline — "work my outreach",
  "what's my outreach today", "am I on track with outreach", "brief the next
  person", "who do I need to nudge", "reassess the outreach queue", "set my
  outreach targets", "add X to my outreach list", "X could refer me",
  "record that X replied to my outreach", "X introduced me to Y". Runs the
  outreach CLI in ~/src/people and supplies the judgment: who to brief, what
  to write, which channel. Not for job-search activity (logging-job-search)
  or the general task list (prioritizing-tasks).
metadata:
  depends-on: "sending-inbox-email, searching-people, fetching-person, creating-person"
---

# Working the outreach queue

The referral pipeline lives in the `people` DB. Every count comes from the
CLI; this skill runs commands and supplies judgment. Design:
`~/src/people/docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md`.

```bash
cd ~/src/people
O=".venv/bin/python scripts/outreach.py"
```

`<person>` is an email, an E.164 phone (`+1…`) or a people id.
`OUTREACH_TARGETS` and `OUTREACH_FLOORS` come from `.env`
(`scripts/fetch-env.sh`). If `today` says no targets file, run that script.

## Rules

- Briefs and email nudges go from **`ben@drolet.ai`**. Mail from the default
  sender, `ben@drolet.cloud`, never counts.
- **Never send.** Email is left as an Outlook draft; other channels are
  Ben's to send.
- Record a non-email brief, nudge or reply **only after Ben confirms it
  happened**. Never record by hand anything email proves (briefs, email
  nudges, email replies): the `email_sent` / `email_classified` events do it.
- **Closing is always Ben's call.** Propose; never run `mark … declined`
  without a yes.

## Today

```bash
$O today --json
```

Lead with `verdict` (`ON TRACK` / `ON PACE` / `BEHIND`) and the one next
action: briefs still owed (`need_today`), then overdue nudges, then closes.
If `low_queue` is true, say so and offer a reassessment.

## Brief

1. Take the first item in `next_up` (rank order).
2. Read its context: `channels`, `last_messages`, `notes`, `outreach_notes`,
   `labels`; use `fetching-person` when that's not enough.
3. **Email is the best channel:** dispatch the `email-builder` agent with
   the brief, the recipient's address and the explicit identity
   `ben@drolet.ai`. It leaves a draft; Ben sends it.
4. **Another channel:** write the text for Ben to send from his phone.
   After he confirms it went:
   `$O mark <person> briefed --channel imessage|whatsapp|linkedin|call|in_person`.
5. **Verify an email brief:** once Ben says it's sent, re-run
   `$O today --json`. The week's briefs should rise and the person should
   leave `next_up`. If not, it most likely went from `ben@drolet.cloud` and
   does not count. Say so; record it by hand only if Ben confirms it went
   from `drolet.ai`: `$O mark <person> briefed --channel email --from ben@drolet.ai`.

## Nudge

Work the `nudges` items in `today --json`.

- **`nudge 1`:** short and light, same channel. For an email brief,
  dispatch `email-builder` as a **reply to `brief.graph_message_id`** from
  `ben@drolet.ai`, so it lands in the same thread.
- **`nudge 2`:** use `suggested_channel` when it is set (otherwise the same
  channel). Word it as a graceful exit: no pressure, door open.
- A non-email nudge, after Ben confirms it went:
  `$O nudge <person> --channel <c> [--note "..."]`. Email nudges record
  themselves; verify them against `nudges_week` as for briefs.

## Close

For each `closes` item, show the brief date, the nudges and the silence, and
ask. On yes:

```bash
$O mark <person> declined --note "no reply after 2 nudges"
```

The note is free text. A late reply later is `$O mark <person> replied --channel <c> --force`.

## Add a person

1. Find them with `searching-people`. If they're missing, create them with
   `creating-person` (needs an email or an E.164 phone).
2. `$O decide <person>` — moves them to `to_contact` and adds the
   `consulting-outreach` label.
3. If Ben gives a position ("near the top"): `$O rank <person> <n>`.
4. Many people at once: point Ben to `scripts/import_contacts.py`, then
   `scripts/outreach_seed.py --dry-run`. He runs those.

## Record outcomes

- Email replies record themselves. Other replies:
  `$O mark <person> replied --channel <c>`.
- A lead with no introduction yet: `$O lead <person> "<name, company>"`.
- An introduction: `$O intro <referrer> "<buyer name, company>" [--buyer <person>]`.
  It prints the opportunity id.
- Buyer stages: `$O conversation|proposal|signed <opportunity-id>`.
- Lost: `$O lost <opportunity-id> --reason no_budget|no_fit|price|scope|timing|no_response|other`.

## Reassess the queue

On request, or when `today` warns the queue is low.

1. `$O queue --json --limit 500` and `$O queue --stage candidate --json`.
2. Propose an order with a one-line reason each, showing the current
   `rank`. Rank on how likely they are to know buyers (role, company,
   industry), warmth (recency, two-way threads) and channel strength.
3. Propose skips, each with a reason.
4. If the queue is still below the low-water mark, propose candidates to
   promote.
5. Ask Ben to confirm, then apply:
   - skips: `$O decide <person> --skip --note "<reason>"`;
   - promotions: `$O decide <person>`;
   - order: write one identifier per line, best first, to a temp file and run
     `$O rank --file <path>`. A bad line refuses the whole file and lists
     every problem; fix and re-run.

## Targets

`~/src/consulting/docs/outreach_targets.toml` holds the brief targets
(`[briefs] per_day, per_week, start, end, total`, `[queue] low_water_days`,
`[workdays] days`). On a request like "4 briefs a day from next week":

1. Edit the file and show the diff.
2. Commit it in `~/src/consulting` with a one-line message. Don't push.
3. A change of plan (dates or total) needs `client_funnel.md` updated first,
   per that file's header. Say so; don't edit the funnel document.

## Report

```bash
$O report
```

Floors come from `OUTREACH_FLOORS`.
