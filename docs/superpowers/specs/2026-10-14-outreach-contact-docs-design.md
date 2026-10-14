# Outreach contact docs and reloadable sessions

**Status:** design, written 2026-10-14 after Ben made these decisions in
conversation:
- doc plus session resume;
- the docs are machine-maintained;
- they live in a new My Drive folder, `Consulting/Outreach`;
- a short spec comes before the build;
- the doc link is stored in `outreach_contacts.doc_url` (D8);
- the doc carries the raw messages (D6).

Revised the same day after a review against the code, docs-api, the docs repo's
filing flow and live Drive exports (§10 lists what changed).

**Builds on:** PR #26 (`outreach-sessions`, the `outreach-launcher` agent, and
`working-outreach-queue`'s Prep a brief procedure), and the docs repo's filing
catalog (`~/src/docs`, `filing/manifest.yaml`).

## 1. Problem

Ben wants one Claude session per outreach contact: start it when outreach
begins, close it when done, and say "reload the session for <name>" days later
to pick it back up.

Today `outreach-sessions` forgets a session as soon as it closes. The context
then lives only in Claude's local transcript, which:
- `claude rm` deletes;
- is tied to this Mac and the directory the session last ran in;
- grows without limit over months of brief, nudges, reply and intro;
- can't be read outside Claude.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **A Google Doc per contact is the durable record.** Sessions are disposable workspaces. | It survives `claude rm`, compaction and a new machine. Ben can read it on his phone before a call and add his own notes. It stays a curated summary instead of a growing transcript. |
| D2 | **A reload resumes the exact session when it can.** If its transcript exists and the directory it last ran in still exists, `claude --bg --resume <session-id>` from that directory brings back the same id and full conversation. Otherwise a fresh session starts from the doc. | Ben's choice: doc plus resume. Verified 2026-10-13: resuming a closed session brought it back under the same id. A session's directory can change mid-session (entering a worktree moves its transcript to that worktree's project directory), so the directory is read from the transcript, not remembered from the spawn (§4). |
| D3 | **Every reload refreshes.** The session re-pulls the DB snapshot and email, works out what changed since the doc's last log entry, updates the doc, and rewrites the talking points for the next step. | Ben chose "reopen and refresh". Between visits the person moves (brief → nudge → reply), so stale talking points would mislead. |
| D4 | **Outreach docs are machine-maintained.** Sessions write them without asking first. This is an explicit exception to `editing-doc`'s confirm-every-write rule (Guardrails 2, 4 and 5), for these docs only. The write protocol (§3) replaces the confirmation with code-enforced preservation and a conflict check. | Ben's choice. A reload or outcome that waits for a yes defeats the point. |
| D5 | **Docs live in a new My Drive folder, `Consulting/Outreach`,** blessed with a `_FOLDER.md` (catalog id `consulting-outreach`). | Filed by subject (filing rule 6): Ben's consulting business, which no existing folder covers. `employment` is Ben as a candidate, and no shared drive's subject fits. Its home is My Drive, next to `Employment` and `Evaluacy`. `Consulting` does not exist yet and is created as a plain parent; only `Outreach` is a filing target. |
| D6 | **The doc also holds the raw messages:** the most recent `DOC_MESSAGES = 100` per channel (iMessage, WhatsApp, LinkedIn), oldest first, in a `## Messages` section regenerated from the DB on every write. | Ben asked for it (2026-10-14). The text goes into his private My Drive (D5). It also becomes searchable through docs-api's index (`searching-docs`), which is a deliberate change from iMessage and WhatsApp §7, where message text never left the machine; the docstrings that say so are updated. The cap keeps the largest thread on file (3,533 messages) under docs-api's live-export size limit (413), since every write replaces the whole doc. |
| D7 | **No drafted messages, in the doc or anywhere.** Talking points only. | Ben's standing rule (`business_strategy.md`; memory `outreach-talking-points-not-drafts`). |
| D8 | **The doc link is stored in a new `outreach_contacts.doc_url` column.** `services/outreach.py` writes it, and it's the only record of which doc belongs to whom. | Ben asked for the link in the DB and chose the pipeline table: it sits beside the rest of the outreach record, has a single writer, and cascades with the person. The `people` columns all come from Google or are counters, so this doesn't belong there. |

## 3. The doc

The title is `Outreach — <display name>` and the folder is `Consulting/Outreach`.
The structure is fixed so code can merge into it safely:

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

## Talking points (next step: <brief | nudge 1 | nudge 2 | close | reply | intro | none>)
<fragments and questions for Ben to write from; never a drafted message>

## Ben's notes
<Ben's own; sessions never edit this section>

## Messages (last 100 per channel, regenerated <YYYY-MM-DD>; edits here are overwritten)
### iMessage
- 2025-12-01 23:30 Ben: …
- 2025-12-01 23:31 <first name>: …
### WhatsApp
### LinkedIn
```

The H1 and the header line are written by code from the DB, not by the session.

`## Messages` comes from the DB (`services/outreach_session.py::snapshot`,
with `limit = DOC_MESSAGES`), never from the session's own judgment. It's
rendered by a pure function, `services/outreach_doc.py::render_messages`:
- one list item per message, so Drive keeps each on its own line;
- New York date and time;
- whitespace flattened and Markdown punctuation escaped, so a message can't
  turn into a heading or a link;
- nothing truncated (unlike the 300-character cap in the session context);
- an empty channel is omitted.

### What Drive's export looks like

Checked against live exports on 2026-10-14:
- Drive escapes punctuation on export (`\.`, `\)`, `\_`, `\-`, `\!`) and puts a
  blank line after each heading. The parser compares headings and log lines
  after removing backslash escapes and straightening curly apostrophes. It
  keeps text it doesn't rewrite exactly as exported.
- `version` (Drive's `headRevisionId`) was empty on every Google Doc checked.
  `if_version` is therefore usually inert (`editing-doc` Guardrail 5), and the
  write protocol can't rely on it alone.

### Write protocol

Every write follows these rules, so the exception in D4 can never destroy
anything:
1. **Fetch first.** GET the doc and take its `content` and `version`.
2. **Merge** (code: `services/outreach_doc.py::merge`):
   - keep `## Ben's notes` exactly as exported, along with any section that
     follows it (Ben's own headings);
   - keep every existing `## Log` line, and append each new one that isn't
     already there;
   - rewrite the H1, the header line, `Who they are`, `History` and `Talking
     points`;
   - keep any other unrecognized section, placed before `Ben's notes`;
   - keep any unrecognized text between the header line and the first
     section;
   - regenerate `## Messages` from the DB.

   The session supplies only its new sections, in a file it writes:
   `outreach-sessions --write-doc <person> --sections <file>`. Sessions don't
   hand-assemble the doc, so the preservation rules can't be skipped.
3. **Check for a concurrent edit.** GET the doc again just before writing. If
   its `content` or `version` changed since step 1, treat it as a conflict.
   This is the guard that works when `version` is empty.
4. **Replace with a version check.** PUT the merged doc with `if_version` set
   to the version from step 1.
5. **On a conflict** (step 3, or a 409 from step 4), re-fetch, re-merge and
   retry once. If it still conflicts, stop and tell Ben. Never blind-overwrite.
6. **Google comments are wiped by every replace** (docs-api has no comment
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
{"<person id>": {"session_id": "<full uuid>", "cwd": "<directory it last ran in>",
                 "name": "outreach: <name>"}}
```

An older entry holding a bare short id is read as `{"session_id": "<short id>"}`.
Every lookup matches session ids by prefix, so a short id still finds its live
session and its transcript. The record is rewritten with the full uuid and cwd
the first time it's resumed.

### Live, closed, gone

- **Live:** listed by `claude agents --json` (without `--all`, which also lists
  exited sessions). PR #26's check used `--all`, so it counted an exited
  session as open; this design corrects that.
- **Closed:** not live, but `~/.claude/projects/*/<session id>*.jsonl` exists.
  The search covers every project directory, because a session's transcript
  follows it into a worktree. The directory to resume from is the last `cwd`
  written in that transcript. The project-directory name can't be decoded back
  into a path, because `-` stands for `/`, `.` and itself.
- **Gone:** no transcript, or the transcript's directory no longer exists (a
  removed worktree).

### Resolving who is meant

`outreach-sessions <who>` accepts an email, an E.164 phone, a people id, or a
**name**. A name is matched case-insensitively as a substring, in two places,
in order:
1. the recorded session names;
2. pipeline people whose display name matches.

One match proceeds. Several stop with the candidate ids. None stops with "not
found".

### What `--spawn` does per person

| State | Action | Row |
|---|---|---|
| Live | nothing (the record's cwd is refreshed from the listing) | `already open <id>` |
| Closed | `claude --bg --resume <uuid>` from the transcript's cwd, prompt = `RELOAD_PROMPT` | `resumed <id>` |
| No record, or gone | fresh `claude --bg` from the repo, with the snapshot **plus the doc's content when `doc_url` is set**, prompt = `PREP_PROMPT` (or `RELOAD_PROMPT` when a doc exists) | `spawned <id>` |

`--idle` skips the prompt. The dry run (no `--spawn`) prints `already open`,
`resume` or `spawn` per row. If the doc can't be fetched on a fresh spawn, the
session starts anyway; the row says so, and `RELOAD_PROMPT` has the session
fetch the doc itself.

### The prompts

`RELOAD_PROMPT` tells the session, in order, to:
1. re-pull `outreach-sessions --print-context <id>` and the doc;
2. search email since the doc's last log date;
3. say what changed;
4. update the doc per §3 (`--write-doc`);
5. rewrite the talking points for the next step;
6. wait.

Like `PREP_PROMPT`, it says never to draft the message.

### Recording the doc link (D8)

- **Schema:** `ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS doc_url
  TEXT;` in `repo/schema.sql`. It's added to `_CONTACT_COLUMNS` and
  `_CONTACT_UPDATABLE`.
- **Writer:** `services/outreach.py::set_doc(conn, person_id, url) -> dict`.
  It refuses a person with no pipeline row (`NotInPipeline`) and accepts only
  a `https://docs.google.com/document/d/<id>` URL (`InvalidDocUrl`). `None`
  clears the link.
- **CLI:** `outreach doc <person> <url>` and `outreach doc <person> --clear`.
  `--write-doc` records the link itself after creating a doc. The CLI is for a
  hand fix, or to clear a link to a deleted doc.
- **Reads:**
  - `doc_url` is added to `_pipeline_rows` and `queue_item` (so `queue --json`
    and `today --json` carry it), and to the session context block;
  - `outreach-sessions` reads it from the snapshot to fetch the doc on a fresh
    spawn, using the doc id parsed from the URL;
  - `outreach queue` shows a `doc` column (✓ when set).
- **docs-api client:** `clients/docs_api.py` (Cloud Run IAM through
  `clients/gcp_auth.py`, base URL from `DOCS_API_URL`, which
  `scripts/fetch-env.sh` reads from `gcloud run services describe docs-api`).

## 5. The folder (docs repo)

Ben's docs repo gets a new blessed folder entry:
- **`filing/manifest.yaml`:** a `create_mydrive_folder` target with
  `path: [Consulting, Outreach]`.
- **`scripts/filing.py`:**
  - `create_mydrive_folder` learns `path:` (find-or-create each segment),
    alongside today's single `name:`;
  - `bootstrap` learns `--only <seed file>`, so applying one new entry doesn't
    re-push every other seed. Drive is canonical after bootstrap, and today
    some Drive `_FOLDER.md`/`_FILING.md` files are ahead of the repo's
    committed seeds (rule 6, for one). A full re-bootstrap would revert them.
- **`filing/consulting-outreach.md`:** the `_FOLDER.md` seed. It covers:
  - **id:** `consulting-outreach`;
  - **Description:** one living doc per referral-outreach contact,
    machine-maintained by `outreach-sessions`;
  - **Naming:** `Outreach — <name>`;
  - **Privacy:** private, relationship material, My Drive only;
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

**`outreach-launcher`:**
- the description gains "reload";
- it passes names straight to `outreach-sessions`;
- it still only runs `outreach-sessions`.

## 7. Testing

- **`tests/test_claude_sessions.py`:**
  - resume argv;
  - finding a transcript by id prefix across project directories, including a
    worktree's;
  - reading the last `cwd` from a transcript;
  - liveness from `claude agents --json` without `--all`, by id prefix;
  - a legacy short-id entry normalized on read.
- **`tests/test_outreach_sessions_cli.py`:**
  - live → already open;
  - closed with a transcript → resumed, with `RELOAD_PROMPT` and the
    transcript's cwd;
  - transcript gone, or its directory removed → fresh spawn, with the doc
    content in the context when `doc_url` is set;
  - name resolution: a session name, a pipeline name, ambiguous, not found;
  - a closed session no longer frees the person.
- **`tests/test_outreach.py` and `tests/test_outreach_cli.py`:**
  - `set_doc` stores and clears the URL;
  - it refuses a non-Docs URL and someone not in the pipeline;
  - `outreach doc` round-trips;
  - `queue_item` carries `doc_url`, and `queue` shows the ✓.
- **`tests/test_outreach_session.py`:**
  - `context_block` includes the doc section when one is given;
  - `RELOAD_PROMPT` names the steps and forbids drafting.
- **The doc merge** (a pure function, `services/outreach_doc.py::merge`):
  - `Ben's notes` and anything after it are preserved exactly;
  - log lines are kept and appended, without duplicates, even when the export
    escaped them;
  - the other sections are replaced;
  - unexpected extra sections are kept, placed before `Ben's notes`;
  - `## Messages` is regenerated, not merged;
  - `render_messages` respects the per-channel cap, ordering, date and time
    format and escaping, and omits empty channels.
- **`--write-doc`, against fake docs-api calls:**
  - creates the doc in the catalog folder, then calls `set_doc`;
  - on update: GET, merge, GET again, then PUT with `if_version`;
  - a changed second GET or a 409 triggers one re-fetch, re-merge and retry,
    then stops.
- **Real-Postgres:**
  - the migration runs twice cleanly;
  - `doc_url` round-trips through `update_contact` and `pipeline_row`;
  - the name lookup finds pipeline people only.

## 8. Rollout

1. Merge the docs-repo PR (§5). There, run
   `scripts/filing.py bootstrap --only filing/consulting-outreach.md` (dry run
   first). Confirm `GET /folders` lists `consulting-outreach`.
2. Run `scripts/migrate_db.py` against production **before merging**: the new
   code reads `outreach_contacts.doc_url`. Then merge this PR, and in
   `~/src/people` on `main` run `scripts/fetch-env.sh` and
   `scripts/link-skills.sh`.
3. Today's five sessions are already in the state file as short ids, which
   the new code reads as-is. Two of them last ran in worktrees
   (`.claude/worktrees/outreach-sessions` and
   `.claude/worktrees/linkedin-url-match`). Keep both worktrees until those
   sessions have been reloaded once and have a doc. After that, a removed
   worktree only means a fresh spawn from the doc.
4. Pick one of the five whose session has exited, run
   `outreach-sessions <id> --spawn`, and check that the row says `resumed`
   and the doc gets created.

## 9. Out of scope

- Syncing the doc back into the people DB. The DB stays the source of pipeline
  truth; the doc is the narrative.
- Preserving Google comments (a docs-api limitation, see `editing-doc`).
- Sharing or exporting the docs.
- Merging duplicate people rows.

## 10. Revisions after review (2026-10-14)

- **D2 / §4:** a session's directory can move. One of today's sessions started
  in the repo and moved into a worktree, and its transcript moved with it. So
  the cwd now comes from the transcript, and transcripts are searched across
  all project directories.
- **§4:** liveness uses `claude agents --json` without `--all`. Three of
  today's five sessions had exited, yet PR #26 counted them as open.
- **D4 / §3:** live exports had empty `version`s, so a second GET guards the
  write. Drive's export escaping is handled by the parser. Messages are list
  items, so Drive keeps them on separate lines.
- **D5 / §5:** the folder is justified by subject (rule 6), not by rule 3.
  `Consulting` doesn't exist in My Drive, so `filing.py` learns nested
  `create_mydrive_folder` paths and `bootstrap --only`.
- **§8:** no backfill step is needed (legacy entries are read as-is). Real
  names are removed, since the repo is public.
