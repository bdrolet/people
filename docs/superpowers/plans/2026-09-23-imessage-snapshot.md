# iMessage Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import iMessage/SMS history from the local `~/Library/Messages/chat.db` into four new `imessage_*` tables in the `people` database, linked to people through Google Contacts phone numbers, and serve per-handle stats (never message text) from `people-api`.

**Architecture:** A local-only script (`scripts/import_imessage.py`) reads `chat.db` read-only, upserts by message GUID from a `ROWID` watermark, recomputes per-handle stats in SQL, and records an audit row. Layering follows the repo's existing rules: `clients/` does I/O, `services/` is pure logic, `repo/` takes an open connection, `api/routers/` is thin transport. It mirrors the LinkedIn snapshot (`services/linkedin_export.py`, `repo/linkedin.py`, `api/routers/linkedin.py`) — read those first.

**Tech Stack:** Python 3.13, stdlib `sqlite3` (read-only URI mode), `phonenumbers` (new dependency), pg8000/psycopg via `clients/db.py`, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md` — read it before Task 1; every task cites its sections.

## Global Constraints

- **Branch:** `imessage-snapshot` (already exists, spec committed). Never commit to `main`; open the PR with the `/pr-open` skill when the work is done.
- **Message text is never served by `people-api`.** No response model in `api/` may have a `text` or `content` field for iMessage. Task 8 enforces this with a test.
- **Output and logs print counts only** — never names, phone numbers, or message content.
- **`chat.db` is opened read-only** (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`), never copied into the repo, never written.
- **Test fixtures use `+1555010xxxx` numbers and `example.com` addresses only.**
- **Layer rules** (CLAUDE.md): `clients/` I/O only; `repo/` takes an open connection and never opens or commits one; `services/` pure, no I/O; `models/` imports nothing from other layers.
- **Every task ends green:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`.
- **Timestamps are timezone-aware UTC** (`datetime.now(UTC)`), matching the LinkedIn code.
- **Python version floor:** 3.13. Use `X | None`, not `Optional[X]`.

---

## Task 0: Confirm the real `chat.db` shape

**This task is a prerequisite and produces no code.** Spec §5.1 states that column names must be confirmed against the real database before fixtures are built on them. Every later task depends on the answer.

**Files:** none (findings are recorded in the task's commit message on the spec, if a correction is needed).

**Interfaces:**
- Produces: a confirmed column list for `message`, `handle`, `chat`, `chat_handle_join`, `chat_message_join` — consumed by Tasks 1, 3, 4, 5.

- [ ] **Step 1: Check Full Disk Access**

```bash
sqlite3 -readonly ~/Library/Messages/chat.db ".tables"
```

Expected: a table list. If it prints `authorization denied`, **stop and ask the user** to grant Full Disk Access (System Settings → Privacy & Security → Full Disk Access → add the terminal, then restart it). Do not guess the schema and do not proceed.

- [ ] **Step 2: Dump the columns the import reads**

```bash
for t in message handle chat chat_handle_join chat_message_join; do
  echo "== $t"; sqlite3 -readonly ~/Library/Messages/chat.db "PRAGMA table_info($t);" | cut -d'|' -f2,3
done
```

- [ ] **Step 3: Verify each column the spec names exists**

Required on `message`: `ROWID`, `guid`, `text`, `attributedBody`, `handle_id`, `is_from_me`, `date`, `service`, `cache_has_attachments`, `associated_message_type`.
Optional (may be absent on older macOS): `date_edited`, `date_retracted`.
Required on `handle`: `ROWID`, `id`, `service`. On `chat`: `ROWID`, `guid`, `display_name`, `style`. On `chat_handle_join`: `chat_id`, `handle_id`. On `chat_message_join`: `chat_id`, `message_id`.

- [ ] **Step 4: Sanity-check the date encoding and scale**

```bash
sqlite3 -readonly ~/Library/Messages/chat.db \
  "SELECT COUNT(*) FROM message;
   SELECT datetime(date/1000000000 + 978307200,'unixepoch') FROM message ORDER BY ROWID DESC LIMIT 1;
   SELECT COUNT(*) FROM message WHERE text IS NULL AND attributedBody IS NOT NULL;"
```

Expected: the second line is a plausible recent date (confirming nanoseconds since 2001-01-01 UTC, spec §5.2). Record the message count — it sets expectations for the first `--full` run.

- [ ] **Step 5: If anything differs from the spec, correct the spec first**

Edit `docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md` §5.1/§5.2 to match reality, then:

```bash
git commit -am "docs: correct chat.db column names against the real database"
```

If nothing differs, there is nothing to commit — say so and move on.

---

## Task 1: Schema — four tables plus the `person_email` foreign keys

**Files:**
- Modify: `repo/schema.sql` (append)
- Create: `tests/test_schema.py`

**Interfaces:**
- Produces: tables `imessage_handles`, `imessage_chats`, `imessage_messages`, `imessage_imports` exactly as in spec §4; the `linkedin_connections_person_email_fkey` constraint. Consumed by every later task.

- [ ] **Step 1: Write the failing test**

`tests/test_schema.py`. It runs against a scratch Postgres database and skips when none is configured, so CI without Postgres stays green.

```python
"""Schema tests against a real Postgres (spec §4). Skipped unless TEST_DATABASE_URL
is set, e.g. postgresql://localhost/people_schema_test."""

import os
from pathlib import Path

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

SCHEMA = Path(__file__).resolve().parent.parent / "repo" / "schema.sql"


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        yield c


def _person(conn, email="alice@example.com"):
    conn.execute(
        "INSERT INTO people (email, first_seen) VALUES (%s, now()) ON CONFLICT DO NOTHING", (email,)
    )


def test_schema_is_idempotent(conn):
    conn.execute(SCHEMA.read_text())  # must not raise: IF NOT EXISTS + DO $$ guard


def test_handle_person_email_must_exist(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO imessage_handles (handle, person_email) VALUES (%s, %s)",
            ("+15550100001", "nobody@example.com"),
        )


def test_deleting_person_nulls_links(conn):
    _person(conn)
    conn.execute(
        "INSERT INTO imessage_handles (handle, person_email) VALUES (%s, %s)",
        ("+15550100001", "alice@example.com"),
    )
    conn.execute(
        "INSERT INTO linkedin_connections (profile_url, full_name, person_email, snapshot_at)"
        " VALUES (%s, %s, %s, now())",
        ("linkedin.com/in/alice-example", "Alice Example", "alice@example.com"),
    )
    conn.execute("DELETE FROM people WHERE email = 'alice@example.com'")
    assert conn.execute("SELECT person_email FROM imessage_handles").fetchone()[0] is None
    assert conn.execute("SELECT person_email FROM linkedin_connections").fetchone()[0] is None
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
createdb people_schema_test 2>/dev/null; TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -v
```

Expected: FAIL — `relation "imessage_handles" does not exist`. If no local Postgres is installed, the tests skip; install one (`brew install postgresql@16 && brew services start postgresql@16`) so this task is actually verified.

- [ ] **Step 3: Append the tables to `repo/schema.sql`**

Copy the four `CREATE TABLE` statements and their indexes verbatim from spec §4.1–§4.4, then the migration block from §4:

```sql
-- iMessage snapshot (docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §4).
-- Upserted incrementally by scripts/import_imessage.py; message text is stored here
-- but is never served by people-api.

CREATE TABLE IF NOT EXISTS imessage_handles (
    handle                 TEXT PRIMARY KEY,
    display_name           TEXT,
    google_resource_name   TEXT,
    person_email           TEXT REFERENCES people(email) ON DELETE SET NULL,
    match_method           TEXT,
    message_count          INT NOT NULL DEFAULT 0,
    my_message_count       INT NOT NULL DEFAULT 0,
    last_message_at        TIMESTAMPTZ,
    last_my_message_at     TIMESTAMPTZ,
    group_message_count    INT NOT NULL DEFAULT 0,
    last_group_message_at  TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- ... remaining tables and indexes exactly as spec §4.1–§4.4 ...

-- Backfill the same constraint onto the LinkedIn snapshot (spec §4, Migration).
DO $$ BEGIN
    ALTER TABLE linkedin_connections
        ADD CONSTRAINT linkedin_connections_person_email_fkey
        FOREIGN KEY (person_email) REFERENCES people(email) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql tests/test_schema.py
git commit -m "feat: imessage_* tables and person_email foreign keys"
```

---

## Task 2: Models

**Files:**
- Create: `models/imessage.py`

**Interfaces:**
- Produces: the dataclasses every later task passes around.

```python
IMessageHandle(handle, display_name=None, google_resource_name=None,
               person_email=None, match_method=None)
IMessageChat(chat_guid, display_name, is_group, participant_handles: list[str],
             last_message_at: datetime | None)
IMessageMessage(guid, chat_guid, sender_handle, from_me, sent_at, text,
                service, has_attachments, edited_at, retracted)
IMessageBatch(mode: str, max_rowid: int, chats, messages, handles,
              undecoded: int, retracted: int, short_codes_dropped: int,
              reactions_skipped: int, all_guids: set[str] | None,
              all_chat_guids: set[str] | None)
```
`all_guids` / `all_chat_guids` are populated only in `--full` mode (Task 7 uses them for the reconciling delete); they are `None` for incremental runs.

- [ ] **Step 1: Write the file**

Follow `models/linkedin.py` exactly in style — module docstring citing the spec, `@dataclass`, no imports from other layers.

```python
"""iMessage snapshot records (docs/superpowers/specs/2026-09-21-imessage-snapshot-design.md §4).

Pure types: built by services/imessage_export.py, written by repo/imessage.py.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IMessageHandle:
    handle: str  # E.164 phone or lowercased email
    display_name: str | None = None
    google_resource_name: str | None = None
    person_email: str | None = None
    match_method: str | None = None  # 'email' | 'google' | None


@dataclass
class IMessageChat:
    chat_guid: str
    display_name: str | None
    is_group: bool
    participant_handles: list[str]
    last_message_at: datetime | None = None


@dataclass
class IMessageMessage:
    guid: str
    chat_guid: str
    sender_handle: str | None  # None when from_me
    from_me: bool
    sent_at: datetime
    text: str | None
    service: str | None
    has_attachments: bool = False
    edited_at: datetime | None = None
    retracted: bool = False


@dataclass
class IMessageBatch:
    mode: str  # 'incremental' | 'full'
    max_rowid: int
    chats: list[IMessageChat] = field(default_factory=list)
    messages: list[IMessageMessage] = field(default_factory=list)
    handles: list[IMessageHandle] = field(default_factory=list)
    undecoded: int = 0
    retracted: int = 0
    short_codes_dropped: int = 0
    reactions_skipped: int = 0
    matched_by_email: int = 0
    matched_by_google: int = 0
    linked_to_people: int = 0
    all_guids: set[str] | None = None
    all_chat_guids: set[str] | None = None
```

- [ ] **Step 2: Verify it typechecks**

```bash
.venv/bin/mypy models/ && .venv/bin/ruff check models/ && .venv/bin/ruff format --check models/
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add models/imessage.py
git commit -m "feat: iMessage snapshot models"
```

---

## Task 3: `clients/imessage_local.py` — read `chat.db` read-only

**Files:**
- Create: `clients/imessage_local.py`
- Create: `tests/fixtures/imessage.py`
- Create: `tests/test_imessage_local.py`

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
@dataclass
class RawChatDb:
    messages: list[dict]       # ROWID, guid, text, attributedBody, handle_id, is_from_me,
                               # date, date_edited, date_retracted, service,
                               # cache_has_attachments, associated_message_type, chat_id
    handles: dict[int, str]    # handle ROWID -> raw id ('+15035550123' or an email)
    chats: list[dict]          # ROWID, guid, display_name
    chat_handles: dict[int, list[int]]   # chat ROWID -> handle ROWIDs
    max_rowid: int             # max message ROWID seen in the DB (not just the selection)
    all_guids: set[str] | None
    all_chat_guids: set[str] | None

def read(path: Path, *, since_rowid: int = 0, window_start: datetime | None = None,
         full: bool = False) -> RawChatDb
```
`FullDiskAccessError(Exception)` is raised when the file cannot be opened.
`fixtures/imessage.py` produces `build_chat_db(tmp_path) -> Path`, used by Tasks 3, 4, 5 and 7.

- [ ] **Step 1: Write the fixture builder**

`tests/fixtures/imessage.py`. Shapes come from Task 0. `ATTR_BODY` is a minimal real `typedstream` blob; get one from the real DB with
`sqlite3 -readonly ~/Library/Messages/chat.db "SELECT quote(attributedBody) FROM message WHERE attributedBody IS NOT NULL LIMIT 1;"`
and replace its text payload with `Fixture body text` (keeping the length byte correct), or hand-build it as below.

```python
"""Synthetic chat.db (spec §10). Invented numbers (+1555010…) and example.com only."""

import sqlite3
from pathlib import Path

NS = 1_000_000_000
EPOCH_2001 = 978307200

# typedstream fragment: an NSString payload "Attr only body" inside an NSAttributedString.
ATTR_BODY = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84\x12NSAttributedString"
    b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
    b"\x0eAttr only body\x86"
)

DDL = """
CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
  handle_id INTEGER, is_from_me INTEGER, date INTEGER, date_edited INTEGER,
  date_retracted INTEGER, service TEXT, cache_has_attachments INTEGER,
  associated_message_type INTEGER);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT, style INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
"""


def apple_ns(unix_seconds: int) -> int:
    return (unix_seconds - EPOCH_2001) * NS


def build_chat_db(tmp_path: Path) -> Path:
    """1:1 chat with Alice, a group chat, a short-code chat. Covers every decode path."""
    path = tmp_path / "chat.db"
    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.executemany(
        "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
        [(1, "+15550100001", "iMessage"), (2, "bob@example.com", "iMessage"),
         (3, "+15550100002", "SMS"), (4, "262966", "SMS")],
    )
    db.executemany(
        "INSERT INTO chat (ROWID, guid, display_name, style) VALUES (?, ?, ?, ?)",
        [(1, "iMessage;-;+15550100001", None, 45),
         (2, "iMessage;+;chat9", "Fixture Group", 43),
         (3, "SMS;-;262966", None, 45)],
    )
    db.executemany(
        "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
        [(1, 1), (2, 1), (2, 2), (2, 3), (3, 4)],
    )
    rows = [
        # (rowid, guid, text, attr, handle_id, from_me, date, edited, retracted,
        #  service, attach, assoc, chat)
        (1, "g1", "Hi from Alice", None, 1, 0, apple_ns(1_750_000_000), 0, 0, "iMessage", 0, 0, 1),
        (2, "g2", "Reply from me", None, 0, 1, apple_ns(1_750_000_060), 0, 0, "iMessage", 0, 0, 1),
        (3, "g3", None, ATTR_BODY, 1, 0, apple_ns(1_750_000_120), 0, 0, "iMessage", 0, 0, 1),
        (4, "g4", None, b"\x04\x0bstreamtyped\xff\xff", 1, 0, apple_ns(1_750_000_180), 0, 0,
         "iMessage", 0, 0, 1),                                    # undecodable
        (5, "g5", "Liked a message", None, 1, 0, apple_ns(1_750_000_240), 0, 0, "iMessage", 0,
         2000, 1),                                                # reaction, skipped
        (6, "g6", "Edited text", None, 1, 0, apple_ns(1_750_000_300),
         apple_ns(1_750_000_400), 0, "iMessage", 0, 0, 1),        # edited
        (7, "g7", "Unsent", None, 1, 0, apple_ns(1_750_000_360), 0,
         apple_ns(1_750_000_500), "iMessage", 0, 0, 1),           # retracted
        (8, "g8", "Group hello", None, 2, 0, apple_ns(1_750_000_420), 0, 0, "iMessage", 0, 0, 2),
        (9, "g9", "Your code is 123456", None, 4, 0, apple_ns(1_750_000_480), 0, 0, "SMS", 0,
         0, 3),                                                   # short code, dropped
        (10, "g10", "Old one", None, 1, 0, 550_000_000, 0, 0, "iMessage", 1, 0, 1),  # seconds-era
    ]
    db.executemany(
        "INSERT INTO message (ROWID, guid, text, attributedBody, handle_id, is_from_me, date,"
        " date_edited, date_retracted, service, cache_has_attachments, associated_message_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [r[:-1] for r in rows],
    )
    db.executemany(
        "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
        [(r[-1], r[0]) for r in rows],
    )
    db.commit()
    db.close()
    return path
```

- [ ] **Step 2: Write the failing test**

`tests/test_imessage_local.py`:

```python
from datetime import UTC, datetime

import pytest

from clients import imessage_local
from tests.fixtures.imessage import build_chat_db


def test_read_returns_messages_handles_and_chats(tmp_path):
    raw = imessage_local.read(build_chat_db(tmp_path))
    assert len(raw.messages) == 10
    assert raw.handles[1] == "+15550100001"
    assert raw.chat_handles[2] == [1, 2, 3]
    assert raw.max_rowid == 10


def test_incremental_selection_uses_watermark(tmp_path):
    raw = imessage_local.read(build_chat_db(tmp_path), since_rowid=8)
    assert {m["guid"] for m in raw.messages} == {"g9", "g10"}
    assert raw.max_rowid == 10  # watermark is the DB max, not the selection max


def test_window_start_pulls_older_rows_back_in(tmp_path):
    """Rows 1-8 are below the watermark but inside the window, so they come back
    for edit/unsend refresh (spec §5.1). Row 10 is the legacy seconds-era row and
    stays out; row 9 is above the watermark."""
    raw = imessage_local.read(
        build_chat_db(tmp_path), since_rowid=9, window_start=datetime(2025, 6, 1, tzinfo=UTC)
    )
    assert {m["guid"] for m in raw.messages} == {f"g{i}" for i in range(1, 10)}


def test_full_collects_all_guids(tmp_path):
    raw = imessage_local.read(build_chat_db(tmp_path), full=True)
    assert raw.all_guids == {f"g{i}" for i in range(1, 11)}
    assert raw.all_chat_guids == {"iMessage;-;+15550100001", "iMessage;+;chat9", "SMS;-;262966"}


def test_missing_optional_column_is_none(tmp_path):
    import sqlite3

    path = build_chat_db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript(
        "ALTER TABLE message RENAME TO m_old;"
        "CREATE TABLE message AS SELECT ROWID, guid, text, attributedBody, handle_id,"
        " is_from_me, date, service, cache_has_attachments, associated_message_type FROM m_old;"
    )
    db.commit()
    db.close()
    raw = imessage_local.read(path)
    assert raw.messages[0]["date_edited"] is None


def test_missing_required_column_fails_naming_it(tmp_path):
    import sqlite3

    path = build_chat_db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript("ALTER TABLE message DROP COLUMN guid;")
    db.commit()
    db.close()
    with pytest.raises(imessage_local.SchemaError, match="guid"):
        imessage_local.read(path)


def test_missing_file_raises_full_disk_access_error(tmp_path):
    with pytest.raises(imessage_local.FullDiskAccessError):
        imessage_local.read(tmp_path / "nope.db")
```

Fix the deliberately convoluted expression in `test_window_start_pulls_older_rows_back_in` while writing it: the assertion is simply that all ten GUIDs come back.

- [ ] **Step 3: Run it to verify it fails**

```bash
.venv/bin/pytest tests/test_imessage_local.py -v
```

Expected: FAIL — `ModuleNotFoundError: clients.imessage_local`.

- [ ] **Step 4: Implement the client**

`clients/imessage_local.py`. Key points: read-only URI connect; `sqlite3.Row`; introspect columns with `PRAGMA table_info(message)` so optional columns become `None` and a missing required column raises `SchemaError` naming it; select with `ROWID > ? OR date >= ?`; always compute `max_rowid` as `SELECT MAX(ROWID) FROM message`; join `chat_message_join` taking `MIN(chat_id)` per message (spec §4.3).

```python
"""Read-only access to the local Messages database (spec §5.1). Local script use
only — people's Cloud Functions never read iMessage, exactly as clients/graph_local.py
is import-only."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

EPOCH_2001 = 978307200
REQUIRED = ("ROWID", "guid", "text", "attributedBody", "handle_id", "is_from_me", "date",
            "service", "cache_has_attachments", "associated_message_type")  # fmt: skip
OPTIONAL = ("date_edited", "date_retracted")


class FullDiskAccessError(Exception):
    """chat.db could not be opened — usually missing Full Disk Access."""


class SchemaError(Exception):
    """chat.db is missing a column the import requires."""


@dataclass
class RawChatDb:
    messages: list[dict]
    handles: dict[int, str]
    chats: list[dict]
    chat_handles: dict[int, list[int]]
    max_rowid: int
    all_guids: set[str] | None = None
    all_chat_guids: set[str] | None = None


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.Error as e:
        raise FullDiskAccessError(str(e)) from e


def read(path: Path, *, since_rowid: int = 0, window_start: datetime | None = None,
         full: bool = False) -> RawChatDb:
    conn = _connect(path)
    try:
        present = {r["name"] for r in conn.execute("PRAGMA table_info(message)")}
        present.add("ROWID")
        missing = [c for c in REQUIRED if c not in present]
        if missing:
            raise SchemaError(f"chat.db message table is missing: {', '.join(missing)}")
        cols = ", ".join(
            [f"m.{c}" for c in REQUIRED]
            + [(f"m.{c}" if c in present else f"NULL AS {c}") for c in OPTIONAL]
        )
        where, params = "1=1", []
        if not full:
            where = "m.ROWID > ?"
            params = [since_rowid]
            if window_start is not None:
                where += " OR m.date >= ?"
                params.append(int((window_start.timestamp() - EPOCH_2001) * 1_000_000_000))
        rows = conn.execute(
            f"SELECT {cols}, (SELECT MIN(chat_id) FROM chat_message_join j"
            f"  WHERE j.message_id = m.ROWID) AS chat_id"
            f" FROM message m WHERE {where} ORDER BY m.ROWID",
            params,
        ).fetchall()
        ...
    finally:
        conn.close()
```

Finish by building `handles`, `chats`, `chat_handles` from full-table reads (spec §5.1: these tables are small and always read whole), setting `max_rowid` from `SELECT MAX(ROWID) FROM message` (0 when empty), and, when `full`, `all_guids` / `all_chat_guids` from full scans.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_imessage_local.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add clients/imessage_local.py tests/fixtures/imessage.py tests/test_imessage_local.py
git commit -m "feat: read-only chat.db reader"
```

---

## Task 4: `services/imessage_export.py` — decoding and normalization

**Files:**
- Create: `services/imessage_export.py`
- Create: `tests/test_imessage_export.py`
- Modify: `requirements.txt` (add `phonenumbers>=8.13`)

**Interfaces:**
- Consumes: `clients.imessage_local.RawChatDb` (types only; this module does no I/O).
- Produces:

```python
apple_ts(value: int | None) -> datetime | None
decode_attributed_body(blob: bytes | None) -> str | None
normalize_handle(raw: str, region: str = "US") -> str | None   # None = short code / unparseable
is_reaction(associated_message_type: int | None) -> bool
```

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime

from services import imessage_export as ex
from tests.fixtures.imessage import ATTR_BODY, apple_ns


def test_apple_ts_handles_nanoseconds():
    assert ex.apple_ts(apple_ns(1_750_000_000)) == datetime.fromtimestamp(1_750_000_000, UTC)


def test_apple_ts_handles_legacy_seconds():
    assert ex.apple_ts(550_000_000) == datetime.fromtimestamp(550_000_000 + 978307200, UTC)


def test_apple_ts_treats_zero_and_none_as_missing():
    assert ex.apple_ts(0) is None and ex.apple_ts(None) is None


def test_decode_attributed_body_extracts_text():
    assert ex.decode_attributed_body(ATTR_BODY) == "Attr only body"


def test_decode_attributed_body_returns_none_when_undecodable():
    assert ex.decode_attributed_body(b"\x04\x0bstreamtyped\xff\xff") is None


def test_normalize_handle_formats_e164():
    assert ex.normalize_handle("(555) 010-0001", region="US") == "+15550100001"
    assert ex.normalize_handle("+1 555 010 0001") == "+15550100001"


def test_normalize_handle_lowercases_email():
    assert ex.normalize_handle("Bob@Example.COM ") == "bob@example.com"


def test_normalize_handle_drops_short_codes():
    assert ex.normalize_handle("262966") is None
    assert ex.normalize_handle("") is None


def test_is_reaction_covers_tapback_range():
    assert ex.is_reaction(2000) and ex.is_reaction(3001)
    assert not ex.is_reaction(0) and not ex.is_reaction(None)
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_imessage_export.py -v
```

Expected: FAIL — `ModuleNotFoundError: services.imessage_export`.

- [ ] **Step 3: Add the dependency**

Append to `requirements.txt` (it is imported by `services/`, which ships in both deploy images):

```
# Phone-number normalization (iMessage snapshot)
phonenumbers>=8.13
```

Then `.venv/bin/pip install -r requirements.txt`.

- [ ] **Step 4: Implement**

`typedstream` decoding: locate the `NSString` marker, skip the class/type bytes, read the length-prefixed UTF-8 payload; return `None` on any `IndexError`/`UnicodeDecodeError`/`ValueError` rather than raising. Values below `10**11` are legacy seconds (spec §5.2). A handle containing `@` goes through `services.eligibility.normalize`; otherwise `phonenumbers.parse` + `is_valid_number`, formatted `E164`, returning `None` when invalid or under 7 digits.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_imessage_export.py -v
```

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add services/imessage_export.py tests/test_imessage_export.py requirements.txt
git commit -m "feat: iMessage decoding and handle normalization"
```

---

## Task 5: `services/imessage_export.py` — batch building and matching

**Files:**
- Modify: `services/imessage_export.py`
- Modify: `tests/test_imessage_export.py`

**Interfaces:**
- Consumes: Task 4's helpers; `RawChatDb` from Task 3; `models.imessage` from Task 2.
- Produces:

```python
build_batch(raw: RawChatDb, *, mode: str) -> IMessageBatch
match_handles(batch: IMessageBatch, phone_index: dict[str, list[dict]],
              people_rows: list[dict]) -> None   # mutates batch.handles + counters
```
`phone_index` maps E.164 → list of `{"resource_name", "display_name"}` (Task 6). `people_rows` are `{"email", "display_name", "google_resource_name"}` dicts.

- [ ] **Step 1: Write the failing tests**

```python
from clients import imessage_local
from models.imessage import IMessageBatch, IMessageHandle
from services import imessage_export as ex
from tests.fixtures.imessage import build_chat_db


def batch(tmp_path, **kw):
    return ex.build_batch(imessage_local.read(build_chat_db(tmp_path), full=True), mode="full", **kw)


def test_build_batch_skips_reactions_and_short_codes(tmp_path):
    b = batch(tmp_path)
    guids = {m.guid for m in b.messages}
    assert "g5" not in guids and b.reactions_skipped == 1
    assert "g9" not in guids and b.short_codes_dropped == 1
    assert {h.handle for h in b.handles} == {"+15550100001", "bob@example.com", "+15550100002"}


def test_build_batch_decodes_and_flags(tmp_path):
    by_guid = {m.guid: m for m in batch(tmp_path).messages}
    assert by_guid["g3"].text == "Attr only body"
    assert by_guid["g4"].text is None          # undecodable
    assert by_guid["g6"].edited_at is not None
    assert by_guid["g7"].retracted and by_guid["g7"].text is None
    assert by_guid["g10"].has_attachments
    assert by_guid["g2"].from_me and by_guid["g2"].sender_handle is None


def test_build_batch_classifies_chats(tmp_path):
    chats = {c.chat_guid: c for c in batch(tmp_path).chats}
    assert chats["iMessage;+;chat9"].is_group
    assert chats["iMessage;+;chat9"].participant_handles == [
        "+15550100001", "bob@example.com", "+15550100002"
    ]
    assert not chats["iMessage;-;+15550100001"].is_group
    assert "SMS;-;262966" not in chats        # short-code chat dropped entirely


def test_match_handles_links_email_to_person():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("bob@example.com")])
    ex.match_handles(b, {}, [{"email": "bob@example.com", "display_name": "Bob",
                              "google_resource_name": None}])
    assert b.handles[0].person_email == "bob@example.com"
    assert b.handles[0].match_method == "email"
    assert b.matched_by_email == 1 and b.linked_to_people == 1


def test_match_handles_links_phone_via_unique_google_contact():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {"+15550100001": [{"resource_name": "people/c1", "display_name": "Alice Example"}]}
    ex.match_handles(b, index, [{"email": "alice@example.com", "display_name": "Alice",
                                 "google_resource_name": "people/c1"}])
    h = b.handles[0]
    assert (h.google_resource_name, h.display_name, h.person_email, h.match_method) == (
        "people/c1", "Alice Example", "alice@example.com", "google")
    assert b.matched_by_google == 1 and b.linked_to_people == 1


def test_match_handles_google_contact_without_people_row():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {"+15550100001": [{"resource_name": "people/c9", "display_name": "Carol"}]}
    ex.match_handles(b, index, [])
    h = b.handles[0]
    assert h.google_resource_name == "people/c9" and h.display_name == "Carol"
    assert h.person_email is None and h.match_method == "google"
    assert b.matched_by_google == 1 and b.linked_to_people == 0


def test_match_handles_skips_ambiguous_number():
    b = IMessageBatch(mode="full", max_rowid=0, handles=[IMessageHandle("+15550100001")])
    index = {"+15550100001": [{"resource_name": "people/c1", "display_name": "Alice"},
                              {"resource_name": "people/c2", "display_name": "Alias"}]}
    ex.match_handles(b, index, [])
    assert b.handles[0].match_method is None and b.handles[0].google_resource_name is None
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_imessage_export.py -k "build_batch or match_handles" -v
```

Expected: FAIL — `module 'services.imessage_export' has no attribute 'build_batch'`.

- [ ] **Step 3: Implement**

`build_batch`: normalize every handle ROWID → handle string (dropping unparseable ones and counting them once per distinct raw handle); drop chats whose participants all dropped out; build `IMessageChat` with `is_group = len(participants) > 1`; for each message, skip reactions, resolve `chat_guid` from `chat_id`, skip messages whose chat was dropped, set `sender_handle = None` when `is_from_me`, decode text (counting `undecoded`), clear text when retracted (counting `retracted`), and set `chats[].last_message_at` to the max `sent_at` seen. Carry `raw.max_rowid`, `raw.all_guids`, `raw.all_chat_guids` through.

`match_handles`: email handles match a `people` row by exact address (`match_method="email"`, `display_name` from the row); otherwise look the handle up in `phone_index` and require exactly one entry; set `google_resource_name`/`display_name`, then `person_email` from the `people` row whose `google_resource_name` matches (`match_method="google"`). Increment `matched_by_email`, `matched_by_google`, `linked_to_people` as it goes.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_imessage_export.py -v
```

Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add services/imessage_export.py tests/test_imessage_export.py
git commit -m "feat: build iMessage batches and match handles to people"
```

---

## Task 6: `list_phone_index()` on the Google Contacts client

**Files:**
- Modify: `clients/google_contacts.py`
- Modify: `tests/test_google_contacts.py`

**Interfaces:**
- Consumes: the module's existing `_svc()`.
- Produces: `list_phone_index(region: str = "US") -> dict[str, list[dict]]` — E.164 → `[{"resource_name", "display_name"}]`. It must **not** use or request a sync token, and must leave `PERSON_FIELDS` untouched (spec §3, §5.4).

- [ ] **Step 1: Write the failing test**

Follow the existing fake-service pattern in `tests/test_google_contacts.py`.

```python
def test_list_phone_index_groups_contacts_by_normalized_number(monkeypatch):
    pages = [
        {"connections": [
            {"resourceName": "people/c1",
             "names": [{"displayName": "Alice Example"}],
             "phoneNumbers": [{"value": "(555) 010-0001"}, {"value": "+1 555 010 0003"}]},
            {"resourceName": "people/c2",
             "names": [{"displayName": "Alias Example"}],
             "phoneNumbers": [{"value": "555-010-0001"}]},
        ], "nextPageToken": "p2"},
        {"connections": [
            {"resourceName": "people/c3", "names": [{"displayName": "No Phone"}]},
        ]},
    ]
    calls = []

    class FakeReq:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class FakeConnections:
        def list(self, **kw):
            calls.append(kw)
            return FakeReq(pages[len(calls) - 1])

    class FakePeople:
        def connections(self):
            return FakeConnections()

    class FakeService:
        def people(self):
            return FakePeople()

    monkeypatch.setattr(google_contacts, "_svc", lambda: FakeService())

    index = google_contacts.list_phone_index()
    assert sorted(e["resource_name"] for e in index["+15550100001"]) == ["people/c1", "people/c2"]
    assert index["+15550100003"] == [{"resource_name": "people/c1", "display_name": "Alice Example"}]
    assert all("syncToken" not in c and not c.get("requestSyncToken") for c in calls)
    assert calls[0]["personFields"] == "names,phoneNumbers,metadata"
    assert calls[1]["pageToken"] == "p2"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_google_contacts.py -k phone_index -v
```

Expected: FAIL — no attribute `list_phone_index`.

- [ ] **Step 3: Implement**

```python
_PHONE_FIELDS = "names,phoneNumbers,metadata"


def list_phone_index(region: str = "US") -> dict[str, list[dict]]:
    """Every contact's phone numbers, E.164-normalized, for the iMessage import
    (spec §5.4). Deliberately does NOT request or use a sync token: the nightly
    sync in repo/sync_state.py owns that token."""
    from services.imessage_export import normalize_handle

    index: dict[str, list[dict]] = {}
    page_token = None
    while True:
        resp = (
            _svc().people().connections().list(
                resourceName="people/me",
                personFields=_PHONE_FIELDS,
                pageSize=1000,
                pageToken=page_token,
            ).execute()
        )
        for person in resp.get("connections", []):
            entry = {
                "resource_name": person["resourceName"],
                "display_name": (person.get("names") or [{}])[0].get("displayName"),
            }
            for number in person.get("phoneNumbers", []):
                e164 = normalize_handle(number.get("value", ""), region=region)
                if e164:
                    index.setdefault(e164, []).append(entry)
        page_token = resp.get("nextPageToken")
        if not page_token:
            return index
```

The local import avoids a `clients/` → `services/` import at module load; note it in a comment, since it inverts the usual layer direction.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_google_contacts.py -v
```

Expected: all pass, including the pre-existing sync tests (proving the token path is untouched).

- [ ] **Step 5: Commit**

```bash
git add clients/google_contacts.py tests/test_google_contacts.py
git commit -m "feat: Google Contacts phone index for iMessage matching"
```

---

## Task 7: `repo/imessage.py` — upserts, stats, audit, reads

**Files:**
- Create: `repo/imessage.py`
- Create: `tests/test_repo_imessage.py`

**Interfaces:**
- Consumes: `models.imessage`; `FakeConn` from `tests/test_repo_people.py`.
- Produces:

```python
latest_watermark(conn) -> int
upsert_chats(conn, chats: list[IMessageChat], chunk: int = 500) -> None
upsert_messages(conn, messages: list[IMessageMessage], chunk: int = 500) -> int
upsert_handles(conn, handles: list[IMessageHandle], chunk: int = 500) -> None
delete_missing(conn, guids: set[str], chat_guids: set[str]) -> int
recompute_handle_stats(conn) -> None
record_import(conn, b: IMessageBatch, *, messages_deleted: int) -> None
handles(conn, *, q=None, min_messages=None, replied=None, quiet_since=None,
        unmatched=None, include_groups=False, limit=50) -> list[dict]
handle(conn, handle: str) -> dict | None
handle_groups(conn, handle: str) -> list[dict]
summary_for_person(conn, email: str) -> dict | None
search_handles(conn, q: str, limit: int) -> list[dict]
latest_import(conn) -> dict | None
```

- [ ] **Step 1: Write the failing tests**

Mirror `tests/test_repo_linkedin.py`: assert on the SQL text and params recorded by `FakeConn`.

```python
from datetime import UTC, datetime

from models.imessage import IMessageBatch, IMessageChat, IMessageHandle, IMessageMessage
from repo import imessage
from tests.test_repo_people import FakeConn

TS = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_latest_watermark_defaults_to_zero():
    assert imessage.latest_watermark(FakeConn(results=[[]])) == 0


def test_latest_watermark_reads_max_rowid():
    conn = FakeConn(results=[[{"max_rowid": 42}]])
    assert imessage.latest_watermark(conn) == 42
    assert "ORDER BY id DESC" in conn.calls[0][0]


def test_upsert_messages_is_idempotent_on_guid():
    conn = FakeConn()
    imessage.upsert_messages(conn, [IMessageMessage(
        guid="g1", chat_guid="c1", sender_handle="+15550100001", from_me=False,
        sent_at=TS, text="hi", service="iMessage")])
    sql, params = conn.calls[0]
    assert "INSERT INTO imessage_messages" in sql
    assert "ON CONFLICT (guid) DO UPDATE" in sql
    assert "text = EXCLUDED.text" in sql and "updated_at = now()" in sql
    assert params[0] == "g1"


def test_upsert_handles_preserves_stats_columns():
    conn = FakeConn()
    imessage.upsert_handles(conn, [IMessageHandle("+15550100001", display_name="Alice")])
    sql, _ = conn.calls[0]
    assert "ON CONFLICT (handle) DO UPDATE" in sql
    for stat in ("message_count", "my_message_count", "group_message_count"):
        assert f"{stat} = EXCLUDED" not in sql  # stats come from recompute, not the upsert


def test_delete_missing_uses_temp_tables_not_a_guid_list():
    conn = FakeConn(results=[[], [], [], [], [{"n": 3}], []])
    imessage.delete_missing(conn, {"g1"}, {"c1"})
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "CREATE TEMP TABLE" in joined and "NOT EXISTS" in joined
    assert "NOT IN (" not in joined


def test_recompute_handle_stats_splits_one_to_one_and_group():
    conn = FakeConn()
    imessage.recompute_handle_stats(conn)
    sql, _ = conn.calls[0]
    assert "UPDATE imessage_handles" in sql
    assert "is_group = false" in sql and "is_group = true" in sql
    assert "from_me" in sql


def test_record_import_writes_audit_row():
    conn = FakeConn()
    batch = IMessageBatch(mode="incremental", max_rowid=99, matched_by_email=2,
                          matched_by_google=5, linked_to_people=3, undecoded=1)
    imessage.record_import(conn, batch, messages_deleted=0)
    sql, params = conn.calls[0]
    assert "INSERT INTO imessage_imports" in sql
    assert "incremental" in params and 99 in params


def test_handles_filters_build_expected_sql():
    conn = FakeConn(results=[[]])
    imessage.handles(conn, q="ali", min_messages=5, replied=True, unmatched=True, limit=10)
    sql, params = conn.calls[0]
    assert "display_name ILIKE %s OR handle ILIKE %s" in sql
    assert "message_count >= %s" in sql and "my_message_count > 0" in sql
    assert "person_email IS NULL AND google_resource_name IS NULL" in sql
    assert params[-1] == 10


def test_handles_include_groups_changes_ordering():
    conn = FakeConn(results=[[]])
    imessage.handles(conn, include_groups=True)
    assert "GREATEST" in conn.calls[0][0]
```

Fix `test_record_import_writes_audit_row` while writing it — pass `conn` as the first argument and assert `"INSERT INTO imessage_imports" in conn.calls[0][0]` and that `mode` and `max_rowid` appear in the params.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_repo_imessage.py -v
```

Expected: FAIL — `ModuleNotFoundError: repo.imessage`.

- [ ] **Step 3: Implement**

Reuse the `_insert_many` chunking helper shape from `repo/linkedin.py`, adding `ON CONFLICT` clauses. `recompute_handle_stats` is one statement:

```sql
UPDATE imessage_handles h SET
    message_count = COALESCE(s.one_to_one, 0),
    my_message_count = COALESCE(s.mine, 0),
    last_message_at = s.last_at,
    last_my_message_at = s.last_mine_at,
    group_message_count = COALESCE(s.grp, 0),
    last_group_message_at = s.last_group_at,
    updated_at = now()
FROM (
    SELECT p.handle,
           COUNT(*) FILTER (WHERE NOT c.is_group)                AS one_to_one,
           COUNT(*) FILTER (WHERE NOT c.is_group AND m.from_me)  AS mine,
           MAX(m.sent_at) FILTER (WHERE NOT c.is_group)          AS last_at,
           MAX(m.sent_at) FILTER (WHERE NOT c.is_group AND m.from_me) AS last_mine_at,
           COUNT(*) FILTER (WHERE c.is_group)                    AS grp,
           MAX(m.sent_at) FILTER (WHERE c.is_group)              AS last_group_at
    FROM imessage_chats c
    CROSS JOIN LATERAL unnest(c.participant_handles) AS p(handle)
    JOIN imessage_messages m ON m.chat_guid = c.chat_guid
    GROUP BY p.handle
) s
WHERE h.handle = s.handle;
```

Note for the implementer: a 1:1 chat has exactly one participant, so this counts both sides' messages for that participant — which is what `message_count` means (spec §4.1). Handles with no messages keep their defaults; add a second statement zeroing rows absent from `s` only if a test demands it (it does not).

`delete_missing` creates `TEMP TABLE tmp_guids(guid TEXT PRIMARY KEY) ON COMMIT DROP`, inserts in chunks, deletes with `NOT EXISTS`, and returns the row count.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_repo_imessage.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add repo/imessage.py tests/test_repo_imessage.py
git commit -m "feat: imessage repo — upserts, stats recompute, reads"
```

---

## Task 8: `scripts/import_imessage.py`

**Files:**
- Create: `scripts/import_imessage.py`
- Create: `tests/test_import_imessage.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Tasks 2–7.
- Produces: `run(get_conn, path, *, full, dry_run, region) -> IMessageBatch` and `summary(batch, *, dry_run, deleted) -> str`, mirroring `scripts/import_linkedin.py`.

- [ ] **Step 1: Write the failing tests**

```python
import sqlite3

import pytest

from scripts import import_imessage
from tests.fixtures.imessage import build_chat_db


class FakeConn:
    def __init__(self):
        self.calls, self.committed, self.rolled_back = [], False, False

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))
        class C:
            def fetchone(self_inner): return {"max_rowid": 0}
            def fetchall(self_inner): return []
        return C()

    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def no_google(monkeypatch):
    monkeypatch.setattr(import_imessage.google_contacts, "list_phone_index", lambda: {})


def test_dry_run_writes_nothing(tmp_path, no_google):
    conn = FakeConn()
    import_imessage.run(lambda: conn, build_chat_db(tmp_path), full=True, dry_run=True)
    assert not any("INSERT INTO imessage_" in c for c in conn.calls)
    assert conn.rolled_back and not conn.committed


def test_real_run_upserts_and_records_import(tmp_path, no_google):
    conn = FakeConn()
    batch = import_imessage.run(lambda: conn, build_chat_db(tmp_path), full=True, dry_run=False)
    joined = " ".join(conn.calls)
    assert "INSERT INTO imessage_messages" in joined
    assert "INSERT INTO imessage_imports" in joined
    assert batch.max_rowid == 10


def test_missing_full_disk_access_exits_2(tmp_path, capsys, no_google):
    with pytest.raises(SystemExit) as e:
        import_imessage.main_with(["--db", str(tmp_path / "nope.db")], lambda: FakeConn())
    assert e.value.code == 2
    assert "Full Disk Access" in capsys.readouterr().err


def test_summary_prints_counts_not_content(tmp_path, no_google):
    batch = import_imessage.run(
        lambda: FakeConn(), build_chat_db(tmp_path), full=True, dry_run=True)
    out = import_imessage.summary(batch, dry_run=True, deleted=0)
    assert "messages" in out and "handles" in out
    assert "Hi from Alice" not in out and "+15550100001" not in out
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_import_imessage.py -v
```

Expected: FAIL — `ModuleNotFoundError: scripts.import_imessage`.

- [ ] **Step 3: Implement**

Copy the structure of `scripts/import_linkedin.py`: shebang, docstring citing spec §5, `sys.path` insert, `load_dotenv()`, then imports. `main_with(argv, get_conn)` exists so the test can drive it without patching `sys.argv`; `main()` calls it with `sys.argv[1:]` and the real `clients.db.get_conn`.

Order inside `run`: read the watermark (skip when `full`), `imessage_local.read(...)` with `window_start = now - 14 days` for incremental runs, `build_batch`, `google_contacts.list_phone_index()`, then open the connection and, in one transaction: `people_repo.names_for_matching`-style read (add `google_resource_name` — see Step 4), `match_handles`, `upsert_chats`, `upsert_messages`, `delete_missing` when `full`, `upsert_handles`, `recompute_handle_stats`, `record_import`. `--dry-run` rolls back instead of committing. Catch `FullDiskAccessError` → print the Full Disk Access message to stderr and `sys.exit(2)`; catch `SchemaError` → print and `sys.exit(2)`.

- [ ] **Step 4: Extend the people repo read**

`repo/people.names_for_matching` returns only `email, display_name`. Matching needs `google_resource_name` too. Add a sibling rather than changing the LinkedIn caller:

```python
def rows_for_imessage_matching(conn: Any) -> list[dict]:
    """Email, display name, and Google link for scripts/import_imessage.py (spec §5.4)."""
    return conn.execute(
        "SELECT email, display_name, google_resource_name FROM people"
    ).fetchall()
```

Add a matching test to `tests/test_repo_people.py` asserting the SQL contains all three columns.

- [ ] **Step 5: Ignore local exports**

Append to `.gitignore`:

```
# Local Messages database copies — personal data, never committed (iMessage spec §6)
chat.db*
imessage-export*/
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/import_imessage.py tests/test_import_imessage.py tests/test_repo_people.py repo/people.py .gitignore
git commit -m "feat: import_imessage script"
```

---

## Task 9: `people-api` — handles, person summary, search

**Files:**
- Create: `api/routers/imessage.py`
- Modify: `api/main.py` (include the router)
- Modify: `api/routers/people.py` (add `imessage` to `PersonOut`)
- Modify: `api/routers/search.py` (add `imessage_results`)
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `repo.imessage` reads from Task 7.
- Produces: the endpoints in spec §7.3 and the additive fields in §7.1–§7.2. **No response model carries message text.**

- [ ] **Step 1: Write the failing tests**

Follow the existing fake-connection pattern in `tests/test_api.py`.

```python
def test_person_includes_imessage_summary(client, fake_db):
    fake_db.queue(person_row(), imessage_summary_row())
    body = client.get("/people/alice@example.com").json()
    assert body["imessage"]["message_count"] == 212
    assert body["imessage"]["handles"] == ["+15550100001"]


def test_person_imessage_is_null_when_unlinked(client, fake_db):
    fake_db.queue(person_row(), None)
    assert client.get("/people/alice@example.com").json()["imessage"] is None


def test_list_people_omits_imessage(client, fake_db):
    fake_db.queue([person_row()])
    assert client.get("/people?recent=5").json()["results"][0]["imessage"] is None


def test_search_returns_imessage_results(client, fake_db):
    fake_db.queue([person_row()], [], [handle_row()])
    body = client.post("/search", json={"q": "ali"}).json()
    assert body["imessage_results"][0]["handle"] == "+15550100001"


def test_handles_endpoint_applies_filters(client, fake_db):
    fake_db.queue([handle_row()])
    assert client.get("/imessage/handles?replied=true&min_messages=3").status_code == 200


def test_handle_detail_returns_groups_and_404s(client, fake_db):
    fake_db.queue(handle_row(), [group_row()])
    body = client.get("/imessage/handles/%2B15550100001").json()
    assert body["groups"][0]["display_name"] == "Fixture Group"
    fake_db.queue(None)
    assert client.get("/imessage/handles/%2B15550100002").status_code == 404


def test_imports_latest_404s_when_never_imported(client, fake_db):
    fake_db.queue(None)
    assert client.get("/imessage/imports/latest").status_code == 404


def test_no_imessage_response_model_exposes_message_text():
    """Spec §6: people-api never serves iMessage content."""
    import api.routers.imessage as mod
    from pydantic import BaseModel

    banned = {"text", "content", "body", "message", "messages"}
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            assert not (set(obj.model_fields) & banned), f"{name} exposes message content"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/pytest tests/test_api.py -k imessage -v
```

Expected: FAIL — router missing.

- [ ] **Step 3: Implement the router**

Model `api/routers/imessage.py` on `api/routers/linkedin.py`: `APIRouter(dependencies=[Depends(verify_token)])`, pydantic models `IMessageSummary`, `IMessageHandleOut`, `IMessageHandleList`, `IMessageGroupOut`, `IMessageHandleDetail`, `IMessageImportOut`. Routes per spec §7.3, with `limit: int = Query(50, le=500)`. The `{handle}` path parameter arrives URL-decoded by Starlette; no extra handling is needed.

- [ ] **Step 4: Wire the additive fields**

In `api/main.py`, `from api.routers import imessage, linkedin, people, search` and `app.include_router(imessage.router)`. In `people.py`, add `imessage: IMessageSummary | None = None` to `PersonOut` and populate it on the detail and PATCH paths via `repo.imessage.summary_for_person`; list responses leave it `None`. In `search.py`, add `imessage_results: list[IMessageHandleOut] = []` fed by `repo.imessage.search_handles`.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/ -q && .venv/bin/mypy api/
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/ tests/test_api.py
git commit -m "feat: people-api iMessage handle endpoints and person summary"
```

---

## Task 10: Skill and docs

**Files:**
- Create: `.claude/skills/importing-imessage/SKILL.md`
- Modify: `.claude/skills/searching-people/SKILL.md`, `fetching-person/SKILL.md`, `querying-people-db/SKILL.md`, `people-architecture/SKILL.md`
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation).

- [ ] **Step 1: Write the new skill**

`importing-imessage/SKILL.md`, modeled on `importing-linkedin/SKILL.md`. Frontmatter description: "Use when the user wants to load, refresh, or re-import their iMessage history into the people database, run scripts/import_imessage.py, or check how old the iMessage snapshot is." Body covers: granting Full Disk Access; first run `--full --dry-run` then `--full`; routine incremental runs; how to read the counts; checking freshness via `GET /imessage/imports/latest`; and that message text is only reachable by direct DB query, never through the API.

- [ ] **Step 2: Update the existing skills**

`searching-people`: `imessage_results` and the `/imessage/handles` filters. `fetching-person`: the `imessage` block. `querying-people-db`: the four tables, that text lives only in `imessage_messages`, and two example queries. `people-architecture`: the local-import path and that Cloud Functions never touch `chat.db`.

- [ ] **Step 3: Update CLAUDE.md**

Add to the code-layout block: `clients/imessage_local.py`, `services/imessage_export.py`, `repo/imessage.py`, `models/imessage.py`, `scripts/import_imessage.py`, `api/routers/imessage.py`, and the new skill. Add the database tables to the Stack table's Database row. Add the source-of-truth row from spec §4.5. Add the local command to the Local dev section.

- [ ] **Step 4: Verify**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add .claude/skills CLAUDE.md
git commit -m "docs: iMessage snapshot skills and layout"
```

---

## Task 11: Migrate, verify against real data, open the PR

**Files:** none (verification), then the PR.

- [ ] **Step 1: Apply the schema to the real database**

```bash
.venv/bin/python scripts/migrate_db.py
```

Expected: no error. The new tables are `IF NOT EXISTS` and the LinkedIn constraint is guarded, so this is safe against the running service (spec §11). If the constraint fails to validate, re-run `scripts/import_linkedin.py` and repeat.

- [ ] **Step 2: Dry-run the full import**

```bash
.venv/bin/python scripts/import_imessage.py --full --dry-run
```

Expected: counts printed, nothing written. Sanity-check that the message count is near Task 0's total and that no names, numbers, or message text appear in the output.

- [ ] **Step 3: Run the real full import**

```bash
.venv/bin/python scripts/import_imessage.py --full
```

- [ ] **Step 4: Verify the incremental path**

```bash
.venv/bin/python scripts/import_imessage.py
```

Expected: `mode incremental`, few or no new messages, a small refreshed count from the 14-day window, and the watermark unchanged or slightly advanced.

- [ ] **Step 5: Spot-check through the API**

Run the API locally and check a handle you recognize, per the `verifying-pr-locally` skill:

```bash
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080) &
curl -s -H "Authorization: Bearer $PEOPLE_API_TOKEN" \
  'http://localhost:8080/imessage/handles?limit=5' | jq '.results[] | {display_name, message_count}'
curl -s -H "Authorization: Bearer $PEOPLE_API_TOKEN" \
  'http://localhost:8080/imessage/imports/latest' | jq
```

Confirm the top handles are people you actually text, that group-heavy contacts are not inflating the 1:1 ranking, and that **no endpoint returns message text**.

- [ ] **Step 6: Open the PR**

Use the `/pr-open` skill (CLAUDE.md requires it for this repo). The description should cover the new tables, the foreign-key backfill, the local-only import path, and that the API serves stats only. Include the verification counts from Steps 2–5, with names and numbers redacted.

---

## Self-Review

**Spec coverage:** §4 tables and FKs → Task 1; §4.5 source-of-truth row → Task 10; §5.1 reading → Tasks 0, 3; §5.2 decoding → Task 4; §5.3 normalization → Task 4; §5.4 matching → Tasks 5, 6; §5.5 write path → Tasks 7, 8; §5.6 output → Task 8; §6 privacy → Tasks 1 (`.gitignore` in 8), 8, 9 (the no-text test); §7 API → Task 9; §8 skills → Task 10; §9 observability → no work needed (local script emits no metrics; API routes are covered by the existing middleware); §10 testing → spread across Tasks 1–9; §11 rollout → Task 11.

**Known gaps, deliberately left:** the nightly `launchd` job is a non-goal (spec §2). `repo/imessage.py` does not zero stats for handles whose messages all disappear, because only a `--full` run can remove messages and `delete_missing` does not orphan handles; if that changes, add a zeroing statement to `recompute_handle_stats`.

**Type consistency:** `build_batch`/`match_handles` (Task 5) match their use in Task 8. `normalize_handle` is used in Tasks 4, 5 and 6 with the same signature. `IMessageBatch` counter names (`matched_by_email`, `matched_by_google`, `linked_to_people`, `undecoded`, `retracted`, `short_codes_dropped`, `reactions_skipped`) are the same in Tasks 2, 5, 7 and 8, and match the `imessage_imports` columns in spec §4.4.
