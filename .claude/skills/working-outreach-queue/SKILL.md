---
name: working-outreach-queue
description: >
  Use when working Ben's referral outreach pipeline — "work my outreach",
  "what's my outreach today", "am I on track with outreach", "brief the next
  person", "who do I need to nudge", "reassess the outreach queue", "set my
  outreach targets", "add X to my outreach list", "X could refer me",
  "record that X replied to my outreach", "X introduced me to Y", "prep my
  brief for X", "start a session for X", "open sessions for my next briefs", "reload the session for X",
  "reopen X's session", "update X's outreach doc".
  Runs the outreach CLI in ~/src/people and supplies the judgment: who to
  brief, what to write, which channel. Not for job-search activity
  (logging-job-search) or the general task list (prioritizing-tasks).
metadata:
  depends-on: "searching-inbox-emails, searching-people, fetching-person, creating-person, anthropic-skills:chrome-browser"
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

- **Ben writes every message himself. Never draft one.** No brief, nudge or
  reply text: no iMessage text, no email draft, no `email-builder`, no
  `matching-writing-style`. The skill does the research and gives **talking
  points**; Ben writes the note, decides whether to send it, and sends it. Why:
  `~/src/consulting/docs/business_strategy.md` — AI-drafted warm messages are
  detectable and get fewer replies than genuine hand-written notes, and
  `client_funnel.md` defines a brief as "a hand-written note".
- Briefs and email nudges go from **the consulting identity** (the first entry of
  `OUTREACH_FROM_ADDRESSES` in `~/src/people/.env`; read it with
  `grep '^OUTREACH_FROM_ADDRESSES=' ~/src/people/.env`, first comma-separated
  entry). Remind Ben to send from it; mail from the default sender (inbox-api's
  `DEFAULT_FROM_ADDRESS`) never counts.
- Record a non-email brief, nudge or reply **only after Ben confirms it
  happened**. Never record by hand anything email proves (briefs, email
  nudges, email replies): the `email_sent` / `email_classified` events do it.
- **Closing is always Ben's call.** Propose; never run `mark … declined`
  without a yes.
- **Contact docs are machine-maintained — an explicit exception to `editing-doc`.**
  Each pipeline person has one Google Doc, `Outreach — <name>` in My Drive
  `Consulting/Outreach`, linked from `outreach_contacts.doc_url`. Write it
  without asking first, and **only** with
  `outreach-sessions --write-doc <id> --sections <file>` — never with
  `editing-doc` or `creating-doc`. The script keeps Ben's notes and the log,
  rewrites the rest, regenerates Messages from the DB, and refuses rather than
  overwrite a concurrent edit it detects. It deletes Google comments on every
  write; the doc tells Ben to write in `Ben's notes` instead. Never put a drafted
  message in it.
- **Write the sections file with the Write tool** (to your scratchpad), never a
  heredoc or `$(…)`: background sessions block shell substitution.

## Today

```bash
$O today --json
```

Lead with `verdict` (`ON TRACK` / `ON PACE` / `BEHIND`) and the one next
action: briefs still owed (`need_today`), then overdue nudges, then closes.
If `low_queue` is true, say so and offer a reassessment.

## Brief

1. Take the first item in `next_up` (rank order).
2. Prep it (**Prep a brief**, below) — or start a session for it
   (**Start sessions**) so it preps in the background.
3. Ben writes and sends the note himself. **Email** goes from the consulting
   identity and records itself. **Any other channel:** after he confirms it
   went, run
   `$O mark <person> briefed --channel imessage|whatsapp|linkedin|call|in_person`.
4. **Verify an email brief:** once Ben says it's sent, re-run
   `$O today --json`. The week's briefs should rise and the person should
   leave `next_up`. If not, it most likely went from the default sender and
   does not count. Say so; record it by hand only if Ben confirms it went
   from the consulting identity: `$O mark <person> briefed --channel email --from <consulting identity>`.

## Start sessions

To work a person in their own session ("start a session for Jonah", "open
sessions for my next 5", "reload the session for Jonah"), dispatch the
`outreach-launcher` agent with who is meant — a name is fine. It runs
`outreach-sessions`, which:
- leaves a running session alone (`already open`);
- resumes the person's exited session, same id and conversation (`resumed`);
- or spawns a fresh one preloaded with their DB snapshot and contact doc
  (`spawned`), already prepping or reloading.

It returns `claude attach <id>` lines. Attaching needs a real terminal. To close a
session without losing it: `claude stop <id>`. Never `claude rm` one — that
deletes its transcript (and a worktree it ran in).

## Prep a brief

Run this in a session opened by `outreach-sessions` (its system prompt carries
the person's snapshot), or on request for one person here. Do it all before
Ben attaches; then present it and wait.

1. **Context.** Read the snapshot: identity, pipeline stage, notes, and the
   message history on every channel. Re-pull with
   `outreach-sessions --print-context <id>` if it looks stale.
2. **Email.** Search with `searching-inbox-emails` (graph mode) for the
   person's surname and each email address on file. Note threads that matter.
3. **News.** With `anthropic-skills:chrome-browser`, in a new tab: a Google
   search for `"<full name>"`, then Google News for the person (past year) and
   for their company (past 3 months). Close the tabs you opened. If Chrome isn't
   connected, use web search and say so. Only what's relevant to the
   conversation.
4. **Briefing.** Write it for Ben — research and talking points, **never a
   drafted message** (see Rules):
   - **Who they are now** — role, company, what they work on, what they care
     about outside work.
   - **History with Ben** — how they know each other, favors in both
     directions, and **the last thread** (date, what it was about, anything left
     open). Message text is private: summarize, quote only what's needed.
   - **Recent news** — or "nothing on them personally".
   - **Talking points** — notes Ben writes from, as short fragments and
     questions, not sentences to paste:
     - *Open with them:* the open thread or the thing left hanging, their life,
       what to ask about first.
     - *Connect:* a question about their work that leads toward Ben's.
     - *Ben's news:* the one-line offer, from
       `~/src/consulting/docs/offer_one_pager.md` — the specific problem
       (healthcare AI stuck in pilot) and the specific buyer (healthcare startups).
     - *The ask:* one name — who comes to mind at a healthcare or health-tech
       startup with an AI pilot stuck short of production. Their own networks
       that could jog their memory. The "say it back to me" check from
       `client_funnel.md`. Ask for a three-way intro, not just a name.
     - *Close:* a concrete next touch.
   - **Worth checking** — anything that makes this a bad moment (bad news on
     their side, an unanswered message from them, a long silence that wants a
     re-warming exchange before any ask). Whether to reach out now is Ben's
     call; surface what bears on it.
   - **Mechanics** — the best channel; for email, send from the consulting
     identity; how the brief gets recorded (email proves itself; any other
     channel is `outreach mark <id> briefed --channel <c>` after Ben confirms).
5. **Write the doc.** With the Write tool, write `<scratchpad>/doc-<id>.md`
   holding exactly these sections (nothing else — the script refuses other
   headings):
   ```markdown
   ## Who they are
   <role, company, life outside work, how Ben knows them>

   ## History
   <past threads, favors both ways, open loops — summarize; quote only what's needed>

   ## Log
   - <YYYY-MM-DD> · prepped brief (<best channel>)

   ## Talking points (next step: brief)
   <the talking points from step 4: fragments and questions, never a drafted message>
   ```
   Then run `outreach-sessions --write-doc <id> --sections <scratchpad>/doc-<id>.md`.
   It prints `created <url>` or `updated <url>`. If it prints
   `error: the doc changed while it was being written`, Ben is editing it: tell him,
   and stop. If it prints `DocNotRecorded`, run the `outreach doc` command it names.
6. **Stop.** Present the briefing with the doc link and wait. Don't offer to write
   the message. Record nothing until Ben says he sent it.

## Reload

A session started or resumed with the reload prompt, or Ben saying "reload" /
"refresh" inside a person's session.

1. **Context.** `outreach-sessions --print-context <id>` — the DB snapshot with
   the contact doc inside it. The doc's last `## Log` date is where you pick up.
2. **Email.** `searching-inbox-emails` (graph mode) for the person since that date.
3. **What changed.** Tell Ben in a few lines: stage moves, replies, new messages
   on any channel, news. Say "nothing new" when nothing is.
4. **Next step.** `briefed` → `$O followups` says `nudge 1`, `nudge 2` or
   `close`; `to_contact` → `brief`; `replied` → `reply`; `introduced` → `intro`;
   `declined`/`skipped` → `none`.
5. **Update the doc.** Same sections file as Prep a brief step 5: Who they are and
   History rewritten as facts changed, a Log line per thing that happened since
   (`- <YYYY-MM-DD> · replied by iMessage`), Talking points for the next step.
   `outreach-sessions --write-doc <id> --sections <file>`.
6. **Stop.** Present what changed and the talking points, with the doc link. Never
   draft the message.

## Nudge

Work the `nudges` items in `today --json`.

Give Ben talking points for each nudge; he writes it (see Rules).

- **`nudge 1`:** short and light, same channel. For an email brief, tell Ben
  to reply in the brief's thread (`brief.graph_message_id`) from the
  consulting identity.
- **`nudge 2`:** use `suggested_channel` when it is set (otherwise the same
  channel). Points for a graceful exit: no pressure, door open.
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
   `creating-person` (needs an email, an E.164 phone, or a LinkedIn profile URL).
2. `$O decide <person>` — moves them to `to_contact` and adds the
   `consulting-outreach` label.
3. If Ben gives a position ("near the top"): `$O rank <person> <n>`.
4. Many people at once: point Ben to `scripts/import_contacts.py`, then
   `scripts/outreach_seed.py --dry-run`. He runs those.
   The seed creates LinkedIn-only people itself (label `from-linkedin`) and
   lists possible duplicates for Ben to resolve by adding the LinkedIn URL to
   the existing contact with `editing-person`. A LinkedIn-only person has no
   email, so their brief is never email-proven: record it with
   `outreach mark <person> briefed --channel linkedin` after Ben confirms.

## Record outcomes

- Email replies record themselves. Other replies:
  `$O mark <person> replied --channel <c>`.
- A lead with no introduction yet: `$O lead <person> "<name, company>"`.
- An introduction: `$O intro <referrer> "<buyer name, company>" [--buyer <person>]`.
  It prints the opportunity id.
- Buyer stages: `$O conversation|proposal|signed <opportunity-id>`.
- Lost: `$O lost <opportunity-id> --reason no_budget|no_fit|price|scope|timing|no_response|other`.
- After any recorded outcome — `mark`, `nudge`, `intro`, `lead`,
  `decide --skip` — update the contact doc: a Log line for what happened
  (`- <YYYY-MM-DD> · nudged by WhatsApp`) and talking points for the new next step,
  through `--write-doc` as in Prep a brief step 5.
- A doc Ben deleted in Drive: `$O doc <person> --clear`; the next `--write-doc`
  creates a fresh one.

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
