# outreach-sessions on agent-deck

**Date:** 2026-11-02
**Status:** implemented
**Umbrella spec:** `~/src/scripts/docs/superpowers/specs/2026-11-02-deck-sessions-design.md`
— read it first. It defines `deck-sessions` (the shared agent-deck launcher
this port calls), its JSON contract, every verified agent-deck behaviour, and
the rollout order. This is step 3: `deck-sessions` must be built and on PATH,
and tasks already switched to it.

**Reference implementation:** tasks PR #55 and
`~/src/tasks/docs/superpowers/specs/2026-11-02-agent-deck-task-sessions-design.md`
— the same move for per-task sessions, with its live experiments.

**Predecessor here:** `2026-10-14-outreach-contact-docs-design.md` (the
current launcher, contact docs, resume behaviour).

## Problem

`outreach-sessions` (`scripts/outreach_sessions.py`, wrapper
`scripts/outreach-sessions` running it under the venv, client
`clients/claude_sessions.py`) gives each referral-outreach person a
background `claude --bg` session. Ben now runs sessions in agent-deck, under
conductors. The current launcher:

- keeps `~/.claude/outreach-sessions.json` (27 records `{session_id, cwd,
  name}`, keyed by people id, never dropped);
- parses the short id out of `claude --bg`'s human output;
- resumes an exited session by finding its transcript under
  `~/.claude/projects/*/<id>*.jsonl`, reading its last `cwd`, and running
  `claude --bg --resume <id>` from there (a resume after `claude stop` forks a
  new id);
- is invisible to agent-deck and to `conductor-outreach`, which exists
  (`agent-deck conductor list`) but has no children and no policy of its own.

## Goals

- Every outreach session is an agent-deck session in group `outreach`, parented
  under `conductor-outreach`, identified by the hint `person_id=<id>`.
- agent-deck's registry is the only state; the JSON file is retired after
  migration.
- Behaviour Ben relies on is kept: PREP on a fresh person, RELOAD when a
  contact doc exists or when reopening, `--idle`, `--next N`, name
  resolution, the 10-person cap, `--print-context`, `--write-doc`, Opus by
  default (`OUTREACH_SESSION_MODEL` overrides).
- The 27 existing conversations are carried over, not lost.
- `conductor-outreach` supervises only its own outreach children.

## Design

### D1. What stays, what goes

Stays (domain half): target resolution (`_targets`, `_person`, `_by_name`,
`_unique`, the cap, `--next`), `services/outreach_session.py` (snapshot,
`context_block`, `session_name`, `PREP_PROMPT`, `RELOAD_PROMPT`,
`DRAFT_RULE`), `_doc`, `_session_model`, `--write-doc`, `--print-context`,
the `USER_ERRORS` handling and exit codes.

Goes: `clients/claude_sessions.py` and `tests/test_claude_sessions.py`
(spawn/resume argv, id parsing, `live_sessions`, transcript lookup, state
file), and the state-file handling in `run`.

### D2. One spec per person

```json
{"key_hint": "person_id", "key": "388",
 "title": "outreach: <label> #388",
 "adopt_prefix": null,
 "context": "<context_block(snap, today, doc)>",
 "cwd": "/Users/ben/src/people", "group": "outreach",
 "parent": "conductor-outreach", "parent_is_default": true,
 "hints": {}, "model": "opus",
 "prompt": "<RELOAD_PROMPT if doc_url else PREP_PROMPT; null with --idle>",
 "reopen_prompt": "<RELOAD_PROMPT; null with --idle>",
 "refresh": false}
```

- **Title gains ` #<id>`.** agent-deck refuses two sessions with the same
  title at the same path (`ALREADY_EXISTS`), and two people can share a
  display name. Keep `session_name` ≤60 chars by truncating the label, not
  the id.
- **Group/parent config** mirrors tasks: `--group` → `OUTREACH_SESSIONS_GROUP`
  → `outreach`; `--parent none|self|<session>` → `OUTREACH_SESSIONS_PARENT` →
  `conductor-outreach`.
- **Reopen sends RELOAD.** A resumed conversation keeps its original system
  prompt (umbrella, verified fact 6), so the stale snapshot is not refreshed
  by a restart. RELOAD already re-pulls `--print-context` itself, which is
  exactly what a reopened session needs. Today's resume path sends RELOAD
  too, so this preserves behaviour.
- A person not in the pipeline is reported `not in the pipeline (run
  \`outreach decide\` first)` and never sent to `deck-sessions`.
- **Model:** the spec's `model` field; check `deck-sessions` passes
  `--model opus` and that agent-deck accepts the alias (`launch --model`).

### D3. Name resolution without the state file

`_by_name` matches recorded session names first, then pipeline names. Replace
"recorded session names" with `deck-sessions sessions --key-hint person_id`
(titles + keys from agent-deck), stripping the `outreach: ` prefix and the
` #<id>` suffix before matching. Pipeline-name fallback is unchanged.

### D4. Output

Rows stay `id  name  → outcome`. Map `deck-sessions` outcomes to the familiar
words where they exist: `launched <id8>` replaces `spawned`, `reopened
<id8>` replaces `resumed`, `already open <id8> (<status>)`, `duplicate`,
`failed: ...`, `would launch`/`would reopen` in dry run. Keep the
`(doc unavailable: ...)` suffix. Closing hints become
`agent-deck session attach <id>` and `agent-deck -g outreach`.

### D5. Migrating the 27 existing sessions

One-time `outreach-sessions --migrate [--apply]` (dry run by default), then
the JSON file is renamed `outreach-sessions.json.migrated`:

For each record `{session_id, cwd, name}`:
1. If `claude agents --json` still lists it running, `claude stop <id>` first
   (two processes on one conversation corrupt it).
2. Adopt the conversation into agent-deck without starting it:
   `agent-deck add --json --no-parent ... --resume-session <session_id>` with
   the D2 title, group, hint, model and context file, `<cwd>` from the
   record (resume works from the transcript's project directory), then
   `session set-parent <new id> conductor-outreach` (or pass `--parent`).
   **Verify first** in a throwaway profile that `add --resume-session`
   accepts a `claude --bg` transcript id and that `session start` resumes it
   with history intact. If it doesn't, fall back to: no adoption; the next
   `outreach-sessions` run launches fresh with RELOAD (the contact doc holds
   the durable state, per the contact-docs spec), and the old transcripts stay
   readable via `claude --resume`.
3. Skip records whose transcript is gone, or whose person left the pipeline;
   report them.

Do not start or prompt the migrated sessions; they reopen with RELOAD the
next time Ben asks for that person.

### D6. Prune

`--prune [--spawn]`: `deck-sessions sessions --key-hint person_id --group
outreach` → each person's stage → archive sessions whose stage is terminal
(`introduced`, `declined`, `skipped`; buyer `signed`, `lost`) via
`deck-sessions archive`. **Decided (Ben, 2026-11-02):** `declined` and
`skipped` count as terminal, though both are reopenable in
`services/outreach.py` (an archived session reopens on the next run for that
person). A session whose person has left the pipeline is kept. Archive keeps the
transcript; a later run reopens it.

### D7. conductor-outreach policy

Write `~/.local/share/agent-deck/conductor/outreach/POLICY.md`, modelled on
`~/.local/share/agent-deck/conductor/tasks/POLICY.md` (read it). Its
`CLAUDE.md` is the stock conductor file and falls back to the shared
`../POLICY.md` until this exists.

- **Scope:** only sessions it parents that carry a `person_id` hint. Never
  touch other conductors' children, `my-sessions`, or hand-made sessions.
  Never restart a `stopped` session.
- **Hard rule from `DRAFT_RULE`:** never send, and never tell a child to send,
  any message to the person. Talking points by default; the message text
  only when Ben asked. LinkedIn/email/text sending is always Ben's.
- **Answer itself:** lookups (`fetching-person`, `searching-people`,
  `searching-inbox-emails`, `fetching-doc`), "continue prep?" when still
  read-only.
- **Escalate:** anything that records a pipeline event or changes a stage
  (`outreach` CLI writes), sending anything, money, and low confidence.
- **On DONE:** the brief lives in the contact doc (written by the session via
  `outreach-sessions --write-doc`); notify Ben with one line per person,
  batched. No Asana comment.
- **Housekeeping:** at most daily, `outreach-sessions --prune --spawn` once
  D6 is decided; never launch on its own. Launch only for people Ben names,
  dry run first, `--parent self`.

Ben has narrowed `conductor-tasks`'s escalation by hand since its policy was
written (it decides alone when highly confident, escalating payments,
unapproved security changes, unapproved outward messages and low
confidence). **Decided (Ben, 2026-11-02):** outreach stays stricter, as
above. The policy also never restarts an outreach session in any status (see
the D5 notes).

Implementation notes (D5): `add --resume-session` was verified in a throwaway
profile first (umbrella facts 17–18). `--migrate` takes `--apply` (an alias of
`--spawn`). It skips people already in agent-deck (so it can be rerun) and
`claude --bg` sessions that are busy. It takes `<cwd>` from the transcript's
last recorded directory, writes the context file where `deck-sessions` would
(`~/.claude/deck-context/person_id/<id>.md`), and keeps the JSON file when any
adoption fails. It then **archives** each adopted session, because a
never-started session reads `error` / `unknown-exit`. On the first live run
(2026-11-02) conductor-outreach restarted all 25 as crashed children. No
turns were sent, and they were stopped by hand. A never-started session
can't be `session stop`ped, but it can be archived, and `ensure` reopens an
archived session with RELOAD.

### D8. Agent and docs

- `.claude/agents/outreach-launcher.md`: attach line becomes
  `agent-deck session attach <id>`; outcomes renamed per D4; never run
  `agent-deck` or `claude --bg` directly.
- `CLAUDE.md` (Referral outreach row and the `clients/claude_sessions.py` /
  `outreach_sessions.py` lines in the layout section): describe agent-deck,
  `deck-sessions`, the group/parent/hint, and the retired state file.
- `scripts/link-skills.sh`: warn when `deck-sessions` or `agent-deck` is
  missing.
- Update the contact-docs spec's status line to point here.

## Tests

- Rewrite `tests/test_outreach_sessions_cli.py` (~35 tests) against a fake
  `deck-sessions` runner: spec fields per person (title with id, prompts by
  doc presence, `--idle`, model, group/parent precedence), outcome mapping,
  name resolution from `sessions` output, dedupe and cap, not-in-pipeline
  rows, `--migrate` dry run and apply (fake `claude agents`/`agent-deck add`),
  prune by stage, registry-unavailable exit.
- Delete `tests/test_claude_sessions.py` with the client.
- `.venv/bin/pytest -q` passes.

## Acceptance

1. Tests pass.
2. Migration dry run lists all 27 records with a planned action each; apply
   migrates them; `agent-deck session children conductor-outreach --json`
   lists the adopted sessions; reopening one (`outreach-sessions <id>
   --spawn`) resumes its old conversation and runs RELOAD.
3. A person with no session: dry run `would launch`; `--spawn` launches into
   `outreach` under `conductor-outreach` on Opus with PREP or RELOAD; a rerun
   reports `already open`.
4. `conductor-outreach` policy written; Ben confirms its escalation level.

Note: the people checkout was on branch `docs/hubspot-writes-paused` when this
spec was written. Branch from `main` for the implementation.
