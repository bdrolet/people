# Outreach Contact Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every referral-outreach person a machine-maintained Google Doc that outlasts their Claude sessions. Make `outreach-sessions` resume a person's exited session (same id, same conversation), or start a fresh one from the doc.

**Architecture:**
- The doc link is a new `outreach_contacts.doc_url` column, and `services/outreach.py::set_doc` is its only writer.
- A pure module, `services/outreach_doc.py`, renders the Messages section, parses the sections file a session writes, and merges it into the existing doc. Ben's notes and the log survive every write.
- `services/outreach_doc_write.py` runs the write protocol against docs-api (`clients/docs_api.py`): GET, merge, GET again, PUT with `if_version`, one retry.
- `clients/claude_sessions.py` learns three things:
  - liveness from `claude agents --json` (without `--all`);
  - transcript lookup across every project directory, with the cwd read from the transcript;
  - `claude --bg --resume`.
- `scripts/outreach_sessions.py` ties these together and adds name lookup and `--write-doc`.
- A separate docs-repo PR creates the `Consulting/Outreach` filing folder.

**Tech Stack:** Python 3.13, Postgres (pg8000 on Cloud SQL / psycopg3 locally), httpx, pytest, ruff, mypy; the `claude` CLI (`--bg`, `--resume`, `agents --json`); docs-api (Cloud Run IAM).

**Spec:** `docs/superpowers/specs/2026-10-14-outreach-contact-docs-design.md`. Read all of it before starting, especially §3 (the doc, the write protocol and the export findings), §4 (live / closed / gone) and §10 (revisions). It builds on PR #26 (`outreach-sessions`) and `docs/superpowers/specs/2026-10-13-outreach-working-queue-design.md`.

## Global Constraints

- **Repo is public.** No real names, addresses, message text, Drive ids or session ids from Ben's machine in code, tests, fixtures, commit messages or the PR. Fixtures use `example.com` addresses, `+1555010…` numbers, made-up uuids such as `ab12cd34-0000-4000-8000-000000000001`, and made-up doc ids such as `doc388`.
- **One writer.** Only `services/outreach.py` writes `outreach_contacts` (`set_doc` writes `doc_url`). The writer module and the CLIs call it; nothing calls `repo/outreach.py` writes directly.
- **Docs are written only through the merge.** Every doc body comes from `services/outreach_doc.py::merge`. Nothing builds doc markdown by hand, and no session calls `editing-doc` or `creating-doc` on an outreach doc.
- **Never a drafted message** (spec D7). No code path, prompt, fixture or skill text produces message text for Ben to send. Prompts and the context block say so explicitly.
- **docs-api is local-only.** `clients/docs_api.py` is called from `scripts/outreach_sessions.py` (through `services/outreach_doc_write.py`) and nothing else: never from `main.py`, `handlers/` or `api/`. `tests/test_outreach_session.py::test_people_api_never_reads_message_text` must keep passing.
- **Layer rules (CLAUDE.md).**
  - `clients/`: I/O only.
  - `repo/`: takes an open connection, never commits, decides nothing.
  - `services/`: business logic. `outreach_doc.py` is pure; `outreach_doc_write.py` orchestrates and receives its docs-api client as a parameter.
  - `models/`: pure types.
  - scripts call services.
- **Time zone.** Every date and message time in the doc is `America/New_York` (`services/outreach.py::TZ`).
- **Exact strings.**
  - Doc title prefix: `Outreach — ` (em dash).
  - Catalog entry id: `consulting-outreach`.
  - `DOC_MESSAGES = 100`.
  - Next steps: `brief`, `nudge 1`, `nudge 2`, `close`, `reply`, `intro`, `none`.
  - Row outcomes: `already open <short>`, `resume`, `resumed <short>`, `resume failed`, `spawn`, `spawned <short>`, `spawn failed`, `not in the pipeline (run \`outreach decide\` first)`.
- **pg8000 gotcha.** Keep `%` out of SQL text: use `position(lower(%s) in lower(...)) > 0`, never `ILIKE '%' || …`.
- **Background-session guard.** Sessions this feature spawns can't use shell command substitution or `git -C`. Skill text tells them to write the sections file with the Write tool and pass its path, never a heredoc or `$(…)`.
- **Never `claude rm` an outreach session.** It deletes the transcript, and for a session started in a worktree it deletes the worktree too. Tests and docs use `claude stop`.
- **Where to work.** Implementation goes in the worktree `/Users/ben/src/people/.claude/worktrees/outreach-session-reload` on branch `outreach-contact-docs`. That branch already holds the spec and this plan, pushed to origin, so spec, plan and code ship in one PR. Run every command from that directory. It has no venv or `.env` yet. Before Task 1:
  ```bash
  python3.13 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt
  cp /Users/ben/src/people/.env .env    # gitignored; only the scripts read it
  ```
  Open the PR with the `/pr-open` skill (Task 12), never by hand.
- **Commit trailer.** End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012tPVb52FgjpdEUxAMJyfvL
  ```
- **Format first.** Code blocks here are not pre-formatted. Run `.venv/bin/ruff check --fix . && .venv/bin/ruff format .` before the checks.
- **Checks before every commit:**
  ```bash
  .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
  ```
  **One exception:** Task 6 changes `clients/claude_sessions.py`'s API, and `scripts/outreach_sessions.py` only moves to it in Task 9. Task 6's commit runs every check except `tests/test_outreach_sessions_cli.py` (`--deselect` it, or pass the other test files explicitly), and says so in its body. From Task 9 on, the full suite is green again.
- **Real-Postgres tests.** `tests/test_outreach_db.py` is skipped unless `TEST_DATABASE_URL` is set. CI sets it. Locally, use a throwaway container before every commit that touches SQL:
  ```bash
  docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
  export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
  ```
  If Docker is unavailable, say so in the task report and rely on CI; do not claim the SQL was verified locally.
- **Production schema.** `scripts/migrate_db.py` runs against production **before** this PR merges (Task 13). The code reads `outreach_contacts.doc_url` on every queue read.

## Decisions this plan makes where the spec is silent or loose

Each is called out so a reviewer can reject it on its own.

1. **Liveness is `claude agents --json` without `--all`.** The id is matched by prefix in both directions, so a PR #26 short id still finds its running session. With `--all`, an exited session looked open; three of the five sessions in today's state file have exited.
2. **No backfill step.** A legacy short-id entry is read as `{"session_id": "<short>"}`. Transcripts are found by `<id>*.jsonl` prefix across every `~/.claude/projects/*/` directory. The record is rewritten with the full uuid and cwd on its first resume or live sighting.
3. **The resume cwd is the last `cwd` written in the transcript.** If it no longer exists (a removed worktree), the person gets a fresh spawn from the doc instead of a failing resume.
4. **A second GET guards every write.** The `content` and `version` from just before the PUT must equal what was merged from. Live Google Docs report `version: ""`, so `if_version` alone protects nothing. `if_version` is still sent.
5. **The sections file may hold only `Who they are`, `History`, `Log` and `Talking points (next step: …)`.** Any other heading, including `Ben's notes` and `Messages`, is refused, and nothing is written. The H1 and the header line are written by code from the DB.
6. **Next steps include `close` and `none`.** `close` is the follow-up kind after nudge 2. `none` covers `declined` and `skipped` people. The spec listed five.
7. **Sections after `Ben's notes` are Ben's.** They stay after it, in order. Unrecognized sections before it stay before it. Unrecognized text between the header and the first section is kept. The spec only said "kept, placed before Ben's notes".
8. **A second copy of a machine heading** (for example two `## History` sections) is kept as an unrecognized section. Only the first copy is replaced.
9. **`--print-context` includes the doc.** A reloaded session gets snapshot and doc in one command, without `fetching-doc`.
10. **A doc that can't be fetched doesn't block a fresh spawn.** The row reads `spawned <id> (doc unavailable: <ErrorType>)`, the prompt is still `RELOAD_PROMPT`, and the session fetches the doc itself.
11. **`outreach doc <person> --clear`.** It's the way back from a doc Ben deleted in Drive. The next `--write-doc` then creates a fresh one.
12. **`DocNotRecorded`.** If the doc is created but `set_doc` fails, the error carries the new URL and the exact `outreach doc` command to record it. The next write would otherwise create a duplicate doc.
13. **Name matching is a case-insensitive substring.** It checks recorded session names (with `outreach: ` stripped) first, then pipeline display names. Several matches at either step stop with the ids.
14. **`resume` returns the short id even if `claude` prints no `backgrounded · <id>` line**, since a resume keeps its id. A non-zero exit is still `resume failed`.
15. **`services/outreach_doc_write.py::write` commits** right after `set_doc`, so the link is durable before the command returns. `scripts/outreach_sessions.py` never commits.
16. **docs-api timeout is 60 s.** A live export of a doc holding 300 messages is slower than tasks-api's 10 s budget allows.

## Review Focus

1. **A doc that went through Drive's markdown export.** Drive escapes punctuation (`\.`, `\-`, `\(`), curls apostrophes (`Ben’s notes`) and adds blank lines after headings. Expected: every section is still recognized, Ben's notes keep their exact text, and log lines aren't duplicated. Pinned in Task 5 (`test_merge_reads_a_drive_export`).
2. **Ben edits the doc on his phone while a session writes, and the doc's `version` is empty.** Expected: the second GET sees the change, the write re-merges with Ben's edit, and nothing is clobbered. Pinned in Task 8 (`test_an_edit_between_the_two_gets_is_re_merged`).
3. **A session that moved into a worktree that was later removed.** Expected: a fresh spawn from the doc, not a resume that fails or starts in the wrong directory. Pinned in Task 9 (`test_a_transcript_whose_directory_is_gone_gets_a_fresh_spawn`).
4. **A message whose text looks like Markdown** (`# heading`, `[a](b)`, `*bold*`, a pasted table row). Expected: it shows literally, on its own line, and can't create a heading that would split the doc. Pinned in Task 5 (`test_render_messages_escapes_markdown`).
5. **The same sections written twice** (a reload right after a prep). Expected: the second doc equals the first, apart from the date: no duplicate log lines and no growing header block. Pinned in Task 5 (`test_merge_is_idempotent`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `repo/schema.sql` | Modify | `outreach_contacts.doc_url`. |
| `repo/outreach.py` | Modify | `doc_url` in contact columns, the updatable set and `_pipeline_rows`; `pipeline_people_named`. |
| `services/outreach.py` | Modify | `InvalidDocUrl`, `doc_id`, `set_doc`, `people_named`; `doc_url` in `queue_item`. |
| `scripts/outreach.py` | Modify | `doc` subcommand; a `doc` column in `queue`. |
| `clients/docs_api.py` | Create | docs-api: `get_doc`, `create_doc`, `replace_doc`, `folder_id`. |
| `scripts/fetch-env.sh` | Modify | `DOCS_API_URL`. |
| `services/outreach_doc.py` | Create | Pure: `title`, `them`, `render_messages`, `Sections`, `parse_sections`, `merge`. |
| `clients/claude_sessions.py` | Modify | `resume`, `live_sessions`, `find_live`, `find_transcript`, `transcript_cwd`, record state; drop `live_session_ids`/`reconcile`. |
| `services/outreach_session.py` | Modify | `RELOAD_PROMPT`; `context_block(..., doc=)`; the doc line; docstring (D6). |
| `repo/imessage.py`, `repo/whatsapp.py` | Modify | Docstrings: message text now also reaches the contact doc (D6). |
| `services/outreach_doc_write.py` | Create | The write protocol: create or GET → merge → GET → PUT, one retry. |
| `scripts/outreach_sessions.py` | Modify | Names, resume / spawn / already open, the doc on spawn and print-context, `--write-doc`. |
| `.claude/skills/working-outreach-queue/SKILL.md` | Modify | D4 rule, Prep a brief writes the doc, Reload, Record outcomes. |
| `.claude/agents/outreach-launcher.md` | Modify | Reload, names passed through, new row outcomes. |
| `CLAUDE.md`, `.claude/skills/people-architecture/SKILL.md` | Modify | Document the contact docs. |
| `tests/test_outreach_db.py`, `tests/test_outreach.py`, `tests/test_outreach_cli.py`, `tests/test_claude_sessions.py`, `tests/test_outreach_session.py`, `tests/test_outreach_sessions_cli.py` | Modify | As each task says. |
| `tests/test_docs_api.py`, `tests/test_outreach_doc.py`, `tests/test_outreach_doc_write.py` | Create | New-module tests. |
| `~/src/docs`: `scripts/filing.py`, `filing/manifest.yaml`, `filing/consulting-outreach.md`, `tests/unit/test_filing.py` | Separate PR (Task 11) | The `Consulting/Outreach` folder. |

---

### Task 1: Schema and repo — `doc_url`, pipeline names

**Files:**
- Modify: `repo/schema.sql` (beside the other `ALTER TABLE outreach_contacts` lines, ~line 481)
- Modify: `repo/outreach.py:8-30` (`_CONTACT_COLUMNS`, `_CONTACT_UPDATABLE`), `repo/outreach.py:246-276` (`_pipeline_rows`); add `pipeline_people_named`
- Test: `tests/test_outreach_db.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `outreach_contacts.doc_url TEXT` (nullable);
  - `doc_url` in every row `get_contact`, `lock_contact`, `update_contact`, `queue` and `pipeline_row` return;
  - `"doc_url"` accepted by `update_contact`;
  - `repo.outreach.pipeline_people_named(conn, text: str) -> list[dict]`: rows `{id, display_name}`, ordered by id, pipeline people only.

- [ ] **Step 1: Write the failing test** (append to `tests/test_outreach_db.py`)

```python
def test_doc_url_round_trips_and_names_find_pipeline_people(conn):
    pid = _person(conn, display_name="Jamie Example")
    other = _person(conn, email="other@example.com", display_name="Jamie Other")
    _person(conn, email="outside@example.com", display_name="Jamie Outside")
    for p in (pid, other):
        outreach_repo.insert_contact_if_missing(conn, p)

    assert outreach_repo.get_contact(conn, pid)["doc_url"] is None
    url = "https://docs.google.com/document/d/doc1/edit"
    assert outreach_repo.update_contact(conn, pid, {"doc_url": url})["doc_url"] == url
    assert outreach_repo.pipeline_row(conn, pid)["doc_url"] == url
    assert outreach_repo.update_contact(conn, pid, {"doc_url": None})["doc_url"] is None

    named = outreach_repo.pipeline_people_named(conn, "JAMIE")
    assert [r["id"] for r in named] == [pid, other]  # the person outside the pipeline is not
    assert outreach_repo.pipeline_people_named(conn, "e exa")[0]["display_name"] == "Jamie Example"
    assert outreach_repo.pipeline_people_named(conn, "outside") == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_outreach_db.py::test_doc_url_round_trips_and_names_find_pipeline_people -v` (with `TEST_DATABASE_URL` set)
Expected: FAIL with `KeyError: 'doc_url'`.

- [ ] **Step 3: Implement**

In `repo/schema.sql`, after `ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS rank ...`:

```sql
ALTER TABLE outreach_contacts ADD COLUMN IF NOT EXISTS doc_url        TEXT;     -- the contact's living Google Doc (contact-docs spec D8)
```

In `repo/outreach.py`, `_CONTACT_COLUMNS` becomes:

```python
_CONTACT_COLUMNS = """
    person_id, stage, cohort_week, briefed_at, replied_at, channel, has_lead,
    follow_up_on, notes, nudge_count, last_nudged_at, rank, doc_url, hubspot_synced_at,
    created_at, updated_at
"""
```

Add `"doc_url",` to `_CONTACT_UPDATABLE` (after `"rank",`). In `_pipeline_rows`, change the line `c.briefed_at, c.channel, c.nudge_count, c.last_nudged_at,` to:

```python
               c.briefed_at, c.channel, c.nudge_count, c.last_nudged_at, c.doc_url,
```

Add after `pipeline_row`:

```python
def pipeline_people_named(conn: Any, text: str) -> list[dict]:
    """Pipeline people whose display name contains `text`, case-insensitively (contact-docs
    spec §4). position(), not ILIKE: this SQL holds no percent sign for a paramstyle to
    misread."""
    return conn.execute(
        """
        SELECT p.id, p.display_name
        FROM outreach_contacts c JOIN people p ON p.id = c.person_id
        WHERE position(lower(%s) in lower(COALESCE(p.display_name, ''))) > 0
        ORDER BY p.id
        """,
        (text,),
    ).fetchall()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_db.py tests/test_schema.py -v`
Expected: PASS, including `test_schema_is_still_idempotent` (the migration runs twice cleanly).

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql repo/outreach.py tests/test_outreach_db.py
git commit -m "outreach: doc_url column and pipeline name lookup (contact docs)"
```

---

### Task 2: Service — `set_doc`, `doc_id`, `people_named`, `doc_url` in the queue item

**Files:**
- Modify: `services/outreach.py` (imports; new class and functions after `set_lead`; `queue_item`)
- Test: `tests/test_outreach.py`

**Interfaces:**
- Consumes: `repo.outreach.lock_contact`, `update_contact`, `pipeline_people_named` (Task 1).
- Produces:
  - `services.outreach.InvalidDocUrl(ValueError)`;
  - `doc_id(url: str) -> str` (raises `InvalidDocUrl`);
  - `set_doc(conn, person_id: int, url: str | None) -> dict` (raises `InvalidDocUrl`, `NotInPipeline`);
  - `people_named(conn, text: str) -> list[dict]`;
  - `queue_item(r)["doc_url"]: str | None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_outreach.py`; also add `"doc_url": "https://docs.google.com/document/d/doc42/edit",` to both the `row` dict and the expected dict of `test_queue_item_matches_the_spec_shape`)

```python
def test_set_doc_stores_and_clears_the_link(repo):
    _at(repo, 1, "to_contact")
    url = "https://docs.google.com/document/d/1AbC_d-9/edit"
    assert svc.set_doc(None, 1, url)["doc_url"] == url
    assert svc.set_doc(None, 1, None)["doc_url"] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com/file/d/x/view",
        "http://docs.google.com/document/d/x",
        "https://docs.google.com/spreadsheets/d/x/edit",
        "https://docs.google.com/document/d/",
        "not a url",
    ],
)
def test_set_doc_refuses_anything_but_a_docs_link(repo, url):
    _at(repo, 1, "to_contact")
    with pytest.raises(svc.InvalidDocUrl):
        svc.set_doc(None, 1, url)
    assert repo.contacts[1].get("doc_url") is None


def test_set_doc_refuses_someone_outside_the_pipeline(repo):
    with pytest.raises(svc.NotInPipeline):
        svc.set_doc(None, 9, "https://docs.google.com/document/d/doc9")


def test_doc_id_parses_the_link():
    assert svc.doc_id("https://docs.google.com/document/d/1AbC_d-9/edit?tab=t.0") == "1AbC_d-9"
    assert svc.doc_id(" https://docs.google.com/document/d/doc7 ") == "doc7"


def test_people_named_trims_and_skips_a_blank_name(repo):
    seen = []

    def named(conn, text):
        seen.append(text)
        return [{"id": 1, "display_name": "Jo Example"}]

    repo.pipeline_people_named = named
    assert svc.people_named(None, "  jo ") == [{"id": 1, "display_name": "Jo Example"}]
    assert svc.people_named(None, "   ") == [] and seen == ["jo"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach.py -k "doc or people_named or queue_item" -v`
Expected: FAIL with `AttributeError: module 'services.outreach' has no attribute 'set_doc'` (and a `KeyError: 'doc_url'` mismatch in the queue-item test).

- [ ] **Step 3: Implement**

Add `import re` to the imports of `services/outreach.py`. After the `NotQueued` class, add:

```python
class InvalidDocUrl(ValueError):
    """Only a Google Docs document link can be a contact doc (contact-docs spec D8)."""


DOC_URL = re.compile(r"^https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)(?:[/?#]\S*)?$")
```

After `set_lead`, add:

```python
def doc_id(url: str) -> str:
    """The Drive file id inside a Google Docs link."""
    match = DOC_URL.match(url.strip())
    if match is None:
        raise InvalidDocUrl(f"not a Google Docs link: {url}")
    return match.group(1)


def set_doc(conn: Any, person_id: int, url: str | None) -> dict:
    """Record the person's contact doc, or clear it with None (contact-docs spec D8). The
    link is the only record of which doc belongs to whom."""
    if url is not None:
        doc_id(url)
        url = url.strip()
    if outreach_repo.lock_contact(conn, person_id) is None:
        raise NotInPipeline(str(person_id))
    return outreach_repo.update_contact(conn, person_id, {"doc_url": url})


def people_named(conn: Any, text: str) -> list[dict]:
    """Pipeline people whose display name contains `text` (contact-docs spec §4)."""
    text = text.strip()
    return outreach_repo.pipeline_people_named(conn, text) if text else []
```

In `queue_item`, add after `"follow_up_on": ...`:

```python
        "doc_url": r["doc_url"],
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/outreach.py tests/test_outreach.py
git commit -m "outreach: set_doc, doc_id and people_named; queue items carry doc_url"
```

---

### Task 3: CLI — `outreach doc`, the queue's `doc` column

**Files:**
- Modify: `scripts/outreach.py`:
  - module docstring usage block;
  - `USER_ERRORS` (~line 73);
  - `cmd_queue` (~line 124);
  - new `cmd_doc` after `cmd_lead`;
  - `COMMANDS`;
  - `build_parser`.
- Test: `tests/test_outreach_cli.py`

**Interfaces:**
- Consumes: `outreach.set_doc`, `outreach.InvalidDocUrl` (Task 2); `queue_item["doc_url"]`.
- Produces: `outreach doc <person> <url>`; `outreach doc <person> --clear`; a `doc` column in the `queue` table.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_outreach_cli.py`)

```python
DOC = "https://docs.google.com/document/d/doc7/edit"


def test_doc_records_the_link(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(
        cli.outreach, "set_doc", lambda c, pid, url: seen.update(pid=pid, url=url) or {}
    )
    code, out, _ = run("doc", "alice@example.com", DOC)
    assert code == 0 and seen == {"pid": 7, "url": DOC}
    assert f"doc recorded ({DOC})" in out and run.conn.log == ["commit"]


def test_doc_clear(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(cli.outreach, "set_doc", lambda c, pid, url: seen.update(url=url) or {})
    code, out, _ = run("doc", "alice@example.com", "--clear")
    assert code == 0 and seen == {"url": None} and "doc link cleared" in out


@pytest.mark.parametrize("argv", [("doc", "alice@example.com"), ("doc", "alice@example.com", DOC, "--clear")])
def test_doc_needs_a_link_or_clear(run, argv):
    code, _, err = run(*argv)
    assert code == 1 and "--clear" in err


def test_doc_refuses_a_non_docs_link(monkeypatch, run):
    def set_doc(c, pid, url):
        raise svc.InvalidDocUrl(f"not a Google Docs link: {url}")

    monkeypatch.setattr(cli.outreach, "set_doc", set_doc)
    code, _, err = run("doc", "alice@example.com", "https://example.com/x")
    assert code == 1 and "not a Google Docs link" in err and run.conn.log == ["rollback"]


def test_queue_table_marks_people_with_a_doc(monkeypatch, run):
    def item(pid, name, doc_url):
        return {
            "person": {"id": pid, "email": None, "display_name": name, "company": None},
            "rank": None,
            "last_interaction": None,
            "channels": {"email": True},
            "follow_up_on": None,
            "has_lead": False,
            "doc_url": doc_url,
        }

    items = [item(7, "Alice Example", DOC), item(8, "Bob Example", None)]
    monkeypatch.setattr(cli.outreach, "queue", lambda c, *, stage, limit: items)
    monkeypatch.setattr(cli.outreach, "shared_briefs", lambda c: [])
    code, out, _ = run("queue")
    lines = out.splitlines()
    assert code == 0 and "doc" in lines[0]
    assert "✓" in next(line for line in lines if "Alice" in line)
    assert "✓" not in next(line for line in lines if "Bob" in line)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_cli.py -k "doc" -v`
Expected: FAIL. argparse exits with `invalid choice: 'doc'`, and the queue header has no `doc`.

- [ ] **Step 3: Implement**

In the module docstring's usage block, after the `lead` line:

```
  python scripts/outreach.py doc <person> <google-docs-url> | --clear
```

Add `outreach.InvalidDocUrl,` to `USER_ERRORS` (after `outreach.NotQueued,`).

In `cmd_queue`, the row list gains a column before the lead flag, and the header gains `"doc"`:

```python
        rows = [
            [
                i["person"]["id"],
                i["rank"] or "",
                _label(i["person"]),
                i["person"]["company"] or "",
                i["last_interaction"] or "never",
                " ".join(k for k, v in i["channels"].items() if v),
                i["follow_up_on"] or "",
                "✓" if i["doc_url"] else "",
                "lead" if i["has_lead"] else "",
            ]
            for i in items
        ]
        print(
            outreach_report.table(
                ["id", "rank", "name", "company", "last", "channels", "follow-up", "doc", ""],
                rows,
            ),
            file=out,
        )
```

After `cmd_lead`:

```python
def cmd_doc(conn: Any, args: argparse.Namespace, out: TextIO, now: datetime) -> None:
    """Record or clear a contact doc by hand (contact-docs spec §4). `outreach-sessions
    --write-doc` records the link itself; this is for a fix, or after Ben deletes a doc."""
    if bool(args.url) == args.clear:
        raise CliError("give a Google Docs link or --clear, not both or neither")
    person = _resolve(conn, args.person)
    outreach.set_doc(conn, person["id"], None if args.clear else args.url)
    conn.commit()
    done = "doc link cleared" if args.clear else f"doc recorded ({args.url})"
    print(f"{_label(person)}: {done}", file=out)
```

Add `"doc": cmd_doc,` to `COMMANDS` (after `"lead": cmd_lead,`). In `build_parser`, after the `lead` parser:

```python
    dc = sub.add_parser("doc")
    dc.add_argument("person")
    dc.add_argument("url", nargs="?")
    dc.add_argument("--clear", action="store_true")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/outreach.py tests/test_outreach_cli.py
git commit -m "outreach CLI: doc subcommand and a doc column in queue"
```

---

### Task 4: docs-api client

**Files:**
- Create: `clients/docs_api.py`
- Modify: `scripts/fetch-env.sh` (after the `INBOX_API_URL=` line)
- Test: `tests/test_docs_api.py`

**Interfaces:**
- Consumes: `clients.gcp_auth.id_token_for(audience: str) -> str`.
- Produces (all raise on failure):
  - `clients.docs_api.configured() -> bool`;
  - `get_doc(doc_id: str) -> dict` (DocRef plus `content`);
  - `create_doc(title: str, markdown: str, parent: str) -> dict` (DocRef, with `web_url`);
  - `replace_doc(doc_id: str, markdown: str, if_version: str) -> dict`;
  - `folder_id(entry_id: str) -> str`;
  - exceptions `DocsApiUnavailable(RuntimeError)`, `VersionConflict(RuntimeError)`, `FolderNotFound(LookupError)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_docs_api.py`)

```python
import httpx
import pytest

from clients import docs_api

BASE = "https://docs.example.test"
AUTH = {"Authorization": f"Bearer tok-for-{BASE}"}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DOCS_API_URL", BASE + "/")
    monkeypatch.setattr(docs_api.gcp_auth, "id_token_for", lambda aud: f"tok-for-{aud}")


def test_get_doc(monkeypatch, env):
    seen = {}

    def get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return Resp(200, {"content": "x", "version": ""})

    monkeypatch.setattr(docs_api.httpx, "get", get)
    assert docs_api.get_doc("doc7") == {"content": "x", "version": ""}
    assert seen == {"url": f"{BASE}/docs/gdrive/doc7", "headers": AUTH}


def test_create_doc_posts_title_markdown_and_parent(monkeypatch, env):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return Resp(201, {"web_url": "https://docs.google.com/document/d/new1/edit"})

    monkeypatch.setattr(docs_api.httpx, "post", post)
    ref = docs_api.create_doc("Outreach — Alex Example", "# x\n", "folder-1")
    assert ref["web_url"].endswith("/new1/edit")
    assert seen["url"] == f"{BASE}/docs" and seen["headers"] == AUTH
    assert seen["json"] == {"title": "Outreach — Alex Example", "markdown": "# x\n", "parent": "folder-1"}


def test_replace_doc_sends_if_version_and_maps_409(monkeypatch, env):
    calls = []

    def put(url, json, headers, timeout):
        calls.append((url, json))
        return Resp(409 if len(calls) == 2 else 200, {"version": "v2"})

    monkeypatch.setattr(docs_api.httpx, "put", put)
    assert docs_api.replace_doc("doc7", "m", "v1") == {"version": "v2"}
    assert calls[0] == (f"{BASE}/docs/gdrive/doc7", {"markdown": "m", "if_version": "v1"})
    with pytest.raises(docs_api.VersionConflict):
        docs_api.replace_doc("doc7", "m", "")


def test_folder_id_reads_the_catalog(monkeypatch, env):
    body = {
        "folders": [
            {"id": "inbox", "folder_id": "f0"},
            {"id": "consulting-outreach", "folder_id": "f1"},
        ]
    }
    monkeypatch.setattr(docs_api.httpx, "get", lambda url, headers, timeout: Resp(200, body))
    assert docs_api.folder_id("consulting-outreach") == "f1"
    with pytest.raises(docs_api.FolderNotFound, match="nope"):
        docs_api.folder_id("nope")


def test_an_unset_url_is_unavailable(monkeypatch):
    monkeypatch.delenv("DOCS_API_URL", raising=False)
    assert not docs_api.configured()
    with pytest.raises(docs_api.DocsApiUnavailable):
        docs_api.get_doc("doc7")


def test_http_errors_raise(monkeypatch, env):
    monkeypatch.setattr(docs_api.httpx, "get", lambda url, headers, timeout: Resp(413))
    with pytest.raises(httpx.HTTPStatusError):
        docs_api.get_doc("doc7")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_docs_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'docs_api'`.

- [ ] **Step 3: Implement**

`clients/docs_api.py`:

```python
"""docs-api (Cloud Run, IAM-authenticated): fetch, create and replace the outreach
contact docs (contact-docs spec §3) and look up a filing-catalog folder. Every call
raises on failure. Local only: services/outreach_doc_write.py, run by
scripts/outreach_sessions.py, is the one caller. The URL is read per call so tests can
set it late; it is also the ID-token audience."""

import os

import httpx

from clients import gcp_auth

TIMEOUT_S = 60  # a live export of a doc holding 300 messages is slow


class DocsApiUnavailable(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    """409: the doc changed since the version the merge started from."""


class FolderNotFound(LookupError):
    pass


def configured() -> bool:
    return bool(os.environ.get("DOCS_API_URL", "").strip())


def _base() -> str:
    url = os.environ.get("DOCS_API_URL", "").strip().rstrip("/")
    if not url:
        raise DocsApiUnavailable("DOCS_API_URL unset (scripts/fetch-env.sh writes it)")
    return url


def _headers(base: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"}


def get_doc(doc_id: str) -> dict:
    """A live export: `content` (markdown) and `version` (often empty for a Google Doc)."""
    base = _base()
    resp = httpx.get(f"{base}/docs/gdrive/{doc_id}", headers=_headers(base), timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def create_doc(title: str, markdown: str, parent: str) -> dict:
    base = _base()
    resp = httpx.post(
        f"{base}/docs",
        json={"title": title, "markdown": markdown, "parent": parent},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def replace_doc(doc_id: str, markdown: str, if_version: str) -> dict:
    """A full-body replace. Deletes the doc's Google comments (docs-api has no comment
    endpoint); the doc says so in Ben's notes."""
    base = _base()
    resp = httpx.put(
        f"{base}/docs/gdrive/{doc_id}",
        json={"markdown": markdown, "if_version": if_version},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    if resp.status_code == 409:
        raise VersionConflict(doc_id)
    resp.raise_for_status()
    return resp.json()


def folder_id(entry_id: str) -> str:
    """The Drive folder behind a filing-catalog entry, so no Drive id is hard-coded."""
    base = _base()
    resp = httpx.get(f"{base}/folders", headers=_headers(base), timeout=TIMEOUT_S)
    resp.raise_for_status()
    for folder in resp.json().get("folders", []):
        if folder.get("id") == entry_id and folder.get("folder_id"):
            return str(folder["folder_id"])
    raise FolderNotFound(
        f"the filing catalog has no '{entry_id}' folder (docs repo filing/manifest.yaml)"
    )
```

In `scripts/fetch-env.sh`, after `INBOX_API_URL=https://inbox-api.drolet.cloud`:

```bash
DOCS_API_URL=$(gcloud run services describe docs-api --region us-central1 --project "$PROJECT" --format='value(status.url)' 2>/dev/null || true)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_docs_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clients/docs_api.py tests/test_docs_api.py scripts/fetch-env.sh
git commit -m "docs-api client for outreach contact docs; DOCS_API_URL in fetch-env"
```

---

### Task 5: The doc — render, parse, merge (pure)

**Files:**
- Create: `services/outreach_doc.py`
- Test: `tests/test_outreach_doc.py`

**Interfaces:**
- Consumes: `services.outreach.TZ`; `services.outreach_session.CHANNEL_TITLES` (`{"imessage": "iMessage", "whatsapp": "WhatsApp", "linkedin": "LinkedIn"}`).
- Produces:
  - constants `DOC_MESSAGES = 100`, `FOLDER_ENTRY = "consulting-outreach"`, `TITLE_PREFIX`, `NEXT_STEPS`, `NOTES_PREAMBLE`;
  - `SectionsError(ValueError)`;
  - `@dataclass(frozen=True) Sections(who: str, history: str, next_step: str, talking_points: str, log: tuple[str, ...] = ())`;
  - `title(person: dict) -> str`;
  - `them(person: dict) -> str`;
  - `render_messages(messages: dict[str, list[dict]], who: str, *, today: date) -> str`;
  - `parse_sections(text: str) -> Sections`;
  - `merge(existing: str | None, update: Sections, *, person: dict, stage: str, messages: str, today: date) -> str`.

- [ ] **Step 1: Write the failing tests** (`tests/test_outreach_doc.py`)

```python
from datetime import UTC, date, datetime

import pytest

from services import outreach_doc as doc

TODAY = date(2026, 10, 15)
PERSON = {"id": 7, "email": "alex@example.com", "display_name": "Alex Example"}
TS = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)  # 11:30 in New York
UPDATE = doc.Sections(
    who="Founder at a clinic-software startup.",
    history="Worked together in 2019.",
    next_step="nudge 1",
    talking_points="- ask how the launch went",
    log=("- 2026-10-14 · prepped brief (iMessage)", "- 2026-10-15 · briefed by iMessage"),
)

# What Drive's markdown export of a doc looks like (checked live 2026-10-14): punctuation
# escaped, apostrophes possibly curled, a blank line after each heading.
EXPORTED = r"""# Outreach — Alex Example

people id: 7 · stage: to\_contact · last updated: 2026\-10\-14

Met through a friend; prefers calls\.

## Who they are

Old role\.

## History

Old history\.

## Log

- 2026\-10\-14 · prepped brief \(iMessage\)

## Talking points \(next step: brief\)

Old points\.

## Side research

Their company raised a round\.

## Ben’s notes

Call on *Tuesday*\. Kids: two\.

## Call prep

Ask about the move\.

## Messages \(last 100 per channel, regenerated 2026\-10\-14; edits here are overwritten\)

### iMessage

- 2025\-12\-01 23:30 Ben: edited by hand
"""


def _merge(existing, update=UPDATE):
    messages = doc.render_messages(
        {"imessage": [{"sent_at": TS, "from_me": False, "text": "hi", "has_attachments": False}]},
        "Alex",
        today=TODAY,
    )
    return doc.merge(existing, update, person=PERSON, stage="briefed", messages=messages, today=TODAY)


def test_title_and_them():
    assert doc.title(PERSON) == "Outreach — Alex Example"
    assert doc.title({"id": 9, "display_name": None, "email": None}) == "Outreach — person 9"
    assert doc.them(PERSON) == "Alex" and doc.them({"display_name": None}) == "Them"


def test_render_messages_items_times_order_and_empty_channels():
    out = doc.render_messages(
        {
            "imessage": [
                {"sent_at": TS, "from_me": False, "text": "hi\nthere", "has_attachments": False},
                {"sent_at": TS, "from_me": True, "text": None, "has_attachments": True},
            ],
            "whatsapp": [],
            "linkedin": [{"sent_at": TS, "from_me": True, "text": "x" * 500, "has_attachments": False}],
        },
        "Alex",
        today=TODAY,
    )
    lines = out.splitlines()
    assert lines[0] == (
        "## Messages (last 100 per channel, regenerated 2026-10-15; edits here are overwritten)"
    )
    assert "- 2026-10-13 11:30 Alex: hi there" in lines
    assert "- 2026-10-13 11:30 Ben: [attachment]" in lines
    assert "### WhatsApp" not in out
    assert out.index("### iMessage") < out.index("### LinkedIn")
    assert "x" * 500 in out  # never truncated, unlike the session context


def test_render_messages_keeps_the_newest_per_channel():
    many = [
        {"sent_at": TS, "from_me": True, "text": f"m{i}", "has_attachments": False}
        for i in range(doc.DOC_MESSAGES + 5)
    ]
    out = doc.render_messages({"imessage": many}, "Alex", today=TODAY)
    assert "Ben: m4\n" not in out + "\n" and "Ben: m5\n" in out + "\n"
    assert out.count("\n- ") == doc.DOC_MESSAGES


def test_render_messages_escapes_markdown():
    text = "# not a heading [link](http://x) *bold* | a | b |"
    out = doc.render_messages(
        {"whatsapp": [{"sent_at": TS, "from_me": False, "text": text, "has_attachments": False}]},
        "Alex",
        today=TODAY,
    )
    item = out.splitlines()[-1]
    assert item.startswith("- 2026-10-13 11:30 Alex: ")
    assert r"\# not a heading \[link\](http://x) \*bold\* \| a \| b \|" in item
    assert "\n## " not in out.split("\n", 1)[1]


def test_render_messages_with_nothing_on_file():
    assert doc.render_messages({}, "Alex", today=TODAY).endswith("No direct messages on file.")


SECTIONS = """## Who they are

Founder.

## History

Old friend.

## Log

- 2026-10-14 · prepped brief (iMessage)

## Talking points (next step: brief)

- ask about the launch
"""


def test_parse_sections():
    s = doc.parse_sections(SECTIONS)
    assert s == doc.Sections(
        who="Founder.",
        history="Old friend.",
        next_step="brief",
        talking_points="- ask about the launch",
        log=("- 2026-10-14 · prepped brief (iMessage)",),
    )
    assert doc.parse_sections(SECTIONS.replace("## Log\n\n- 2026-10-14 · prepped brief (iMessage)\n\n", "")).log == ()


@pytest.mark.parametrize(
    "text,message",
    [
        (SECTIONS.replace("## History\n\nOld friend.\n\n", ""), "missing or empty: history"),
        (SECTIONS.replace("Founder.", ""), "missing or empty: who they are"),
        (SECTIONS.replace("(next step: brief)", "(next step: later)"), "next step"),
        (SECTIONS.replace(" (next step: brief)", ""), "next step"),
        (SECTIONS + "\n## Ben's notes\n\nmine\n", "Ben's notes"),
        (SECTIONS + "\n## Messages\n\n- x\n", "## Messages"),
        (SECTIONS + "\n## History\n\nagain\n", "appears twice"),
        (SECTIONS.replace("- 2026-10-14 · prepped", "prepped"), "a log line"),
    ],
)
def test_parse_sections_refuses(text, message):
    with pytest.raises(doc.SectionsError, match=message):
        doc.parse_sections(text)


def test_merge_builds_a_new_doc():
    out = _merge(None)
    assert out.startswith(
        "# Outreach — Alex Example\n\npeople id: 7 · stage: briefed · last updated: 2026-10-15\n\n"
    )
    order = ["## Who they are", "## History", "## Log", "## Talking points (next step: nudge 1)",
             "## Ben's notes", "## Messages"]
    positions = [out.index(h) for h in order]
    assert positions == sorted(positions)
    assert doc.NOTES_PREAMBLE in out
    assert "- 2026-10-14 · prepped brief (iMessage)\n- 2026-10-15 · briefed by iMessage" in out
    assert "- 2026-10-13 11:30 Alex: hi" in out and out.endswith("\n")


def test_merge_reads_a_drive_export():
    out = _merge(EXPORTED)
    # Ben's notes and the section Ben put after them: exactly as exported, still in order.
    assert "## Ben’s notes\n\nCall on *Tuesday*\\. Kids: two\\." in out
    assert out.index("## Ben’s notes") < out.index("## Call prep") < out.index("## Messages")
    assert out.count("## Ben") == 1 and doc.NOTES_PREAMBLE not in out
    # The log: kept as exported, the escaped twin of a new line not repeated, the new one appended.
    assert out.count("prepped brief") == 1
    assert "- 2026\\-10\\-14 · prepped brief \\(iMessage\\)\n- 2026-10-15 · briefed by iMessage" in out
    # Machine sections replaced; unrecognized ones kept before Ben's notes.
    assert "Old role" not in out and "Old history" not in out and "Old points" not in out
    assert out.index("## Talking points (next step: nudge 1)") < out.index("## Side research")
    assert out.index("## Side research") < out.index("## Ben’s notes")
    # Text Ben added under the header survives; the old header line does not.
    assert "Met through a friend; prefers calls\\." in out and "to\\_contact" not in out
    # Messages regenerated: Ben's edit there is gone.
    assert "edited by hand" not in out and "Alex: hi" in out
    assert out.count("## Messages") == 1


def test_merge_keeps_a_second_copy_of_a_machine_heading():
    out = _merge(EXPORTED.replace("## Side research", "## History"))
    assert "Their company raised a round" in out and "Old history" not in out


def test_merge_is_idempotent():
    once = _merge(EXPORTED)
    assert _merge(once) == once
    assert _merge(_merge(None)) == _merge(None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_doc.py -v`
Expected: FAIL with `ImportError: cannot import name 'outreach_doc'`.

- [ ] **Step 3: Implement** (`services/outreach_doc.py`)

```python
"""The outreach contact doc (contact-docs spec §3): its title, the regenerated Messages
section, the sections file a session writes, and the merge that builds every version of
the doc. Pure, no I/O. Every write goes through `merge`, so the preservation rules (Ben's
notes, the log) can't be skipped. Drive's markdown export escapes punctuation and may
curl apostrophes, so headings and log lines are compared after `_plain`; text this module
doesn't rewrite is kept exactly as exported."""

import re
from dataclasses import dataclass
from datetime import date

from services import outreach
from services.outreach_session import CHANNEL_TITLES

DOC_MESSAGES = 100
FOLDER_ENTRY = "consulting-outreach"
TITLE_PREFIX = "Outreach — "
NEXT_STEPS = ("brief", "nudge 1", "nudge 2", "close", "reply", "intro", "none")
SESSION_SECTIONS = ("who they are", "history", "talking points")
NOTES = "ben's notes"
NOTES_PREAMBLE = (
    "_Ben's own notes: sessions never edit this section. Write here, not in Google "
    "comments: every machine update deletes comments._"
)

_ESCAPED = re.compile(r"\\(.)")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~])")
_NEXT_STEP = re.compile(r"\(next step:\s*([^)]*)\)")
_LOG_LINE = re.compile(r"^[-*] \d{4}-\d{2}-\d{2} · \S")


class SectionsError(ValueError):
    """The session's sections file can't be merged; nothing is written."""


@dataclass(frozen=True)
class Sections:
    who: str
    history: str
    next_step: str
    talking_points: str
    log: tuple[str, ...] = ()


def _plain(text: str) -> str:
    """Text as Ben sees it: Drive's export escapes removed, apostrophes straightened."""
    return _ESCAPED.sub(r"\1", text).replace("’", "'")


def _key(heading: str) -> str:
    """`## Talking points (next step: brief)` → `talking points`."""
    return _plain(heading[3:]).strip().lower().split(" (", 1)[0].strip()


def _split(markdown: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """The lines before the first `## ` heading, then (heading, body lines) per section."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            sections.append((line.rstrip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, sections


def _trim(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n").rstrip()


def _escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def title(person: dict) -> str:
    name = person.get("display_name") or person.get("email") or f"person {person['id']}"
    return TITLE_PREFIX + name


def them(person: dict) -> str:
    return (person.get("display_name") or "").split(" ")[0] or "Them"


def _message_item(m: dict, who: str) -> str:
    text = " ".join((m.get("text") or "").split())
    if text:
        text = _escape(text)
    else:
        text = "[attachment]" if m.get("has_attachments") else "[no text]"
    stamp = m["sent_at"].astimezone(outreach.TZ).strftime("%Y-%m-%d %H:%M")
    return f"- {stamp} {'Ben' if m['from_me'] else _escape(who)}: {text}"


def render_messages(messages: dict[str, list[dict]], who: str, *, today: date) -> str:
    """The `## Messages` section (spec D6): the newest DOC_MESSAGES per channel, oldest
    first, one list item each so Drive keeps them on separate lines. Regenerated on every
    write; never merged."""
    lines = [
        f"## Messages (last {DOC_MESSAGES} per channel, regenerated {today.isoformat()}; "
        "edits here are overwritten)"
    ]
    for channel, channel_title in CHANNEL_TITLES.items():
        recent = (messages.get(channel) or [])[-DOC_MESSAGES:]
        if recent:
            lines += ["", f"### {channel_title}", ""]
            lines += [_message_item(m, who) for m in recent]
    if len(lines) == 1:
        lines += ["", "No direct messages on file."]
    return "\n".join(lines)


def parse_sections(text: str) -> Sections:
    """The file a session writes: Who they are, History, Talking points (next step: …)
    and, optionally, Log lines to append. Anything else is refused."""
    _, sections = _split(text)
    found: dict[str, tuple[str, str]] = {}
    for heading, body in sections:
        key = _key(heading)
        if key not in (*SESSION_SECTIONS, "log"):
            raise SectionsError(
                f"a session writes only Who they are, History, Log and Talking points, "
                f"not {heading!r}"
            )
        if key in found:
            raise SectionsError(f"{heading!r} appears twice")
        found[key] = (heading, _trim(body))
    missing = [k for k in SESSION_SECTIONS if not found.get(k, ("", ""))[1]]
    if missing:
        raise SectionsError(f"missing or empty: {', '.join(missing)}")
    match = _NEXT_STEP.search(_plain(found["talking points"][0]))
    step = match.group(1).strip().lower() if match else ""
    if step not in NEXT_STEPS:
        raise SectionsError(
            f"the Talking points heading needs '(next step: <{' | '.join(NEXT_STEPS)}>)'"
        )
    log = tuple(
        line.strip() for line in found.get("log", ("", ""))[1].splitlines() if line.strip()
    )
    for line in log:
        if not _LOG_LINE.match(line):
            raise SectionsError(f"a log line is '- YYYY-MM-DD · what happened', not {line!r}")
    return Sections(
        who=found["who they are"][1],
        history=found["history"][1],
        next_step=step,
        talking_points=found["talking points"][1],
        log=log,
    )


def _same_log(line: str) -> str:
    return " ".join(_plain(line).lstrip("-* ").split())


def merge(
    existing: str | None,
    update: Sections,
    *,
    person: dict,
    stage: str,
    messages: str,
    today: date,
) -> str:
    """The whole new doc. Keeps from `existing`: `Ben's notes` and every section after it
    (Ben's), every log line, unrecognized sections (before Ben's notes) and unrecognized
    text under the header. Rewrites the title, the header line and the session's sections;
    `messages` (render_messages) replaces the Messages section."""
    preamble, sections = _split(existing or "")
    extra = [
        line
        for line in preamble
        if line.strip()
        and not line.startswith("# ")
        and not _plain(line).startswith("people id:")
    ]
    log: list[str] = []
    replaced: set[str] = set()
    before_notes: list[str] = []
    notes: list[str] = []  # Ben's notes, then whatever Ben put after them
    for heading, body in sections:
        key = _key(heading)
        block = f"{heading}\n\n{_trim(body)}".rstrip()
        if key == "messages":
            continue
        if key == "log":
            log += [line.rstrip() for line in body if line.strip()]
        elif key in SESSION_SECTIONS and key not in replaced and not notes:
            replaced.add(key)
        elif key == NOTES or notes:
            notes.append(block)
        else:
            before_notes.append(block)

    seen = {_same_log(line) for line in log}
    for line in update.log:
        if _same_log(line) not in seen:
            log.append(line)
            seen.add(_same_log(line))
    if not notes:
        notes = [f"## Ben's notes\n\n{NOTES_PREAMBLE}"]

    parts = [
        f"# {title(person)}",
        f"people id: {person['id']} · stage: {stage} · last updated: {today.isoformat()}",
        *(["\n".join(extra)] if extra else []),
        f"## Who they are\n\n{update.who}",
        f"## History\n\n{update.history}",
        "## Log" + ("\n\n" + "\n".join(log) if log else ""),
        f"## Talking points (next step: {update.next_step})\n\n{update.talking_points}",
        *before_notes,
        *notes,
        messages,
    ]
    return "\n\n".join(parts) + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_doc.py -v`
Expected: PASS. If `test_merge_is_idempotent` fails, diff the two outputs before changing anything. The usual cause is a block gaining or losing a blank line; fix it in `merge`, not in the test.

- [ ] **Step 5: Commit**

```bash
git add services/outreach_doc.py tests/test_outreach_doc.py
git commit -m "outreach_doc: render messages, parse a session's sections, merge into the doc"
```

---

### Task 6: Claude sessions — resume, liveness, transcripts, records

**Files:**
- Modify: `clients/claude_sessions.py` (replace `live_session_ids`, `load_state`, `save_state`, `reconcile`; add the rest)
- Test: `tests/test_claude_sessions.py` (replace `test_live_session_ids_reads_short_ids` and `test_state_round_trip_and_reconcile`; keep the four spawn tests)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `PROJECTS_DIR: Path`;
  - `resume_argv(session_id: str, prompt: str | None = None) -> list[str]`;
  - `resume(session_id: str, *, cwd: Path, prompt: str | None = None, runner=_run) -> str | None` (short id);
  - `live_sessions(*, cwd: Path, runner=_run) -> dict[str, str]` (full or short id → cwd);
  - `find_live(session_id: str, live: dict[str, str]) -> str | None`;
  - `find_transcript(session_id: str, projects_dir: Path | None = None) -> Path | None`;
  - `transcript_cwd(path: Path) -> str | None`;
  - `load_state(path) -> dict[str, dict]`;
  - `save_state(path, state: dict[str, dict]) -> None`;
  - removes `live_session_ids` and `reconcile`.

- [ ] **Step 1: Write the failing tests** (in `tests/test_claude_sessions.py`, delete the last two tests and append)

```python
SID = "ab12cd34-0000-4000-8000-000000000001"


def test_resume_argv_puts_the_prompt_last():
    assert cs.resume_argv(SID) == ["claude", "--bg", "--resume", SID]
    assert cs.resume_argv(SID, "go") == ["claude", "--bg", "--resume", SID, "go"]


def test_resume_runs_in_cwd_and_keeps_the_id():
    seen = {}

    def runner(argv, cwd):
        seen.update(argv=argv, cwd=cwd)
        return _result("resumed in the background")

    assert cs.resume(SID, cwd=CWD, prompt="go", runner=runner) == "ab12cd34"
    assert seen == {"argv": ["claude", "--bg", "--resume", SID, "go"], "cwd": CWD}
    ok = lambda argv, cwd: _result("backgrounded · ab12cd34 · outreach: X")  # noqa: E731
    assert cs.resume(SID, cwd=CWD, runner=ok) == "ab12cd34"
    assert cs.resume(SID, cwd=CWD, runner=lambda argv, cwd: _result("", 1)) is None


def test_live_sessions_lists_running_sessions_only():
    seen = {}
    listing = [{"sessionId": SID, "cwd": "/a"}, {"id": "ef56ab78", "cwd": "/b"}, {}]

    def runner(argv, cwd):
        seen["argv"] = argv
        return _result(json.dumps(listing))

    assert cs.live_sessions(cwd=CWD, runner=runner) == {SID: "/a", "ef56ab78": "/b"}
    assert seen["argv"] == ["claude", "agents", "--json"]  # --all would list exited sessions
    assert cs.live_sessions(cwd=CWD, runner=lambda argv, cwd: _result("not json")) == {}


def test_find_live_matches_by_prefix_either_way():
    live = {SID: "/a", "ef56ab78": "/b"}
    assert cs.find_live("ab12cd34", live) == SID
    assert cs.find_live(SID, live) == SID
    assert cs.find_live("ef56ab78-1111-4000-8000-000000000000", live) == "ef56ab78"
    assert cs.find_live("", live) is None and cs.find_live("99999999", live) is None


def test_find_transcript_searches_every_project_dir(tmp_path):
    worktree = tmp_path / "-Users-me-src-repo--claude-worktrees-feature"
    worktree.mkdir()
    (tmp_path / "-Users-me-src-repo").mkdir()
    (worktree / f"{SID}.jsonl").write_text("{}\n")
    assert cs.find_transcript("ab12cd34", tmp_path) == worktree / f"{SID}.jsonl"
    assert cs.find_transcript(SID, tmp_path) == worktree / f"{SID}.jsonl"
    assert cs.find_transcript("ffffffff", tmp_path) is None
    assert cs.find_transcript("ab12", tmp_path) is None  # too short to trust
    assert cs.find_transcript("../../etc", tmp_path) is None


def test_find_transcript_refuses_an_ambiguous_prefix(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / f"ab12cd34-{d}.jsonl").write_text("")
    assert cs.find_transcript("ab12cd34", tmp_path) is None


def test_transcript_cwd_is_the_last_one_recorded(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"cwd": "/repo"}),
        "not json",
        json.dumps([1]),
        json.dumps({"type": "summary"}),
        json.dumps({"cwd": "/repo/.claude/worktrees/w"}),
    ]
    path.write_text("\n".join(lines) + "\n")
    assert cs.transcript_cwd(path) == "/repo/.claude/worktrees/w"
    assert cs.transcript_cwd(tmp_path / "missing.jsonl") is None


def test_state_records_and_legacy_entries(tmp_path):
    path = tmp_path / "state.json"
    assert cs.load_state(path) == {}
    record = {"session_id": SID, "cwd": "/r", "name": "outreach: X"}
    path.write_text(json.dumps({"388": "ab12cd34", "1": "", "2": {"cwd": "/x"}, "3": record}))
    assert cs.load_state(path) == {"388": {"session_id": "ab12cd34"}, "3": record}
    cs.save_state(path, {"3": record})
    assert cs.load_state(path) == {"3": record}
    path.write_text("[1, 2]")
    assert cs.load_state(path) == {}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_claude_sessions.py -v`
Expected: FAIL with `AttributeError: module 'clients.claude_sessions' has no attribute 'resume_argv'`.

- [ ] **Step 3: Implement**

Replace the module docstring and everything after `spawn` in `clients/claude_sessions.py`. `_run`, `spawn_argv`, `parse_session_id` and `spawn` stay as they are. Add `import re` to the imports.

```python
"""Background Claude Code sessions for local scripts: spawn one with `claude --bg`,
resume an exited one with `claude --bg --resume`, list the running ones, find a
session's transcript, and keep the per-key session records. I/O only. Mirrors
~/src/tasks/scripts/task_sessions.py's spawn half."""

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

SHORT_ID = 8
PROJECTS_DIR = Path.home() / ".claude" / "projects"
_SESSION_ID = re.compile(r"[0-9a-f-]{8,36}")
```

…then, after `spawn`:

```python
def resume_argv(session_id: str, prompt: str | None = None) -> list[str]:
    argv = ["claude", "--bg", "--resume", session_id]
    return [*argv, prompt] if prompt else argv


def resume(
    session_id: str, *, cwd: Path, prompt: str | None = None, runner: Runner = _run
) -> str | None:
    """Bring an exited session back under its own id. `cwd` must be the directory it last
    ran in (transcript_cwd): the transcript is looked up under that directory's project.
    A resume keeps its id, so the short id stands even if no `backgrounded` line prints."""
    result = runner(resume_argv(session_id, prompt), cwd)
    if result.returncode != 0:
        return None
    return parse_session_id(result.stdout) or session_id[:SHORT_ID]


def live_sessions(*, cwd: Path, runner: Runner = _run) -> dict[str, str]:
    """Session id (full when listed, else short) → cwd for every running session.
    `claude agents --json` without --all: with it, exited sessions are listed too."""
    try:
        listing = json.loads(runner(["claude", "agents", "--json"], cwd).stdout or "[]")
    except (OSError, json.JSONDecodeError):
        return {}
    live = {}
    for session in listing:
        sid = session.get("sessionId") or session.get("id")
        if sid:
            live[sid] = session.get("cwd") or ""
    return live


def find_live(session_id: str, live: dict[str, str]) -> str | None:
    """The running session `session_id` (full or short) names, by prefix either way."""
    if not session_id:
        return None
    for sid in live:
        if sid.startswith(session_id) or session_id.startswith(sid):
            return sid
    return None


def find_transcript(session_id: str, projects_dir: Path | None = None) -> Path | None:
    """The session's transcript in any project directory: a session that entered a
    worktree writes under that worktree's directory from then on. None when missing,
    ambiguous, or `session_id` doesn't look like an id."""
    if not _SESSION_ID.fullmatch(session_id):
        return None
    hits = sorted((projects_dir or PROJECTS_DIR).glob(f"*/{session_id}*.jsonl"))
    return hits[0] if len(hits) == 1 else None


def transcript_cwd(path: Path) -> str | None:
    """The last working directory the transcript records. The project-directory name
    can't be decoded instead: it writes `/` and `.` both as `-`."""
    cwd = None
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    value = json.loads(line).get("cwd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(value, str) and value:
                    cwd = value
    except OSError:
        return None
    return cwd


def load_state(path: Path) -> dict[str, dict]:
    """Key → {"session_id", "cwd", "name"}. A PR #26 entry (a bare short id) reads as
    {"session_id": <short id>}; every lookup matches ids by prefix, so it still works."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, dict] = {}
    for key, value in raw.items():
        if isinstance(value, str) and value:
            state[key] = {"session_id": value}
        elif isinstance(value, dict) and value.get("session_id"):
            state[key] = dict(value)
    return state


def save_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_claude_sessions.py -v`
Expected: PASS. `tests/test_outreach_sessions_cli.py` now fails, because the script still calls `live_session_ids` and `reconcile`. Task 9 rewrites it; don't commit with the full suite red. Run only this file's tests for this commit, and note that in the commit body.

- [ ] **Step 5: Commit**

```bash
git add clients/claude_sessions.py tests/test_claude_sessions.py
git commit -m "claude_sessions: resume, running-only liveness, transcript lookup, session records

scripts/outreach_sessions.py moves to the new API in a later commit on this branch."
```

---

### Task 7: Session context — the doc, `RELOAD_PROMPT`, D6 docstrings

**Files:**
- Modify: `services/outreach_session.py` (docstring, `RELOAD_PROMPT`, `context_block`)
- Modify: `repo/imessage.py:338-341`, `repo/whatsapp.py:405-409` (docstrings only)
- Test: `tests/test_outreach_session.py` (add `"doc_url": None,` to `ROW`; append tests)

**Interfaces:**
- Consumes: `queue_item["doc_url"]` (Task 2).
- Produces: `services.outreach_session.RELOAD_PROMPT: str`; `context_block(snap, *, today: date, doc: str | None = None) -> str`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_outreach_session.py`)

```python
def test_context_block_names_the_doc_and_carries_its_content(monkeypatch):
    url = "https://docs.google.com/document/d/doc388/edit"
    _patch(monkeypatch, FakeRepos(row=ROW | {"doc_url": url}))
    snap = svc.snapshot(None, 388, limit=60)
    block = svc.context_block(snap, today=date(2026, 10, 13), doc="# Outreach — Jonah Example\n\nbody")
    assert f"- Contact doc: {url}" in block
    assert "## Contact doc (fetched 2026-10-13)" in block and "\nbody" in block
    assert block.index("## Contact doc") < block.index("## Message history")
    assert "--write-doc" in block


def test_context_block_without_a_doc(monkeypatch):
    _patch(monkeypatch, FakeRepos())
    block = svc.context_block(svc.snapshot(None, 388, limit=60), today=date(2026, 10, 13))
    assert "- Contact doc: none yet (Prep a brief creates it)" in block
    assert "## Contact doc (" not in block


def test_reload_prompt_names_the_steps_in_order_and_forbids_drafting():
    p = svc.RELOAD_PROMPT
    assert "working-outreach-queue" in p and '"Reload"' in p
    steps = ["--print-context", "search email", "what changed", "--write-doc", "talking points", "wait"]
    positions = [p.index(s) for s in steps]
    assert positions == sorted(positions)
    assert "never draft" in p
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_session.py -v`
Expected: FAIL. `context_block` gets an unexpected keyword argument `doc`, and `RELOAD_PROMPT` doesn't exist.

- [ ] **Step 3: Implement**

Module docstring of `services/outreach_session.py`, replacing the last two sentences (from "Email, web news and talking points…"):

```python
"""What a per-person outreach session boots with (scripts/outreach_sessions.py).
`snapshot` reads everything the DB knows about one pipeline person, including
their recent 1:1 messages on every channel; `context_block` renders it, plus the
person's contact doc when there is one, as the markdown appended to the spawned
session's system prompt. Email, web news and talking points are the session's own job
("Prep a brief" and "Reload" in the working-outreach-queue skill). Message text is
private: it goes into a local session's prompt and into the contact doc's Messages
section in Ben's own My Drive (contact-docs spec D6), never into people-api."""
```

After `PREP_PROMPT`:

```python
RELOAD_PROMPT = (
    "Reload this outreach session: follow the working-outreach-queue skill's \"Reload\" "
    "procedure for the person in your system prompt. In order: re-pull "
    "`outreach-sessions --print-context <people id>` (the snapshot and the contact doc); "
    "search email since the doc's last log date; tell Ben what changed; update the doc "
    "with `outreach-sessions --write-doc`; rewrite the talking points for the next step. "
    "Give Ben talking points — never draft the message; Ben writes it himself. Then wait "
    "for Ben."
)
```

`context_block` signature and body changes:

```python
def context_block(snap: dict, *, today: date, doc: str | None = None) -> str:
```

After the `if item["outreach_notes"]:` lines, add:

```python
    doc_url = item["doc_url"]
    lines.append(
        f"- Contact doc: {doc_url}" if doc_url else "- Contact doc: none yet (Prep a brief creates it)"
    )
```

Replace the block from `lines += [` (the one that starts `"", "## Pipeline",`) through `"## Message history (1:1, oldest first)",` `]` with:

```python
    lines += [
        "",
        "## Pipeline",
        "",
        f"- Stage: {_stage_line(item, snap['pipeline'])}",
        f"- Channels: {channels}",
        f"- Last message by channel: {last}",
    ]
    if doc is not None:
        lines += ["", f"## Contact doc (fetched {today.isoformat()})", "", doc.strip() or "(empty)"]
    lines += ["", "## Message history (1:1, oldest first)"]
```

Replace the closing paragraph (`"Never draft the message. ..."`) with:

```python
    lines += [
        "",
        "Never draft the message. Ben writes every brief, nudge and reply himself; "
        "give him research and talking points, and record nothing until he says he "
        "sent it. Change the contact doc only with `outreach-sessions --write-doc` "
        "(working-outreach-queue skill). Work only this person.",
    ]
```

`repo/imessage.py`, `recent_direct_messages` docstring:

```python
    """The newest `limit` messages from 1:1 chats with any handle linked to this
    person, returned oldest first. Message text is private: only local scripts
    (scripts/outreach_sessions.py) read it, for a local session's prompt and the contact
    doc in Ben's My Drive (contact-docs spec D6); never people-api."""
```

`repo/whatsapp.py`, `recent_direct_messages` docstring:

```python
    """The newest `limit` messages from direct chats with any handle linked to this
    person, returned oldest first. Local-only, and deliberately outside the reads
    section below: message text goes into a local Claude session's prompt and the contact
    doc in Ben's My Drive (scripts/outreach_sessions.py, contact-docs spec D6), and is
    never served by people-api (§7)."""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_session.py -v`
Expected: PASS, including `test_people_api_never_reads_message_text`.

- [ ] **Step 5: Commit**

```bash
git add services/outreach_session.py repo/imessage.py repo/whatsapp.py tests/test_outreach_session.py
git commit -m "outreach_session: contact doc in the context, RELOAD_PROMPT, D6 docstrings"
```

---

### Task 8: The write protocol

**Files:**
- Create: `services/outreach_doc_write.py`
- Test: `tests/test_outreach_doc_write.py`

**Interfaces:**
- Consumes:
  - `outreach_session.snapshot(conn, person_id, *, limit) -> dict | None` (its `item` carries `person`, `stage`, `doc_url`);
  - `outreach.set_doc`, `outreach.doc_id`, `outreach.NotInPipeline` (Task 2);
  - `outreach_doc.render_messages`, `merge`, `title`, `them`, `Sections`, `DOC_MESSAGES`, `FOLDER_ENTRY` (Task 5);
  - a docs client with `get_doc`, `create_doc`, `replace_doc`, `folder_id` and the `VersionConflict` attribute (Task 4).
- Produces:
  - `services.outreach_doc_write.write(conn, person_id: int, sections: Sections, *, today: date, docs=docs_api) -> str` (`"created <url>"` | `"updated <url>"`);
  - `DocConflict(RuntimeError)`;
  - `DocNotRecorded(RuntimeError)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_outreach_doc_write.py`)

```python
from datetime import UTC, date, datetime

import pytest

from clients import docs_api
from services import outreach
from services import outreach_doc_write as writer
from services.outreach_doc import DOC_MESSAGES, Sections

TODAY = date(2026, 10, 15)
TS = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)
PERSON = {"id": 7, "email": "alex@example.com", "display_name": "Alex Example"}
URL = "https://docs.google.com/document/d/doc7/edit"
EXISTING = "# Outreach — Alex Example\n\n## Ben's notes\n\nkeep me\n"
SECTIONS = Sections(
    who="Founder at a clinic-software startup.",
    history="Worked together in 2019.",
    next_step="brief",
    talking_points="- ask how the launch went",
    log=("- 2026-10-14 · prepped brief (iMessage)",),
)


class Conn:
    def __init__(self):
        self.log = []

    def commit(self):
        self.log.append("commit")


class FakeDocs:
    VersionConflict = docs_api.VersionConflict

    def __init__(self, content="", version="", on_get=(), conflicts=0):
        self.content, self.version = content, version
        self.on_get = list(on_get)  # one entry per GET: None, or a callable that edits the doc
        self.conflicts = conflicts
        self.gets, self.created, self.replaced, self.folders = 0, [], [], []

    def folder_id(self, entry_id):
        self.folders.append(entry_id)
        return "folder-1"

    def create_doc(self, title, markdown, parent):
        self.created.append({"title": title, "markdown": markdown, "parent": parent})
        return {"web_url": "https://docs.google.com/document/d/new1/edit"}

    def get_doc(self, doc_id):
        self.gets += 1
        edit = self.on_get.pop(0) if self.on_get else None
        if edit:
            edit(self)
        return {"content": self.content, "version": self.version}

    def replace_doc(self, doc_id, markdown, if_version):
        if self.conflicts:
            self.conflicts -= 1
            raise self.VersionConflict(doc_id)
        self.replaced.append((doc_id, markdown, if_version))
        self.content = markdown
        return {}


@pytest.fixture
def env(monkeypatch):
    state = {"doc_url": None, "recorded": [], "set_doc_fails": False}

    def snapshot(conn, pid, limit):
        assert limit == DOC_MESSAGES
        if pid != 7:
            return None
        return {
            "item": {"person": PERSON, "stage": "to_contact", "doc_url": state["doc_url"]},
            "messages": {
                "imessage": [{"sent_at": TS, "from_me": False, "text": "hi", "has_attachments": False}],
                "whatsapp": [],
                "linkedin": [],
            },
        }

    def set_doc(conn, pid, url):
        if state["set_doc_fails"]:
            raise RuntimeError("db unavailable")
        state["recorded"].append((pid, url))
        return {}

    monkeypatch.setattr(writer.outreach_session, "snapshot", snapshot)
    monkeypatch.setattr(writer.outreach, "set_doc", set_doc)
    return state


def test_first_write_creates_the_doc_in_the_catalog_folder_and_records_it(env):
    docs, conn = FakeDocs(), Conn()
    result = writer.write(conn, 7, SECTIONS, today=TODAY, docs=docs)
    assert result == "created https://docs.google.com/document/d/new1/edit"
    assert docs.folders == ["consulting-outreach"]
    [created] = docs.created
    assert created["title"] == "Outreach — Alex Example" and created["parent"] == "folder-1"
    assert "## Talking points (next step: brief)" in created["markdown"]
    assert "- 2026-10-13 11:30 Alex: hi" in created["markdown"]
    assert env["recorded"] == [(7, "https://docs.google.com/document/d/new1/edit")]
    assert conn.log == ["commit"]


def test_update_merges_with_a_second_get_and_if_version(env):
    env["doc_url"] = URL
    docs = FakeDocs(content=EXISTING, version="v1")
    assert writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs) == f"updated {URL}"
    assert docs.gets == 2 and docs.created == []
    [(doc_id, markdown, if_version)] = docs.replaced
    assert doc_id == "doc7" and if_version == "v1"
    assert "## Ben's notes\n\nkeep me" in markdown and "people id: 7 · stage: to_contact" in markdown


def test_an_edit_between_the_two_gets_is_re_merged(env):
    env["doc_url"] = URL

    def ben_edits(d):
        d.content = d.content.replace("keep me", "keep me, and call Tuesday")

    docs = FakeDocs(content=EXISTING, on_get=[None, ben_edits])  # version stays "" throughout
    writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs)
    assert docs.gets == 4
    [(_, markdown, _)] = docs.replaced
    assert "keep me, and call Tuesday" in markdown


def test_a_409_is_retried_once_after_a_fresh_merge(env):
    env["doc_url"] = URL
    docs = FakeDocs(content=EXISTING, version="v1", conflicts=1)
    assert writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs) == f"updated {URL}"
    assert docs.gets == 4 and len(docs.replaced) == 1


def test_two_conflicts_stop_without_writing(env):
    env["doc_url"] = URL
    docs = FakeDocs(content=EXISTING, version="v1", conflicts=2)
    with pytest.raises(writer.DocConflict, match="nothing written"):
        writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs)
    assert docs.replaced == []


def test_someone_outside_the_pipeline_is_refused(env):
    with pytest.raises(outreach.NotInPipeline):
        writer.write(Conn(), 9, SECTIONS, today=TODAY, docs=FakeDocs())


def test_a_created_doc_that_cannot_be_recorded_names_its_url(env):
    env["set_doc_fails"] = True
    with pytest.raises(writer.DocNotRecorded, match="new1") as excinfo:
        writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=FakeDocs())
    assert "outreach doc 7" in str(excinfo.value)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_doc_write.py -v`
Expected: FAIL with `ImportError: cannot import name 'outreach_doc_write'`.

- [ ] **Step 3: Implement** (`services/outreach_doc_write.py`)

```python
"""Create or merge one person's contact doc (contact-docs spec §3 write protocol). The
session supplies its sections; this module reads the DB snapshot, fetches the doc,
merges (services/outreach_doc.py), checks for a concurrent edit with a second GET (live
Google Docs usually report an empty version, so if_version alone protects nothing),
then replaces it, retrying once. It writes without asking: that's spec D4's exception
to editing-doc's confirm-every-write rule, made safe by the merge. Called only by
`outreach-sessions --write-doc`."""

from datetime import date
from typing import Any

from clients import docs_api
from services import outreach, outreach_doc, outreach_session

ATTEMPTS = 2


class DocConflict(RuntimeError):
    """The doc changed during every attempt; nothing was written (spec §3 step 5)."""


class DocNotRecorded(RuntimeError):
    """The doc was created but its link could not be stored in outreach_contacts."""


def write(
    conn: Any,
    person_id: int,
    sections: outreach_doc.Sections,
    *,
    today: date,
    docs: Any = docs_api,
) -> str:
    """`created <url>` or `updated <url>`. Raises outreach.NotInPipeline, DocConflict,
    DocNotRecorded, and docs-api errors."""
    snap = outreach_session.snapshot(conn, person_id, limit=outreach_doc.DOC_MESSAGES)
    if snap is None:
        raise outreach.NotInPipeline(str(person_id))
    item = snap["item"]
    person = item["person"]
    messages = outreach_doc.render_messages(
        snap["messages"], outreach_doc.them(person), today=today
    )

    def build(existing: str | None) -> str:
        return outreach_doc.merge(
            existing, sections, person=person, stage=item["stage"], messages=messages, today=today
        )

    url = item["doc_url"]
    if not url:
        parent = docs.folder_id(outreach_doc.FOLDER_ENTRY)
        ref = docs.create_doc(outreach_doc.title(person), build(None), parent)
        try:
            outreach.set_doc(conn, person_id, ref["web_url"])
            conn.commit()
        except Exception as e:
            raise DocNotRecorded(
                f"created {ref['web_url']} but could not record it ({e}); record it with "
                f"`outreach doc {person_id} {ref['web_url']}` before writing again"
            ) from e
        return f"created {ref['web_url']}"

    doc_id = outreach.doc_id(url)
    for _ in range(ATTEMPTS):
        before = docs.get_doc(doc_id)
        merged = build(before.get("content") or "")
        check = docs.get_doc(doc_id)
        if (check.get("content"), check.get("version")) != (
            before.get("content"),
            before.get("version"),
        ):
            continue
        try:
            docs.replace_doc(doc_id, merged, before.get("version") or "")
        except docs.VersionConflict:
            continue
        return f"updated {url}"
    raise DocConflict(f"the doc changed while it was being written, twice; nothing written: {url}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_outreach_doc_write.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/outreach_doc_write.py tests/test_outreach_doc_write.py
git commit -m "outreach_doc_write: create or merge a contact doc with a conflict guard"
```

---

### Task 9: `outreach-sessions` — names, resume, the doc, `--write-doc`

**Files:**
- Modify: `scripts/outreach_sessions.py` (rewrite: module docstring, imports, `USER_ERRORS`, `_by_name`, `_person`, `_targets`, `_doc`, `_one`, `_write_doc`, `run`, `build_parser`, `main_with`)
- Test: `tests/test_outreach_sessions_cli.py` (rewrite)

**Interfaces:**
- Consumes:
  - Task 6: `claude_sessions.load_state`, `save_state`, `live_sessions`, `find_live`, `find_transcript`, `transcript_cwd`, `spawn`, `resume`, `SHORT_ID`;
  - `outreach.people_named`, `outreach.doc_id`, `outreach.InvalidDocUrl` (Task 2);
  - `docs_api.get_doc` and its errors (Task 4);
  - `outreach_doc.parse_sections`, `SectionsError` (Task 5);
  - `outreach_session.context_block(..., doc=)`, `RELOAD_PROMPT` (Task 7);
  - `outreach_doc_write.write`, `DocConflict`, `DocNotRecorded` (Task 8).
- Produces: the CLI the skill and agent use (Task 10):
  - `outreach-sessions <email|phone|id|name> [--spawn] [--idle]`;
  - `--next N`;
  - `--print-context PERSON`;
  - `--write-doc PERSON --sections FILE`.

- [ ] **Step 1: Write the failing tests** (replace `tests/test_outreach_sessions_cli.py` entirely)

```python
import io
import json
from datetime import date

import pytest

from scripts import outreach_sessions as cli

JONAH = {"id": 388, "email": "jonah@example.com", "display_name": "Jonah Example"}
ANA = {"id": 41, "email": "ana@example.com", "display_name": "Ana Example"}
TODAY = date(2026, 10, 13)
SID = "ab12cd34-0000-4000-8000-000000000001"
DOC = "https://docs.google.com/document/d/doc388/edit"
SECTIONS = """## Who they are

Director at a health-tech startup.

## History

Worked together in 2019.

## Log

- 2026-10-14 · prepped brief (iMessage)

## Talking points (next step: brief)

- ask about the new role
"""


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def run(monkeypatch, tmp_path):
    state_path = tmp_path / "sessions.json"
    projects = tmp_path / "projects"
    projects.mkdir()
    people = {"jonah@example.com": JONAH, "388": JONAH, "ana@example.com": ANA, "41": ANA}
    spawned, resumed, named = [], [], []

    def resolve(conn, ident):
        if ident not in people:
            raise cli.person_lookup.NotFound(ident)
        return people[ident]

    def snapshot(conn, pid, limit):
        if pid == 41:
            return None
        return {"pid": pid, "limit": limit, "item": {"doc_url": _run.doc_urls.get(pid)}}

    def spawn(name, context, *, cwd, prompt=None):
        spawned.append({"name": name, "context": context, "prompt": prompt, "cwd": cwd})
        short = f"id{len(spawned):06d}"
        _run.live[f"{short}-0000-4000-8000-000000000000"] = str(cwd)
        return short

    def resume(session_id, *, cwd, prompt=None):
        resumed.append({"session_id": session_id, "cwd": cwd, "prompt": prompt})
        return session_id[:8]

    def people_named(conn, text):
        named.append(text)
        return _run.named_result

    monkeypatch.setattr(cli.person_lookup, "resolve", resolve)
    monkeypatch.setattr(cli.outreach_session, "snapshot", snapshot)
    monkeypatch.setattr(
        cli.outreach_session,
        "context_block",
        lambda snap, today, doc=None: f"CONTEXT {snap['pid']}" + (f" + {doc}" if doc else ""),
    )
    monkeypatch.setattr(cli.claude_sessions, "spawn", spawn)
    monkeypatch.setattr(cli.claude_sessions, "resume", resume)
    monkeypatch.setattr(cli.claude_sessions, "live_sessions", lambda cwd: dict(_run.live))
    monkeypatch.setattr(cli.claude_sessions, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cli.outreach, "people_named", people_named)
    monkeypatch.setattr(cli.docs_api, "get_doc", lambda doc_id: {"content": f"DOC {doc_id}"})

    def _run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main_with(
            list(argv), lambda: Conn(), out=out, err=err, today=TODAY, state_path=state_path
        )
        return code, out.getvalue(), err.getvalue()

    _run.spawned, _run.resumed, _run.named = spawned, resumed, named
    _run.live, _run.doc_urls, _run.named_result = {}, {}, []
    _run.state_path, _run.projects = state_path, projects
    return _run


def _record(run, record):
    run.state_path.write_text(json.dumps({"388": record}))


def _transcript(run, session_id, cwd):
    project = run.projects / "-Users-me-src-repo--claude-worktrees-w"
    project.mkdir(exist_ok=True)
    (project / f"{session_id}.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")


def test_dry_run_spawns_nothing(run):
    code, out, err = run("jonah@example.com")
    assert code == 0 and "388" in out and "→ spawn" in out and run.spawned == []
    assert "dry run" in err and not run.state_path.exists()


def test_spawn_preloads_context_starts_the_prep_and_records_the_session(run):
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "spawned id000001" in out
    [s] = run.spawned
    assert s["name"] == "outreach: Jonah Example" and s["context"] == "CONTEXT 388"
    assert s["prompt"] == cli.outreach_session.PREP_PROMPT and s["cwd"] == cli.REPO
    assert json.loads(run.state_path.read_text())["388"] == {
        "session_id": "id000001-0000-4000-8000-000000000000",
        "cwd": str(cli.REPO),
        "name": "outreach: Jonah Example",
    }


def test_idle_spawns_without_a_prompt(run):
    run("388", "--spawn", "--idle")
    assert run.spawned[0]["prompt"] is None


def test_a_live_session_is_not_duplicated(run):
    run("388", "--spawn")
    code, out, _ = run("388", "--spawn")
    assert "already open id000001" in out and len(run.spawned) == 1 and run.resumed == []


def test_a_closed_session_is_resumed_from_its_transcript_cwd(run, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _record(run, {"session_id": SID, "cwd": str(cli.REPO), "name": "outreach: Jonah Example"})
    _transcript(run, SID, worktree)
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "resumed ab12cd34" in out and run.spawned == []
    assert run.resumed == [
        {"session_id": SID, "cwd": worktree, "prompt": cli.outreach_session.RELOAD_PROMPT}
    ]
    assert json.loads(run.state_path.read_text())["388"]["cwd"] == str(worktree)


def test_dry_run_says_resume(run, tmp_path):
    _record(run, {"session_id": SID})
    _transcript(run, SID, tmp_path)
    code, out, _ = run("388")
    assert "→ resume" in out and run.resumed == []


def test_idle_resume_has_no_prompt(run, tmp_path):
    _record(run, {"session_id": SID})
    _transcript(run, SID, tmp_path)
    run("388", "--spawn", "--idle")
    assert run.resumed[0]["prompt"] is None


def test_a_legacy_short_id_is_resumed_and_upgraded(run, tmp_path):
    run.state_path.write_text(json.dumps({"388": "ab12cd34"}))
    _transcript(run, SID, tmp_path)
    code, out, _ = run("388", "--spawn")
    assert "resumed ab12cd34" in out
    assert json.loads(run.state_path.read_text())["388"] == {
        "session_id": SID,
        "cwd": str(tmp_path),
        "name": "outreach: Jonah Example",
    }


def test_a_live_legacy_entry_is_already_open(run):
    run.state_path.write_text(json.dumps({"388": "ab12cd34"}))
    run.live = {SID: "/somewhere"}
    code, out, _ = run("388", "--spawn")
    assert "already open ab12cd34" in out and run.spawned == [] and run.resumed == []


def test_a_closed_session_without_a_transcript_gets_a_fresh_spawn(run):
    _record(run, {"session_id": SID})
    code, out, _ = run("388", "--spawn")
    assert "spawned id000001" in out and run.resumed == []


def test_a_transcript_whose_directory_is_gone_gets_a_fresh_spawn(run, tmp_path):
    _record(run, {"session_id": SID})
    _transcript(run, SID, tmp_path / "removed-worktree")
    code, out, _ = run("388", "--spawn")
    assert "spawned id000001" in out and run.resumed == []


def test_a_fresh_spawn_with_a_doc_carries_it_and_reloads(run):
    run.doc_urls[388] = DOC
    run("388", "--spawn")
    [s] = run.spawned
    assert s["context"] == "CONTEXT 388 + DOC doc388"
    assert s["prompt"] == cli.outreach_session.RELOAD_PROMPT


def test_an_unavailable_doc_does_not_block_the_spawn(run, monkeypatch):
    run.doc_urls[388] = DOC

    def unavailable(doc_id):
        raise cli.docs_api.DocsApiUnavailable("DOCS_API_URL unset")

    monkeypatch.setattr(cli.docs_api, "get_doc", unavailable)
    code, out, _ = run("388", "--spawn")
    assert "spawned id000001 (doc unavailable: DocsApiUnavailable)" in out
    assert run.spawned[0]["context"] == "CONTEXT 388"
    assert run.spawned[0]["prompt"] == cli.outreach_session.RELOAD_PROMPT


def test_a_name_matches_a_recorded_session_first(run):
    _record(run, {"session_id": SID, "name": "outreach: Jonah Example"})
    run.live = {SID: "/x"}
    code, out, _ = run("JONAH")
    assert code == 0 and out.startswith("388\t") and "already open ab12cd34" in out
    assert run.named == []


def test_a_name_falls_back_to_pipeline_people(run):
    run.named_result = [{"id": 388, "display_name": "Jonah Example"}]
    code, out, _ = run("jonah ex")
    assert code == 0 and out.startswith("388\t") and run.named == ["jonah ex"]


def test_an_ambiguous_name_stops_with_the_ids(run):
    run.named_result = [{"id": 388, "display_name": "Jo A"}, {"id": 41, "display_name": "Jo B"}]
    code, _, err = run("jo", "--spawn")
    assert code == 1 and "[388, 41]" in err and run.spawned == []


def test_an_unknown_name_is_not_found(run):
    code, _, err = run("zed")
    assert code == 1 and "no person matches zed" in err


def test_someone_outside_the_pipeline_is_skipped(run):
    code, out, _ = run("ana@example.com", "--spawn")
    assert code == 0 and "not in the pipeline" in out and run.spawned == []


def test_unknown_person_is_a_user_error(run):
    code, _, err = run("nobody@example.com", "--spawn")
    assert code == 1 and "no person matches nobody@example.com" in err and run.spawned == []


def test_next_takes_the_queue_in_rank_order(run, monkeypatch):
    seen = {}

    def queue(conn, *, stage="to_contact", limit=50):
        seen.update(stage=stage, limit=limit)
        return [{"person": JONAH}]

    monkeypatch.setattr(cli.outreach, "queue", queue)
    code, out, _ = run("--next", "3", "--spawn")
    assert code == 0 and seen == {"stage": "to_contact", "limit": 3} and len(run.spawned) == 1


def test_people_and_next_are_exclusive(run):
    code, _, err = run("388", "--next", "2")
    assert code == 1 and "--next" in err


def test_the_cap_is_enforced(run):
    code, _, err = run("--next", "11")
    assert code == 1 and str(cli.MAX_PEOPLE) in err


def test_print_context(run):
    code, out, _ = run("--print-context", "388")
    assert code == 0 and out.strip() == "CONTEXT 388" and run.spawned == []


def test_print_context_includes_the_doc(run):
    run.doc_urls[388] = DOC
    code, out, _ = run("--print-context", "388")
    assert code == 0 and out.strip() == "CONTEXT 388 + DOC doc388"


def test_spawn_failure_is_reported(run, monkeypatch):
    monkeypatch.setattr(cli.claude_sessions, "spawn", lambda *a, **k: None)
    code, out, _ = run("388", "--spawn")
    assert code == 0 and "spawn failed" in out


def test_write_doc_parses_the_sections_and_writes(run, monkeypatch, tmp_path):
    path = tmp_path / "sections.md"
    path.write_text(SECTIONS)
    seen = {}

    def write(conn, pid, sections, *, today):
        seen.update(pid=pid, step=sections.next_step, today=today)
        return "created https://docs.google.com/document/d/new1/edit"

    monkeypatch.setattr(cli.outreach_doc_write, "write", write)
    code, out, _ = run("--write-doc", "388", "--sections", str(path))
    assert code == 0 and seen == {"pid": 388, "step": "brief", "today": TODAY}
    assert "388\toutreach: Jonah Example\t→ created" in out


def test_write_doc_refuses_bad_sections(run, tmp_path):
    path = tmp_path / "sections.md"
    path.write_text("## History\n\nonly this\n")
    code, _, err = run("--write-doc", "388", "--sections", str(path))
    assert code == 1 and "missing or empty" in err


def test_write_doc_needs_a_sections_file(run, tmp_path):
    code, _, err = run("--write-doc", "388")
    assert code == 1 and "--sections" in err
    code, _, err = run("--write-doc", "388", "--sections", str(tmp_path / "missing.md"))
    assert code == 1 and "cannot read" in err


def test_write_doc_reports_a_conflict(run, monkeypatch, tmp_path):
    path = tmp_path / "sections.md"
    path.write_text(SECTIONS)

    def write(conn, pid, sections, *, today):
        raise cli.outreach_doc_write.DocConflict("the doc changed while it was being written")

    monkeypatch.setattr(cli.outreach_doc_write, "write", write)
    code, _, err = run("--write-doc", "388", "--sections", str(path))
    assert code == 1 and "changed while it was being written" in err
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_outreach_sessions_cli.py -v`
Expected: FAIL with `AttributeError: module 'clients.claude_sessions' has no attribute 'live_session_ids'` (removed in Task 6), plus missing `--write-doc`.

- [ ] **Step 3: Implement** (replace `scripts/outreach_sessions.py`)

```python
#!/usr/bin/env python3
"""One background Claude Code session per outreach person, and one living Google Doc per
person that outlasts the sessions (docs/superpowers/specs/2026-10-14-outreach-contact-docs-design.md).
Local only.

A fresh session boots with that person's DB snapshot in its system prompt
(services/outreach_session.py: identity, pipeline state, recent 1:1 iMessage, WhatsApp
and LinkedIn messages) plus their contact doc when one exists, and, unless --idle,
starts at once on the working-outreach-queue skill's "Prep a brief" procedure ("Reload"
when a doc exists). A person whose last session has exited gets it back instead: same
id, same conversation, resumed from the directory its transcript says it last ran in.

`scripts/link-skills.sh` puts the `outreach-sessions` wrapper on PATH; it runs
this file under the repo's venv.

    outreach-sessions 388                      # dry run: already open / resume / spawn
    outreach-sessions alex --spawn             # a name: session names, then pipeline names
    outreach-sessions --next 5 --spawn         # the top of the to_contact queue
    outreach-sessions --print-context 388      # the block a session boots with, doc included
    outreach-sessions --write-doc 388 --sections /path/to/sections.md

~/.claude/outreach-sessions.json keeps one record per person ({session_id, cwd, name}),
never dropped when a session exits. The doc link lives in outreach_contacts.doc_url.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

import httpx

from clients import claude_sessions, docs_api
from services import (
    identity,
    outreach,
    outreach_doc,
    outreach_doc_write,
    outreach_session,
    person_lookup,
)

STATE_PATH = Path.home() / ".claude" / "outreach-sessions.json"
DEFAULT_MESSAGES = 60
MAX_PEOPLE = 10


class CliError(Exception):
    pass


USER_ERRORS = (
    CliError,
    outreach.NotInPipeline,
    outreach.InvalidDocUrl,
    outreach_doc.SectionsError,
    outreach_doc_write.DocConflict,
    outreach_doc_write.DocNotRecorded,
    docs_api.DocsApiUnavailable,
    docs_api.FolderNotFound,
    httpx.HTTPError,
)


def _resolve(conn: Any, ident: str) -> dict:
    try:
        return person_lookup.resolve(conn, ident)
    except person_lookup.Ambiguous as e:
        raise CliError(f"{ident} matches several people ({e.candidates}); use an id") from e
    except person_lookup.NotFound as e:
        raise CliError(f"no person matches {ident}") from e


def _by_name(conn: Any, name: str, state: dict[str, dict]) -> dict:
    """Recorded session names first, then pipeline display names (contact-docs spec §4)."""
    needle = name.strip().lower()
    ids = sorted(
        int(key)
        for key, record in state.items()
        if needle and needle in (record.get("name") or "").lower().removeprefix("outreach: ")
    )
    where = "sessions"
    if not ids:
        ids, where = [int(r["id"]) for r in outreach.people_named(conn, name)], "pipeline people"
    if len(ids) > 1:
        raise CliError(f"{name} matches several {where} ({ids}); use an id")
    if not ids:
        raise CliError(f"no person matches {name}")
    return _resolve(conn, str(ids[0]))


def _person(conn: Any, ident: str, state: dict[str, dict]) -> dict:
    """An email, E.164 phone or people id resolves as people-api does; anything else is a name."""
    if identity.classify(ident) is None:
        return _by_name(conn, ident, state)
    return _resolve(conn, ident)


def _targets(conn: Any, args: argparse.Namespace, state: dict[str, dict]) -> list[dict]:
    if args.people and args.next:
        raise CliError("give people or --next N, not both")
    if args.next:
        if args.next > MAX_PEOPLE:
            raise CliError(f"--next is capped at {MAX_PEOPLE} sessions per run")
        return [
            item["person"] for item in outreach.queue(conn, stage="to_contact", limit=args.next)
        ]
    if not args.people:
        raise CliError("name at least one person, or use --next N")
    if len(args.people) > MAX_PEOPLE:
        raise CliError(f"at most {MAX_PEOPLE} people per run")
    return [_person(conn, ident, state) for ident in args.people]


def _doc(url: str | None) -> tuple[str | None, str]:
    """The contact doc's markdown, and a note for the row when it couldn't be fetched (the
    session starts anyway; RELOAD_PROMPT has it fetch the doc itself)."""
    if not url:
        return None, ""
    try:
        return docs_api.get_doc(outreach.doc_id(url)).get("content") or "", ""
    except Exception as e:  # noqa: BLE001 — never block a session on the doc
        return None, f" (doc unavailable: {type(e).__name__})"


def _one(
    conn: Any,
    args: argparse.Namespace,
    person: dict,
    name: str,
    record: dict | None,
    live: dict[str, str],
    *,
    today: date,
) -> tuple[str, dict | None]:
    """One person's row and their new session record (None: keep the old one)."""
    running = claude_sessions.find_live(record["session_id"], live) if record else None
    if running:
        cwd = live[running] or (record or {}).get("cwd") or str(REPO)
        short = running[: claude_sessions.SHORT_ID]
        return f"already open {short}", {"session_id": running, "cwd": cwd, "name": name}

    transcript = claude_sessions.find_transcript(record["session_id"]) if record else None
    cwd = claude_sessions.transcript_cwd(transcript) if transcript else None
    if transcript and cwd and Path(cwd).is_dir():
        if not args.spawn:
            return "resume", None
        prompt = None if args.idle else outreach_session.RELOAD_PROMPT
        short = claude_sessions.resume(transcript.stem, cwd=Path(cwd), prompt=prompt)
        if not short:
            return "resume failed", None
        return f"resumed {short}", {"session_id": transcript.stem, "cwd": cwd, "name": name}

    snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
    if snap is None:
        return "not in the pipeline (run `outreach decide` first)", None
    if not args.spawn:
        return "spawn", None
    url = snap["item"]["doc_url"]
    doc, note = _doc(url)
    if args.idle:
        prompt = None
    else:
        prompt = outreach_session.RELOAD_PROMPT if url else outreach_session.PREP_PROMPT
    short = claude_sessions.spawn(
        name, outreach_session.context_block(snap, today=today, doc=doc), cwd=REPO, prompt=prompt
    )
    if not short:
        return "spawn failed", None
    full = claude_sessions.find_live(short, claude_sessions.live_sessions(cwd=REPO)) or short
    return f"spawned {short}{note}", {"session_id": full, "cwd": str(REPO), "name": name}


def _write_doc(
    conn: Any, args: argparse.Namespace, out: TextIO, state: dict[str, dict], *, today: date
) -> None:
    if not args.sections:
        raise CliError("--write-doc needs --sections FILE (the session's new sections)")
    person = _person(conn, args.write_doc, state)
    try:
        text = Path(args.sections).read_text(encoding="utf-8")
    except OSError as e:
        raise CliError(f"cannot read {args.sections}: {e.strerror}") from e
    sections = outreach_doc.parse_sections(text)
    result = outreach_doc_write.write(conn, person["id"], sections, today=today)
    print(f"{person['id']}\t{outreach_session.session_name(person)}\t→ {result}", file=out)


def run(
    conn: Any,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    *,
    today: date,
    state_path: Path,
) -> None:
    state = claude_sessions.load_state(state_path)
    if args.write_doc:
        _write_doc(conn, args, out, state, today=today)
        return
    if args.print_context:
        person = _person(conn, args.print_context, state)
        snap = outreach_session.snapshot(conn, person["id"], limit=args.messages)
        if snap is None:
            raise CliError(f"person {person['id']} is not in the pipeline")
        doc, note = _doc(snap["item"]["doc_url"])
        if note:
            print(f"warning:{note}", file=err)
        print(outreach_session.context_block(snap, today=today, doc=doc), file=out)
        return

    people = _targets(conn, args, state)
    live = claude_sessions.live_sessions(cwd=REPO)
    for person in people:
        key = str(person["id"])
        name = outreach_session.session_name(person)
        outcome, record = _one(conn, args, person, name, state.get(key), live, today=today)
        if record is not None:
            state[key] = record
        print(f"{person['id']}\t{name}\t→ {outcome}", file=out)

    if args.spawn:
        claude_sessions.save_state(state_path, state)
        print("\nclaude agents          # the view", file=err)
        print("claude attach <id>     # work one (needs a real terminal)", file=err)
    else:
        print("\ndry run — add --spawn to launch these", file=err)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outreach-sessions",
        description="Give each outreach person their own background Claude session and doc.",
    )
    p.add_argument("people", nargs="*", help="email, E.164 phone, people id or name")
    p.add_argument("--next", type=int, help="the top N of the to_contact queue")
    p.add_argument("--spawn", action="store_true", help="actually launch or resume sessions")
    p.add_argument("--idle", action="store_true", help="launch without starting the prep/reload")
    p.add_argument("--messages", type=int, default=DEFAULT_MESSAGES, help="per channel")
    p.add_argument("--print-context", metavar="PERSON", help="print one person's context block")
    p.add_argument("--write-doc", metavar="PERSON", help="create or merge the person's doc")
    p.add_argument("--sections", metavar="FILE", help="with --write-doc: the session's sections")
    return p


def main_with(
    argv: list[str],
    get_conn: Callable[[], Any],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    today: date | None = None,
    state_path: Path = STATE_PATH,
) -> int:
    args = build_parser().parse_args(argv)
    today = today or datetime.now(UTC).astimezone(outreach.TZ).date()
    with get_conn() as conn:
        try:
            run(conn, args, out, err, today=today, state_path=state_path)
        except USER_ERRORS as e:
            print(f"error: {e}", file=err)
            return 1
    return 0


def main() -> None:
    from clients.db import get_conn

    sys.exit(main_with(sys.argv[1:], get_conn))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest tests/ -q` (and the full checks from Global Constraints)
Expected: PASS. If mypy flags `record["session_id"]` on `dict | None`, keep the `if record` guards exactly as written; don't add `# type: ignore`.

- [ ] **Step 5: Commit**

```bash
git add scripts/outreach_sessions.py tests/test_outreach_sessions_cli.py
git commit -m "outreach-sessions: resume exited sessions, names, the contact doc, --write-doc"
```

---

### Task 10: Skill, agent and docs

**Files:**
- Modify: `.claude/skills/working-outreach-queue/SKILL.md` (Rules, Start sessions, Prep a brief steps 4–5, new Reload section, Record outcomes; the description's trigger list)
- Modify: `.claude/agents/outreach-launcher.md` (description, §1, §2 row list)
- Modify: `CLAUDE.md` (stack row, code layout, source-of-truth table)
- Modify: `.claude/skills/people-architecture/SKILL.md` (Referral outreach bullets)
- Modify: `docs/superpowers/specs/2026-10-14-outreach-contact-docs-design.md` (Status line)

**Interfaces:**
- Consumes: the CLI from Task 9 and `outreach doc` from Task 3, exactly as written there.
- Produces: the procedures a session follows.

- [ ] **Step 1: The skill**

In the frontmatter `description`, add to the trigger list, after `"open sessions for my next briefs"`: `"reload the session for X", "reopen X's session", "update X's outreach doc"`.

In **Rules**, append:

```markdown
- **Contact docs are machine-maintained — an explicit exception to `editing-doc`.**
  Each pipeline person has one Google Doc, `Outreach — <name>` in My Drive
  `Consulting/Outreach`, linked from `outreach_contacts.doc_url`. Write it
  without asking first, and **only** with
  `outreach-sessions --write-doc <id> --sections <file>` — never with
  `editing-doc` or `creating-doc`. The script keeps Ben's notes and the log,
  rewrites the rest, regenerates Messages from the DB, and refuses rather than
  overwrite a concurrent edit. It deletes Google comments on every write; the doc
  tells Ben to write in `Ben's notes` instead. Never put a drafted message in it.
- **Write the sections file with the Write tool** (to your scratchpad), never a
  heredoc or `$(…)`: background sessions block shell substitution.
```

Replace the **Start sessions** paragraph with:

```markdown
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
```

In **Prep a brief**, replace step 5 with steps 5 and 6:

```markdown
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
```

Add a new section after **Prep a brief**:

```markdown
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
```

In **Record outcomes**, append:

```markdown
- After any recorded outcome — `mark`, `nudge`, `intro`, `lead`,
  `decide --skip` — update the contact doc: a Log line for what happened
  (`- <YYYY-MM-DD> · nudged by WhatsApp`) and talking points for the new next step,
  through `--write-doc` as in Prep a brief step 5.
- A doc Ben deleted in Drive: `$O doc <person> --clear`; the next `--write-doc`
  creates a fresh one.
```

- [ ] **Step 2: The agent**

In `.claude/agents/outreach-launcher.md`, the frontmatter `description` becomes:

```yaml
description: >
  Open or reload a background Claude Code session per referral-outreach person.
  A person whose last session exited gets it back (same conversation); anyone
  else gets a fresh session preloaded with what the people DB knows (identity,
  pipeline stage, recent iMessage, WhatsApp and LinkedIn messages) and their
  contact doc. Each session immediately preps the brief (or refreshes it on a
  reload): email history, recent news via Chrome, and talking points. Resolves
  which people are meant, runs outreach-sessions, and reports the attach
  commands. Does not brief anyone or change the pipeline. Use when the answer is
  "give me a session for that person", "reload X's session" or "open sessions
  for my next N briefs".
```

Replace **## 1. Resolve the targets** with:

```markdown
## 1. Resolve the targets

`outreach-sessions` takes an email, an E.164 phone, a people id, or a name. It
matches a name against recorded session names first, then pipeline display
names, and stops with the candidate ids when it's ambiguous.

- **"The next N" / "today's briefs"** → `--next N` (today's briefs: `--next 5`).
- **An email, phone, id or name in the dispatch** → pass it as given. If the
  script reports `matches several …`, report `AMBIGUOUS` with the ids it
  printed; if `no person matches …`, report `NOT FOUND`. Never guess.
```

In **## 2. Launch**, replace the row-format paragraph with:

```markdown
Each row is `<id>  outreach: <name>  → <outcome>`, where the outcome is
`spawned <session>`, `resumed <session>` (the person's earlier session, back
with its conversation), `already open <session>`, `not in the pipeline …`,
`spawn failed` or `resume failed`. A `(doc unavailable: …)` suffix means the
session started without its doc and fetches it itself. Report what it printed;
a row you did not see is not a session you can claim.
```

In **## 3. Report**, change `Launched:` to `Launched or resumed:`.

- [ ] **Step 3: `CLAUDE.md` and the architecture skill**

`CLAUDE.md` Stack table, **Referral outreach** row: append before the closing `|`:

```
; one machine-maintained Google Doc per pipeline person (`Outreach — <name>`, My Drive `Consulting/Outreach`, link in `outreach_contacts.doc_url`) written only by `outreach-sessions --write-doc` via docs-api (`clients/docs_api.py`, `DOCS_API_URL`); `outreach-sessions` resumes a person's exited session (`claude --bg --resume`) or spawns a fresh one from the doc
```

Code layout, `clients/`:
- replace the `claude_sessions.py` line with:
  ```
    claude_sessions.py        `claude --bg` spawn / resume, running sessions, transcript lookup, per-person records — CLI-only (outreach-sessions)
  ```
- add:
  ```
    docs_api.py               docs-api: get / create / replace a doc, filing-catalog folder lookup — CLI-only (outreach-sessions)
  ```

Code layout, `services/`, after `outreach_session.py`'s lines, add:

```
  outreach_doc.py           pure: contact-doc title, Messages render, a session's sections file, merge
                             (keeps Ben's notes and the log)
  outreach_doc_write.py     contact-doc write protocol: create, or GET → merge → GET → PUT if_version, one retry
```

Code layout, `scripts/`:
- replace the `outreach_sessions.py` entry with:
  ```
    outreach_sessions.py      one background Claude session per outreach person: resume an exited one, or spawn
                               preloaded with the snapshot and contact doc; --write-doc creates or merges the doc
                               (`outreach-sessions` wrapper runs it under the venv) — --next N, --spawn, --idle
  ```
- in the `outreach.py` CLI line, add `doc` after `lead`.

Source-of-truth table, after the `outreach_*` row, add:

```
| `outreach_contacts.doc_url` and the contact doc | DB holds the link (`services/outreach.py::set_doc`); the doc lives in Drive | `outreach-sessions --write-doc` creates or merges it: Ben's notes and the log are kept, Messages is regenerated from the DB. Never read back into the DB. The doc is the one place message text leaves the machine — Ben's private My Drive, indexed by docs-api (contact-docs spec D6). |
```

`.claude/skills/people-architecture/SKILL.md`, after the **`outreach today`** bullet:

```markdown
- **Contact docs (contact-docs spec).** One Google Doc per pipeline person
  (`Outreach — <name>`, My Drive `Consulting/Outreach`), linked from
  `outreach_contacts.doc_url`. `outreach-sessions --write-doc` is the only writer:
  `services/outreach_doc.py` merges (Ben's notes and the log survive, Messages is
  regenerated), `services/outreach_doc_write.py` guards the PUT with a second GET.
  `outreach-sessions` resumes a person's exited session from the cwd its transcript
  records, or spawns a fresh one from the doc.
```

In the spec, change the first line of **Status** to `**Status:** implemented on branch \`outreach-contact-docs\`; design written 2026-10-14 after Ben made these decisions in`.

- [ ] **Step 4: Check the skill against the CLI**

```bash
.venv/bin/python scripts/outreach_sessions.py --help
.venv/bin/python scripts/outreach.py doc --help
```

Every flag the skill and agent name (`--write-doc`, `--sections`, `--print-context`, `--spawn`, `--idle`, `--next`, `doc … --clear`) must appear exactly. Fix the docs, not the CLI.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/working-outreach-queue/SKILL.md .claude/agents/outreach-launcher.md CLAUDE.md .claude/skills/people-architecture/SKILL.md docs/superpowers/specs/2026-10-14-outreach-contact-docs-design.md
git commit -m "docs: contact docs and reload in the outreach skill, launcher agent and CLAUDE.md"
```

---

### Task 11: Docs repo — the `Consulting/Outreach` folder (separate PR in `~/src/docs`)

Independent of Tasks 1–10. It can run in parallel, but must merge and be applied (Task 13) before anyone runs `--write-doc` for real.

**Files (in `~/src/docs`):**
- Modify: `scripts/filing.py` (`resolve_target`'s `create_mydrive_folder` branch; `bootstrap --only`; new `mydrive_folder_names`, `select_seeds`)
- Modify: `filing/manifest.yaml`
- Create: `filing/consulting-outreach.md`
- Test: `tests/unit/test_filing.py` (new; `scripts/filing.py` has no tests today)

**Interfaces:**
- Produces:
  - a catalog entry `consulting-outreach` (`GET /folders`), which `clients/docs_api.py::folder_id("consulting-outreach")` reads;
  - `scripts/filing.py bootstrap [--dry-run] [--only SEED_FILE]...`.

- [ ] **Step 1: Branch in a worktree off `origin/main`**

The docs repo's `main` checkout has uncommitted edits from other work (`docs/drive-spine.md`, `filing/_FILING.md`, three seeds). Don't touch them, and don't branch from them:

```bash
git -C ~/src/docs fetch -q origin
git -C ~/src/docs worktree add ~/src/docs-outreach-folder -b consulting-outreach-folder origin/main
cd ~/src/docs-outreach-folder
python3.13 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_filing.py`)

```python
"""scripts/filing.py: nested create_mydrive_folder paths and bootstrap --only
(people repo contact-docs spec §5)."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "filing_script", Path(__file__).resolve().parents[2] / "scripts" / "filing.py"
)
assert _SPEC and _SPEC.loader
filing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(filing)

NESTED = {"kind": "create_mydrive_folder", "path": ["Consulting", "Outreach"]}


class FakeDrive:
    def __init__(self, folders=None):
        self.folders = dict(folders or {})  # (parent id, name) -> id
        self.created = []

    def find_files_by_name(self, name, drive_id=None):
        return [
            {"id": fid, "parents": [parent], "mimeType": filing.FOLDER_MIME}
            for (parent, n), fid in self.folders.items()
            if n == name
        ]

    def ensure_folder(self, parent_id, name):
        if (parent_id, name) not in self.folders:
            self.folders[(parent_id, name)] = f"new-{name}"
            self.created.append((parent_id, name))
        return self.folders[(parent_id, name)]


def test_nested_path_creates_each_missing_segment():
    drive = FakeDrive()
    assert filing.resolve_target(drive, NESTED, {}, "root") == {
        "folder_id": "new-Outreach",
        "drive_id": None,
    }
    assert drive.created == [("root", "Consulting"), ("new-Consulting", "Outreach")]


def test_nested_path_reuses_an_existing_parent():
    drive = FakeDrive({("root", "Consulting"): "c1"})
    assert filing.resolve_target(drive, NESTED, {}, "root")["folder_id"] == "new-Outreach"
    assert drive.created == [("c1", "Outreach")]


def test_dry_run_names_the_missing_tail_and_creates_nothing():
    drive = FakeDrive()
    got = filing.resolve_target(drive, NESTED, {}, "root", dry_run=True)
    assert got == {"folder_id": None, "drive_id": None, "would_create": "Consulting/Outreach"}
    partial = FakeDrive({("root", "Consulting"): "c1"})
    assert filing.resolve_target(partial, NESTED, {}, "root", dry_run=True)["would_create"] == "Outreach"
    assert drive.created == [] and partial.created == []


def test_dry_run_resolves_an_existing_nested_folder():
    drive = FakeDrive({("root", "Consulting"): "c1", ("c1", "Outreach"): "o1"})
    assert filing.resolve_target(drive, NESTED, {}, "root", dry_run=True) == {
        "folder_id": "o1",
        "drive_id": None,
    }


def test_the_single_name_form_is_unchanged():
    target = {"kind": "create_mydrive_folder", "name": "_Inbox"}
    assert filing.mydrive_folder_names(target) == ["_Inbox"]
    got = filing.resolve_target(FakeDrive(), target, {}, "root", dry_run=True)
    assert got["would_create"] == "_Inbox"


def test_select_seeds():
    seeds = [{"file": "filing/inbox.md"}, {"file": "filing/consulting-outreach.md"}]
    assert filing.select_seeds(seeds, []) == seeds
    assert filing.select_seeds(seeds, ["filing/consulting-outreach.md"]) == [seeds[1]]
    with pytest.raises(SystemExit, match="filing/nope.md"):
        filing.select_seeds(seeds, ["filing/nope.md"])


def test_the_manifest_lists_the_outreach_folder():
    import yaml

    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "filing" / "manifest.yaml").read_text()
    )
    [seed] = [s for s in manifest["seeds"] if s["file"] == "filing/consulting-outreach.md"]
    assert seed["target"] == NESTED
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_filing.py -v`
Expected: FAIL. `resolve_target` raises `KeyError: 'name'` on a `path` target, and `mydrive_folder_names` / `select_seeds` don't exist.

- [ ] **Step 4: Implement**

In `scripts/filing.py`, add before `resolve_target`:

```python
def mydrive_folder_names(target: dict) -> list[str]:
    """`create_mydrive_folder` takes `name` (one folder under My Drive) or `path` (nested,
    each segment found or created in turn)."""
    return list(target["path"]) if "path" in target else [target["name"]]


def select_seeds(seeds: list[dict], only: list[str]) -> list[dict]:
    """`bootstrap --only`: apply just these seed files. Drive is canonical after the first
    bootstrap, so re-pushing every seed would revert Drive-side edits."""
    if not only:
        return seeds
    unknown = sorted(set(only) - {s["file"] for s in seeds})
    if unknown:
        raise SystemExit(f"not in filing/manifest.yaml: {unknown}")
    return [s for s in seeds if s["file"] in only]
```

Replace the `create_mydrive_folder` branch of `resolve_target` with:

```python
    if kind == "create_mydrive_folder":
        names = mydrive_folder_names(target)
        current = mydrive_root
        for i, name in enumerate(names):
            if not dry_run:
                current = client.ensure_folder(current, name)
                continue
            found = [
                h
                for h in _children_named(client, name, current, None)
                if h.get("mimeType") == FOLDER_MIME
            ]
            if len(found) > 1:
                raise SystemExit(f"folder {name!r}: expected at most 1 match, found {len(found)}")
            if not found:
                return {"folder_id": None, "drive_id": None, "would_create": "/".join(names[i:])}
            current = found[0]["id"]
        return {"folder_id": current, "drive_id": None}
```

Update the docstring of `resolve_target`: `'would_create'` names the missing part of the path, e.g. `Consulting/Outreach`. Then, in `main()`:

```python
    p_boot.add_argument(
        "--only", action="append", default=[], metavar="SEED_FILE",
        help="apply only this seed (repeatable), e.g. filing/consulting-outreach.md",
    )
```

…and in the bootstrap loop, iterate `select_seeds(manifest["seeds"], args.only)` instead of `manifest["seeds"]`. Update the module docstring's subcommand line to `bootstrap [--dry-run] [--only SEED_FILE]...`.

Append to `filing/manifest.yaml`:

```yaml
  - file: filing/consulting-outreach.md
    target: {kind: create_mydrive_folder, path: ["Consulting", "Outreach"]}
```

Create `filing/consulting-outreach.md`:

```markdown
---
id: consulting-outreach
naming: "Outreach — <name>"
---

**Private.** Ben's referral-outreach contacts for his consulting business: one
living Google Doc per person — who they are, the history with Ben, a log of
every brief, nudge, reply and introduction, the talking points for the next
step, Ben's own notes, and the person's recent iMessage, WhatsApp and LinkedIn
messages.

Machine-maintained: `outreach-sessions` (people repo) creates and rewrites these
docs on every prep, reload and recorded outcome, keeping the `Ben's notes`
section and the log. File nothing else here. Don't comment on these docs: every
update deletes comments — write in `Ben's notes`.

Trigger terms: referral outreach, outreach contact, referrer, brief, nudge,
warm intro.

Examples: "Outreach — Alex Example".

Elsewhere: Ben's own job search (applications, interviews, offers) → employment.
```

- [ ] **Step 5: Run the checks**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
```

Expected: PASS (integration tests skipped without `DOCS_IT_GOOGLE_FILE_ID`).

- [ ] **Step 6: Commit and open the PR**

```bash
git add scripts/filing.py filing/manifest.yaml filing/consulting-outreach.md tests/unit/test_filing.py
git commit -m "filing: Consulting/Outreach folder; nested create_mydrive_folder paths; bootstrap --only"
```

Open the PR with the `/pr-open` skill from `~/src/docs-outreach-folder` (base `main`). The description states:
- the people repo's contact-docs spec §5 and its PR link;
- that applying it is `scripts/filing.py bootstrap --dry-run --only filing/consulting-outreach.md`, then the same without `--dry-run`;
- **never a bare `bootstrap`**: Drive's `_FILING.md` and some `_FOLDER.md` files are ahead of the committed seeds.

---

### Task 12: Whole-branch verification and the PR

**Files:** none new.

- [ ] **Step 1: Full checks with the real-Postgres tests**

```bash
docker run --rm -d --name people-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py && .venv/bin/pytest tests/ -q
docker stop people-test-pg
```

Expected: all pass, with `tests/test_outreach_db.py` and `tests/test_schema.py` **not** skipped (check the summary's skip count).

- [ ] **Step 2: A read-only smoke test against production**

Use `.env` from Global Constraints, and write nothing: no `--spawn`, no `--write-doc`.

```bash
.venv/bin/python scripts/outreach_sessions.py --next 3
```

Expected: this errors with `column c.doc_url does not exist` until Task 13 step 2 has run. That's fine, and it confirms the migration is required. Don't run the migration from here. Say in the PR that the smoke test waits for it.

- [ ] **Step 3: Open the PR**

Use the `/pr-open` skill on branch `outreach-contact-docs` (base `main`). The description must state:
- **The docs-repo PR (Task 11) merges and is applied first**, and **`scripts/migrate_db.py` runs against production before this merges.** Every queue read now selects `outreach_contacts.doc_url`.
- The rollout steps of Task 13.
- Decisions 1–16 above, briefly.
- Whether the real-Postgres tests ran locally (Docker) or only in CI.
- That merging redeploys `people-process`, `people-sync` and `people-api` (paths under `repo/`, `services/`, `clients/`). Only the `doc_url` column and `queue_item` change reach them.

---

### Task 13: Rollout (Ben runs these; production-touching)

Not for an agent to run unattended. Listed so the PR carries them.

1. **Create the folder** (docs repo, after the Task 11 PR merges):
   ```bash
   git -C ~/src/docs worktree remove ~/src/docs-outreach-folder
   cd ~/src/docs && git switch main && git pull --ff-only
   .venv/bin/python scripts/filing.py bootstrap --dry-run --only filing/consulting-outreach.md
   .venv/bin/python scripts/filing.py bootstrap --only filing/consulting-outreach.md
   ```
   - `git pull` here won't touch the uncommitted local edits unless they conflict with the merge.
   - The dry run must print `would create folder 'Consulting/Outreach'` and nothing for other seeds.
   - Then confirm the catalog entry:
     ```bash
     curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$(gcloud run services describe docs-api --region us-central1 --project bens-project-462804 --format='value(status.url)')/folders?refresh=true" | python3 -c 'import json,sys; print([f["path"] for f in json.load(sys.stdin)["folders"] if f["id"] == "consulting-outreach"])'
     ```
     Expected: `['My Drive/Consulting/Outreach']`.
2. **Migrate production before merging** (people repo, `.env` from `scripts/fetch-env.sh`):
   ```bash
   cd ~/src/people && .venv/bin/python scripts/migrate_db.py
   ```
3. **Merge the people PR**, then in `~/src/people` on `main`:
   ```bash
   git pull --ff-only && scripts/fetch-env.sh && scripts/link-skills.sh
   grep -q '^DOCS_API_URL=https' .env && echo ok
   ```
4. **Keep two worktrees** until their sessions have been reloaded once and have a doc:
   - `~/src/people/.claude/worktrees/outreach-sessions`;
   - `~/src/people/.claude/worktrees/linkedin-url-match`.

   The state file needs no backfill: its short ids are read as-is (decision 2).
5. **Check a resume.** `outreach-sessions <people id>` (dry run) for each of the five people in `~/.claude/outreach-sessions.json`. Every row should say `already open` (running) or `resume` (exited); a `spawn` row means its transcript wasn't found. Then pick one whose row says `resume`, and run:
   ```bash
   outreach-sessions <people id> --spawn      # row: resumed <id>
   claude attach <id>
   ```
   In that session, the reload should run `--write-doc` and print `created https://docs.google.com/document/d/…`. Check that `outreach queue --stage briefed` shows ✓ in the `doc` column for that person.
6. **Close sessions with `claude stop <id>`, never `claude rm`.**
