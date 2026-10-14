# Outreach contact docs and reloadable sessions

**Status:** design, written 2026-10-14 after Ben made these decisions in
conversation:
- doc plus session resume;
- the docs are machine-maintained;
- they live in a new My Drive folder, `Consulting/Outreach`;
- a short spec comes before the build;
- the doc link is stored in `outreach_contacts.doc_url` (D8);
- the doc carries the raw messages (D6).

**Builds on:** PR #26 (`outreach-sessions`, the `outreach-launcher` agent, and
`working-outreach-queue`'s Prep a brief procedure), and the docs repo's filing
catalog (`~/src/docs`, `filing/manifest.yaml`).

## 1. Problem

Ben wants one Claude session per outreach contact: start it when outreach
begins, close it when done, and say "reload the session for Jonah" days later
to pick it back up.

Today `outreach-sessions` forgets a session as soon as it closes. The context
then lives only in Claude's local transcript, which:
- `claude rm` deletes;
- is tied to this Mac and the directory the session started in;
- grows without limit over months of brief, nudges, reply and intro;
- can't be read outside Claude.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **A Google Doc per contact is the durable record.** Sessions are disposable workspaces. | It survives `claude rm`, compaction and a new machine. Ben can read it on his phone before a call and add his own notes. It stays a curated summary instead of a growing transcript. |
| D2 | **A reload resumes the exact session when it can.** If its transcript exists, `claude --bg --resume <session-id>` brings back the same id and full conversation. Otherwise a fresh session starts from the doc. | Ben's choice: doc plus resume. Verified 2026-10-13: resuming a closed session brought it back under the same id. |
| D3 | **Every reload refreshes.** The session re-pulls the DB snapshot and email, works out what changed since the doc's last log entry, updates the doc, and rewrites the talking points for the next step. | Ben chose "reopen and refresh". Between visits the person moves (brief → nudge → reply), so stale talking points would mislead. |
| D4 | **Outreach docs are machine-maintained.** Sessions write them without asking first. This is an explicit exception to `editing-doc`'s confirm-every-write rule, for these docs only. | Ben's choice. A reload or outcome that waits for a yes defeats the point. |
| D5 | **Docs live in a new private My Drive folder, `Consulting/Outreach`,** blessed with a `_FOLDER.md`. | Filing rule 3: relationship material stays in My Drive. Rule 5: a new document type gets its own blessed folder. |
| D6 | **The doc also holds the raw messages:** the most recent `DOC_MESSAGES = 100` per channel (iMessage, WhatsApp, LinkedIn), oldest first, in a `## Messages` section regenerated from the DB on every write. | Ben asked for it (2026-10-14). The text goes into his private My Drive (D5). It also becomes searchable through docs-api's index (`searching-docs`), which is a deliberate change from iMessage and WhatsApp §7, where message text never left the machine. The cap keeps a large thread (Tesa's, at 3,533 messages) under docs-api's live-export size limit (413), since every write replaces the whole doc. |
| D7 | **No drafted messages, in the doc or anywhere.** Talking points only. | Ben's standing rule (`business_strategy.md`; memory `outreach-talking-points-not-drafts`). |
| D8 | **The doc link is stored in a new `outreach_contacts.doc_url` column.** `services/outreach.py` writes it, and it's the only record of which doc belongs to whom. | Ben asked for the link in the DB and chose the pipeline table: it sits beside the rest of the outreach record, has a single writer, and cascades with the person. The `people` columns all come from Google or are counters, so this doesn't belong there. |

## 3. The doc

The title is `Outreach — <display name>` and the folder is `Consulting/Outreach`.
The structure is fixed so a session can merge into it safely:

```markdown
# Outreach — <name>

people id: <id> · stage: <stage> · last updated: <YYYY-MM-DD>

## Who they are
<role, company, life outside work, how Ben knows them — rewritten as facts change>

## History
<summary of past threads, favors both ways, open loops>

## Log
- 2026-10-14 · prepped brief (iMessage)
- 2026-10-15 · briefed by iMessage
- …append-only, newest last

## Talking points (next step: <brief | nudge 1 | nudge 2 | reply | intro>)
<fragments and questions for Ben to write from; never a drafted message>

## Ben's notes
<Ben's own; sessions never edit this section>

## Messages (last 100 per channel, regenerated <YYYY-MM-DD>)
### iMessage
2025-12-01 23:30 Ben: …
2025-12-01 23:31 Jonah: …
### WhatsApp
### LinkedIn
```

`## Messages` comes from the DB (`services/outreach_session.py::snapshot`,
with `limit = DOC_MESSAGES`), never from the session's own judgment. It's
rendered by a pure function, `services/outreach_doc.py::render_messages`. Lines
carry a date and time, and nothing is truncated (unlike the 300-character cap
in the session context). An empty channel is omitted.

### Write protocol

Every write follows these rules, so the exception in D4 can never destroy
anything:
1. **Fetch first.** GET the doc and take its `version`.
2. **Merge.**
   - Keep `## Ben's notes` byte for byte.
   - Keep every existing `## Log` line and append new ones.
   - Rewrite only the header line, `Who they are`, `History` and `Talking
     points`.
   - Regenerate `## Messages` from the DB.

   The merge and the message render are code
   (`outreach-sessions --write-doc <person>`, reading the session's new
   sections from a file). Sessions don't hand-assemble the doc, so the
   preservation rules can't be skipped.
3. **Replace with a version check.** PUT with `if_version` set to that version.
4. **On a 409** (Ben edited the doc meanwhile), re-fetch, re-merge and retry
   once. If it still conflicts, stop and tell Ben. Never blind-overwrite.
5. **Google comments are wiped by every replace** (docs-api has no comment
   endpoint). The doc says so at the top of `Ben's notes`: write notes there,
   not as comments.

### When a session writes

- At the end of Prep a brief: the doc is created, or merged into if it exists.
- On every reload, after the refresh.
- When an outcome is recorded (`mark`, `nudge`, `intro`, `lead`, `decide
  --skip`): a log line goes in, and the talking points move to the next step.

## 4. State and reload (`outreach-sessions`)

### State file

`~/.claude/outreach-sessions.json` becomes a record per person, which is never
dropped when a session closes. It only tracks sessions; the doc link is in the
DB (D8):

```json
{"388": {"session_id": "<full uuid>", "cwd": "/Users/ben/src/people",
         "name": "outreach: Jonah Stiennon"}}
```

An older entry holding a bare short id is upgraded on read: its full uuid comes
from the transcript filename in Claude's project directory for that cwd.

### Resolving who is meant

`outreach-sessions <who>` accepts an email, an E.164 phone, a people id, or a
**name**. A name is looked up in two places, in order:
1. the recorded session names;
2. pipeline people whose display name matches.

One match proceeds. Several stop with the candidate ids. None stops with "not
found".

### What `--spawn` does per person

| State | Action | Row |
|---|---|---|
| Recorded session is live (`claude agents --json --all`) | nothing | `already open <id>` |
| Recorded session closed, transcript exists | `claude --bg --resume <uuid>` from the recorded cwd, prompt = `RELOAD_PROMPT` | `resumed <id>` |
| No record, or transcript gone | fresh `claude --bg` with the snapshot, **plus the doc's content when `doc_url` is set**, prompt = `PREP_PROMPT` (or `RELOAD_PROMPT` when a doc exists) | `spawned <id>` |

`--idle` skips the prompt. The dry run (no `--spawn`) prints `resume` or
`spawn` per row.

### The prompts

`RELOAD_PROMPT` tells the session, in order, to:
1. re-pull `outreach-sessions --print-context <id>` and the doc;
2. search email since the doc's last log date;
3. say what changed;
4. update the doc per §3;
5. rewrite the talking points for the next step;
6. wait.

Like `PREP_PROMPT`, it says never to draft the message.

### Recording the doc link (D8)

- **Schema:** `ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS doc_url
  TEXT;` in `repo/schema.sql`. It's added to `_CONTACT_COLUMNS` and
  `_CONTACT_UPDATABLE`.
- **Writer:** `services/outreach.py::set_doc(conn, person_id, url) -> dict`.
  It refuses a person with no pipeline row (`NotInPipeline`) and accepts only
  a `https://docs.google.com/document/d/<id>` URL.
- **CLI:** `outreach doc <person> <url>`. After creating the doc, the session
  runs it with the `web_url` docs-api returned.
- **Reads:**
  - `doc_url` is added to `_pipeline_rows`, `queue_item` (so `queue --json` and
    `today --json` carry it) and the session context block;
  - `outreach-sessions` reads it from the snapshot to fetch the doc on a fresh
    spawn, using the doc id parsed from the URL;
  - `outreach queue` shows a `doc` column (✓ when set).

## 5. The folder (docs repo)

Ben's docs repo gets a new blessed folder entry, applied with the existing
`scripts/filing.py bootstrap` flow:
- **`filing/manifest.yaml`:** a `create_mydrive_folder` target, `Consulting/Outreach`.
- **`filing/consulting-outreach.md`:** the `_FOLDER.md` seed. It covers:
  - **Description:** one living doc per referral-outreach contact,
    machine-maintained by `outreach-sessions`.
  - **Naming:** `Outreach — <name>`.
  - **Privacy:** private, relationship material, My Drive only.
  - **`Elsewhere:`** Ben's job search goes to `employment`.

This is a separate PR in `~/src/docs`. `outreach-sessions` reads the folder id
from the catalog (`GET /folders`) by its entry id, so the people repo never
hard-codes a Drive id.

## 6. Skill and agent

**`working-outreach-queue`:**
- **Prep a brief** step 5 creates or merges the doc instead of only presenting
  the briefing.
- A new **Reload** procedure, with triggers "reload / reopen the session for X".
- **Record outcomes** gains "append to the contact doc's log".
- The **D4 exception and the write protocol** are written into the Rules.

**`outreach-launcher`:** the description gains "reload"; it still only runs
`outreach-sessions`.

## 7. Testing

- **`tests/test_claude_sessions.py`:**
  - resume argv;
  - finding the transcript path and slug for a cwd, including the worktree
    form `-Users-…--claude-worktrees-…`;
  - upgrading a legacy state entry.
- **`tests/test_outreach_sessions_cli.py`:**
  - live → already open;
  - closed with a transcript → resumed, with `RELOAD_PROMPT` and the recorded
    cwd;
  - transcript gone → fresh spawn, with the doc content in the context when
    `doc_url` is set;
  - name resolution: a session name, a pipeline name, ambiguous, not found;
  - a closed session no longer frees the person.
- **`tests/test_outreach.py` and `tests/test_outreach_cli.py`:**
  - `set_doc` stores the URL;
  - it refuses a non-Docs URL and someone not in the pipeline;
  - `outreach doc` round-trips;
  - `queue_item` carries `doc_url`.
- **`tests/test_outreach_session.py`:**
  - `context_block` includes the doc section when one is given;
  - `RELOAD_PROMPT` names the steps and forbids drafting.
- **The doc merge** (a pure function, `services/outreach_doc.py::merge(existing,
  update)`):
  - `Ben's notes` is preserved exactly;
  - log lines are kept and appended;
  - the other sections are replaced;
  - it doesn't break when `existing` has unexpected extra sections (they're
    kept, placed before `Ben's notes`);
  - `## Messages` is regenerated, not merged: an edit Ben made there is
    overwritten, and the doc says so in that section's heading;
  - `render_messages` respects the per-channel cap, ordering and date/time
    format, and omits empty channels.
- **`--write-doc`, against fake docs-api calls:**
  - creates the doc, then calls `set_doc`;
  - on update: GET, merge, then PUT with `if_version`;
  - a 409 triggers one re-fetch, re-merge and retry, then stops.
- **Real-Postgres:**
  - the migration runs twice cleanly;
  - `doc_url` round-trips through `update_contact` and `pipeline_row`.

## 8. Rollout

1. Merge the docs-repo PR (§5) and run `scripts/filing.py bootstrap` there.
   Confirm `GET /folders` lists `consulting-outreach`.
2. Run `scripts/migrate_db.py` against production **before merging**: the new
   code reads `outreach_contacts.doc_url`. Then merge this PR, and in
   `~/src/people` on `main` run `scripts/link-skills.sh`.
3. Backfill today's five sessions (Jonah, Ahmed, Paul, Coki, Anshu) into the
   new state format. Each one's doc is created on its next reload.
4. Try `outreach-sessions Jonah --spawn` and check that the row says
   `resumed`.

## 9. Out of scope

- Syncing the doc back into the people DB. The DB stays the source of pipeline
  truth; the doc is the narrative.
- Preserving Google comments (a docs-api limitation, see `editing-doc`).
- Sharing or exporting the docs.
- Merging duplicate people rows (BiL Castine, Renee Drolet).
