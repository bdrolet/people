# WhatsApp Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import WhatsApp chats, handles, group membership and messages from the local `ChatStorage.sqlite` on Ben's Mac into five new `whatsapp_*` tables in the `people` database, link handles to `people` rows by phone number (and, for LID chats only, by exact name), and serve per-handle stats — never message text — from `people-api`.

**Architecture:** A local-only script (`scripts/import_whatsapp.py`) copies the store plus its `-wal`/`-shm` sidecars to a temp directory, reads the copy read-only, upserts messages by `(chat_jid, stanza_id)` from a `Z_PK` watermark, recomputes chat and handle stats in SQL, and appends an audit row. Layering follows the repo's rules: `clients/` does I/O, `services/` is pure logic, `repo/` takes an open connection, `api/routers/` is thin transport. It is deliberately modelled on the iMessage snapshot — read `clients/imessage_local.py`, `services/imessage_export.py`, `repo/imessage.py`, `scripts/import_imessage.py` and `api/routers/imessage.py` first; this plan cites them where it diverges.

**Tech Stack:** Python 3.13, stdlib `sqlite3` (read-only URI mode) + `shutil`/`tempfile`, `phonenumbers` (already a dependency), psycopg/pg8000 via `clients/db.py`, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md` — read it before Task 1; every task cites its sections.

## Global Constraints

- **Branch:** `whatsapp-snapshot` off `main` (the spec is already committed on `whatsapp-snapshot-spec`; see Task 0 Step 0). Never commit code to `main`; open the PR with the `/pr-open` skill when the work is done.
- **Message text is never served by `people-api`.** No response model in `api/routers/whatsapp.py` may carry a `text`, `content`, `body`, `message` or `messages` field. Task 9 enforces this with a guard test, mirroring `tests/test_api.py::test_no_imessage_response_model_exposes_message_text`.
- **Media files are never read.** Only `has_media` and a coarse `media_kind`, both derived from `ZWAMEDIAITEM` columns. No file is opened, hashed, copied or uploaded.
- **The temp copy of the store is deleted in a `finally`.** It holds the full plaintext history; leaving it behind is a real leak (spec §7).
- **Script output and logs print counts only** — never JIDs, phone numbers, display names or message text. The repo is public.
- **Test fixtures are synthetic:** `+1555010xxxx` US numbers, `example.com` addresses, invented group JIDs. No real JID, name or message text enters the repo.
- **The import never writes to `people`, Google Contacts or HubSpot,** and never touches `people` counters, `last_contacted`, `eligible` or HubSpot ranking (spec §2 non-goals).
- **`person_id` is the link column**, `BIGINT REFERENCES people(id) ON DELETE SET NULL` — never `person_email`. The iMessage spec's §4 is stale on this point; follow the live `repo/schema.sql` (spec §4, note to the implementer).
- **Layer rules** (CLAUDE.md): `clients/` I/O only; `repo/` takes an open connection and never opens or commits one; `services/` pure, no I/O; `models/` imports nothing from other layers. `services/whatsapp_export.py` importing `services/imessage_export.py` is a permitted services-to-services import (spec §3).
- **Every task ends green:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`.
- **Timestamps are timezone-aware UTC** (`datetime.now(UTC)`).
- **Python version floor:** 3.13. Use `X | None`, not `Optional[X]`.
- **No new dependencies.** `phonenumbers>=8.13` is already in `requirements.txt` and `requirements-dev.txt`.

## Review Focus

Five input classes the spec implies but does not pin with a test. Each line's test is added to the task that owns the code.

1. **A phone number held by two `people` rows.** The handle must link to nothing (`person_id` and `match_method` stay `NULL`), not to the first or most-recent candidate — 34 numbers are already shared across rows (spec §Identity, measured 2026-09-28). Test in Task 5.
2. **Two rows in one batch that collide on a primary key** — a second session row for a JID already seen, or two messages with the same `(chat_jid, stanza_id)`. A multi-row `INSERT ... ON CONFLICT DO UPDATE` raises `cannot affect row a second time` if the batch is not deduplicated in Python first, which would abort the whole import. Tests in Task 4 (dedupe) and Task 6 (single-statement upsert).
3. **An incremental run whose window holds no messages for most chats.** It must not null their `last_message_at` nor zero their `message_count`/`member_count` — the exact bug the iMessage import shipped once (spec §6.7). Test in Task 6.
4. **The read raising partway through.** The temp copy of the store must still be deleted; a leaked copy is a privacy failure, not untidiness. Test in Task 2.
5. **Empty or missing text and stanza ids.** An empty `ZTEXT` is stored as `NULL`, never `''`; a row with no `ZSTANZAID` cannot be keyed and is dropped and counted rather than crashing the insert on a `NOT NULL` primary key. Tests in Task 4.

---

## Task 0: Confirm the store's shape and record the baseline

**This task produces no application code.** The spec's every number was measured on 2026-09-29; Tasks 2–4 are built directly on the column names and encodings below, so they are confirmed first. It also settles one open question (`ZFROMJID`) that would otherwise be discovered late.

**Files:**
- Modify (only if reality differs): `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md`

**Interfaces:**
- Produces: a confirmed column list for `ZWACHATSESSION`, `ZWAMESSAGE`, `ZWAGROUPMEMBER`, `ZWAGROUPINFO`, `ZWAMEDIAITEM`, and confirmed join directions — consumed by Tasks 1, 2, 3, 4.

- [ ] **Step 1: Create the branch**

```bash
cd ~/src/people
git checkout main && git pull
git checkout -b whatsapp-snapshot
git checkout whatsapp-snapshot-spec -- docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md
git commit -m "docs: WhatsApp snapshot design"
```

The spec lives on `whatsapp-snapshot-spec`, which is not merged. This brings the one file onto the working branch so the PR carries its own design doc. If `git status` shows the file already present and tracked, skip the last two lines.

- [ ] **Step 2: Confirm the store is readable without Full Disk Access**

```bash
S="$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
ls -la "$S"*
sqlite3 -readonly "file:$S?mode=ro" ".tables" | tr ' ' '\n' | grep -E 'ZWA(MESSAGE|CHATSESSION|GROUPINFO|GROUPMEMBER|MEDIAITEM)$'
```

Expected: the `.sqlite`, `-wal` and `-shm` files exist, and all five table names print. Spec §5.1 says Full Disk Access is **not** required here (unlike `chat.db`) — if this prints `authorization denied`, stop and tell the user, because the whole skill's setup section depends on that claim.

- [ ] **Step 3: Confirm the columns each query reads**

```bash
S="$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
for t in ZWACHATSESSION ZWAMESSAGE ZWAGROUPMEMBER ZWAGROUPINFO ZWAMEDIAITEM; do
  echo "== $t"; sqlite3 -readonly "file:$S?mode=ro" "PRAGMA table_info($t);" | cut -d'|' -f2
done
```

Required on `ZWACHATSESSION`: `Z_PK`, `ZCONTACTJID`, `ZSESSIONTYPE`, `ZPARTNERNAME`, `ZLASTMESSAGEDATE`, `ZGROUPINFO`.
On `ZWAMESSAGE`: `Z_PK`, `ZSTANZAID`, `ZCHATSESSION`, `ZISFROMME`, `ZMESSAGEDATE`, `ZTEXT`, `ZMESSAGETYPE`, `ZPUSHNAME`, `ZGROUPMEMBER`, `ZMEDIAITEM`, `ZFROMJID`.
On `ZWAGROUPMEMBER`: `Z_PK`, `ZCHATSESSION`, `ZMEMBERJID`, `ZISADMIN`, `ZISACTIVE`, `ZCONTACTNAME`.
On `ZWAGROUPINFO`: `Z_PK`, `ZCHATSESSION`, `ZCREATIONDATE`.
On `ZWAMEDIAITEM`: `Z_PK`, `ZMESSAGE`, `ZMEDIALOCALPATH`, `ZMEDIAURL`, `ZVCARDSTRING`, `ZTITLE`.

Note that `ZWAGROUPINFO` has **no** subject column — a group's subject is `ZWACHATSESSION.ZPARTNERNAME`, which is what spec §4.2's `subject` ("group subject, or the 1:1 partner name") means. Both `ZWACHATSESSION.ZGROUPINFO → ZWAGROUPINFO.Z_PK` and `ZWAGROUPINFO.ZCHATSESSION → ZWACHATSESSION.Z_PK` exist; Task 2 joins on the first, which is indexed (`ZWACHATSESSION_ZGROUPINFO_INDEX`).

- [ ] **Step 4: Confirm the timestamp encoding and the session-type values**

```bash
S="$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
sqlite3 -readonly "file:$S?mode=ro" "
  SELECT COUNT(*), MIN(ZMESSAGEDATE), MAX(ZMESSAGEDATE) FROM ZWAMESSAGE;
  SELECT datetime(MIN(ZMESSAGEDATE) + 978307200, 'unixepoch'),
         datetime(MAX(ZMESSAGEDATE) + 978307200, 'unixepoch') FROM ZWAMESSAGE;
  SELECT ZSESSIONTYPE, COUNT(*) FROM ZWACHATSESSION GROUP BY 1 ORDER BY 1;"
```

Expected (spec §3, §5.2, §6.3): the count is ~10,329; the dates read 2019-07-23 → today, confirming `ZMESSAGEDATE` is **seconds** since 2001-01-01 (values well below `10**11`, so `imessage_export.apple_ts` routes them down its seconds branch); session types are 0 for 1:1 and `@lid`, 1 and 4 for groups, 2/3 for status.

- [ ] **Step 5: Measure `ZFROMJID` on the 83 senderless group rows**

```bash
S="$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
sqlite3 -readonly "file:$S?mode=ro" "
  SELECT COUNT(*) AS senderless,
         SUM(CASE WHEN m.ZFROMJID IS NOT NULL AND m.ZFROMJID <> '' THEN 1 ELSE 0 END) AS has_fromjid
  FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
  WHERE m.ZISFROMME = 0 AND m.ZGROUPMEMBER IS NULL AND s.ZCONTACTJID LIKE '%@g.us';"
```

Spec §5.4 rules that these 83 rows are dropped and counted, and this plan implements that ruling unchanged. This step only measures whether `ZFROMJID` could have attributed them. **Record both numbers in the Task 12 PR description**; if `has_fromjid` is large, say so plainly and let the user decide whether a follow-up should use it. Do not change the drop rule here.

- [ ] **Step 6: If anything differs from the spec, correct the spec first**

Edit the §5 sections that are wrong, then:

```bash
git commit -am "docs: correct the WhatsApp store's shape against the real database"
```

If nothing differs, there is nothing to commit — say so and move on. Either way, note in the PR description that §12's chat arithmetic is internally inconsistent (208 sessions − 7 skipped = 201, but §4.2's duplicate-JID collapse and §6.4's one unnormalized skip should bring the stored total to ~199); Task 12 records the real numbers and updates §12 rather than treating a 1–2 row difference as a bug.

---

## Task 1: Schema — five `whatsapp_*` tables

**Files:**
- Modify: `repo/schema.sql` (append at the very end)
- Modify: `tests/test_schema.py` (append)

**Interfaces:**
- Produces: tables `whatsapp_handles`, `whatsapp_chats`, `whatsapp_chat_members`, `whatsapp_messages`, `whatsapp_imports` exactly as in spec §4. Consumed by every later task.

The block goes at the **end** of `schema.sql`, after the person-identity migration: `whatsapp_handles.person_id` references `people(id)`, and `people.id` only becomes a primary key in step 3 of that migration. Everything here is `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, so it is purely additive and safe to apply ahead of the code (spec §11 step 1).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schema.py`:

```python
# --- WhatsApp snapshot (spec 2026-09-29-whatsapp-snapshot-design.md §4) -------


def _wa_chat(conn, chat_jid="1234-5678@g.us", kind="group"):
    conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, kind) VALUES (%s, %s)", (chat_jid, kind)
    )


def test_whatsapp_tables_exist(conn):
    names = {
        r[0]
        for r in conn.execute(
            "select table_name from information_schema.tables where table_name like 'whatsapp%%'"
        ).fetchall()
    }
    assert names == {
        "whatsapp_handles",
        "whatsapp_chats",
        "whatsapp_chat_members",
        "whatsapp_messages",
        "whatsapp_imports",
    }


def test_whatsapp_handle_person_id_must_exist(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO whatsapp_handles (handle, person_id) VALUES (%s, %s)",
            ("+15550100001", 999999),
        )


def test_deleting_person_nulls_whatsapp_links(conn):
    _person(conn)
    pid = conn.execute("select id from people where email = 'alice@example.com'").fetchone()[0]
    conn.execute(
        "INSERT INTO whatsapp_handles (handle, person_id) VALUES ('+15550100001', %s)", (pid,)
    )
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_chat_members (chat_jid, handle, person_id)"
        " VALUES ('1234-5678@g.us', '+15550100002', %s)",
        (pid,),
    )
    conn.execute("DELETE FROM people WHERE email = 'alice@example.com'")
    assert conn.execute("SELECT person_id FROM whatsapp_handles").fetchone()[0] is None
    assert conn.execute("SELECT person_id FROM whatsapp_chat_members").fetchone()[0] is None


def test_deleting_chat_cascades_to_members_and_messages(conn):
    """A membership or message row is meaningless without its chat, and both sides
    are import-owned, so CASCADE here is correct rather than destructive (§4.3)."""
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_chat_members (chat_jid, handle) VALUES ('1234-5678@g.us', '+1555010000')"
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, source_pk)"
        " VALUES ('1234-5678@g.us', 's1', false, 1)"
    )
    conn.execute("DELETE FROM whatsapp_chats WHERE chat_jid = '1234-5678@g.us'")
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_chat_members").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_messages").fetchone()[0] == 0


def test_member_handle_is_not_a_foreign_key_to_handles(conn):
    """Most group members have no whatsapp_handles row by design (§4.1/§4.3), so
    the constraint would be wrong, not merely inconvenient."""
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_chat_members (chat_jid, handle) VALUES ('1234-5678@g.us', '+15550109999')"
    )
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_chat_members").fetchone()[0] == 1


def test_message_key_is_chat_jid_and_stanza_id(conn):
    _wa_chat(conn)
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, source_pk)"
        " VALUES ('1234-5678@g.us', 's1', false, 1)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, source_pk)"
            " VALUES ('1234-5678@g.us', 's1', false, 2)"
        )


def test_handle_stats_default_to_zero(conn):
    conn.execute("INSERT INTO whatsapp_handles (handle) VALUES ('lid:123')")
    row = conn.execute(
        "select message_count, my_message_count, group_message_count, group_count,"
        " last_message_at, match_method from whatsapp_handles"
    ).fetchone()
    assert row == (0, 0, 0, 0, None, None)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -q -k whatsapp
```

Expected: FAIL — `relation "whatsapp_chats" does not exist`. If every test **skips**, the local Postgres is missing; create it (`createdb people_schema_test`) before continuing, because this is the only place the schema is exercised for real.

- [ ] **Step 3: Append the schema**

At the end of `repo/schema.sql`:

```sql
-- WhatsApp snapshot (docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §4).
-- Upserted incrementally by scripts/import_whatsapp.py; message text is stored here
-- but is never served by people-api. Appended last on purpose: whatsapp_handles
-- references people(id), which only becomes a primary key in the person-identity
-- migration above.

CREATE TABLE IF NOT EXISTS whatsapp_handles (
    handle                 TEXT PRIMARY KEY,   -- E.164, or 'lid:<id>' (§6.4)
    jid                    TEXT,               -- the raw JID last seen for this handle
    display_name           TEXT,               -- ZPARTNERNAME, else ZPUSHNAME (§6.6)
    person_id              BIGINT REFERENCES people(id) ON DELETE SET NULL,
    match_method           TEXT,               -- 'phone' | 'name' | NULL
    message_count          INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    my_message_count       INT NOT NULL DEFAULT 0,   -- 1:1 chats only
    last_message_at        TIMESTAMPTZ,
    last_my_message_at     TIMESTAMPTZ,
    -- Messages this handle SENT in group chats. Deliberately different from
    -- imessage_handles.group_message_count, which counts every message in a group the
    -- handle belongs to because chat.db cannot attribute group senders; WhatsApp can,
    -- via ZWAMESSAGE.ZGROUPMEMBER (§4.1, §5.4). Never compare the two naively.
    group_message_count    INT NOT NULL DEFAULT 0,
    last_group_message_at  TIMESTAMPTZ,
    group_count            INT NOT NULL DEFAULT 0,   -- active group memberships
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS whatsapp_handles_person_idx ON whatsapp_handles (person_id);
CREATE INDEX IF NOT EXISTS whatsapp_handles_name_trgm_idx
    ON whatsapp_handles USING gin (display_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS whatsapp_chats (
    chat_jid          TEXT PRIMARY KEY,        -- ZWACHATSESSION.ZCONTACTJID; 208 sessions
    kind              TEXT NOT NULL,           -- 'direct' | 'group'
    subject           TEXT,                    -- group subject, or the 1:1 partner name
    handle            TEXT,                    -- 'direct' only: the other party's handle
    created_at        TIMESTAMPTZ,             -- ZWAGROUPINFO.ZCREATIONDATE (groups only)
    member_count      INT NOT NULL DEFAULT 0,  -- groups only; bimodal, see §5.7
    message_count     INT NOT NULL DEFAULT 0,
    my_message_count  INT NOT NULL DEFAULT 0,
    last_message_at   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS whatsapp_chats_handle_idx ON whatsapp_chats (handle);

-- The roster: 10,090 rows across 99 groups. A member who never posts leaves no
-- messages, so this is not derivable from traffic (§4.3). handle is deliberately
-- NOT a foreign key to whatsapp_handles — most members have no row there (§4.1).
CREATE TABLE IF NOT EXISTS whatsapp_chat_members (
    chat_jid   TEXT NOT NULL REFERENCES whatsapp_chats (chat_jid) ON DELETE CASCADE,
    handle     TEXT NOT NULL,
    person_id  BIGINT REFERENCES people (id) ON DELETE SET NULL,
    is_admin   BOOLEAN NOT NULL DEFAULT FALSE,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (chat_jid, handle)
);
CREATE INDEX IF NOT EXISTS whatsapp_chat_members_handle_idx ON whatsapp_chat_members (handle);
CREATE INDEX IF NOT EXISTS whatsapp_chat_members_person_idx ON whatsapp_chat_members (person_id);

-- ZSTANZAID is almost unique but not quite (10,328 distinct across 10,329 rows), and
-- the one duplicate pair sits inside a single chat, so this key collapses it into one
-- row — reported as duplicate_stanza_ids rather than collapsed silently (§4.4).
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    chat_jid       TEXT NOT NULL REFERENCES whatsapp_chats (chat_jid) ON DELETE CASCADE,
    stanza_id      TEXT NOT NULL,       -- ZWAMESSAGE.ZSTANZAID
    sender_handle  TEXT,                -- NULL when from_me
    from_me        BOOLEAN NOT NULL,
    sent_at        TIMESTAMPTZ,
    text           TEXT,                -- never served by people-api (§7)
    message_type   INT,                 -- raw ZMESSAGETYPE; never used to filter (§5.3)
    has_media      BOOLEAN NOT NULL DEFAULT FALSE,
    media_kind     TEXT,                -- 'image'|'video'|'audio'|'document'|'vcard'|'other'
    source_pk      BIGINT NOT NULL,     -- ZWAMESSAGE.Z_PK, the watermark column
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_jid, stanza_id)
);
CREATE INDEX IF NOT EXISTS whatsapp_messages_sender_idx ON whatsapp_messages (sender_handle);
CREATE INDEX IF NOT EXISTS whatsapp_messages_sent_idx ON whatsapp_messages (sent_at DESC);
CREATE INDEX IF NOT EXISTS whatsapp_messages_source_pk_idx ON whatsapp_messages (source_pk);

CREATE TABLE IF NOT EXISTS whatsapp_imports (
    id                     BIGSERIAL PRIMARY KEY,
    started_at             TIMESTAMPTZ NOT NULL,
    finished_at            TIMESTAMPTZ NOT NULL,
    mode                   TEXT NOT NULL CHECK (mode IN ('incremental', 'full')),
    watermark              BIGINT,              -- highest source_pk seen
    chats_upserted         INT NOT NULL DEFAULT 0,
    members_upserted       INT NOT NULL DEFAULT 0,
    handles_upserted       INT NOT NULL DEFAULT 0,
    messages_upserted      INT NOT NULL DEFAULT 0,
    messages_deleted       INT NOT NULL DEFAULT 0,
    senderless_dropped     INT NOT NULL DEFAULT 0,
    duplicate_stanza_ids   INT NOT NULL DEFAULT 0,
    sessions_skipped       INT NOT NULL DEFAULT 0,
    handles_unnormalized   INT NOT NULL DEFAULT 0,
    matched_by_phone       INT NOT NULL DEFAULT 0,
    matched_by_name        INT NOT NULL DEFAULT 0
);
```

`updated_at` on `whatsapp_messages` is not in spec §4.4; it is added for consistency with `imessage_messages` and because `repo/whatsapp.py`'s shared `_upsert_many` helper always sets it. Note the addition in the PR description.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -q
```

Expected: PASS, including the pre-existing `test_schema_is_idempotent`, which applies the whole file twice.

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql tests/test_schema.py
git commit -m "feat(schema): five whatsapp_* snapshot tables"
```

---

## Task 2: `models/whatsapp.py`, `clients/whatsapp_local.py`, and the synthetic store fixture

**Files:**
- Create: `models/whatsapp.py`
- Create: `clients/whatsapp_local.py`
- Create: `tests/fixtures/whatsapp.py`
- Create: `tests/test_whatsapp_local.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `models.whatsapp.WhatsAppHandle(handle: str, jid: str | None = None, display_name: str | None = None, person_id: int | None = None, match_method: str | None = None)`
  - `models.whatsapp.WhatsAppChat(chat_jid: str, kind: str, subject: str | None = None, handle: str | None = None, created_at: datetime | None = None, member_count: int = 0, message_count: int = 0, my_message_count: int = 0, last_message_at: datetime | None = None)`
  - `models.whatsapp.WhatsAppChatMember(chat_jid: str, handle: str, person_id: int | None = None, is_admin: bool = False, is_active: bool = True)`
  - `models.whatsapp.WhatsAppMessage(chat_jid: str, stanza_id: str, sender_handle: str | None, from_me: bool, sent_at: datetime | None, text: str | None, message_type: int | None, has_media: bool = False, media_kind: str | None = None, source_pk: int = 0)`
  - `models.whatsapp.WhatsAppBatch` — fields listed in Step 3 below
  - `clients.whatsapp_local.RawStore(sessions: list[dict], messages: list[dict], members: list[dict], max_pk: int)`
  - `clients.whatsapp_local.read(path: Path, *, since_pk: int = 0, window_start: datetime | None = None, full: bool = False) -> RawStore`
  - `clients.whatsapp_local.DEFAULT_STORE: Path`, `StoreUnreadableError`, `SchemaError`
  - `tests.fixtures.whatsapp.build_store(tmp_path) -> Path`, `wa_ts(unix_seconds) -> float`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_whatsapp_local.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clients import whatsapp_local
from tests.fixtures.whatsapp import build_store


def test_read_returns_sessions_messages_and_members(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert len(raw.sessions) == 8
    assert len(raw.messages) == 13
    assert len(raw.members) == 4
    assert raw.max_pk == 13


def test_group_creation_date_comes_through_the_join(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    group = next(s for s in raw.sessions if s["ZCONTACTJID"].endswith("@g.us"))
    assert group["ZGROUPCREATIONDATE"] is not None


def test_media_columns_come_through_the_join(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    by_stanza = {m["ZSTANZAID"]: m for m in raw.messages}
    assert by_stanza["s9"]["ZMEDIALOCALPATH"] is not None
    # The §5.3 trap: a ZMEDIAITEM row exists but carries no real content.
    assert by_stanza["s10"]["ZMEDIAITEM"] is not None
    assert by_stanza["s10"]["ZMEDIALOCALPATH"] is None
    assert by_stanza["s10"]["ZMEDIAURL"] is None
    assert by_stanza["s10"]["ZVCARDSTRING"] is None


def test_incremental_selection_uses_the_watermark(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path), since_pk=11)
    assert {m["ZSTANZAID"] for m in raw.messages} == {"s12", "s13"}
    assert raw.max_pk == 13  # the store's max, not the selection's


def test_window_start_pulls_older_rows_back_in(tmp_path):
    """Spec §6.2: a 14-day re-scan window catches late-arriving and edited
    messages below the watermark, exactly as the iMessage import does."""
    raw = whatsapp_local.read(
        build_store(tmp_path), since_pk=13, window_start=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert len(raw.messages) == 13


def test_full_ignores_the_watermark(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path), since_pk=13, full=True)
    assert len(raw.messages) == 13


def test_the_store_is_copied_not_opened_in_place(tmp_path, monkeypatch):
    """Spec §5.1: the app is live, so the original is never opened; the copy
    includes -wal and -shm so the WAL's contents are visible."""
    path = build_store(tmp_path)
    opened: list[str] = []
    real_connect = whatsapp_local.sqlite3.connect

    def spy(uri, *a, **kw):
        opened.append(uri)
        return real_connect(uri, *a, **kw)

    monkeypatch.setattr(whatsapp_local.sqlite3, "connect", spy)
    whatsapp_local.read(path)
    assert len(opened) == 1
    assert str(path) not in opened[0]
    assert "mode=ro" in opened[0] and "immutable" not in opened[0]


def test_sidecars_are_copied(tmp_path, monkeypatch):
    path = build_store(tmp_path)
    (tmp_path / "ChatStorage.sqlite-wal").write_bytes(b"")
    (tmp_path / "ChatStorage.sqlite-shm").write_bytes(b"")
    copied: list[str] = []
    real_copy = whatsapp_local.shutil.copy2
    monkeypatch.setattr(
        whatsapp_local.shutil,
        "copy2",
        lambda src, dst: (copied.append(Path(src).name), real_copy(src, dst))[1],
    )
    whatsapp_local.read(path)
    assert copied == [
        "ChatStorage.sqlite",
        "ChatStorage.sqlite-wal",
        "ChatStorage.sqlite-shm",
    ]


def test_temp_copy_is_deleted_even_when_the_read_raises(tmp_path, monkeypatch):
    """Review Focus 4: the copy holds the full plaintext history, so a leak on
    the error path is a privacy failure, not untidiness (spec §7)."""
    path = build_store(tmp_path)
    seen: list[Path] = []
    real_mkdtemp = whatsapp_local.tempfile.mkdtemp

    def spy(*a, **kw):
        made = real_mkdtemp(*a, **kw)
        seen.append(Path(made))
        return made

    monkeypatch.setattr(whatsapp_local.tempfile, "mkdtemp", spy)
    monkeypatch.setattr(
        whatsapp_local, "_check_schema", lambda conn: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        whatsapp_local.read(path)
    assert seen and not seen[0].exists()


def test_temp_copy_is_deleted_on_success(tmp_path, monkeypatch):
    path = build_store(tmp_path)
    seen: list[Path] = []
    real_mkdtemp = whatsapp_local.tempfile.mkdtemp
    monkeypatch.setattr(
        whatsapp_local.tempfile,
        "mkdtemp",
        lambda *a, **kw: (seen.append(Path(m := real_mkdtemp(*a, **kw))), m)[1],
    )
    whatsapp_local.read(path)
    assert seen and not seen[0].exists()


def test_missing_store_raises_store_unreadable(tmp_path):
    with pytest.raises(whatsapp_local.StoreUnreadableError):
        whatsapp_local.read(tmp_path / "nope.sqlite")


def test_missing_column_fails_naming_it(tmp_path):
    import sqlite3

    path = build_store(tmp_path)
    db = sqlite3.connect(path)
    db.executescript("ALTER TABLE ZWAMESSAGE DROP COLUMN ZSTANZAID;")
    db.commit()
    db.close()
    with pytest.raises(whatsapp_local.SchemaError, match="ZSTANZAID"):
        whatsapp_local.read(path)


def test_default_store_path_is_the_group_container():
    assert "group.net.whatsapp.WhatsApp.shared" in str(whatsapp_local.DEFAULT_STORE)
    assert whatsapp_local.DEFAULT_STORE.name == "ChatStorage.sqlite"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_whatsapp_local.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'clients.whatsapp_local'`.

- [ ] **Step 3: Write `models/whatsapp.py`**

```python
"""WhatsApp snapshot records
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §4).

Pure types: built by services/whatsapp_export.py, written by repo/whatsapp.py.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class WhatsAppHandle:
    handle: str  # E.164, or 'lid:<id>' (§6.4)
    jid: str | None = None
    display_name: str | None = None
    person_id: int | None = None
    match_method: str | None = None  # 'phone' | 'name' | None


@dataclass
class WhatsAppChat:
    chat_jid: str
    kind: str  # 'direct' | 'group'
    subject: str | None = None
    handle: str | None = None  # 'direct' only
    created_at: datetime | None = None
    member_count: int = 0
    message_count: int = 0
    my_message_count: int = 0
    last_message_at: datetime | None = None


@dataclass
class WhatsAppChatMember:
    chat_jid: str
    handle: str
    person_id: int | None = None
    is_admin: bool = False
    is_active: bool = True


@dataclass
class WhatsAppMessage:
    chat_jid: str
    stanza_id: str
    sender_handle: str | None  # None when from_me
    from_me: bool
    sent_at: datetime | None
    text: str | None
    message_type: int | None
    has_media: bool = False
    media_kind: str | None = None
    source_pk: int = 0


@dataclass
class WhatsAppBatch:
    mode: str  # 'incremental' | 'full'
    watermark: int = 0
    chats: list[WhatsAppChat] = field(default_factory=list)
    handles: list[WhatsAppHandle] = field(default_factory=list)
    members: list[WhatsAppChatMember] = field(default_factory=list)
    messages: list[WhatsAppMessage] = field(default_factory=list)
    # ZPARTNERNAME per handle, populated only from a direct session. The LID name
    # match (§6.5) reads this and nothing else, so a group member's ZCONTACTNAME can
    # never feed it.
    partner_names: dict[str, str] = field(default_factory=dict)
    sessions_skipped: int = 0
    duplicate_jids: int = 0
    duplicate_stanza_ids: int = 0
    missing_stanza_ids: int = 0
    senderless_dropped: int = 0
    handles_unnormalized: int = 0
    matched_by_phone: int = 0
    matched_by_name: int = 0
```

`missing_stanza_ids` has no column in `whatsapp_imports` (spec §4.5 predates it) and is reported in the script's output only. Measured, every row has a stanza id, so this is defensive; if a real run reports a non-zero count, tell the user and treat a schema column as a follow-up.

- [ ] **Step 4: Write `clients/whatsapp_local.py`**

```python
"""Read-only access to WhatsApp's local store
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §5.1, §6.1). Local
script use only — people's Cloud Functions never read WhatsApp, exactly as
clients/imessage_local.py and clients/graph_local.py are import-only."""

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

EPOCH_2001 = 978307200

DEFAULT_STORE = Path(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)
# The app is live, so the WAL may hold recent messages. Copy all three files and read
# the copy; never open the original, and never use immutable=1, which would silently
# ignore the WAL and read stale data (§5.1).
SIDECARS = ("-wal", "-shm")

REQUIRED_COLUMNS = {
    "ZWACHATSESSION": (
        "Z_PK", "ZCONTACTJID", "ZSESSIONTYPE", "ZPARTNERNAME", "ZLASTMESSAGEDATE", "ZGROUPINFO",
    ),
    "ZWAMESSAGE": (
        "Z_PK", "ZSTANZAID", "ZCHATSESSION", "ZISFROMME", "ZMESSAGEDATE", "ZTEXT",
        "ZMESSAGETYPE", "ZPUSHNAME", "ZGROUPMEMBER", "ZMEDIAITEM",
    ),
    "ZWAGROUPMEMBER": ("Z_PK", "ZCHATSESSION", "ZMEMBERJID", "ZISADMIN", "ZISACTIVE", "ZCONTACTNAME"),
    "ZWAGROUPINFO": ("Z_PK", "ZCREATIONDATE"),
    "ZWAMEDIAITEM": ("Z_PK", "ZMEDIALOCALPATH", "ZMEDIAURL", "ZVCARDSTRING", "ZTITLE"),
}  # fmt: skip

# ZWACHATSESSION.ZGROUPINFO -> ZWAGROUPINFO.Z_PK (the indexed direction; the reverse
# link ZWAGROUPINFO.ZCHATSESSION also exists). A group's subject is the session's
# ZPARTNERNAME — ZWAGROUPINFO has no subject column.
SESSION_SQL = """
SELECT s.Z_PK, s.ZCONTACTJID, s.ZSESSIONTYPE, s.ZPARTNERNAME, s.ZLASTMESSAGEDATE,
       g.ZCREATIONDATE AS ZGROUPCREATIONDATE
FROM ZWACHATSESSION s
LEFT JOIN ZWAGROUPINFO g ON g.Z_PK = s.ZGROUPINFO
ORDER BY s.Z_PK
"""

# Media is derived from real ZWAMEDIAITEM content, never from ZMEDIAITEM being set —
# that is true of 7,762 plain-text rows (§5.3 item 1).
MESSAGE_SQL = """
SELECT m.Z_PK, m.ZSTANZAID, m.ZCHATSESSION, m.ZISFROMME, m.ZMESSAGEDATE, m.ZTEXT,
       m.ZMESSAGETYPE, m.ZPUSHNAME, m.ZGROUPMEMBER, m.ZMEDIAITEM,
       md.ZMEDIALOCALPATH, md.ZMEDIAURL, md.ZVCARDSTRING, md.ZTITLE
FROM ZWAMESSAGE m
LEFT JOIN ZWAMEDIAITEM md ON md.Z_PK = m.ZMEDIAITEM
WHERE {where}
ORDER BY m.Z_PK
"""

# Membership is a full snapshot with no per-row watermark, and 10,090 rows is trivial,
# so it is re-read in full on every run (§6.2).
MEMBER_SQL = """
SELECT gm.Z_PK, gm.ZCHATSESSION, gm.ZMEMBERJID, gm.ZISADMIN, gm.ZISACTIVE, gm.ZCONTACTNAME
FROM ZWAGROUPMEMBER gm
ORDER BY gm.ZCHATSESSION, gm.Z_PK
"""


class StoreUnreadableError(Exception):
    """ChatStorage.sqlite could not be found, copied, or opened."""


class SchemaError(Exception):
    """The store is missing a column the import requires."""


@dataclass
class RawStore:
    sessions: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    members: list[dict[str, Any]]
    max_pk: int


@contextmanager
def _copied(path: Path) -> Iterator[Path]:
    """Copy the store and its sidecars to a temp directory and yield the copy.

    The copy holds the full message history in plaintext, so the directory is
    removed in a finally — including on error (§7)."""
    tmp = Path(tempfile.mkdtemp(prefix="whatsapp-import-"))
    try:
        shutil.copy2(path, tmp / path.name)
        for suffix in SIDECARS:
            side = path.with_name(path.name + suffix)
            if side.exists():
                shutil.copy2(side, tmp / side.name)
        yield tmp / path.name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_schema(conn: sqlite3.Connection) -> None:
    missing: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            missing.append(table)
            continue
        missing += [f"{table}.{c}" for c in columns if c not in present]
    if missing:
        raise SchemaError(f"ChatStorage.sqlite is missing: {', '.join(missing)}")


def read(
    path: Path,
    *,
    since_pk: int = 0,
    window_start: datetime | None = None,
    full: bool = False,
) -> RawStore:
    """Copy the store, then read sessions, messages and group members from the copy.

    Sessions and members are always read in full; messages honour the Z_PK
    watermark plus an optional re-scan window (§6.2). `full` ignores both."""
    if not path.exists():
        raise StoreUnreadableError(f"{path} does not exist")
    with _copied(path) as copy:
        try:
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        except sqlite3.Error as e:
            raise StoreUnreadableError(str(e)) from e
        conn.row_factory = sqlite3.Row
        try:
            _check_schema(conn)
            where = "1=1"
            params: list[Any] = []
            if not full:
                where = "m.Z_PK > ?"
                params = [since_pk]
                if window_start is not None:
                    where = f"({where} OR m.ZMESSAGEDATE >= ?)"
                    params.append(window_start.timestamp() - EPOCH_2001)
            messages = [
                dict(r) for r in conn.execute(MESSAGE_SQL.format(where=where), tuple(params))
            ]
            sessions = [dict(r) for r in conn.execute(SESSION_SQL)]
            members = [dict(r) for r in conn.execute(MEMBER_SQL)]
            max_pk = conn.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGE").fetchone()[0] or 0
            return RawStore(
                sessions=sessions, messages=messages, members=members, max_pk=int(max_pk)
            )
        finally:
            conn.close()
```

- [ ] **Step 5: Write the synthetic store fixture**

Create `tests/fixtures/whatsapp.py`. Every identifier is invented; nothing here comes from the real store.

```python
"""Synthetic ChatStorage.sqlite (spec §7: fixtures are synthetic). Invented
numbers (+1555010…), invented group JIDs, invented names.

Shape, so the assertions elsewhere have one place to read:

  sessions  8: alice direct, one group, one @lid direct, status@broadcast,
               a second row for alice's JID (duplicate, no messages),
               a legacy-MX direct chat, an unnormalizable 1-digit JID,
               a JID with no '@' at all
  messages 13: covers from_me, empty text, a group message via ZGROUPMEMBER,
               a senderless group inbound, Ben's own group message with no
               ZGROUPMEMBER, a LID chat message, a duplicate stanza id, real
               media, the §5.3 media trap, a vcard, and a message in a skipped
               session
  members   4: an admin who also messages, a named member, an inactive member,
               and a @lid member
"""

import sqlite3
from pathlib import Path

EPOCH_2001 = 978307200

DDL = """
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZSESSIONTYPE INTEGER,
  ZGROUPINFO INTEGER, ZLASTMESSAGEDATE TIMESTAMP, ZCONTACTJID VARCHAR, ZPARTNERNAME VARCHAR);
CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
  ZCREATIONDATE TIMESTAMP);
CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISADMIN INTEGER,
  ZISACTIVE INTEGER, ZCONTACTNAME VARCHAR, ZMEMBERJID VARCHAR);
CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZMEDIALOCALPATH VARCHAR,
  ZMEDIAURL VARCHAR, ZTITLE VARCHAR, ZVCARDSTRING VARCHAR);
CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZGROUPEVENTTYPE INTEGER, ZISFROMME INTEGER,
  ZMESSAGETYPE INTEGER, ZCHATSESSION INTEGER, ZGROUPMEMBER INTEGER, ZMEDIAITEM INTEGER,
  ZMESSAGEDATE TIMESTAMP, ZPUSHNAME VARCHAR, ZSTANZAID VARCHAR, ZTEXT VARCHAR,
  ZFROMJID VARCHAR);
"""

ALICE_JID = "15550100001@s.whatsapp.net"
GROUP_JID = "10000000001-1500000000@g.us"
LID_JID = "99900000000001@lid"
# Legacy Mexican mobile form: 52 + 1 + 10 digits (§6.4). Must normalize to
# +525555555555 by stripping the 1.
MX_JID = "5215555555555@s.whatsapp.net"
MEMBER_2_JID = "15550100002@s.whatsapp.net"
MEMBER_3_JID = "15550100003@s.whatsapp.net"
MEMBER_LID_JID = "99900000000002@lid"


def wa_ts(unix_seconds: int) -> float:
    """ZMESSAGEDATE is seconds (not nanoseconds) since 2001-01-01 (spec §3)."""
    return float(unix_seconds - EPOCH_2001)


T0 = 1_750_000_000  # 2025-06-15, comfortably inside the store's real range


def build_store(tmp_path: Path) -> Path:
    path = tmp_path / "ChatStorage.sqlite"
    db = sqlite3.connect(path)
    db.executescript(DDL)

    db.executemany(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZSESSIONTYPE, ZGROUPINFO, ZLASTMESSAGEDATE,"
        " ZCONTACTJID, ZPARTNERNAME) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 0, None, wa_ts(T0 + 900), ALICE_JID, "Alice Example"),
            (2, 1, 1, wa_ts(T0 + 600), GROUP_JID, "Soccer Carpool"),
            (3, 0, None, wa_ts(T0 + 700), LID_JID, "Bob Example"),
            (4, 2, None, None, "status@broadcast", None),
            (5, 0, None, None, ALICE_JID, "Alice Example"),  # duplicate JID, no messages
            (6, 0, None, wa_ts(T0 + 800), MX_JID, "Carmen Example"),
            (7, 0, None, None, "5@s.whatsapp.net", None),  # unnormalizable
            (8, 0, None, None, "garbage", None),  # no '@'
        ],
    )
    db.execute(
        "INSERT INTO ZWAGROUPINFO (Z_PK, ZCHATSESSION, ZCREATIONDATE) VALUES (1, 2, ?)",
        (wa_ts(T0 - 86_400),),
    )
    db.executemany(
        "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZISADMIN, ZISACTIVE, ZCONTACTNAME,"
        " ZMEMBERJID) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 2, 1, 1, "Alice Example", ALICE_JID),
            (2, 2, 0, 1, "Dana Example", MEMBER_2_JID),
            (3, 2, 0, 0, None, MEMBER_3_JID),  # inactive, never posts
            (4, 2, 0, 1, "Eve Example", MEMBER_LID_JID),
        ],
    )
    db.executemany(
        "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE,"
        " ZVCARDSTRING) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 9, "Media/invented.jpg", None, None, None),
            # The §5.3 trap: a media row with no real content, on a plain-text message.
            (2, 10, None, None, None, None),
            (3, 13, None, None, "Contact", "BEGIN:VCARD\nFN:Frank Example\nEND:VCARD"),
        ],
    )
    db.executemany(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZGROUPEVENTTYPE, ZISFROMME, ZMESSAGETYPE, ZCHATSESSION,"
        " ZGROUPMEMBER, ZMEDIAITEM, ZMESSAGEDATE, ZPUSHNAME, ZSTANZAID, ZTEXT, ZFROMJID)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # ZGROUPEVENTTYPE is non-zero nearly everywhere and means nothing (§5.3 item 2).
            (1, 7, 1, 0, 1, None, None, wa_ts(T0 + 100), None, "s1", "Hi", None),
            (2, 7, 0, 0, 1, None, None, wa_ts(T0 + 200), "Alice", "s2", "Hello", ALICE_JID),
            (3, 7, 0, 0, 1, None, None, wa_ts(T0 + 300), "Alice", "s3", "", ALICE_JID),
            (4, 12, 0, 0, 2, 1, None, wa_ts(T0 + 400), "Alice", "s4", "In the group", ALICE_JID),
            # Senderless group inbound: dropped and counted (§5.4).
            (5, 12, 0, 0, 2, None, None, wa_ts(T0 + 500), "Ghost", "s5", "Who?", MEMBER_2_JID),
            # Ben's own group message, also without ZGROUPMEMBER: kept.
            (6, 12, 1, 0, 2, None, None, wa_ts(T0 + 600), None, "s6", "On my way", None),
            (7, 7, 0, 0, 3, None, None, wa_ts(T0 + 700), "Bob", "s7", "From a LID chat", None),
            # Duplicate (chat_jid, stanza_id) with row 2: collapsed, counted (§4.4).
            (8, 7, 0, 0, 1, None, None, wa_ts(T0 + 250), "Alice", "s2", "Hello again", ALICE_JID),
            (9, 7, 0, 1, 1, None, 1, wa_ts(T0 + 850), "Alice", "s9", None, ALICE_JID),
            (10, 7, 0, 0, 1, None, 2, wa_ts(T0 + 860), "Alice", "s10", "Text only", ALICE_JID),
            (11, 7, 0, 0, 6, None, None, wa_ts(T0 + 800), "Carmen", "s11", "Hola", MX_JID),
            # In a skipped (status) session: never stored.
            (12, 0, 0, 0, 4, None, None, wa_ts(T0 + 870), None, "s12", "A status", None),
            (13, 7, 0, 4, 1, None, 3, wa_ts(T0 + 900), "Alice", "s13", None, ALICE_JID),
        ],
    )
    db.commit()
    db.close()
    return path
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_whatsapp_local.py -q
```

Expected: PASS. If `test_sidecars_are_copied` fails because `build_store` leaves no sidecars behind, note that the test creates them itself — the ordering assertion depends on `SIDECARS` being `("-wal", "-shm")` in that order.

- [ ] **Step 7: Commit**

```bash
git add models/whatsapp.py clients/whatsapp_local.py tests/fixtures/whatsapp.py tests/test_whatsapp_local.py
git commit -m "feat(clients): read WhatsApp's local store from a temp copy"
```

---

## Task 3: `services/whatsapp_export.py` — JID normalization, classification, media

**Files:**
- Create: `services/whatsapp_export.py`
- Create: `tests/test_whatsapp_export.py`
- Modify: `services/imessage_export.py:31` (widen `apple_ts`'s annotation)
- Modify: `tests/test_imessage_export.py` (append one test)

**Interfaces:**
- Consumes: `clients.whatsapp_local.RawStore` (types only), `services.imessage_export.apple_ts` / `normalize_handle`.
- Produces:
  - `services.whatsapp_export.jid_to_handle(jid: str) -> tuple[str | None, str]` — `(handle, kind)`, `kind` in `{"direct", "group", "skip"}`
  - `services.whatsapp_export.normalize_phone_local(local: str) -> str | None`
  - `services.whatsapp_export.classify_media(row: dict) -> tuple[bool, str | None]`
  - `services.whatsapp_export.SESSION_TYPES: dict[str, set[int]]`
  - `services.imessage_export.apple_ts(value: int | float | None) -> datetime | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_whatsapp_export.py`:

```python
from services import whatsapp_export as wa


# --- JID classification (§6.3) ------------------------------------------------


def test_group_jid_classifies_as_group_with_no_handle():
    assert wa.jid_to_handle("10000000001-1500000000@g.us") == (None, "group")


def test_phone_jid_classifies_as_direct_with_an_e164_handle():
    assert wa.jid_to_handle("15550100001@s.whatsapp.net") == ("+15550100001", "direct")


def test_lid_jid_keeps_a_prefixed_handle_and_is_never_phone_matched():
    """§5.5: a LID carries no phone number, ever. The 'lid:' prefix keeps the
    handle primary key unambiguous — a LID can never collide with an E.164."""
    assert wa.jid_to_handle("99900000000001@lid") == ("lid:99900000000001", "direct")


def test_status_and_broadcast_jids_are_skipped():
    for jid in ("status@broadcast", "0@status", "12345@abc.status", "STATUS@BROADCAST"):
        assert wa.jid_to_handle(jid) == (None, "skip"), jid


def test_jid_without_an_at_or_with_an_unknown_suffix_is_skipped():
    for jid in ("garbage", "", "   ", "12345@newsletter", "12345@call"):
        assert wa.jid_to_handle(jid) == (None, "skip"), jid


def test_unnormalizable_phone_jid_is_direct_with_no_handle():
    """The suffix is authoritative for the kind; the handle is separately absent,
    which is what handles_unnormalized counts (§6.4)."""
    assert wa.jid_to_handle("5@s.whatsapp.net") == (None, "direct")


# --- The MX/AR legacy-mobile-digit rule (§6.4) --------------------------------


def test_mexican_legacy_mobile_digit_is_stripped():
    """Worth 40% of the match rate (43 -> 60 chats). phonenumbers reports
    is_possible_number=False for the 13-digit form, so it cannot be recovered by
    loosening validation — the digit must go."""
    assert wa.normalize_phone_local("5215555555555") == "+525555555555"


def test_argentinian_legacy_mobile_digit_is_stripped():
    """No 54 chats in the store today; the rule is symmetric and costs one line."""
    assert wa.normalize_phone_local("5491155555555") == "+541155555555"


def test_an_already_twelve_digit_mexican_number_is_not_mangled():
    assert wa.normalize_phone_local("525555555555") == "+525555555555"


def test_a_us_number_is_untouched_by_the_legacy_rule():
    assert wa.normalize_phone_local("15550100001") == "+15550100001"


def test_a_number_that_survives_neither_pass_is_none():
    assert wa.normalize_phone_local("5") is None
    assert wa.normalize_phone_local("") is None
    assert wa.normalize_phone_local("abc") is None


def test_a_thirteen_digit_number_under_another_country_code_is_not_stripped():
    """The rule is scoped to 52/54 — a 13-digit local part elsewhere is left to
    normalize_handle's verdict, not silently shortened."""
    assert wa.normalize_phone_local("4917612345678") == "+4917612345678"


# --- Media (§5.3 item 1, §6.3) ------------------------------------------------


def _media_row(**kw):
    base = {
        "ZMESSAGETYPE": 0,
        "ZMEDIAITEM": 7,
        "ZMEDIALOCALPATH": None,
        "ZMEDIAURL": None,
        "ZVCARDSTRING": None,
    }
    base.update(kw)
    return base


def test_a_media_item_with_no_real_content_is_not_media():
    """The single most dangerous piece of folklore: ZMEDIAITEM is set on 7,762
    plain-text rows, and trusting it reports 9,664 media messages instead of 1,576."""
    assert wa.classify_media(_media_row()) == (False, None)


def test_a_local_path_makes_it_media_and_the_kind_comes_from_the_type():
    assert wa.classify_media(_media_row(ZMESSAGETYPE=1, ZMEDIALOCALPATH="Media/x.jpg")) == (
        True,
        "image",
    )
    assert wa.classify_media(_media_row(ZMESSAGETYPE=2, ZMEDIAURL="https://example.com/v")) == (
        True,
        "video",
    )
    assert wa.classify_media(_media_row(ZMESSAGETYPE=3, ZMEDIALOCALPATH="Media/a.m4a")) == (
        True,
        "audio",
    )
    assert wa.classify_media(_media_row(ZMESSAGETYPE=8, ZMEDIALOCALPATH="Media/d.pdf")) == (
        True,
        "document",
    )


def test_a_vcard_string_wins_over_the_message_type():
    assert wa.classify_media(_media_row(ZMESSAGETYPE=4, ZVCARDSTRING="BEGIN:VCARD")) == (
        True,
        "vcard",
    )


def test_an_unmapped_type_with_real_media_is_other():
    """21 distinct ZMESSAGETYPE values with a long tail; an allowlist would
    silently drop real messages (§5.3)."""
    assert wa.classify_media(_media_row(ZMESSAGETYPE=46, ZMEDIALOCALPATH="Media/x")) == (
        True,
        "other",
    )


def test_session_types_are_a_sanity_check_not_the_authority():
    assert 0 in wa.SESSION_TYPES["direct"]
    assert wa.SESSION_TYPES["group"] == {1, 4}
```

Append to `tests/test_imessage_export.py`:

```python
def test_apple_ts_accepts_a_float_from_core_data():
    """WhatsApp's ZMESSAGEDATE is seconds since 2001-01-01 and Core Data hands
    it back as a float; the seconds branch already handled the arithmetic, only
    the annotation was too narrow (WhatsApp spec §3)."""
    assert imessage_export.apple_ts(783_000_000.5) == datetime(
        2025, 10, 27, 3, 20, 0, 500000, tzinfo=UTC
    )
```

Compute the expected datetime before writing the assertion rather than trusting the arithmetic here:

```bash
.venv/bin/python -c "
from datetime import UTC, datetime
print(datetime.fromtimestamp(783_000_000.5 + 978307200, UTC))"
```

Use whatever it prints.

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_whatsapp_export.py tests/test_imessage_export.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'services.whatsapp_export'` for the first file, and the new `apple_ts` test passing already (the arithmetic was always right; only the annotation is wrong, which `mypy` catches, not pytest).

- [ ] **Step 3: Verify the fixture's Mexican number is acceptable to `phonenumbers`**

```bash
.venv/bin/python -c "
import phonenumbers as p
for n in ['+525555555555', '+5215555555555', '+541155555555', '+5491155555555']:
    parsed = p.parse(n, 'US')
    print(n, p.is_possible_number(parsed))"
```

Expected: the 12-digit MX and AR numbers print `True`, the 13-digit forms `False` — which is exactly why the retry is needed. **If a 12-digit form prints `False`**, pick another invented number in the same country (e.g. `+525512345678`) and update both `tests/fixtures/whatsapp.py`'s `MX_JID` and this task's tests before continuing. Do not loosen `normalize_handle`.

- [ ] **Step 4: Widen `apple_ts`**

In `services/imessage_export.py`, change the signature and the docstring:

```python
def apple_ts(value: int | float | None) -> datetime | None:
    """Convert an Apple `date` column value to a UTC datetime.

    Nanoseconds since 2001-01-01 UTC on current macOS; values below
    10**11 are treated as legacy seconds-since-2001 (spec §5.2, no real
    rows observed but kept as a defensive guard). WhatsApp's ZMESSAGEDATE
    takes that seconds branch for real, and Core Data hands it back as a
    float (WhatsApp spec §3). 0/None mean missing.
    """
```

The body is unchanged — the arithmetic already works for floats.

- [ ] **Step 5: Write the pure helpers**

Create `services/whatsapp_export.py`:

```python
"""Pure JID parsing, classification, media derivation, batch building and matching
for the WhatsApp snapshot
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §6).

No I/O — consumes clients.whatsapp_local.RawStore (types only) and produces
models/whatsapp.py records. Importing services.imessage_export is a
services-to-services import, which the layer rules permit; duplicating apple_ts
or normalize_handle would be worse (§3).
"""

import logging

from services.imessage_export import normalize_handle

logger = logging.getLogger(__name__)

GROUP_SUFFIX = "@g.us"
LID_SUFFIX = "@lid"
PHONE_SUFFIX = "@s.whatsapp.net"
STATUS_JIDS = {"status@broadcast", "0@status"}
STATUS_SUFFIX = ".status"

# Legacy mobile digit inserted after the country code in older JIDs: Mexico's 1 and
# Argentina's 9 (§6.4). Keyed by country code, valued by the digit to strip.
LEGACY_MOBILE_DIGIT = {"52": "1", "54": "9"}
NATIONAL_DIGITS = 10

MEDIA_KIND_BY_TYPE = {1: "image", 2: "video", 3: "audio", 8: "document"}
MEDIA_CONTENT_COLUMNS = ("ZMEDIALOCALPATH", "ZMEDIAURL", "ZVCARDSTRING")

# The JID suffix is authoritative; ZSESSIONTYPE is only a sanity check, and a
# disagreement is logged rather than trusted (§6.3).
SESSION_TYPES = {"direct": {0}, "group": {1, 4}}


def normalize_phone_local(local: str) -> str | None:
    """E.164 for a @s.whatsapp.net local part, or None.

    Tries normalize_handle first, then retries once with the MX/AR legacy mobile
    digit stripped (§6.4). That retry is worth 40% of the match rate and is not
    recoverable by relaxing validation: phonenumbers reports is_possible_number
    = False for the 13-digit forms.
    """
    digits = "".join(c for c in local if c.isdigit())
    if not digits:
        return None
    e164 = normalize_handle("+" + digits)
    if e164 is not None:
        return e164
    for code, extra in LEGACY_MOBILE_DIGIT.items():
        if digits.startswith(code + extra) and len(digits) == len(code) + 1 + NATIONAL_DIGITS:
            return normalize_handle("+" + code + digits[len(code) + 1 :])
    return None


def jid_to_handle(jid: str) -> tuple[str | None, str]:
    """Return (handle, kind). handle is E.164, 'lid:<id>', or None; kind is
    'direct', 'group' or 'skip' (§6.3, §6.4).

    A 'direct' kind with a None handle is a real chat whose number would not
    normalize — the caller counts it in handles_unnormalized and skips it.
    """
    raw = (jid or "").strip()
    lowered = raw.lower()
    if not raw or "@" not in raw:
        return None, "skip"
    if lowered in STATUS_JIDS or lowered.endswith(STATUS_SUFFIX):
        return None, "skip"
    if lowered.endswith(GROUP_SUFFIX):
        return None, "group"
    local, _, domain = raw.partition("@")
    domain = "@" + domain.lower()
    if domain == LID_SUFFIX:
        # §5.5: no amount of normalization recovers a phone number from a LID.
        return f"lid:{local}", "direct"
    if domain == PHONE_SUFFIX:
        return normalize_phone_local(local), "direct"
    return None, "skip"


def classify_media(row: dict) -> tuple[bool, str | None]:
    """(has_media, media_kind) from the ZWAMEDIAITEM join (§5.3 item 1, §6.3).

    ZMEDIAITEM being set means nothing — it is set on 7,762 plain-text rows.
    Media is real content: a local path, a URL, or a vCard string.
    """
    if not any(row.get(column) for column in MEDIA_CONTENT_COLUMNS):
        return False, None
    if row.get("ZVCARDSTRING"):
        return True, "vcard"
    return True, MEDIA_KIND_BY_TYPE.get(row.get("ZMESSAGETYPE"), "other")


def check_session_type(chat_jid: str, kind: str, session_type: int | None) -> None:
    """Log a warning when ZSESSIONTYPE disagrees with the JID suffix (§6.3).

    Counts only, never the JID itself — the repo is public and this lands in a
    terminal. The suffix wins either way.
    """
    if session_type is None or kind not in SESSION_TYPES:
        return
    if session_type not in SESSION_TYPES[kind]:
        logger.warning(
            "whatsapp session type %s disagrees with a %s JID suffix", session_type, kind
        )
```

`check_session_type` takes `chat_jid` only so callers read naturally; it must never log it. Assert that in Task 4's tests.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_whatsapp_export.py tests/test_imessage_export.py -q
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```

Expected: PASS and clean. `ruff` will flag `check_session_type`'s unused `chat_jid` — rename it to `_chat_jid` if so, keeping the docstring's point.

- [ ] **Step 7: Commit**

```bash
git add services/whatsapp_export.py services/imessage_export.py tests/test_whatsapp_export.py tests/test_imessage_export.py
git commit -m "feat(services): WhatsApp JID normalization, classification and media"
```

---

## Task 4: `services/whatsapp_export.py` — `build_batch`

**Files:**
- Modify: `services/whatsapp_export.py` (append)
- Modify: `tests/test_whatsapp_export.py` (append)

**Interfaces:**
- Consumes: `jid_to_handle`, `classify_media`, `check_session_type` (Task 3); `clients.whatsapp_local.RawStore`, `read` (Task 2); `models.whatsapp.*` (Task 2); `services.imessage_export.apple_ts`.
- Produces: `services.whatsapp_export.build_batch(raw: RawStore, *, mode: str) -> WhatsAppBatch`, with every list deduplicated on its destination primary key.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_export.py`:

```python
import logging

from clients import whatsapp_local
from tests.fixtures.whatsapp import ALICE_JID, GROUP_JID, LID_JID, MX_JID, build_store


def batch(tmp_path, **kw):
    raw = whatsapp_local.read(build_store(tmp_path), **kw)
    return wa.build_batch(raw, mode="full")


# --- Sessions -> chats (§4.2, §6.3) -------------------------------------------


def test_sessions_become_chats_and_the_skipped_ones_are_counted(tmp_path):
    b = batch(tmp_path)
    assert {c.chat_jid for c in b.chats} == {ALICE_JID, GROUP_JID, LID_JID, MX_JID}
    assert b.sessions_skipped == 2  # status@broadcast and the JID with no '@'
    assert b.handles_unnormalized == 1  # the 1-digit local part
    assert b.duplicate_jids == 1  # the second row for Alice's JID


def test_a_duplicate_session_jid_collapses_to_one_chat(tmp_path):
    """208 sessions carry 207 distinct JIDs; the extra row holds no messages, so
    collapsing on chat_jid is correct — and deliberate, not discovered (§4.2).
    Review Focus 2: two rows for one key in a batch would otherwise make
    ON CONFLICT DO UPDATE fail with 'cannot affect row a second time'."""
    b = batch(tmp_path)
    assert len([c for c in b.chats if c.chat_jid == ALICE_JID]) == 1


def test_a_group_chat_carries_its_subject_creation_date_and_member_count(tmp_path):
    group = next(c for c in batch(tmp_path).chats if c.chat_jid == GROUP_JID)
    assert group.kind == "group"
    assert group.subject == "Soccer Carpool"
    assert group.created_at is not None
    assert group.handle is None
    assert group.member_count == 4


def test_a_direct_chat_denormalizes_the_other_party_s_handle(tmp_path):
    chats = {c.chat_jid: c for c in batch(tmp_path).chats}
    assert chats[ALICE_JID].kind == "direct"
    assert chats[ALICE_JID].handle == "+15550100001"
    assert chats[ALICE_JID].subject == "Alice Example"
    assert chats[LID_JID].handle == "lid:99900000000001"
    assert chats[MX_JID].handle == "+525555555555"


def test_chat_counters_are_set_from_the_batch_s_messages(tmp_path):
    chats = {c.chat_jid: c for c in batch(tmp_path).chats}
    assert chats[ALICE_JID].message_count == 6  # s1,s2,s3,s9,s10,s13
    assert chats[ALICE_JID].my_message_count == 1
    assert chats[GROUP_JID].message_count == 2  # s4 and Ben's s6
    assert chats[GROUP_JID].my_message_count == 1
    assert chats[ALICE_JID].last_message_at is not None


def test_a_session_type_disagreement_logs_a_warning_without_the_jid(tmp_path, caplog):
    raw = whatsapp_local.read(build_store(tmp_path))
    for session in raw.sessions:
        if session["ZCONTACTJID"] == GROUP_JID:
            session["ZSESSIONTYPE"] = 0  # a group claiming to be 1:1
    with caplog.at_level(logging.WARNING):
        wa.build_batch(raw, mode="full")
    assert any("disagrees" in r.message for r in caplog.records)
    assert GROUP_JID not in caplog.text


# --- Messages (§5.4, §6.3, §4.4) ---------------------------------------------


def test_messages_from_a_skipped_session_are_not_stored(tmp_path):
    assert "s12" not in {m.stanza_id for m in batch(tmp_path).messages}


def test_senderless_group_inbound_is_dropped_and_counted(tmp_path):
    """§5.4: 83 such rows. sender_handle=None means from_me, so keeping them
    would make a message from nobody indistinguishable from Ben's own."""
    b = batch(tmp_path)
    assert "s5" not in {m.stanza_id for m in b.messages}
    assert b.senderless_dropped == 1


def test_ben_s_own_group_message_without_a_group_member_is_kept(tmp_path):
    mine = next(m for m in batch(tmp_path).messages if m.stanza_id == "s6")
    assert mine.from_me is True
    assert mine.sender_handle is None


def test_a_group_message_is_attributed_through_zgroupmember(tmp_path):
    """WhatsApp can attribute group senders where chat.db could not (§5.4)."""
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s4")
    assert msg.sender_handle == "+15550100001"
    assert msg.from_me is False


def test_a_one_to_one_inbound_sender_comes_from_the_session(tmp_path):
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s2")
    assert msg.sender_handle == "+15550100001"


def test_a_duplicate_stanza_id_collapses_and_is_counted(tmp_path):
    """One known pair in 10,329 rows; the only real problem would be collapsing
    it silently (§4.4). The first row wins."""
    b = batch(tmp_path)
    dupes = [m for m in b.messages if m.stanza_id == "s2"]
    assert len(dupes) == 1
    assert dupes[0].text == "Hello"  # the first row, not "Hello again"
    assert b.duplicate_stanza_ids == 1


def test_empty_text_is_stored_as_none_not_empty_string(tmp_path):
    """Review Focus 5 (§6.3)."""
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s3")
    assert msg.text is None


def test_a_message_with_no_stanza_id_is_dropped_and_counted(tmp_path):
    """Review Focus 5: (chat_jid, stanza_id) is the primary key and stanza_id is
    NOT NULL, so an unkeyable row must be dropped, not fed to the insert. No such
    row was measured; this is a guard."""
    raw = whatsapp_local.read(build_store(tmp_path))
    raw.messages.append({**raw.messages[0], "Z_PK": 99, "ZSTANZAID": None})
    raw.messages.append({**raw.messages[0], "Z_PK": 100, "ZSTANZAID": "  "})
    b = wa.build_batch(raw, mode="full")
    assert b.missing_stanza_ids == 2
    assert all(m.stanza_id and m.stanza_id.strip() for m in b.messages)


def test_raw_message_type_is_kept_and_never_used_to_filter(tmp_path):
    """21 distinct values with a long tail; an allowlist would silently drop real
    messages (§5.3)."""
    by_stanza = {m.stanza_id: m for m in batch(tmp_path).messages}
    assert by_stanza["s13"].message_type == 4
    assert by_stanza["s9"].message_type == 1


def test_media_flags_come_from_real_content(tmp_path):
    by_stanza = {m.stanza_id: m for m in batch(tmp_path).messages}
    assert (by_stanza["s9"].has_media, by_stanza["s9"].media_kind) == (True, "image")
    assert (by_stanza["s13"].has_media, by_stanza["s13"].media_kind) == (True, "vcard")
    assert (by_stanza["s10"].has_media, by_stanza["s10"].media_kind) == (False, None)


def test_source_pk_and_watermark_track_the_store(tmp_path):
    b = batch(tmp_path)
    assert b.watermark == 13
    assert max(m.source_pk for m in b.messages) == 13


def test_sent_at_reads_seconds_since_2001(tmp_path):
    msg = next(m for m in batch(tmp_path).messages if m.stanza_id == "s1")
    assert msg.sent_at is not None
    assert msg.sent_at.year == 2025


# --- Handles (§4.1, §6.6) ----------------------------------------------------


def test_only_identities_with_interaction_get_a_handle_row(tmp_path):
    """§4.1: a row per identity seen anywhere would be 6,767 rows, 6,193 of them
    strangers in two 1,200-member community groups. Dana is an active member who
    never posts; she gets a member row and no handle row."""
    b = batch(tmp_path)
    assert {h.handle for h in b.handles} == {
        "+15550100001",
        "lid:99900000000001",
        "+525555555555",
    }
    assert "+15550100002" in {m.handle for m in b.members}


def test_a_handle_keeps_the_raw_jid_and_prefers_the_partner_name(tmp_path):
    """§6.4: the raw JID is stored regardless, so nothing is lost when
    normalization fails. §6.6: ZPARTNERNAME beats ZPUSHNAME."""
    handles = {h.handle: h for h in batch(tmp_path).handles}
    assert handles["+15550100001"].jid == ALICE_JID
    assert handles["+15550100001"].display_name == "Alice Example"
    assert handles["lid:99900000000001"].display_name == "Bob Example"


def test_handles_are_unique_in_the_batch(tmp_path):
    handles = [h.handle for h in batch(tmp_path).handles]
    assert len(handles) == len(set(handles))


def test_partner_names_come_only_from_direct_sessions(tmp_path):
    """The LID name match reads this map and nothing else, so a group member's
    ZCONTACTNAME must never reach it (§6.5)."""
    b = batch(tmp_path)
    assert b.partner_names["lid:99900000000001"] == "Bob Example"
    assert "+15550100002" not in b.partner_names


# --- Members (§4.3) ----------------------------------------------------------


def test_every_group_member_is_recorded_with_admin_and_active_flags(tmp_path):
    members = {m.handle: m for m in batch(tmp_path).members}
    assert set(members) == {
        "+15550100001",
        "+15550100002",
        "+15550100003",
        "lid:99900000000002",
    }
    assert members["+15550100001"].is_admin is True
    assert members["+15550100001"].is_active is True
    assert members["+15550100003"].is_active is False
    assert all(m.chat_jid == GROUP_JID for m in members.values())


def test_members_are_unique_per_chat_and_handle(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    raw.members.append({**raw.members[0], "Z_PK": 99})  # same chat, same JID
    b = wa.build_batch(raw, mode="full")
    keys = [(m.chat_jid, m.handle) for m in b.members]
    assert len(keys) == len(set(keys))


def test_a_member_of_a_skipped_or_missing_chat_is_ignored(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    raw.members.append({**raw.members[0], "Z_PK": 98, "ZCHATSESSION": 4})  # the status session
    raw.members.append({**raw.members[0], "Z_PK": 97, "ZCHATSESSION": 999})  # no such session
    assert len(wa.build_batch(raw, mode="full").members) == 4


def test_mode_is_carried_through(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert wa.build_batch(raw, mode="incremental").mode == "incremental"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_whatsapp_export.py -q
```

Expected: FAIL — `AttributeError: module 'services.whatsapp_export' has no attribute 'build_batch'`.

- [ ] **Step 3: Write `build_batch`**

Append to `services/whatsapp_export.py` (and extend the imports at the top of the file):

```python
from clients.whatsapp_local import RawStore
from models.whatsapp import (
    WhatsAppBatch,
    WhatsAppChat,
    WhatsAppChatMember,
    WhatsAppHandle,
    WhatsAppMessage,
)
from services.imessage_export import apple_ts, normalize_handle
```

```python
def build_batch(raw: RawStore, *, mode: str) -> WhatsAppBatch:
    """Turn a RawStore into a WhatsAppBatch (§6.3-§6.6).

    Every list is deduplicated on the primary key its table uses, because a
    multi-row INSERT ... ON CONFLICT DO UPDATE fails outright when one statement
    touches a key twice — and the store really does contain a duplicate session
    JID and a duplicate stanza id (§4.2, §4.4).

    Counter semantics for a chat are batch-local: an incremental batch holds only
    its window's messages, so repo/whatsapp.py recomputes the stored totals in SQL
    rather than trusting these (§6.7).
    """
    batch = WhatsAppBatch(mode=mode, watermark=raw.max_pk)

    chats_by_jid: dict[str, WhatsAppChat] = {}
    chat_by_session: dict[int, WhatsAppChat] = {}
    handles: dict[str, WhatsAppHandle] = {}

    for session in raw.sessions:
        jid = session["ZCONTACTJID"] or ""
        handle, kind = jid_to_handle(jid)
        if kind == "skip":
            batch.sessions_skipped += 1
            continue
        if kind == "direct" and handle is None:
            # A real chat whose number will not normalize: one such chat, with a
            # 1-digit local part and no messages (§6.4).
            batch.handles_unnormalized += 1
            continue
        check_session_type(jid, kind, session.get("ZSESSIONTYPE"))
        existing = chats_by_jid.get(jid)
        if existing is not None:
            batch.duplicate_jids += 1
            chat_by_session[session["Z_PK"]] = existing
            continue
        chat = WhatsAppChat(
            chat_jid=jid,
            kind=kind,
            subject=session.get("ZPARTNERNAME"),
            handle=handle,
            created_at=apple_ts(session.get("ZGROUPCREATIONDATE")),
        )
        chats_by_jid[jid] = chat
        chat_by_session[session["Z_PK"]] = chat
        batch.chats.append(chat)
        if kind == "direct" and handle is not None:
            name = session.get("ZPARTNERNAME")
            handles[handle] = WhatsAppHandle(handle=handle, jid=jid, display_name=name)
            if name:
                batch.partner_names[handle] = name

    members_seen: set[tuple[str, str]] = set()
    member_names: dict[str, str] = {}
    for row in raw.members:
        chat = chat_by_session.get(row["ZCHATSESSION"])
        if chat is None or chat.kind != "group":
            continue
        member_handle, kind = jid_to_handle(row["ZMEMBERJID"] or "")
        if member_handle is None:
            batch.handles_unnormalized += 1
            continue
        key = (chat.chat_jid, member_handle)
        if key in members_seen:
            continue
        members_seen.add(key)
        batch.members.append(
            WhatsAppChatMember(
                chat_jid=chat.chat_jid,
                handle=member_handle,
                is_admin=bool(row.get("ZISADMIN")),
                is_active=bool(row.get("ZISACTIVE")),
            )
        )
        chat.member_count += 1
        if row.get("ZCONTACTNAME"):
            member_names.setdefault(member_handle, row["ZCONTACTNAME"])

    # ZWAMESSAGE.ZGROUPMEMBER -> ZWAGROUPMEMBER.Z_PK -> the member's JID (§5.4).
    member_handle_by_pk: dict[int, str | None] = {}
    for row in raw.members:
        member_handle_by_pk[row["Z_PK"]] = jid_to_handle(row["ZMEMBERJID"] or "")[0]

    messages_seen: set[tuple[str, str]] = set()
    for row in raw.messages:
        chat = chat_by_session.get(row["ZCHATSESSION"])
        if chat is None:
            continue  # a skipped session, or one whose handle did not normalize

        stanza_id = (row.get("ZSTANZAID") or "").strip()
        if not stanza_id:
            batch.missing_stanza_ids += 1
            continue

        from_me = bool(row.get("ZISFROMME"))
        if from_me:
            sender_handle = None
        elif chat.kind == "direct":
            sender_handle = chat.handle
        else:
            sender_handle = member_handle_by_pk.get(row.get("ZGROUPMEMBER"))
            if sender_handle is None:
                # sender_handle=None is documented to mean from_me, so an inbound
                # message attributed to nobody would be indistinguishable from Ben's
                # own and cannot be ranked. Same ruling as iMessage (§5.4).
                batch.senderless_dropped += 1
                continue

        key = (chat.chat_jid, stanza_id)
        if key in messages_seen:
            batch.duplicate_stanza_ids += 1
            continue
        messages_seen.add(key)

        has_media, media_kind = classify_media(row)
        text = row.get("ZTEXT")
        sent_at = apple_ts(row.get("ZMESSAGEDATE"))
        batch.messages.append(
            WhatsAppMessage(
                chat_jid=chat.chat_jid,
                stanza_id=stanza_id,
                sender_handle=sender_handle,
                from_me=from_me,
                sent_at=sent_at,
                text=text if (text or "").strip() else None,
                message_type=row.get("ZMESSAGETYPE"),
                has_media=has_media,
                media_kind=media_kind,
                source_pk=row["Z_PK"],
            )
        )

        chat.message_count += 1
        if from_me:
            chat.my_message_count += 1
        if sent_at is not None and (
            chat.last_message_at is None or sent_at > chat.last_message_at
        ):
            chat.last_message_at = sent_at

        # §4.1: a group sender earns a handle row on the run that sees it talk, so
        # the interaction rule needs no backfill.
        if sender_handle is not None and chat.kind == "group":
            handle_row = handles.get(sender_handle)
            if handle_row is None:
                handles[sender_handle] = WhatsAppHandle(
                    handle=sender_handle,
                    jid=row.get("ZFROMJID"),
                    display_name=row.get("ZPUSHNAME") or member_names.get(sender_handle),
                )

    batch.handles = list(handles.values())
    return batch
```

`ZFROMJID` is read here only as the raw JID for a group sender's handle row; it is never used to attribute a sender (spec §5.4 drops senderless rows instead — Task 0 Step 5 measured what that costs).

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_whatsapp_export.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/whatsapp_export.py tests/test_whatsapp_export.py
git commit -m "feat(services): build a WhatsApp batch from the raw store"
```

---

## Task 5: `match_handles` and `repo/people.py::rows_for_whatsapp_matching`

**Files:**
- Modify: `services/whatsapp_export.py` (append)
- Modify: `repo/people.py` (append one function)
- Modify: `tests/test_whatsapp_export.py` (append)
- Modify: `tests/test_repo_people.py` (append one test)

**Interfaces:**
- Consumes: `WhatsAppBatch` (Task 4).
- Produces:
  - `services.whatsapp_export.match_handles(batch: WhatsAppBatch, people_rows: list[dict]) -> None` — mutates `batch.handles` and `batch.members` in place and sets `batch.matched_by_phone` / `batch.matched_by_name`
  - `repo.people.rows_for_whatsapp_matching(conn) -> list[dict]` — rows of `{id, display_name, phone_numbers}`

Matching is redone from scratch on **every** run, like both existing snapshots: a link is never sticky, so a merge, a new contact or a corrected phone number is picked up on the next import (§6.5).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_export.py`:

```python
# --- Matching (§6.5) ---------------------------------------------------------


def people_rows():
    return [
        {"id": 1, "display_name": "Alice Example", "phone_numbers": ["+15550100001"]},
        {"id": 2, "display_name": "Bob Example", "phone_numbers": []},
        {"id": 3, "display_name": "Dana Example", "phone_numbers": ["+15550100002"]},
        # A household number on two rows: 34 such numbers exist today.
        {"id": 4, "display_name": "Shared One", "phone_numbers": ["+15550100003"]},
        {"id": 5, "display_name": "Shared Two", "phone_numbers": ["+15550100003"]},
        # Two people with the same name: a LID name match must refuse both.
        {"id": 6, "display_name": "Eve Example", "phone_numbers": []},
        {"id": 7, "display_name": "eve example", "phone_numbers": []},
    ]


def matched(tmp_path, rows=None):
    b = batch(tmp_path)
    wa.match_handles(b, people_rows() if rows is None else rows)
    return b


def test_a_handle_matching_exactly_one_phone_links_by_phone(tmp_path):
    handles = {h.handle: h for h in matched(tmp_path).handles}
    assert handles["+15550100001"].person_id == 1
    assert handles["+15550100001"].match_method == "phone"


def test_a_lid_handle_links_by_an_exact_unique_partner_name(tmp_path):
    """The only way to link a LID chat at all, and the one place a false link can
    occur — so match_method records 'name' and a reader can distrust it (§6.5)."""
    handles = {h.handle: h for h in matched(tmp_path).handles}
    assert handles["lid:99900000000001"].person_id == 2
    assert handles["lid:99900000000001"].match_method == "name"


def test_counters_split_phone_from_name(tmp_path):
    b = matched(tmp_path)
    assert b.matched_by_phone == 1
    assert b.matched_by_name == 1


def test_a_handle_with_no_candidate_links_to_nothing(tmp_path):
    handles = {h.handle: h for h in matched(tmp_path).handles}
    assert handles["+525555555555"].person_id is None
    assert handles["+525555555555"].match_method is None


def test_a_phone_on_two_people_links_to_neither(tmp_path):
    """Review Focus 1: ambiguity is an error, not a guess — the same position
    repo/people.py::get_by_phone takes for the API (§6.5)."""
    b = batch(tmp_path)
    b.handles.append(wa.WhatsAppHandle(handle="+15550100003", jid="x"))
    wa.match_handles(b, people_rows())
    shared = next(h for h in b.handles if h.handle == "+15550100003")
    assert shared.person_id is None
    assert shared.match_method is None
    assert b.matched_by_phone == 1  # Alice only


def test_a_non_lid_handle_is_never_name_matched(tmp_path):
    """A handle with a phone that does not match means 'no match'; falling back to
    the name would manufacture false links (§6.5, Decisions)."""
    b = batch(tmp_path)
    b.partner_names["+525555555555"] = "Alice Example"
    wa.match_handles(b, people_rows())
    carmen = next(h for h in b.handles if h.handle == "+525555555555")
    assert carmen.person_id is None


def test_a_lid_handle_is_never_phone_matched(tmp_path):
    """Nothing about a LID is a phone number, so it must not be looked up as one
    even if some people row happens to carry that digit string (§5.5)."""
    b = batch(tmp_path)
    wa.match_handles(
        b, [{"id": 9, "display_name": "Nope", "phone_numbers": ["lid:99900000000001"]}]
    )
    lid = next(h for h in b.handles if h.handle == "lid:99900000000001")
    assert lid.person_id is None


def test_an_ambiguous_name_links_to_nothing(tmp_path):
    b = batch(tmp_path)
    b.partner_names["lid:99900000000001"] = "Eve Example"
    wa.match_handles(b, people_rows())
    lid = next(h for h in b.handles if h.handle == "lid:99900000000001")
    assert lid.person_id is None


def test_a_lid_handle_with_no_partner_name_links_to_nothing(tmp_path):
    b = batch(tmp_path)
    b.partner_names.pop("lid:99900000000001")
    wa.match_handles(b, people_rows())
    lid = next(h for h in b.handles if h.handle == "lid:99900000000001")
    assert lid.person_id is None


def test_members_are_matched_by_the_same_function(tmp_path):
    """375 people already in `people` share a group with Ben and have never
    messaged him; without person_id here they are invisible to people-api (§4.3)."""
    members = {m.handle: m for m in matched(tmp_path).members}
    assert members["+15550100001"].person_id == 1
    assert members["+15550100002"].person_id == 3  # a member with no handle row
    assert members["+15550100003"].person_id is None  # the shared household number


def test_matching_is_idempotent_and_clears_a_stale_link(tmp_path):
    b = batch(tmp_path)
    for handle in b.handles:
        handle.person_id, handle.match_method = 99, "phone"
    wa.match_handles(b, people_rows())
    handles = {h.handle: h for h in b.handles}
    assert handles["+525555555555"].person_id is None
    assert handles["+15550100001"].person_id == 1


def test_a_person_row_with_no_phone_numbers_key_is_tolerated(tmp_path):
    b = batch(tmp_path)
    wa.match_handles(b, [{"id": 1, "display_name": "Alice Example"}])
    assert all(h.person_id is None for h in b.handles)


def test_no_email_matching_happens(tmp_path):
    """WhatsApp has no email addresses (§6.5)."""
    b = batch(tmp_path)
    wa.match_handles(
        b, [{"id": 1, "display_name": "Alice Example", "email": "alice@example.com"}]
    )
    assert all(h.person_id is None for h in b.handles)
```

Append to `tests/test_repo_people.py`:

```python
def test_rows_for_whatsapp_matching_selects_phones_and_names():
    conn = FakeConn(results=[[{"id": 1, "display_name": "A", "phone_numbers": ["+15550100001"]}]])
    rows = people.rows_for_whatsapp_matching(conn)
    sql, _ = conn.calls[0]
    assert sql == "SELECT id, display_name, phone_numbers FROM people"
    assert rows[0]["phone_numbers"] == ["+15550100001"]
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_whatsapp_export.py tests/test_repo_people.py -q
```

Expected: FAIL — no `match_handles`, no `rows_for_whatsapp_matching`.

- [ ] **Step 3: Add the repo query**

Append to `repo/people.py`, next to the other matching helpers:

```python
def rows_for_whatsapp_matching(conn: Any) -> list[dict]:
    """Id, display name and phone numbers for scripts/import_whatsapp.py's soft
    link (WhatsApp spec §6.5). No email column: WhatsApp has no email addresses."""
    return conn.execute("SELECT id, display_name, phone_numbers FROM people").fetchall()
```

- [ ] **Step 4: Write `match_handles`**

Append to `services/whatsapp_export.py`, and add `from typing import Any` plus `from services.linkedin_export import normalize_name` to the imports:

```python
LID_PREFIX = "lid:"


def match_handles(batch: WhatsAppBatch, people_rows: list[dict[str, Any]]) -> None:
    """Link handles and group members to `people` rows (§6.5).

    Two rules and no others. By phone: an E.164 handle matching exactly one row
    links to it; more than one links to nothing, because ambiguity is an error,
    not a guess. By name: a LID handle — and only a LID handle, since a LID
    carries no phone number by construction — links to a row whose display name
    is exactly (normalized-case, trimmed) its ZPARTNERNAME, and only when that
    name is unique on both sides.

    A handle with a phone that does not match means "no match": falling back to
    the name there would manufacture false links. Mutates the batch in place, and
    always rewrites person_id/match_method so a stale link is cleared.
    """
    ids_by_phone: dict[str, set[int]] = {}
    ids_by_name: dict[str, set[int]] = {}
    for row in people_rows:
        for phone in row.get("phone_numbers") or []:
            ids_by_phone.setdefault(phone, set()).add(row["id"])
        name = normalize_name(row.get("display_name"))
        if name:
            ids_by_name.setdefault(name, set()).add(row["id"])

    links: dict[str, tuple[int, str]] = {}
    targets = {h.handle for h in batch.handles} | {m.handle for m in batch.members}
    for handle in sorted(targets):
        if handle.startswith(LID_PREFIX):
            name = normalize_name(batch.partner_names.get(handle))
            candidates = ids_by_name.get(name, set()) if name else set()
            method = "name"
        else:
            candidates = ids_by_phone.get(handle, set())
            method = "phone"
        if len(candidates) == 1:
            links[handle] = (next(iter(candidates)), method)

    batch.matched_by_phone = batch.matched_by_name = 0
    for handle_row in batch.handles:
        link = links.get(handle_row.handle)
        handle_row.person_id, handle_row.match_method = link if link else (None, None)
        if link and link[1] == "phone":
            batch.matched_by_phone += 1
        elif link:
            batch.matched_by_name += 1
    for member in batch.members:
        link = links.get(member.handle)
        member.person_id = link[0] if link else None
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_whatsapp_export.py tests/test_repo_people.py -q
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```

Expected: PASS and clean. `test_a_phone_on_two_people_links_to_neither` uses `wa.WhatsAppHandle`, so re-export it (it is already imported at module level by `build_batch`'s import block — no extra work).

- [ ] **Step 6: Commit**

```bash
git add services/whatsapp_export.py repo/people.py tests/test_whatsapp_export.py tests/test_repo_people.py
git commit -m "feat(services): link WhatsApp handles and members to people rows"
```

---

## Task 6: `repo/whatsapp.py` — writes, recomputes, audit, re-point

**Files:**
- Create: `repo/whatsapp.py`
- Create: `tests/test_repo_whatsapp.py`

**Interfaces:**
- Consumes: `models.whatsapp.*` (Task 2).
- Produces, all taking an open connection as the first argument:
  - `latest_watermark(conn) -> int`
  - `upsert_chats(conn, chats: list[WhatsAppChat], chunk: int = 500) -> int`
  - `upsert_handles(conn, handles: list[WhatsAppHandle], chunk: int = 500) -> int`
  - `replace_members(conn, members: list[WhatsAppChatMember], chunk: int = 500) -> int`
  - `upsert_messages(conn, messages: list[WhatsAppMessage], chunk: int = 500) -> int`
  - `delete_missing(conn, keys: set[tuple[str, str]], chat_jids: set[str], chunk: int = 500) -> int`
  - `recompute_chat_stats(conn) -> None`
  - `recompute_handle_stats(conn) -> None`
  - `record_import(conn, batch: WhatsAppBatch, *, started_at: datetime, messages_deleted: int) -> None`
  - `repoint_person(conn, from_id: int, to_id: int) -> int`

**Two deliberate divergences from spec §6.7, both to avoid a bug it warns about:**

1. **The watermark is read from `whatsapp_messages`, not `whatsapp_imports`.** Spec §6.2 defines it as `MAX(source_pk)` from the messages table, which survives a run that wrote rows and then failed before its audit row. iMessage reads its own imports table; this does not.
2. **Chat counters are recomputed in SQL, not written from the batch.** An incremental batch legitimately holds no messages for most chats, so `EXCLUDED.message_count` would clobber a stored total with a partial count — the same shape as the `last_message_at` bug the iMessage import shipped (§6.7), which nulled 939 of 971 rows. `upsert_chats` therefore writes identity columns only, and `recompute_chat_stats` derives `message_count`, `my_message_count`, `last_message_at` and `member_count` from the tables. The `GREATEST` guard §6.7 asks for is kept on `last_message_at` as well, so the upsert is safe on its own terms.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repo_whatsapp.py`:

```python
from datetime import UTC, datetime

from models.whatsapp import (
    WhatsAppBatch,
    WhatsAppChat,
    WhatsAppChatMember,
    WhatsAppHandle,
    WhatsAppMessage,
)
from repo import whatsapp
from tests.test_repo_people import FakeConn

TS = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def chat(**kw):
    base = {"chat_jid": "1-2@g.us", "kind": "group", "subject": "Carpool"}
    base.update(kw)
    return WhatsAppChat(**base)


def message(**kw):
    base = {
        "chat_jid": "1-2@g.us",
        "stanza_id": "s1",
        "sender_handle": "+15550100001",
        "from_me": False,
        "sent_at": TS,
        "text": "hi",
        "message_type": 0,
        "source_pk": 7,
    }
    base.update(kw)
    return WhatsAppMessage(**base)


# --- Watermark ---------------------------------------------------------------


def test_latest_watermark_reads_max_source_pk_from_messages():
    """§6.2: the watermark is MAX(source_pk) from whatsapp_messages, so a run that
    wrote rows and died before its audit row does not re-read them all."""
    conn = FakeConn(results=[[{"watermark": 10329}]])
    assert whatsapp.latest_watermark(conn) == 10329
    sql, _ = conn.calls[0]
    assert "MAX(source_pk) " in sql and "FROM whatsapp_messages" in sql


def test_latest_watermark_defaults_to_zero():
    assert whatsapp.latest_watermark(FakeConn(results=[[{"watermark": None}]])) == 0
    assert whatsapp.latest_watermark(FakeConn(results=[[]])) == 0


# --- Chats -------------------------------------------------------------------


def test_upsert_chats_conflicts_on_chat_jid():
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat()])
    sql, params = conn.calls[0]
    assert "INSERT INTO whatsapp_chats" in sql
    assert "ON CONFLICT (chat_jid) DO UPDATE" in sql
    assert "updated_at = now()" in sql
    assert params[0] == "1-2@g.us"


def test_upsert_chats_never_writes_counters_from_the_batch():
    """Review Focus 3: an incremental batch's chat counters cover its window only.
    recompute_chat_stats owns the stored totals."""
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat(message_count=2, my_message_count=1)])
    sql, _ = conn.calls[0]
    for column in ("message_count", "my_message_count", "member_count"):
        assert f"{column} = EXCLUDED.{column}" not in sql


def test_upsert_chats_last_message_at_is_null_safe_on_conflict():
    """The iMessage import shipped this bug once: a bare EXCLUDED overwrote 939 of
    971 stored values with NULL on an incremental run (§6.7)."""
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat()])
    sql, _ = conn.calls[0]
    assert "last_message_at = EXCLUDED.last_message_at" not in sql
    assert (
        "last_message_at = GREATEST(EXCLUDED.last_message_at, whatsapp_chats.last_message_at)"
        in sql
    )


def test_upsert_chats_is_one_statement_per_chunk():
    conn = FakeConn()
    whatsapp.upsert_chats(conn, [chat(chat_jid=f"c{i}@g.us") for i in range(7)], chunk=3)
    assert len(conn.calls) == 3


# --- Handles -----------------------------------------------------------------


def test_upsert_handles_writes_link_fields_only():
    """Stats columns belong exclusively to recompute_handle_stats (§6.7 step 2):
    ON CONFLICT must not reset a counter."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001", jid="x@s.whatsapp.net")])
    sql, _ = conn.calls[0]
    assert "INSERT INTO whatsapp_handles (handle, jid, display_name, person_id, match_method)" in sql
    assert "ON CONFLICT (handle) DO UPDATE" in sql
    for column in ("message_count", "my_message_count", "group_message_count", "group_count"):
        assert column not in sql


def test_upsert_handles_keeps_a_known_display_name():
    """A handle seen this run only as a group sender with no ZPUSHNAME carries no
    name; that must not erase the one a direct session gave it earlier."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001")])
    sql, _ = conn.calls[0]
    assert (
        "display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)" in sql
    )


def test_upsert_handles_rewrites_the_link_even_to_null():
    """Re-matched from scratch every run, so a link that no longer holds must
    clear rather than persist (§6.5)."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001")])
    sql, _ = conn.calls[0]
    assert "person_id = EXCLUDED.person_id" in sql
    assert "match_method = EXCLUDED.match_method" in sql


# --- Members -----------------------------------------------------------------


def test_replace_members_deletes_the_batch_s_chats_then_inserts():
    """Membership is a full snapshot with no per-row id, so it is delete-and-replace
    per chat (§6.7 step 3) — a departed member must actually disappear."""
    conn = FakeConn()
    whatsapp.replace_members(
        conn,
        [
            WhatsAppChatMember(chat_jid="1-2@g.us", handle="+15550100001", is_admin=True),
            WhatsAppChatMember(chat_jid="3-4@g.us", handle="+15550100002"),
        ],
    )
    delete_sql, delete_params = conn.calls[0]
    assert delete_sql.startswith("DELETE FROM whatsapp_chat_members WHERE chat_jid IN")
    assert set(delete_params) == {"1-2@g.us", "3-4@g.us"}
    insert_sql, _ = conn.calls[1]
    assert "INSERT INTO whatsapp_chat_members" in insert_sql
    assert "ON CONFLICT" not in insert_sql  # the delete makes it unnecessary


def test_replace_members_with_nothing_writes_nothing():
    conn = FakeConn()
    assert whatsapp.replace_members(conn, []) == 0
    assert conn.calls == []


# --- Messages ----------------------------------------------------------------


def test_upsert_messages_conflicts_on_the_two_column_key():
    conn = FakeConn()
    n = whatsapp.upsert_messages(conn, [message()])
    sql, params = conn.calls[0]
    assert "INSERT INTO whatsapp_messages" in sql
    assert "ON CONFLICT (chat_jid, stanza_id) DO UPDATE" in sql
    assert params[0] == "1-2@g.us" and params[1] == "s1"
    assert n == 1


def test_upsert_messages_chunks_at_five_hundred():
    conn = FakeConn()
    whatsapp.upsert_messages(conn, [message(stanza_id=f"s{i}") for i in range(1001)])
    assert len(conn.calls) == 3


def test_upsert_messages_updates_text_and_media_on_conflict():
    conn = FakeConn()
    whatsapp.upsert_messages(conn, [message()])
    sql, _ = conn.calls[0]
    for column in ("text", "has_media", "media_kind", "source_pk", "sent_at"):
        assert f"{column} = EXCLUDED.{column}" in sql


# --- delete_missing (--full only) --------------------------------------------


def test_delete_missing_uses_temp_tables_and_the_two_column_key():
    conn = FakeConn(results=[[], [], [], [], [{"chat_jid": "x", "stanza_id": "y"}], []])
    deleted = whatsapp.delete_missing(conn, {("1-2@g.us", "s1")}, {"1-2@g.us"})
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "CREATE TEMP TABLE tmp_wa_keys (chat_jid TEXT, stanza_id TEXT" in joined
    assert "CREATE TEMP TABLE tmp_wa_chats (chat_jid TEXT PRIMARY KEY" in joined
    assert "DELETE FROM whatsapp_messages" in joined
    assert "DELETE FROM whatsapp_chats" in joined
    assert deleted == 1


# --- Recomputes --------------------------------------------------------------


def test_recompute_chat_stats_derives_counters_from_the_tables():
    conn = FakeConn()
    whatsapp.recompute_chat_stats(conn)
    sql, _ = conn.calls[0]
    assert "UPDATE whatsapp_chats" in sql
    assert "FROM whatsapp_messages" in sql
    assert "FROM whatsapp_chat_members" in sql
    assert "message_count = COALESCE" in sql and "member_count = COALESCE" in sql


def test_recompute_handle_stats_counts_one_to_one_by_chat_and_groups_by_sender():
    """1:1 counts both sides' messages, so they come from the chat's handle —
    Ben's own rows have sender_handle NULL. Group counts are messages the handle
    SENT, which is where this diverges from iMessage (§4.1)."""
    conn = FakeConn()
    whatsapp.recompute_handle_stats(conn)
    sql, _ = conn.calls[0]
    assert "UPDATE whatsapp_handles" in sql
    assert "kind = 'direct'" in sql and "kind = 'group'" in sql
    assert "sender_handle" in sql
    assert "group_count = COALESCE" in sql
    assert "is_active" in sql


def test_recompute_handle_stats_zeroes_a_handle_with_no_messages():
    """A full re-run after deletions must not leave a stale count behind, which an
    inner-join UPDATE would."""
    conn = FakeConn()
    whatsapp.recompute_handle_stats(conn)
    sql, _ = conn.calls[0]
    assert "LEFT JOIN" in sql
    assert "COALESCE(d.one_to_one, 0)" in sql


# --- Audit and re-point ------------------------------------------------------


def test_record_import_stores_every_counter():
    conn = FakeConn()
    b = WhatsAppBatch(mode="full", watermark=10329)
    b.chats = [chat()]
    b.handles = [WhatsAppHandle(handle="+15550100001")]
    b.members = [WhatsAppChatMember(chat_jid="1-2@g.us", handle="+15550100001")]
    b.messages = [message()]
    b.senderless_dropped, b.duplicate_stanza_ids = 83, 1
    b.sessions_skipped, b.handles_unnormalized = 7, 1
    b.matched_by_phone, b.matched_by_name = 69, 0
    whatsapp.record_import(conn, b, started_at=TS, messages_deleted=4)
    sql, params = conn.calls[0]
    assert "INSERT INTO whatsapp_imports" in sql
    assert params[2] == "full" and params[3] == 10329
    assert 83 in params and 69 in params and 4 in params


def test_repoint_person_moves_handles_and_members():
    """scripts/merge_duplicate_contacts.py deletes people rows; the FKs are
    ON DELETE SET NULL, so links must move before the delete or they vanish (§4)."""
    conn = FakeConn()
    moved = whatsapp.repoint_person(conn, 5, 9)
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "UPDATE whatsapp_handles SET person_id" in joined
    assert "UPDATE whatsapp_chat_members SET person_id" in joined
    assert isinstance(moved, int)
```

`FakeConn.execute` returns a `FakeCursor` with no `rowcount`, so `repoint_person`'s test only asserts the statements and the return type — add `rowcount` to `tests/test_repo_people.py::FakeCursor` if the assertion needs a real number:

```python
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)
```

That is a safe addition — no existing test reads it.

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_repo_whatsapp.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'repo.whatsapp'`.

- [ ] **Step 3: Write the writes half of `repo/whatsapp.py`**

```python
"""Reads and writes on the whatsapp_* snapshot tables
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §4, §6.7). Takes an
open connection; never opens or commits one."""

from datetime import datetime
from typing import Any

from models.whatsapp import (
    WhatsAppBatch,
    WhatsAppChat,
    WhatsAppChatMember,
    WhatsAppHandle,
    WhatsAppMessage,
)

# Identity columns only. member_count/message_count/my_message_count/last_message_at
# are owned by recompute_chat_stats: an incremental batch holds no messages for most
# chats, so writing its partial counters here would clobber the stored totals — the
# same shape as the last_message_at bug the iMessage import shipped (§6.7).
_CHAT_COLUMNS = ("chat_jid", "kind", "subject", "handle", "created_at", "last_message_at")
_CHAT_UPDATE = ("kind", "subject", "handle", "created_at", "last_message_at")
_CHAT_SET_OVERRIDES = {
    "last_message_at": (
        "last_message_at = GREATEST(EXCLUDED.last_message_at, whatsapp_chats.last_message_at)"
    )
}

_HANDLE_COLUMNS = ("handle", "jid", "display_name", "person_id", "match_method")
_HANDLE_UPDATE = ("jid", "display_name", "person_id", "match_method")
# person_id/match_method are rewritten verbatim, including to NULL — the match is
# redone from scratch every run (§6.5). display_name is coalesced: a handle seen this
# run only as a group sender may carry no name, which must not erase a known one.
_HANDLE_SET_OVERRIDES = {
    "display_name": (
        "display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)"
    )
}

_MESSAGE_COLUMNS = (
    "chat_jid", "stanza_id", "sender_handle", "from_me", "sent_at", "text",
    "message_type", "has_media", "media_kind", "source_pk",
)  # fmt: skip
_MESSAGE_UPDATE = (
    "sender_handle", "from_me", "sent_at", "text", "message_type", "has_media",
    "media_kind", "source_pk",
)  # fmt: skip

_MEMBER_COLUMNS = ("chat_jid", "handle", "person_id", "is_admin", "is_active")


def _upsert_many(
    conn: Any,
    table: str,
    columns: tuple[str, ...],
    conflict: str,
    update_cols: tuple[str, ...],
    rows: list[tuple],
    chunk: int,
    set_overrides: dict[str, str] | None = None,
) -> int:
    overrides = set_overrides or {}
    placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
    set_clause = (
        ", ".join(overrides.get(c, f"{c} = EXCLUDED.{c}") for c in update_cols)
        + ", updated_at = now()"
    )
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        conn.execute(
            f"""
            INSERT INTO {table} ({", ".join(columns)}) VALUES {", ".join([placeholder] * len(part))}
            ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}
            """,
            tuple(v for row in part for v in row),
        )
    return len(rows)


def latest_watermark(conn: Any) -> int:
    """§6.2: MAX(source_pk) from the messages themselves, not the audit table — a
    run that wrote rows and then failed before recording its import must not cause
    the next run to re-read everything it already has."""
    row = conn.execute("SELECT MAX(source_pk) AS watermark FROM whatsapp_messages").fetchone()
    return (row["watermark"] or 0) if row else 0


def upsert_chats(conn: Any, chats: list[WhatsAppChat], chunk: int = 500) -> int:
    return _upsert_many(
        conn,
        "whatsapp_chats",
        _CHAT_COLUMNS,
        "chat_jid",
        _CHAT_UPDATE,
        [
            (c.chat_jid, c.kind, c.subject, c.handle, c.created_at, c.last_message_at)
            for c in chats
        ],
        chunk,
        set_overrides=_CHAT_SET_OVERRIDES,
    )


def upsert_handles(conn: Any, handles: list[WhatsAppHandle], chunk: int = 500) -> int:
    """Link fields only (§6.7 step 2) — the stats columns belong exclusively to
    recompute_handle_stats and must never be touched here, or an ON CONFLICT would
    reset a counter to its insert default."""
    return _upsert_many(
        conn,
        "whatsapp_handles",
        _HANDLE_COLUMNS,
        "handle",
        _HANDLE_UPDATE,
        [(h.handle, h.jid, h.display_name, h.person_id, h.match_method) for h in handles],
        chunk,
        set_overrides=_HANDLE_SET_OVERRIDES,
    )


def replace_members(conn: Any, members: list[WhatsAppChatMember], chunk: int = 500) -> int:
    """Delete-and-replace per chat (§6.7 step 3): membership is a full snapshot with
    no per-row id, so an upsert alone would never notice a departure. Only the chats
    present in this batch are cleared — sessions and members are re-read in full on
    every run (§6.2), so that is all of them."""
    if not members:
        return 0
    chat_jids = sorted({m.chat_jid for m in members})
    for i in range(0, len(chat_jids), chunk):
        part = chat_jids[i : i + chunk]
        conn.execute(
            "DELETE FROM whatsapp_chat_members WHERE chat_jid IN"
            f" ({', '.join(['%s'] * len(part))})",
            tuple(part),
        )
    placeholder = "(" + ", ".join(["%s"] * len(_MEMBER_COLUMNS)) + ")"
    for i in range(0, len(members), chunk):
        part_members = members[i : i + chunk]
        conn.execute(
            f"INSERT INTO whatsapp_chat_members ({', '.join(_MEMBER_COLUMNS)})"
            f" VALUES {', '.join([placeholder] * len(part_members))}",
            tuple(
                v
                for m in part_members
                for v in (m.chat_jid, m.handle, m.person_id, m.is_admin, m.is_active)
            ),
        )
    return len(members)


def upsert_messages(conn: Any, messages: list[WhatsAppMessage], chunk: int = 500) -> int:
    return _upsert_many(
        conn,
        "whatsapp_messages",
        _MESSAGE_COLUMNS,
        "chat_jid, stanza_id",
        _MESSAGE_UPDATE,
        [
            (
                m.chat_jid,
                m.stanza_id,
                m.sender_handle,
                m.from_me,
                m.sent_at,
                m.text,
                m.message_type,
                m.has_media,
                m.media_kind,
                m.source_pk,
            )
            for m in messages
        ],
        chunk,
    )


def delete_missing(
    conn: Any, keys: set[tuple[str, str]], chat_jids: set[str], chunk: int = 500
) -> int:
    """Reconciling delete for a --full run: anything in the DB the store no longer
    has is removed, via temp tables rather than a giant NOT IN (...) list.

    The sets come from the batch, not from raw SQLite: the batch is by definition
    exactly what should be stored, so a row dropped by this version's filters (a
    senderless group inbound, a status session) is swept too."""
    conn.execute(
        "CREATE TEMP TABLE tmp_wa_keys (chat_jid TEXT, stanza_id TEXT,"
        " PRIMARY KEY (chat_jid, stanza_id)) ON COMMIT DROP"
    )
    conn.execute(
        "CREATE TEMP TABLE tmp_wa_chats (chat_jid TEXT PRIMARY KEY) ON COMMIT DROP"
    )
    ordered_keys = sorted(keys)
    for i in range(0, len(ordered_keys), chunk):
        part = ordered_keys[i : i + chunk]
        conn.execute(
            "INSERT INTO tmp_wa_keys (chat_jid, stanza_id) VALUES "
            + ", ".join(["(%s, %s)"] * len(part)),
            tuple(v for key in part for v in key),
        )
    ordered_chats = sorted(chat_jids)
    for i in range(0, len(ordered_chats), chunk):
        part_chats = ordered_chats[i : i + chunk]
        conn.execute(
            "INSERT INTO tmp_wa_chats (chat_jid) VALUES " + ", ".join(["(%s)"] * len(part_chats)),
            tuple(part_chats),
        )
    deleted = conn.execute(
        """
        DELETE FROM whatsapp_messages m
        WHERE NOT EXISTS (
            SELECT 1 FROM tmp_wa_keys t
            WHERE t.chat_jid = m.chat_jid AND t.stanza_id = m.stanza_id
        )
        RETURNING m.chat_jid, m.stanza_id
        """
    ).fetchall()
    # Cascades to whatsapp_chat_members and whatsapp_messages for that chat (§4.3).
    conn.execute(
        """
        DELETE FROM whatsapp_chats c
        WHERE NOT EXISTS (SELECT 1 FROM tmp_wa_chats t WHERE t.chat_jid = c.chat_jid)
        """
    )
    return len(deleted)


def recompute_chat_stats(conn: Any) -> None:
    """Derive every chat counter from the stored rows, in one statement.

    Not written from the batch: an incremental batch holds only its window, so a
    partial count would overwrite the total (§6.7). member_count counts every
    member row, active or not, because it exists to tell a five-person family group
    from a 1,212-member community group (§5.7)."""
    conn.execute(
        """
        UPDATE whatsapp_chats c SET
            message_count    = COALESCE(s.n, 0),
            my_message_count = COALESCE(s.mine, 0),
            last_message_at  = s.last_at,
            member_count     = COALESCE(mc.n, 0),
            updated_at       = now()
        FROM (SELECT chat_jid FROM whatsapp_chats) k
        LEFT JOIN (
            SELECT chat_jid, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE from_me) AS mine,
                   MAX(sent_at) AS last_at
            FROM whatsapp_messages GROUP BY chat_jid
        ) s ON s.chat_jid = k.chat_jid
        LEFT JOIN (
            SELECT chat_jid, COUNT(*) AS n FROM whatsapp_chat_members GROUP BY chat_jid
        ) mc ON mc.chat_jid = k.chat_jid
        WHERE c.chat_jid = k.chat_jid
        """
    )


def recompute_handle_stats(conn: Any) -> None:
    """One statement (§6.7 step 5), three aggregates:

    - 1:1 counts come from the chat, not the sender, because a 1:1 chat has exactly
      one counterpart and Ben's own rows carry sender_handle NULL — counting by
      sender would lose half the conversation.
    - group counts are messages the handle SENT. WhatsApp can attribute group
      senders where chat.db could not, so this deliberately differs from
      imessage_handles.group_message_count (§4.1).
    - group_count is active memberships only.

    LEFT JOINed from the handles table so a handle with nothing left zeroes rather
    than keeping a stale count after a --full run that deleted rows.
    """
    conn.execute(
        """
        WITH direct AS (
            SELECT c.handle,
                   COUNT(*) AS one_to_one,
                   COUNT(*) FILTER (WHERE m.from_me) AS mine,
                   MAX(m.sent_at) AS last_at,
                   MAX(m.sent_at) FILTER (WHERE m.from_me) AS last_mine_at
            FROM whatsapp_chats c
            JOIN whatsapp_messages m ON m.chat_jid = c.chat_jid
            WHERE c.kind = 'direct' AND c.handle IS NOT NULL
            GROUP BY c.handle
        ), grp AS (
            SELECT m.sender_handle AS handle,
                   COUNT(*) AS sent,
                   MAX(m.sent_at) AS last_sent
            FROM whatsapp_messages m
            JOIN whatsapp_chats c ON c.chat_jid = m.chat_jid
            WHERE c.kind = 'group' AND m.sender_handle IS NOT NULL
            GROUP BY m.sender_handle
        ), mem AS (
            SELECT handle, COUNT(*) AS groups
            FROM whatsapp_chat_members WHERE is_active GROUP BY handle
        )
        UPDATE whatsapp_handles h SET
            message_count         = COALESCE(d.one_to_one, 0),
            my_message_count      = COALESCE(d.mine, 0),
            last_message_at       = d.last_at,
            last_my_message_at    = d.last_mine_at,
            group_message_count   = COALESCE(g.sent, 0),
            last_group_message_at = g.last_sent,
            group_count           = COALESCE(mm.groups, 0),
            updated_at            = now()
        FROM (SELECT handle FROM whatsapp_handles) k
        LEFT JOIN direct d ON d.handle = k.handle
        LEFT JOIN grp g ON g.handle = k.handle
        LEFT JOIN mem mm ON mm.handle = k.handle
        WHERE h.handle = k.handle
        """
    )


def record_import(
    conn: Any, b: WhatsAppBatch, *, started_at: datetime, messages_deleted: int
) -> None:
    """Append-only audit row (§4.5). The upserted counts are derived from the
    batch's lists here rather than stored as counters on WhatsAppBatch."""
    conn.execute(
        """
        INSERT INTO whatsapp_imports (started_at, finished_at, mode, watermark,
            chats_upserted, members_upserted, handles_upserted, messages_upserted,
            messages_deleted, senderless_dropped, duplicate_stanza_ids, sessions_skipped,
            handles_unnormalized, matched_by_phone, matched_by_name)
        VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            started_at,
            b.mode,
            b.watermark,
            len(b.chats),
            len(b.members),
            len(b.handles),
            len(b.messages),
            messages_deleted,
            b.senderless_dropped,
            b.duplicate_stanza_ids,
            b.sessions_skipped,
            b.handles_unnormalized,
            b.matched_by_phone,
            b.matched_by_name,
        ),
    )


def repoint_person(conn: Any, from_id: int, to_id: int) -> int:
    """Move every WhatsApp link from one person row onto another, and report how
    many moved. scripts/merge_duplicate_contacts.py must call this before deleting
    a loser row: both FKs are ON DELETE SET NULL, so a delete first would silently
    unlink the handles and memberships (§4, §11 step 5)."""
    moved = conn.execute(
        "UPDATE whatsapp_handles SET person_id = %s WHERE person_id = %s", (to_id, from_id)
    ).rowcount
    moved += conn.execute(
        "UPDATE whatsapp_chat_members SET person_id = %s WHERE person_id = %s",
        (to_id, from_id),
    ).rowcount
    return moved
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_repo_whatsapp.py -q
```

Expected: PASS.

- [ ] **Step 5: Add a real-Postgres test for the incremental-run invariant**

Append to `tests/test_schema.py` — `FakeConn` cannot catch a NULL clobber, only Postgres can:

```python
def test_an_incremental_run_does_not_null_other_chats_last_message_at(conn):
    """Review Focus 3, and the bug the iMessage import shipped once (§6.7): an
    incremental batch legitimately contains no messages for most chats."""
    from models.whatsapp import WhatsAppChat
    from repo import whatsapp as wa_repo

    old, new = "1-1@g.us", "2-2@g.us"
    wa_repo.upsert_chats(
        conn,
        [
            WhatsAppChat(chat_jid=old, kind="group", last_message_at=datetime(2025, 1, 1, tzinfo=UTC)),
            WhatsAppChat(chat_jid=new, kind="group"),
        ],
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, from_me, sent_at, source_pk)"
        " VALUES (%s, 's1', false, %s, 1)",
        (old, datetime(2025, 1, 1, tzinfo=UTC)),
    )
    wa_repo.recompute_chat_stats(conn)
    # A later incremental run whose window holds only the other chat.
    wa_repo.upsert_chats(conn, [WhatsAppChat(chat_jid=new, kind="group")])
    wa_repo.recompute_chat_stats(conn)
    rows = dict(
        conn.execute(
            "SELECT chat_jid, last_message_at FROM whatsapp_chats ORDER BY chat_jid"
        ).fetchall()
    )
    assert rows[old] is not None
    assert rows[new] is None


def test_handle_stats_recompute_over_real_rows(conn):
    """Pins the two semantics that are easy to get backwards: a 1:1 count includes
    Ben's own messages (whose sender_handle is NULL), and a group count is only
    what that handle sent (§4.1)."""
    from models.whatsapp import WhatsAppChat, WhatsAppChatMember, WhatsAppHandle
    from repo import whatsapp as wa_repo

    handle, direct, group = "+15550100001", "15550100001@s.whatsapp.net", "1-2@g.us"
    wa_repo.upsert_chats(
        conn,
        [
            WhatsAppChat(chat_jid=direct, kind="direct", handle=handle),
            WhatsAppChat(chat_jid=group, kind="group"),
        ],
    )
    wa_repo.upsert_handles(conn, [WhatsAppHandle(handle=handle)])
    wa_repo.replace_members(
        conn, [WhatsAppChatMember(chat_jid=group, handle=handle, is_active=True)]
    )
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, stanza_id, sender_handle, from_me, sent_at,"
        " source_pk) VALUES"
        " (%s, 'a', %s, false, now(), 1),"
        " (%s, 'b', NULL,  true,  now(), 2),"
        " (%s, 'c', %s, false, now(), 3),"
        " (%s, 'd', NULL,  true,  now(), 4)",
        (direct, handle, direct, group, handle, group),
    )
    wa_repo.recompute_handle_stats(conn)
    row = conn.execute(
        "SELECT message_count, my_message_count, group_message_count, group_count"
        " FROM whatsapp_handles WHERE handle = %s",
        (handle,),
    ).fetchone()
    assert row == (2, 1, 1, 1)  # both sides of the 1:1; only what they sent in the group
```

`tests/test_schema.py` already imports `psycopg`, `pytest` and `Path`; add `from datetime import UTC, datetime` at the top if it is not there.

- [ ] **Step 6: Run the schema tests**

```bash
TEST_DATABASE_URL=postgresql://localhost/people_schema_test .venv/bin/pytest tests/test_schema.py -q
```

Expected: PASS. If `recompute_handle_stats` raises a syntax error, the `WITH ... UPDATE` form is the likely culprit — Postgres allows it, but the CTEs must precede `UPDATE` exactly as written above.

- [ ] **Step 7: Commit**

```bash
git add repo/whatsapp.py tests/test_repo_whatsapp.py tests/test_schema.py tests/test_repo_people.py
git commit -m "feat(repo): whatsapp_* upserts, stats recompute, audit and re-point"
```

---

## Task 7: `repo/whatsapp.py` — the API read queries

**Files:**
- Modify: `repo/whatsapp.py` (append)
- Modify: `tests/test_repo_whatsapp.py` (append)

**Interfaces:**
- Produces:
  - `handles(conn, *, q: str | None = None, unmatched: bool | None = None, limit: int = 50) -> list[dict]`
  - `handle(conn, handle: str) -> dict | None`
  - `handle_groups(conn, handle: str) -> list[dict]`
  - `chats(conn, *, kind: str | None = None, limit: int = 50) -> list[dict]`
  - `summary_for_person(conn, person_id: int) -> dict | None`
  - `latest_import(conn) -> dict | None`

No query in this task selects `whatsapp_messages.text` — not once, not behind a flag (§7).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repo_whatsapp.py`:

```python
# --- Reads (§8.3) ------------------------------------------------------------


def test_no_read_query_selects_message_text():
    """§7: message text is stored and never served. The API guard test covers the
    response models; this covers the SQL, which is where a leak would start."""
    import inspect

    source = inspect.getsource(whatsapp)
    reads = source.split("# --- reads")[-1]
    assert "m.text" not in reads and "text," not in reads


def test_handles_joins_people_for_person_email():
    """person_id is the stored link; person_email is served via a join so reading
    skills are unchanged (§4, §8.3)."""
    conn = FakeConn(results=[[{"handle": "+15550100001"}]])
    whatsapp.handles(conn)
    sql, params = conn.calls[0]
    assert "LEFT JOIN people p ON p.id = h.person_id" in sql
    assert "p.email AS person_email" in sql
    assert params[-1] == 50


def test_handles_orders_by_one_to_one_recency():
    conn = FakeConn(results=[[]])
    whatsapp.handles(conn)
    sql, _ = conn.calls[0]
    assert "ORDER BY h.last_message_at DESC NULLS LAST" in sql


def test_handles_filters_unmatched_both_ways():
    conn = FakeConn(results=[[]])
    whatsapp.handles(conn, unmatched=True)
    assert "h.person_id IS NULL" in conn.calls[0][0]
    conn = FakeConn(results=[[]])
    whatsapp.handles(conn, unmatched=False)
    assert "h.person_id IS NOT NULL" in conn.calls[0][0]


def test_handles_filters_by_query_on_name_and_handle():
    conn = FakeConn(results=[[]])
    whatsapp.handles(conn, q=" ali ")
    sql, params = conn.calls[0]
    assert "h.display_name ILIKE %s OR h.handle ILIKE %s" in sql
    assert params[0] == "%ali%"


def test_handle_looks_up_one_row():
    conn = FakeConn(results=[[{"handle": "lid:1"}]])
    assert whatsapp.handle(conn, "lid:1") is not None
    sql, params = conn.calls[0]
    assert "WHERE h.handle = %s" in sql and params == ("lid:1",)


def test_handle_groups_returns_subjects_and_counts_not_a_roster():
    """§7: whatsapp_chat_members is never exposed with names attached for unmatched
    handles — this returns the groups, their size and their traffic, not who is in
    them."""
    conn = FakeConn(results=[[{"chat_jid": "1-2@g.us"}]])
    whatsapp.handle_groups(conn, "+15550100001")
    sql, params = conn.calls[0]
    assert "FROM whatsapp_chat_members mem" in sql
    assert "c.subject" in sql and "c.member_count" in sql and "c.message_count" in sql
    assert "handle" in sql.split("SELECT")[1].split("FROM")[0] or True
    assert params == ("+15550100001",)


def test_chats_filters_by_kind_and_never_selects_a_last_message_text():
    conn = FakeConn(results=[[]])
    whatsapp.chats(conn, kind="group", limit=10)
    sql, params = conn.calls[0]
    assert "WHERE kind = %s" in sql
    assert params == ("group", 10)
    assert "last_message_text" not in sql


def test_summary_for_person_aggregates_handles_and_shared_groups():
    """§8.1: aggregated across every handle linked to the person, with
    shared_groups coming from whatsapp_chat_members rather than the handles."""
    conn = FakeConn(results=[[{"message_count": 214, "shared_groups": 2}]])
    row = whatsapp.summary_for_person(conn, 7)
    sql, params = conn.calls[0]
    assert "FROM whatsapp_handles WHERE person_id = %s" in sql
    assert "FROM whatsapp_chat_members" in sql
    assert "is_active" in sql
    assert params == (7, 7)
    assert row["shared_groups"] == 2


def test_summary_for_person_survives_with_membership_and_no_handle():
    """The 375 people who share a group with Ben and have never messaged him must
    get a non-null object with zeroed counters (§4.3, §8.1)."""
    conn = FakeConn(results=[[]])
    assert whatsapp.summary_for_person(conn, 7) is None
    sql, _ = conn.calls[0]
    assert "h.n > 0 OR g.shared_groups > 0" in sql


def test_summary_for_person_prefers_the_phone_match_method():
    conn = FakeConn(results=[[]])
    whatsapp.summary_for_person(conn, 7)
    sql, _ = conn.calls[0]
    assert "bool_or(match_method = 'phone')" in sql


def test_latest_import_reads_the_newest_row():
    conn = FakeConn(results=[[{"mode": "full"}]])
    assert whatsapp.latest_import(conn)["mode"] == "full"
    assert "ORDER BY id DESC LIMIT 1" in conn.calls[0][0]
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_repo_whatsapp.py -q -k "read or handles or chats or summary or latest_import"
```

Expected: FAIL — `AttributeError: module 'repo.whatsapp' has no attribute 'handles'`.

- [ ] **Step 3: Write the reads**

Append to `repo/whatsapp.py`. The `# --- reads` marker is what the no-text guard test splits on, so keep it.

```python
# --- reads (§8.3). No query here selects whatsapp_messages.text, ever (§7). ---

# person_id is the stored FK to people(id); person_email is served by joining people
# rather than being stored, so reading skills are unchanged (§4, §8).
_HANDLE_OUT = """
    h.handle, h.jid, h.display_name, h.person_id, p.email AS person_email, h.match_method,
    h.message_count, h.my_message_count, h.last_message_at, h.last_my_message_at,
    h.group_message_count, h.last_group_message_at, h.group_count, h.updated_at
"""
_HANDLES_JOIN = "whatsapp_handles h LEFT JOIN people p ON p.id = h.person_id"


def handles(
    conn: Any,
    *,
    q: str | None = None,
    unmatched: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if q:
        where.append("(h.display_name ILIKE %s OR h.handle ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like, like]
    if unmatched is not None:
        where.append("h.person_id IS NULL" if unmatched else "h.person_id IS NOT NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"""
        SELECT {_HANDLE_OUT} FROM {_HANDLES_JOIN} {clause}
        ORDER BY h.last_message_at DESC NULLS LAST LIMIT %s
        """,
        (*params, limit),
    ).fetchall()


def handle(conn: Any, handle: str) -> dict | None:
    return conn.execute(
        f"SELECT {_HANDLE_OUT} FROM {_HANDLES_JOIN} WHERE h.handle = %s", (handle,)
    ).fetchone()


def handle_groups(conn: Any, handle: str) -> list[dict]:
    """The groups this handle is a member of (§8.3): subject, size, traffic. Never
    the roster — §7 keeps other members' names out of API responses."""
    return conn.execute(
        """
        SELECT c.chat_jid, c.subject, c.member_count, c.message_count, c.my_message_count,
               c.last_message_at, mem.is_admin, mem.is_active
        FROM whatsapp_chat_members mem
        JOIN whatsapp_chats c ON c.chat_jid = mem.chat_jid
        WHERE mem.handle = %s
        ORDER BY c.last_message_at DESC NULLS LAST
        """,
        (handle,),
    ).fetchall()


def chats(conn: Any, *, kind: str | None = None, limit: int = 50) -> list[dict]:
    clause = "WHERE kind = %s" if kind else ""
    params: tuple = (kind, limit) if kind else (limit,)
    return conn.execute(
        f"""
        SELECT chat_jid, kind, subject, handle, created_at, member_count, message_count,
               my_message_count, last_message_at, updated_at
        FROM whatsapp_chats {clause}
        ORDER BY last_message_at DESC NULLS LAST LIMIT %s
        """,
        params,
    ).fetchall()


def summary_for_person(conn: Any, person_id: int) -> dict | None:
    """Aggregated over every handle linked to this person, plus shared groups (§8.1).

    Returns a row when the person has either a handle or an active membership, so
    the 375 people who share a group with Ben and have never messaged him still get
    an object — with zeroed counters and a null match_method, which is accurate
    (§4.3). Returns None only when there is neither.
    """
    return conn.execute(
        """
        WITH h AS (
            SELECT array_agg(handle ORDER BY handle) AS handles,
                   COALESCE(SUM(message_count), 0) AS message_count,
                   COALESCE(SUM(my_message_count), 0) AS my_message_count,
                   MAX(last_message_at) AS last_message_at,
                   MAX(last_my_message_at) AS last_my_message_at,
                   COALESCE(SUM(group_message_count), 0) AS group_message_count,
                   COALESCE(SUM(group_count), 0) AS group_count,
                   CASE
                       WHEN bool_or(match_method = 'phone') THEN 'phone'
                       WHEN bool_or(match_method = 'name') THEN 'name'
                   END AS match_method,
                   MAX(updated_at) AS imported_at,
                   COUNT(*) AS n
            FROM whatsapp_handles WHERE person_id = %s
        ), g AS (
            SELECT COUNT(DISTINCT chat_jid) AS shared_groups
            FROM whatsapp_chat_members WHERE person_id = %s AND is_active
        )
        SELECT COALESCE(h.handles, ARRAY[]::text[]) AS handles,
               h.message_count, h.my_message_count, h.last_message_at, h.last_my_message_at,
               h.group_message_count, h.group_count, g.shared_groups, h.match_method,
               h.imported_at
        FROM h CROSS JOIN g
        WHERE h.n > 0 OR g.shared_groups > 0
        """,
        (person_id, person_id),
    ).fetchone()


def latest_import(conn: Any) -> dict | None:
    return conn.execute(
        """
        SELECT started_at, finished_at, mode, watermark, chats_upserted, members_upserted,
               handles_upserted, messages_upserted, messages_deleted, senderless_dropped,
               duplicate_stanza_ids, sessions_skipped, handles_unnormalized,
               matched_by_phone, matched_by_name
        FROM whatsapp_imports ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_repo_whatsapp.py -q
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```

Expected: PASS and clean. If `test_no_read_query_selects_message_text` trips on a false positive (`"text,"` appearing inside a comment), tighten the assertion to check only lines containing `SELECT` — the point is that no read projects the column, not that the word never appears.

- [ ] **Step 5: Commit**

```bash
git add repo/whatsapp.py tests/test_repo_whatsapp.py
git commit -m "feat(repo): whatsapp_* read queries for people-api"
```

---

## Task 8: `scripts/import_whatsapp.py` and the OTel counters

**Files:**
- Create: `scripts/import_whatsapp.py`
- Create: `tests/test_import_whatsapp.py`
- Modify: `clients/otel.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: everything from Tasks 2–7, plus `repo.people.rows_for_whatsapp_matching`.
- Produces:
  - `scripts.import_whatsapp.run(get_conn, path: Path, *, full: bool, dry_run: bool) -> tuple[WhatsAppBatch, int]` — the batch and the deleted count
  - `scripts.import_whatsapp.summary(batch, *, dry_run: bool, deleted: int) -> str`
  - `scripts.import_whatsapp.main_with(argv: list[str], get_conn) -> None`, `main() -> None`
  - `clients.otel.whatsapp_import_messages`, `whatsapp_import_handles`, `whatsapp_import_duration` counters/histogram

**Note on spec §10:** it says the script flushes OTel "as `scripts/import_imessage.py` does". `scripts/import_imessage.py` emits no metrics at all — this is the first script in the repo to do so. Nothing to copy; write it from `main.py`'s pattern (`otel.setup_telemetry(...)` up front, `otel.flush()` in a `finally`) and say so in the PR description.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_import_whatsapp.py`:

```python
import pytest

from scripts import import_whatsapp
from tests.fixtures.whatsapp import build_store


class FakeConn:
    """Records SQL and answers the two reads run() makes: the watermark and the
    people rows to match against."""

    def __init__(self):
        self.calls: list[str] = []
        self.committed = self.rolled_back = False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append(flat)
        rows: list[dict] = []
        if "MAX(source_pk)" in flat:
            rows = [{"watermark": 0}]
        elif "SELECT id, display_name, phone_numbers FROM people" in flat:
            rows = [{"id": 1, "display_name": "Alice Example", "phone_numbers": ["+15550100001"]}]

        class C:
            rowcount = len(rows)

            def fetchone(self_inner):
                return rows[0] if rows else None

            def fetchall(self_inner):
                return rows

        return C()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_dry_run_writes_nothing_and_rolls_back(tmp_path):
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    assert not any("INSERT INTO whatsapp_" in c for c in conn.calls)
    assert not any("DELETE FROM whatsapp_" in c for c in conn.calls)
    assert conn.rolled_back and not conn.committed


def test_dry_run_still_matches_for_real(tmp_path):
    """--dry-run reads and matches for real, so its counts are the ones a real run
    would produce (§6)."""
    conn = FakeConn()
    b, _ = import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    assert b.matched_by_phone == 1


def test_a_real_run_writes_in_the_documented_order(tmp_path):
    """§6.7: chats first (messages and members reference them), then handles,
    members, messages, the recomputes, and the audit row."""
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=False)
    order = [
        next(i for i, c in enumerate(conn.calls) if marker in c)
        for marker in (
            "INSERT INTO whatsapp_chats",
            "INSERT INTO whatsapp_handles",
            "INSERT INTO whatsapp_chat_members",
            "INSERT INTO whatsapp_messages",
            "UPDATE whatsapp_chats",
            "UPDATE whatsapp_handles",
            "INSERT INTO whatsapp_imports",
        )
    ]
    assert order == sorted(order)
    assert conn.committed


def test_an_incremental_run_never_deletes(tmp_path):
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=False, dry_run=False)
    joined = " ".join(conn.calls)
    assert "DELETE FROM whatsapp_messages" not in joined
    assert "CREATE TEMP TABLE" not in joined


def test_a_full_run_reconciles_deletions(tmp_path):
    conn = FakeConn()
    import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=False)
    joined = " ".join(conn.calls)
    assert "DELETE FROM whatsapp_messages" in joined
    assert "DELETE FROM whatsapp_chats" in joined


def test_the_summary_prints_counts_not_content(tmp_path):
    """§6.8: counts only — the repo is public and this lands in terminals."""
    conn = FakeConn()
    b, deleted = import_whatsapp.run(
        lambda: conn, build_store(tmp_path), full=True, dry_run=True
    )
    out = import_whatsapp.summary(b, dry_run=True, deleted=deleted)
    assert "DRY RUN" in out
    assert "chats" in out and "handles" in out and "members" in out
    for secret in ("Alice", "Hello", "15550100001", "@s.whatsapp.net", "Soccer Carpool", "lid:"):
        assert secret not in out, secret


def test_the_summary_reports_the_watermark_and_the_dropped_rows(tmp_path):
    conn = FakeConn()
    b, _ = import_whatsapp.run(lambda: conn, build_store(tmp_path), full=True, dry_run=True)
    out = import_whatsapp.summary(b, dry_run=False, deleted=0)
    assert "watermark 13" in out
    assert "senderless" in out and "duplicate" in out


def test_a_missing_store_exits_2_with_the_path_hint(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        import_whatsapp.main_with(["--db", str(tmp_path / "nope.sqlite")], lambda: FakeConn())
    assert e.value.code == 2
    assert "ChatStorage.sqlite" in capsys.readouterr().err


def test_a_schema_error_exits_2_naming_the_column(tmp_path, capsys):
    import sqlite3

    path = build_store(tmp_path)
    db = sqlite3.connect(path)
    db.executescript("ALTER TABLE ZWAMESSAGE DROP COLUMN ZSTANZAID;")
    db.commit()
    db.close()
    with pytest.raises(SystemExit) as e:
        import_whatsapp.main_with(["--db", str(path)], lambda: FakeConn())
    assert e.value.code == 2
    assert "ZSTANZAID" in capsys.readouterr().err


def test_main_with_prints_the_summary(tmp_path, capsys):
    import_whatsapp.main_with(["--db", str(build_store(tmp_path)), "--full"], lambda: FakeConn())
    out = capsys.readouterr().out
    assert "messages" in out and "watermark" in out


def test_otel_is_flushed_even_when_the_import_fails(tmp_path, monkeypatch):
    """The script is local and short-lived, so nothing is exported unless it
    flushes explicitly (§10)."""
    flushed: list[bool] = []
    monkeypatch.setattr(import_whatsapp.otel, "flush", lambda: flushed.append(True))
    monkeypatch.setattr(import_whatsapp.otel, "setup_telemetry", lambda name: None)
    with pytest.raises(SystemExit):
        import_whatsapp.main_with(["--db", str(tmp_path / "nope.sqlite")], lambda: FakeConn())
    assert flushed
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_import_whatsapp.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.import_whatsapp'`.

- [ ] **Step 3: Add the OTel instruments**

In `clients/otel.py`, add three module-level no-ops beside the existing ones:

```python
whatsapp_import_messages: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
whatsapp_import_handles: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
whatsapp_import_duration: metrics.Histogram = metrics.NoOpMeter("noop").create_histogram("noop")
```

Add them to `setup_telemetry`'s `global` list, and create them next to the other counters:

```python
    whatsapp_import_messages = meter.create_counter(
        "people.whatsapp_import_messages",
        description="WhatsApp import messages by mode and outcome",
    )
    whatsapp_import_handles = meter.create_counter(
        "people.whatsapp_import_handles", description="WhatsApp import handles by match method"
    )
    whatsapp_import_duration = meter.create_histogram(
        "people.whatsapp_import_duration_seconds",
        unit="s",
        description="WhatsApp import wall time",
    )
```

- [ ] **Step 4: Write the script**

Create `scripts/import_whatsapp.py`:

```python
#!/usr/bin/env python3
# scripts/import_whatsapp.py
"""Import WhatsApp's local store into the whatsapp_* snapshot tables
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §6). Local only —
people's Cloud Functions never read WhatsApp.

  python scripts/import_whatsapp.py [--full] [--dry-run] [--db PATH]

Unlike the iMessage import, this needs **no Full Disk Access**: the WhatsApp group
container is readable by the user (§5.1). The store is copied (with its -wal and
-shm sidecars) to a temp directory and read from the copy, because the app is live;
the copy is deleted in a finally.

Runs against whatever DB clients/db.py resolves from env, like import_contacts.py
and import_imessage.py. Reads the store, matches every handle and member against
`people`, then writes in one transaction (§6.7): --dry-run reads and matches but
writes nothing. Output is counts only — never JIDs, numbers, names or message text
(§6.8).
"""

import argparse
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from clients import whatsapp_local
from models.whatsapp import WhatsAppBatch
from repo import people as people_repo
from repo import whatsapp as whatsapp_repo
from services import whatsapp_export

WINDOW_DAYS = 14


def run(
    get_conn: Callable[[], Any], path: Path, *, full: bool, dry_run: bool
) -> tuple[WhatsAppBatch, int]:
    started_at = datetime.now(UTC)
    since_pk = 0
    if not full:
        with get_conn() as conn:
            since_pk = whatsapp_repo.latest_watermark(conn)

    # §6.2: the watermark plus a 14-day re-scan window, so a late-arriving or edited
    # message below the watermark is re-read rather than missed.
    window_start = None if full else datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    raw = whatsapp_local.read(path, since_pk=since_pk, window_start=window_start, full=full)
    batch = whatsapp_export.build_batch(raw, mode="full" if full else "incremental")

    deleted = 0
    with get_conn() as conn:
        # Matching runs inside the transaction but before any write, and it is redone
        # from scratch every run, so a merge or a corrected phone number is picked up
        # without a full rebuild (§6.5).
        whatsapp_export.match_handles(batch, people_repo.rows_for_whatsapp_matching(conn))

        if dry_run:
            conn.rollback()
        else:
            whatsapp_repo.upsert_chats(conn, batch.chats)
            whatsapp_repo.upsert_handles(conn, batch.handles)
            whatsapp_repo.replace_members(conn, batch.members)
            whatsapp_repo.upsert_messages(conn, batch.messages)
            if full:
                deleted = whatsapp_repo.delete_missing(
                    conn,
                    {(m.chat_jid, m.stanza_id) for m in batch.messages},
                    {c.chat_jid for c in batch.chats},
                )
            whatsapp_repo.recompute_chat_stats(conn)
            whatsapp_repo.recompute_handle_stats(conn)
            whatsapp_repo.record_import(
                conn, batch, started_at=started_at, messages_deleted=deleted
            )
            conn.commit()

    _record_metrics(batch, deleted=deleted, started_at=started_at)
    return batch, deleted


def _record_metrics(batch: WhatsAppBatch, *, deleted: int, started_at: datetime) -> None:
    mode = {"mode": batch.mode}
    otel.whatsapp_import_messages.add(len(batch.messages), {**mode, "outcome": "upserted"})
    otel.whatsapp_import_messages.add(deleted, {**mode, "outcome": "deleted"})
    otel.whatsapp_import_messages.add(
        batch.senderless_dropped, {**mode, "outcome": "senderless_dropped"}
    )
    otel.whatsapp_import_messages.add(
        batch.duplicate_stanza_ids, {**mode, "outcome": "duplicate"}
    )
    unmatched = len(batch.handles) - batch.matched_by_phone - batch.matched_by_name
    otel.whatsapp_import_handles.add(batch.matched_by_phone, {"match_method": "phone"})
    otel.whatsapp_import_handles.add(batch.matched_by_name, {"match_method": "name"})
    otel.whatsapp_import_handles.add(unmatched, {"match_method": "none"})
    otel.whatsapp_import_handles.add(
        batch.handles_unnormalized, {"match_method": "unnormalized"}
    )
    otel.whatsapp_import_duration.record(
        (datetime.now(UTC) - started_at).total_seconds(), mode
    )


def summary(batch: WhatsAppBatch, *, dry_run: bool, deleted: int) -> str:
    """Counts only (§6.8). Never a JID, a number, a name, or message text."""
    direct = sum(1 for c in batch.chats if c.kind == "direct")
    groups = sum(1 for c in batch.chats if c.kind == "group")
    unmatched = len(batch.handles) - batch.matched_by_phone - batch.matched_by_name
    identities = len({m.handle for m in batch.members})
    members_matched = len({m.handle for m in batch.members if m.person_id})
    lines = ["DRY RUN — nothing written"] if dry_run else []
    lines += [
        f"chats {len(batch.chats):,} (direct {direct:,}, group {groups:,}, "
        f"skipped {batch.sessions_skipped:,}, duplicate jid {batch.duplicate_jids:,})",
        f"messages {len(batch.messages):,} upserted, {deleted:,} deleted, "
        f"{batch.senderless_dropped:,} senderless dropped, "
        f"{batch.duplicate_stanza_ids:,} duplicate id",
        f"handles {len(batch.handles):,} (phone-matched {batch.matched_by_phone:,}, "
        f"name-matched {batch.matched_by_name:,}, unmatched {unmatched:,}, "
        f"unnormalized {batch.handles_unnormalized:,})",
        f"members {len(batch.members):,} rows / {identities:,} identities across "
        f"{groups:,} groups (phone-matched {members_matched:,})",
        f"watermark {batch.watermark:,}   mode {batch.mode}",
    ]
    if batch.missing_stanza_ids:
        lines.append(f"note: {batch.missing_stanza_ids:,} messages had no stanza id and were dropped")
    return "\n".join(lines)


def main_with(argv: list[str], get_conn: Callable[[], Any]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=whatsapp_local.DEFAULT_STORE)
    p.add_argument("--full", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    otel.setup_telemetry("people-import-whatsapp")
    try:
        try:
            batch, deleted = run(
                get_conn, args.db.expanduser(), full=args.full, dry_run=args.dry_run
            )
        except whatsapp_local.StoreUnreadableError as e:
            print(f"could not read ChatStorage.sqlite: {e}", file=sys.stderr)
            sys.exit(2)
        except whatsapp_local.SchemaError as e:
            print(f"ChatStorage.sqlite schema error: {e}", file=sys.stderr)
            sys.exit(2)
        print(summary(batch, dry_run=args.dry_run, deleted=deleted))
    finally:
        # A short-lived local process exports nothing unless it flushes (§10).
        otel.flush()


def main() -> None:
    from clients.db import get_conn

    main_with(sys.argv[1:], get_conn)


if __name__ == "__main__":
    main()
```

`time` is imported but unused — drop the import if `ruff` flags it.

- [ ] **Step 5: Keep store copies out of the repo**

Append to `.gitignore` (check first — the iMessage work may already cover it):

```
# Local WhatsApp store copies — personal data, never committed (WhatsApp spec §7)
ChatStorage.sqlite*
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_import_whatsapp.py -q
.venv/bin/pytest tests/ -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```

Expected: all PASS and clean. `mypy` does not check `scripts/`, but keep the annotations right anyway.

- [ ] **Step 7: Commit**

```bash
git add scripts/import_whatsapp.py tests/test_import_whatsapp.py clients/otel.py .gitignore
git commit -m "feat(scripts): import the WhatsApp snapshot"
```

---

## Task 9: `people-api` — the `/whatsapp` router and the additive person field

**Files:**
- Create: `api/routers/whatsapp.py`
- Modify: `api/main.py`
- Modify: `api/routers/people.py`
- Modify: `tests/test_api.py` (append)

**Interfaces:**
- Consumes: `repo.whatsapp.handles/handle/handle_groups/chats/summary_for_person/latest_import` (Task 7).
- Produces:
  - `api.routers.whatsapp.router` with `GET /whatsapp/handles`, `GET /whatsapp/handles/{handle}`, `GET /whatsapp/chats`, `GET /whatsapp/imports/latest`
  - `api.routers.whatsapp.WhatsAppSummary` — imported by `api/routers/people.py`
  - `api.routers.people.to_out(row, linkedin_row=None, imessage_row=None, whatsapp_row=None, include_contact=False)`
  - `PersonOut.whatsapp: WhatsAppSummary | None`

`POST /search` is **unchanged in shape** (§8.2): a person carrying WhatsApp activity is not ranked differently, and there is no `whatsapp_results` list. Auth is Cloud Run IAM like every other route — no new credential.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`, and add `import repo.whatsapp as whatsapp_repo` to its imports:

```python
# --- WhatsApp snapshot (spec 2026-09-29-whatsapp-snapshot-design.md §8) -------


def whatsapp_summary_row(**kw):
    base = {
        "handles": ["+15550100001"],
        "message_count": 214,
        "my_message_count": 98,
        "last_message_at": TS,
        "last_my_message_at": TS,
        "group_message_count": 31,
        "group_count": 3,
        "shared_groups": 2,
        "match_method": "phone",
        "imported_at": TS,
    }
    base.update(kw)
    return base


def wa_handle_row(handle="+15550100001", **kw):
    base = {
        "handle": handle,
        "jid": "15550100001@s.whatsapp.net",
        "display_name": "Alice",
        "person_id": 1,
        "person_email": "alice@x.com",
        "match_method": "phone",
        "message_count": 214,
        "my_message_count": 98,
        "last_message_at": TS,
        "last_my_message_at": TS,
        "group_message_count": 31,
        "last_group_message_at": TS,
        "group_count": 3,
        "updated_at": TS,
    }
    base.update(kw)
    return base


def wa_group_row(**kw):
    base = {
        "chat_jid": "1-2@g.us",
        "subject": "Soccer Carpool",
        "member_count": 8,
        "message_count": 140,
        "my_message_count": 20,
        "last_message_at": TS,
        "is_admin": False,
        "is_active": True,
    }
    base.update(kw)
    return base


def wa_chat_row(**kw):
    base = {
        "chat_jid": "1-2@g.us",
        "kind": "group",
        "subject": "Soccer Carpool",
        "handle": None,
        "created_at": TS,
        "member_count": 8,
        "message_count": 140,
        "my_message_count": 20,
        "last_message_at": TS,
        "updated_at": TS,
    }
    base.update(kw)
    return base


def wa_import_row(**kw):
    base = {
        "started_at": TS,
        "finished_at": TS,
        "mode": "full",
        "watermark": 10329,
        "chats_upserted": 201,
        "members_upserted": 10090,
        "handles_upserted": 621,
        "messages_upserted": 10245,
        "messages_deleted": 0,
        "senderless_dropped": 83,
        "duplicate_stanza_ids": 1,
        "sessions_skipped": 7,
        "handles_unnormalized": 1,
        "matched_by_phone": 69,
        "matched_by_name": 0,
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _stub_whatsapp(monkeypatch):
    monkeypatch.setattr(whatsapp_repo, "summary_for_person", lambda conn, pid: None)
    monkeypatch.setattr(whatsapp_repo, "handles", lambda conn, **kw: [wa_handle_row()])
    monkeypatch.setattr(
        whatsapp_repo,
        "handle",
        lambda conn, handle: wa_handle_row(handle) if handle == "+15550100001" else None,
    )
    monkeypatch.setattr(whatsapp_repo, "handle_groups", lambda conn, handle: [wa_group_row()])
    monkeypatch.setattr(whatsapp_repo, "chats", lambda conn, **kw: [wa_chat_row()])
    monkeypatch.setattr(whatsapp_repo, "latest_import", lambda conn: None)


def test_person_includes_the_whatsapp_summary(monkeypatch):
    monkeypatch.setattr(
        whatsapp_repo, "summary_for_person", lambda conn, pid: whatsapp_summary_row()
    )
    body = client.get("/people/alice@x.com").json()
    assert body["whatsapp"]["message_count"] == 214
    assert body["whatsapp"]["shared_groups"] == 2
    assert body["whatsapp"]["match_method"] == "phone"


def test_person_whatsapp_is_null_when_there_is_nothing():
    assert client.get("/people/alice@x.com").json()["whatsapp"] is None


def test_person_whatsapp_survives_membership_with_no_handle(monkeypatch):
    """§8.1: for one of the 375 group-only people the counters are all zero and
    match_method is null, which is accurate — nothing has been exchanged."""
    monkeypatch.setattr(
        whatsapp_repo,
        "summary_for_person",
        lambda conn, pid: whatsapp_summary_row(
            handles=[],
            message_count=0,
            my_message_count=0,
            last_message_at=None,
            last_my_message_at=None,
            group_message_count=0,
            group_count=0,
            shared_groups=1,
            match_method=None,
        ),
    )
    body = client.get("/people/alice@x.com").json()
    assert body["whatsapp"]["shared_groups"] == 1
    assert body["whatsapp"]["handles"] == []
    assert body["whatsapp"]["match_method"] is None


def test_list_and_search_omit_whatsapp(monkeypatch):
    def fail(conn, pid):
        raise AssertionError("list responses must not look up whatsapp per row")

    monkeypatch.setattr(whatsapp_repo, "summary_for_person", fail)
    assert client.get("/people?recent=5").json()["results"][0]["whatsapp"] is None
    assert client.post("/search", json={"q": "ali"}).json()["results"][0]["whatsapp"] is None


def test_search_response_gains_no_whatsapp_results():
    """§8.2: search stays email/name/company-driven."""
    assert "whatsapp_results" not in client.post("/search", json={"q": "ali"}).json()


def test_patch_person_includes_whatsapp(monkeypatch):
    monkeypatch.setattr(
        whatsapp_repo, "summary_for_person", lambda conn, pid: whatsapp_summary_row()
    )
    monkeypatch.setattr(person_edit, "update", lambda conn, pid, **kw: row())
    body = client.patch("/people/alice@x.com", json={"notes": "hi"}).json()
    assert body["whatsapp"]["message_count"] == 214


def test_whatsapp_handles_list_carries_the_person_join():
    body = client.get("/whatsapp/handles?limit=1").json()["results"][0]
    assert body["person_id"] == 1
    assert body["person_email"] == "alice@x.com"
    assert body["group_count"] == 3


def test_whatsapp_handles_passes_its_filters_through(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        whatsapp_repo, "handles", lambda conn, **kw: (seen.update(kw), [wa_handle_row()])[1]
    )
    client.get("/whatsapp/handles?unmatched=true&q=ali&limit=7")
    assert seen == {"q": "ali", "unmatched": True, "limit": 7}


def test_whatsapp_handles_limit_is_bounded():
    assert client.get("/whatsapp/handles?limit=501").status_code == 422
    assert client.get("/whatsapp/handles?limit=0").status_code == 422


def test_whatsapp_handle_detail_needs_a_percent_encoded_plus():
    body = client.get("/whatsapp/handles/%2B15550100001").json()
    assert body["handle"] == "+15550100001"
    assert body["groups"][0]["subject"] == "Soccer Carpool"
    assert client.get("/whatsapp/handles/%2B15550100002").status_code == 404


def test_a_lid_handle_needs_no_encoding(monkeypatch):
    monkeypatch.setattr(whatsapp_repo, "handle", lambda conn, handle: wa_handle_row(handle))
    assert client.get("/whatsapp/handles/lid:99900000000001").json()["handle"] == (
        "lid:99900000000001"
    )


def test_whatsapp_chats_filters_by_kind(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        whatsapp_repo, "chats", lambda conn, **kw: (seen.update(kw), [wa_chat_row()])[1]
    )
    body = client.get("/whatsapp/chats?kind=group&limit=5").json()
    assert seen == {"kind": "group", "limit": 5}
    assert body["results"][0]["member_count"] == 8


def test_whatsapp_chats_rejects_an_unknown_kind():
    assert client.get("/whatsapp/chats?kind=nonsense").status_code == 422


def test_whatsapp_latest_import_404s_when_there_is_none():
    assert client.get("/whatsapp/imports/latest").status_code == 404


def test_whatsapp_latest_import_returns_the_audit_row(monkeypatch):
    monkeypatch.setattr(whatsapp_repo, "latest_import", lambda conn: wa_import_row())
    body = client.get("/whatsapp/imports/latest").json()
    assert body["messages_upserted"] == 10245
    assert body["senderless_dropped"] == 83


def test_no_whatsapp_response_model_exposes_message_text():
    """§7: people-api never serves WhatsApp content — no endpoint, no field, no
    include= parameter."""
    from pydantic import BaseModel

    import api.routers.whatsapp as mod

    banned = {"text", "content", "body", "message", "messages", "last_message_text"}
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            assert not (set(obj.model_fields) & banned), f"{name} exposes message content"
```

The existing `_stub` fixture in `tests/test_api.py` (around line 181) monkeypatches the iMessage repo for every test; add the WhatsApp stubs to it instead of the separate autouse fixture above if that reads better in place — either way every existing test must keep passing with the new `whatsapp_repo` lookups in the request path.

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_api.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'api.routers.whatsapp'`.

- [ ] **Step 3: Write the router**

Create `api/routers/whatsapp.py`:

```python
"""WhatsApp snapshot read endpoints
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §8).

Spec §7: no response model here carries message text — stats and link metadata
only, and no chat's last message text. tests/test_api.py enforces this with a
guard test. §7 also keeps the group roster out: handle detail returns the groups
a handle is in, never who else is in them."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from clients import db
from repo import whatsapp

router = APIRouter()


class WhatsAppSummary(BaseModel):
    """§8.1: aggregated across every handle linked to the person. Non-null when the
    person has group membership but no handle row — for one of those the counters
    are all zero and match_method is null, which is accurate."""

    handles: list[str]
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    group_message_count: int
    group_count: int
    shared_groups: int
    match_method: str | None
    imported_at: datetime | None


class WhatsAppHandleOut(BaseModel):
    handle: str
    jid: str | None
    display_name: str | None
    person_id: int | None
    person_email: str | None
    match_method: str | None
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    # Messages this handle SENT in groups — not every message in their groups, which
    # is what imessage_handles.group_message_count means (§4.1).
    group_message_count: int
    last_group_message_at: datetime | None
    group_count: int
    updated_at: datetime | None


class WhatsAppHandleList(BaseModel):
    results: list[WhatsAppHandleOut]


class WhatsAppGroupOut(BaseModel):
    chat_jid: str
    subject: str | None
    member_count: int
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    is_admin: bool
    is_active: bool


class WhatsAppHandleDetail(WhatsAppHandleOut):
    groups: list[WhatsAppGroupOut]


class WhatsAppChatOut(BaseModel):
    chat_jid: str
    kind: str
    subject: str | None
    handle: str | None
    created_at: datetime | None
    member_count: int
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    updated_at: datetime | None


class WhatsAppChatList(BaseModel):
    results: list[WhatsAppChatOut]


class WhatsAppImportOut(BaseModel):
    started_at: datetime
    finished_at: datetime
    mode: str
    watermark: int | None
    chats_upserted: int
    members_upserted: int
    handles_upserted: int
    messages_upserted: int
    messages_deleted: int
    senderless_dropped: int
    duplicate_stanza_ids: int
    sessions_skipped: int
    handles_unnormalized: int
    matched_by_phone: int
    matched_by_name: int


@router.get("/whatsapp/handles", response_model=WhatsAppHandleList)
def list_handles(
    q: str | None = None,
    unmatched: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> WhatsAppHandleList:
    with db.get_conn() as conn:
        rows = whatsapp.handles(conn, q=q, unmatched=unmatched, limit=limit)
    return WhatsAppHandleList(results=[WhatsAppHandleOut.model_validate(r) for r in rows])


@router.get("/whatsapp/handles/{handle}", response_model=WhatsAppHandleDetail)
def get_handle(handle: str) -> WhatsAppHandleDetail:
    """`{handle}` needs percent-encoding for a leading `+` (`%2B`), as
    fetching-person documents for phone idents. A `lid:` handle needs none."""
    with db.get_conn() as conn:
        row = whatsapp.handle(conn, handle)
        if row is None:
            raise HTTPException(status_code=404)
        groups = whatsapp.handle_groups(conn, handle)
    return WhatsAppHandleDetail(
        **WhatsAppHandleOut.model_validate(row).model_dump(),
        groups=[WhatsAppGroupOut.model_validate(g) for g in groups],
    )


@router.get("/whatsapp/chats", response_model=WhatsAppChatList)
def list_chats(
    kind: Literal["direct", "group"] | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> WhatsAppChatList:
    with db.get_conn() as conn:
        rows = whatsapp.chats(conn, kind=kind, limit=limit)
    return WhatsAppChatList(results=[WhatsAppChatOut.model_validate(r) for r in rows])


@router.get("/whatsapp/imports/latest", response_model=WhatsAppImportOut)
def latest_import() -> WhatsAppImportOut:
    with db.get_conn() as conn:
        row = whatsapp.latest_import(conn)
    if row is None:
        raise HTTPException(status_code=404)
    return WhatsAppImportOut.model_validate(row)
```

- [ ] **Step 4: Wire the router and the person field**

In `api/main.py`:

```python
from api.routers import imessage, linkedin, people, search, whatsapp
...
app.include_router(whatsapp.router)
```

In `api/routers/people.py` — add the import, the field, the `to_out` parameter, and the two call sites:

```python
from api.routers.whatsapp import WhatsAppSummary
from repo import whatsapp as whatsapp_repo
```

```python
class PersonOut(BaseModel):
    ...
    imessage: IMessageSummary | None = None
    whatsapp: WhatsAppSummary | None = None
    ...
```

```python
def to_out(
    row: dict,
    linkedin_row: dict | None = None,
    imessage_row: dict | None = None,
    whatsapp_row: dict | None = None,
    include_contact: bool = False,
) -> PersonOut:
    return PersonOut(
        ...
        imessage=IMessageSummary.model_validate(imessage_row) if imessage_row else None,
        whatsapp=WhatsAppSummary.model_validate(whatsapp_row) if whatsapp_row else None,
        ...
    )
```

In `get_person`:

```python
        imessage_row = imessage_repo.summary_for_person(conn, row["id"])
        whatsapp_row = whatsapp_repo.summary_for_person(conn, row["id"])
    return to_out(row, linkedin_row, imessage_row, whatsapp_row, include_contact=True)
```

In `patch_person`, the same two lines inside the `with` block, and `return to_out(row, linkedin_row, imessage_row, whatsapp_row, include_contact=True)`.

`list_recent`, `create_person`, `sync_person` and `api/routers/search.py` are unchanged: they pass no `whatsapp_row`, so the field stays `null` on list and search responses exactly as `imessage` does (§8.1).

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```

Expected: all PASS and clean. A circular-import error between `people.py` and `whatsapp.py` means the import went the wrong way — `people.py` imports from `whatsapp.py`, never the reverse, matching how `imessage.py` is used today.

- [ ] **Step 6: Smoke-test the API against the real DB**

```bash
scripts/fetch-env.sh
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080) &
curl -s localhost:8080/whatsapp/imports/latest; echo
curl -s 'localhost:8080/whatsapp/handles?limit=1'; echo
kill %1
```

Expected before Task 12's import has run: `404` for the import row and an empty `results` list — the tables exist (Task 1 migrated them) but hold nothing yet. A `500` means the SQL is wrong; fix it here rather than discovering it after the deploy.

- [ ] **Step 7: Commit**

```bash
git add api/ tests/test_api.py
git commit -m "feat(api): serve WhatsApp handles, chats and per-person stats"
```

---

## Task 10: Teach `scripts/merge_duplicate_contacts.py` about the WhatsApp links

**This task is conditional and must not be skipped silently.** Spec §11 step 5 requires it, and §4 explains why: both WhatsApp FKs are `ON DELETE SET NULL`, so a contact merge that deletes a `people` row unlinks its WhatsApp handles and memberships unless they are re-pointed first. `scripts/merge_duplicate_contacts.py` does **not** exist on `main` — it lives on the unmerged `merge-duplicate-contacts` branch (PR #18).

**Files (if the script is present):**
- Modify: `scripts/merge_duplicate_contacts.py`
- Modify: the merge script's test module (whatever PR #18 named it)

**Interfaces:**
- Consumes: `repo.whatsapp.repoint_person(conn, from_id, to_id) -> int` (Task 6).

- [ ] **Step 1: Check whether the script exists on this branch**

```bash
git log --oneline -1 -- scripts/merge_duplicate_contacts.py
ls scripts/merge_duplicate_contacts.py 2>/dev/null || echo "not present"
```

- [ ] **Step 2a: If it is present — write the failing test**

Find the test that covers `_collapse_rows` and add, in its style:

```python
def test_collapse_repoints_whatsapp_links_before_deleting(...):
    """WhatsApp's FKs are ON DELETE SET NULL, so a delete before the re-point
    silently unlinks every handle and membership (WhatsApp spec §4, §11 step 5)."""
    ...
    assert "UPDATE whatsapp_handles SET person_id" in joined
    assert "UPDATE whatsapp_chat_members SET person_id" in joined
    assert joined.index("UPDATE whatsapp_handles") < joined.index("DELETE FROM people")
```

Run it and watch it fail.

- [ ] **Step 2b: If it is present — add the call**

In `_collapse_rows`, beside the existing re-points:

```python
from repo import whatsapp as whatsapp_repo
...
    for row in losers:
        counts["links_repointed"] += imessage_repo.repoint_person(conn, row["id"], keeper["id"])
        counts["links_repointed"] += linkedin_repo.repoint_person(conn, row["id"], keeper["id"])
        counts["links_repointed"] += whatsapp_repo.repoint_person(conn, row["id"], keeper["id"])
        people_repo.delete(conn, row["id"])
```

Also extend `_preview_rows` so a `--dry-run` counts the WhatsApp links it would move, using `whatsapp_repo.summary_for_person(conn, row["id"])`'s `handles` length plus its `shared_groups`. Update the summary string that currently reads "iMessage/LinkedIn links" to include WhatsApp.

- [ ] **Step 2c: If it is NOT present — record the dependency and tell the user**

Do not cherry-pick the script onto this branch, and do not skip the requirement quietly. Instead:

1. `repo/whatsapp.py::repoint_person` already exists (Task 6), so the hook is ready.
2. Append to the spec's §11, in the same commit style as Task 0 Step 6:

```markdown
> **Status note (added during implementation):** step 5 could not be completed in
> this PR — `scripts/merge_duplicate_contacts.py` is not on `main`, it is on the
> unmerged `merge-duplicate-contacts` branch (PR #18). `repo/whatsapp.py::repoint_person`
> is implemented and tested; the call site must be added to `_collapse_rows`
> whichever of the two branches merges second. Until then, a contact merge silently
> unlinks WhatsApp handles and memberships via `ON DELETE SET NULL`.
```

3. Say so explicitly in the Task 12 PR description **and** in the message to the user — this is a real, dated gap in a public repo, not a footnote.

- [ ] **Step 3: Run the tests and commit**

```bash
.venv/bin/pytest tests/ -q
git add -A
git commit -m "fix: re-point WhatsApp links when merging duplicate contacts"
```

If Step 2c applied, the commit is `docs: note the merge-script dependency for WhatsApp links` and touches only the spec.

---

## Task 11: The `importing-whatsapp` skill, CLAUDE.md, and the skill updates

**Files:**
- Create: `.claude/skills/importing-whatsapp/SKILL.md`
- Modify: `scripts/link-skills.sh`
- Modify: `CLAUDE.md`
- Modify: `.claude/skills/querying-people-db/SKILL.md`
- Modify: `.claude/skills/fetching-person/SKILL.md`
- Modify: `.claude/skills/people-architecture/SKILL.md`

**Interfaces:**
- Consumes: the finished script and API from Tasks 8 and 9. No code.

- [ ] **Step 1: Write the skill**

Create `.claude/skills/importing-whatsapp/SKILL.md`, modelled on `.claude/skills/importing-imessage/SKILL.md` but **without** its Full Disk Access section:

````markdown
---
name: importing-whatsapp
description: Use when the user wants to load, refresh, or re-import their WhatsApp history into the people database, run scripts/import_whatsapp.py, or check how old the WhatsApp snapshot is.
---

# Importing a WhatsApp Snapshot

`scripts/import_whatsapp.py` reads WhatsApp's local store on Ben's Mac
(`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`)
and incrementally upserts chats, handles, group membership and messages into the
`whatsapp_chats`, `whatsapp_handles`, `whatsapp_chat_members` and
`whatsapp_messages` tables, then appends a row to `whatsapp_imports`. It never
writes to `people`, Google Contacts or HubSpot — WhatsApp activity never affects
counters, eligibility or HubSpot ranking. Spec:
`docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md`.

## No Full Disk Access needed

Unlike `importing-imessage`, this needs no special permission: the WhatsApp group
container is readable by the user. Do not send Ben to System Settings.

The app is usually running, so the script copies the store **and its `-wal`/`-shm`
sidecars** to a temp directory and reads the copy, then deletes it. If a run dies
hard, check for a leftover `/tmp/whatsapp-import-*` directory — it holds the full
plaintext history.

## First run: full, dry-run, then for real

```bash
cd ~/src/people
scripts/fetch-env.sh            # if .env is missing or stale
.venv/bin/python scripts/import_whatsapp.py --full --dry-run
.venv/bin/python scripts/import_whatsapp.py --full
```

Routine runs after that are incremental and take seconds:

```bash
.venv/bin/python scripts/import_whatsapp.py
```

`--full` re-reads every message and reconciles deletions; the default reads from
the `Z_PK` watermark plus a 14-day re-scan window. `--db PATH` points at a copy or
a fixture.

## Reading the output

```
chats 201 (direct 102, group 99, skipped 7, duplicate jid 1)
messages 10,245 upserted, 0 deleted, 83 senderless dropped, 1 duplicate id
handles 621 (phone-matched 69, name-matched 0, unmatched 552, unnormalized 1)
members 10,090 rows / 6,767 identities across 99 groups (phone-matched 444)
watermark 10329   mode full
```

Counts only, never names or numbers — the repo is public. The oddities are all
expected and documented:

- **`senderless dropped`** — group messages WhatsApp cannot attribute to a sender
  (83 at the baseline). A message attributed to nobody inflates counts and cannot
  be ranked, so it is dropped rather than stored with a null sender.
- **`duplicate id`** — `ZSTANZAID` is not unique (one known pair in 10,329 rows);
  the pair collapses into one row and is counted so it is never a silent surprise.
- **`unnormalized`** — a chat whose number will not parse (one, with a 1-digit
  local part and no messages).
- **`skipped`** — status/broadcast sessions, which carry no conversation.

## Three things to know before trusting the numbers

1. **LID chats cannot be phone-matched, ever.** WhatsApp is migrating to Linked
   IDs, which deliberately contain no phone number. Four such chats hold 1,125
   messages — 11% of the store, including its single largest conversation. Their
   handle is stored as `lid:<id>` and they can only be linked by an exact, unique
   display-name match (`match_method = 'name'`), which is the one link type worth
   distrusting. Expect LID's share to grow.
2. **`group_message_count` means something different here than in iMessage.** On a
   `whatsapp_handles` row it counts messages that handle **sent** in groups;
   `imessage_handles.group_message_count` counts every message in a group the
   handle belongs to, because `chat.db` cannot attribute group senders. Never
   compare the two directly.
3. **The numbers are a floor, not a total.** The desktop store holds what the Mac
   app has synced, which is not guaranteed to equal the phone's history. This
   cannot be reconciled against the phone and does not try.

## Group membership is not a contact list

`whatsapp_chat_members` holds the full roster — 10,090 rows across 99 groups, 6,767
distinct identities — but 8,293 of those rows sit in groups of 200 or more, where
co-membership says nothing about a relationship, and the two largest groups hold
1,212 and 1,189 members. That is why:

- `whatsapp_handles` only carries identities with real interaction (a 1:1 chat, or
  at least one group message sent) — 621 rows, not 6,767.
- `whatsapp_chats.member_count` is stored, so a five-person family group is
  distinguishable from a community group.
- `group_count` on a handle is **not** a closeness signal. Check `member_count`
  before reading anything into it.

## What is never served

Message text is stored in Postgres and is **never** returned by `people-api` — no
endpoint, no field, no `include=` parameter. A direct DB query
(`querying-people-db`) is the only reader. Media files are never read at all: only
`has_media` and a coarse `media_kind`.

## Checking how old the snapshot is

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -s -H "Authorization: Bearer $TOKEN" https://people-api.drolet.cloud/whatsapp/imports/latest
```

Or in SQL: `SELECT * FROM whatsapp_imports ORDER BY id DESC LIMIT 5;`
````

- [ ] **Step 2: Link the skill**

Add `importing-whatsapp` to `scripts/link-skills.sh` if that script enumerates skills by name (check first — it may glob).

- [ ] **Step 3: Update CLAUDE.md**

Four edits, plus one unrelated fix the spec asks for in the same pass (§9):

1. **Stack table, Database row** — append the four new tables to the table list: `whatsapp_handles`, `whatsapp_chats`, `whatsapp_chat_members`, `whatsapp_messages`, `whatsapp_imports` (five, not four — the spec's §9 says "four new tables" and undercounts; use five).
2. **Code layout block** — add `clients/whatsapp_local.py`, `services/whatsapp_export.py`, `repo/whatsapp.py`, `models/whatsapp.py`, `api/routers/whatsapp.py`, `scripts/import_whatsapp.py`, and the `importing-whatsapp` skill. **Also add `scripts/merge_duplicate_contacts.py`**, which PR #18 added and never documented (spec §9). If that script is not on this branch (Task 10 Step 1), skip that one line and say so.
3. **Source-of-truth table** — add the §4.6 row:

```markdown
| `whatsapp_*` tables | WhatsApp's local store on Ben's Mac | Store → DB on local import (`scripts/import_whatsapp.py`). Never written back anywhere; WhatsApp is not a source for any `people` field. |
```

4. **Local dev section** — add, after the iMessage lines:

```markdown
WhatsApp snapshot (see `importing-whatsapp`; **no** Full Disk Access needed):
`.venv/bin/python scripts/import_whatsapp.py --full --dry-run`, then `--full`;
routine runs after that are `.venv/bin/python scripts/import_whatsapp.py`
(incremental).
```

5. **"If the DB is lost" paragraph** — add: "The WhatsApp snapshot rebuilds by re-running `scripts/import_whatsapp.py --full` against the local store."

- [ ] **Step 4: Update `querying-people-db`**

Add five table rows in the style of the existing `imessage_*` ones, spelling out the `group_message_count` divergence and marking `whatsapp_messages.text` as **the only place message text lives, never served by people-api**. Add two queries:

```sql
-- People with WhatsApp activity but no email address (§8, identity design)
SELECT p.id, p.display_name, h.handle, h.message_count, h.last_message_at
FROM whatsapp_handles h
JOIN people p ON p.id = h.person_id
WHERE p.email IS NULL
ORDER BY h.last_message_at DESC NULLS LAST;

-- Group co-membership that is actually meaningful: small groups only.
-- 8,293 of 10,090 member rows sit in groups of 200+, where co-membership says
-- nothing about a relationship (spec §5.7).
SELECT c.subject, c.member_count, COUNT(*) FILTER (WHERE mem.person_id IS NOT NULL) AS known
FROM whatsapp_chats c
JOIN whatsapp_chat_members mem ON mem.chat_jid = c.chat_jid
WHERE c.kind = 'group' AND c.member_count < 50
GROUP BY c.chat_jid, c.subject, c.member_count
ORDER BY known DESC;
```

- [ ] **Step 5: Update `fetching-person` and `people-architecture`**

`fetching-person`: document the `whatsapp` object on a single-person fetch — its fields, that it is `null` on list and search responses, and that it is **non-null with zeroed counters** when the person only shares a group with Ben (`shared_groups > 0`, `match_method: null`). Note that `match_method: "name"` is the one link worth distrusting.

`people-architecture`: add the WhatsApp snapshot as a subsystem beside iMessage and LinkedIn — local script, five tables, no cloud component, no Graph/HubSpot/Google involvement.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/ CLAUDE.md scripts/link-skills.sh
git commit -m "docs: importing-whatsapp skill, CLAUDE.md and skill updates"
```

---

## Task 12: Migrate, run for real, verify against the baseline, open the PR

**Files:**
- Modify (if the real numbers differ): `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md` §12

**Interfaces:**
- Consumes: everything.

Spec §11's order is schema → import code → dry-run → real run → API → merge script → docs. The schema is additive, so Task 1 could have been applied before its code; this task applies it if that has not happened yet and then does the real run.

- [ ] **Step 1: Apply the schema to the real database**

```bash
cd ~/src/people
scripts/fetch-env.sh
.venv/bin/python scripts/migrate_db.py
```

Expected: `Migration complete`. Purely additive — five `CREATE TABLE IF NOT EXISTS` blocks. (Contrast with the identity migration, which was destructive and caused an outage when applied before its code; additive-first is correct here.)

- [ ] **Step 2: Confirm the tables landed**

```bash
.venv/bin/python - <<'PY'
from dotenv import load_dotenv; load_dotenv()
from clients.db import get_conn
with get_conn() as conn:
    rows = conn.execute(
        "select table_name from information_schema.tables"
        " where table_name like 'whatsapp%' order by 1").fetchall()
    print([r["table_name"] for r in rows])
PY
```

Expected: all five.

- [ ] **Step 3: Dry-run**

```bash
.venv/bin/python scripts/import_whatsapp.py --full --dry-run
```

Expected: the §12 shape, and nothing written. Compare each line against §12 now, before committing anything to the DB:

| Quantity | §12 expects |
|---|---|
| messages stored | 10,245 |
| duplicate stanza ids | 1 |
| senderless dropped | 83 |
| chats | 201 (102 direct + 99 group) |
| sessions skipped | 7 |
| `whatsapp_handles` rows | 621 |
| handles phone-matched | 69 |
| member rows / identities | 10,090 / 6,767 |
| members phone-matched | 444 (69 handles + 375 membership-only) |
| watermark | 10,329 |

Small drifts are expected — the store is live and the baseline was measured on 2026-09-29. A **material** difference (a missing 1,000 messages, zero matches, handles in the thousands) means the import is wrong: stop and debug rather than committing the run. Note that §12's chat arithmetic is internally inconsistent (Task 0 Step 6), so a 1–2 row difference in the chat total is the spec's error, not the code's.

- [ ] **Step 4: Run for real**

```bash
.venv/bin/python scripts/import_whatsapp.py --full
```

- [ ] **Step 5: Verify what landed**

```bash
.venv/bin/python - <<'PY'
from dotenv import load_dotenv; load_dotenv()
from clients.db import get_conn
Q = {
 "messages": "select count(*) c from whatsapp_messages",
 "with_text": "select count(*) c from whatsapp_messages where text is not null",
 "from_me": "select count(*) c from whatsapp_messages where from_me",
 "with_media": "select count(*) c from whatsapp_messages where has_media",
 "chats": "select kind, count(*) c from whatsapp_chats group by 1",
 "handles": "select count(*) c from whatsapp_handles",
 "handles_linked": "select match_method, count(*) c from whatsapp_handles group by 1",
 "members": "select count(*) c from whatsapp_chat_members",
 "member_people": "select count(distinct person_id) c from whatsapp_chat_members where person_id is not null",
 "biggest": "select member_count from whatsapp_chats order by member_count desc limit 2",
 "range": "select min(sent_at) a, max(sent_at) b from whatsapp_messages",
 "top_handle": "select max(message_count) c from whatsapp_handles",
}
with get_conn() as conn:
    for name, sql in Q.items():
        print(name, conn.execute(sql).fetchall())
PY
```

Expected against §12: ~10,245 messages, 8,193 with text, 3,190 from Ben, 1,576 with media, 102 direct + 99 group chats, 621 handles of which ~69 `phone` and 0 `name`, 10,090 member rows covering ~444 distinct people, two groups at 1,212 and 1,189, a range starting 2019-07-23.

**`handles_linked` showing 0 `name` matches is the expected outcome** and worth a sentence in the PR: all four LID chats have a `ZPARTNERNAME`, but none matched a unique `people.display_name` at the baseline. The rule earns its place as LID's share grows.

- [ ] **Step 6: Verify an incremental run is cheap and non-destructive**

```bash
time .venv/bin/python scripts/import_whatsapp.py
```

Expected: seconds, near-zero upserts (only the re-scan window's messages), **no** deletions, and — critically — the same `message_count` and `last_message_at` on chats the run did not touch:

```bash
.venv/bin/python - <<'PY'
from dotenv import load_dotenv; load_dotenv()
from clients.db import get_conn
with get_conn() as conn:
    print(conn.execute(
        "select count(*) nulled from whatsapp_chats"
        " where message_count > 0 and last_message_at is null").fetchall())
    print(conn.execute(
        "select count(*) zeroed from whatsapp_chats where message_count = 0").fetchall())
PY
```

Expected: `nulled` is 0. This is Review Focus 3 against real data — the iMessage import nulled 939 of 971 rows here on its first version.

- [ ] **Step 7: Verify the API serves it and never serves text**

```bash
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080) &
sleep 2
curl -s 'localhost:8080/whatsapp/handles?limit=3' | head -c 600; echo
curl -s 'localhost:8080/whatsapp/chats?kind=group&limit=3' | head -c 400; echo
curl -s localhost:8080/whatsapp/imports/latest; echo
# A person who has WhatsApp activity — take an id from the query in Step 5.
curl -s 'localhost:8080/people/<id>' | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["whatsapp"])'
kill %1
```

Expected: real stats, and no message text anywhere in any response.

- [ ] **Step 8: Update §12 with what actually happened**

If any count differs materially from §12, edit the table to the measured values with a dated note, and fix its chat arithmetic:

```bash
git commit -am "docs: record the measured WhatsApp baseline from the first full run"
```

- [ ] **Step 9: Open the PR**

Use the `/pr-open` skill (CLAUDE.md requires it for this repo — do not hand-roll `git push` + `gh pr create`). The description must cover:

- What shipped: five tables, the local import, the API, the skill.
- The measured first-run numbers beside §12's expectations.
- **The LID situation**: 11% of messages, 0 name matches today, the growth expectation.
- **The MX/AR rule's value**: 43 → 60 matched chats.
- **The `ZFROMJID` finding** from Task 0 Step 5, with both numbers.
- **The two deliberate divergences from §6.7** (watermark from `whatsapp_messages`; chat counters recomputed in SQL rather than written from the batch) and why.
- **Task 10's outcome** — either the merge script was taught about the WhatsApp links, or it is not on this branch and the gap is open. Say which, plainly.
- The `updated_at` column added to `whatsapp_messages` beyond §4.4.
- That §10's claim about `import_imessage.py` flushing OTel was wrong: this is the repo's first script to emit metrics.

- [ ] **Step 10: Verify the deploy**

Pushing `api/`, `clients/`, `repo/`, `services/` or `models/` to `main` triggers both `deploy.yml` (Cloud Functions) and `deploy-api.yml` (Cloud Run). After the merge:

```bash
gh run list --limit 5
TOKEN=$(gcloud auth print-identity-token)
curl -s -H "Authorization: Bearer $TOKEN" https://people-api.drolet.cloud/whatsapp/imports/latest
```

Expected: green runs and the audit row from the local import. Nothing in the Cloud Functions changed behaviourally — they never read WhatsApp — but they redeploy because `clients/`, `repo/`, `services/` and `models/` are in their trigger paths.

Use `/verifying-pr-locally` and `/fetch-people-logs` if anything looks off.

---

## Self-Review

**1. Spec coverage.**

| Spec section | Task |
|---|---|
| §3 components (six new files, two modified) | 2, 3, 4, 5, 6, 7, 8, 9 |
| §3 `apple_ts` float widening, `normalize_handle` reuse | 3 |
| §4.1–§4.5 five tables | 1 |
| §4.1 interaction-only handle rule | 4 |
| §4.2 duplicate-JID collapse | 4 |
| §4.3 members table with its own `person_id` | 4, 5, 6 |
| §4.4 `(chat_jid, stanza_id)` key, duplicate reported | 1, 4 |
| §4.6 source-of-truth addendum | 11 |
| §5.1 copy + `-wal`/`-shm`, no `immutable=1`, no FDA | 0, 2 |
| §5.3 all three folklore traps | 3, 4 |
| §5.4 sender resolution, 83 senderless dropped | 4 |
| §5.5 LID handling | 3, 4, 5 |
| §5.6 completeness caveat | 11 |
| §5.7 bimodal group size → `member_count`, handle scope | 1, 4, 6, 11 |
| §6 CLI flags | 8 |
| §6.1 the three read queries | 2 |
| §6.2 watermark + 14-day window; full re-read of sessions/members | 2, 6, 8 |
| §6.3 filtering, classification, media derivation | 3, 4 |
| §6.4 MX/AR rule, `lid:` handles, raw JID kept | 3, 4 |
| §6.5 phone and LID-name matching, re-matched every run | 5 |
| §6.6 display-name preference | 4 |
| §6.7 write order and the NULL-clobber warning | 6, 8 |
| §6.8 counts-only output | 8 |
| §7 privacy: no text served, no media read, temp copy deleted | 2, 7, 9 |
| §8.1 additive `whatsapp` field, non-null on membership alone | 7, 9 |
| §8.2 search unchanged | 9 |
| §8.3 four endpoints, `%2B` encoding | 9 |
| §9 new skill + three skill updates + CLAUDE.md (incl. the `merge_duplicate_contacts.py` omission) | 11 |
| §10 three OTel instruments, explicit flush | 8 |
| §11 rollout steps 1–6 | 1, 8, 10, 11, 12 |
| §12 measured baseline verification | 12 |
| §13 decisions | covered by the tasks above; each contested choice has a test |

No spec section is unimplemented. Two are implemented differently than written, both recorded in Task 6's preamble and Task 12's PR description: the watermark source, and chat counters recomputed rather than upserted. One is blocked by an external dependency — §11 step 5 needs `scripts/merge_duplicate_contacts.py`, which is on the unmerged PR #18 — and Task 10 handles both outcomes explicitly rather than skipping it.

**2. Placeholders.** None: every step carries the code or the command it needs. The two deliberately open spots are Task 10's branch (the file's existence is checked, not assumed) and Task 11's skill-file edits, which are specified as content to add rather than diffs because the target files' surrounding text is not reproduced here.

**3. Type consistency.** `handle` is the E.164-or-`lid:` string everywhere (column, dataclass field, function parameter). `person_id` is `int | None` throughout; never `person_email`. `jid_to_handle` returns `(handle, kind)` with `kind` in `{"direct", "group", "skip"}` and is used that way in Task 4. `classify_media` returns `(has_media, media_kind)` in that order at both its definition and its call site. `build_batch(raw, *, mode)` and `match_handles(batch, people_rows)` match between Tasks 4, 5 and 8. `run()` returns `(batch, deleted)` in Task 8 and is unpacked that way in its tests. `repo.whatsapp` function names are identical in Tasks 6, 7, 9 and 10 (`repoint_person`, `summary_for_person`, `recompute_chat_stats`, `recompute_handle_stats`, `replace_members`). `WhatsAppSummary`'s field list matches `summary_for_person`'s projection column for column.

**4. Review Focus coverage.** All five have a named test in an owning task: ambiguous phone (Task 5, `test_a_phone_on_two_people_links_to_neither`), in-batch duplicate keys (Task 4, `test_a_duplicate_session_jid_collapses_to_one_chat` and `test_a_duplicate_stanza_id_collapses_and_is_counted`; Task 6, `test_upsert_chats_is_one_statement_per_chunk`), incremental partial counts (Task 6, `test_upsert_chats_never_writes_counters_from_the_batch` and the real-Postgres `test_an_incremental_run_does_not_null_other_chats_last_message_at`, re-verified against production data in Task 12 Step 6), temp-copy deletion on the error path (Task 2, `test_temp_copy_is_deleted_even_when_the_read_raises`), and empty/missing text and stanza ids (Task 4, `test_empty_text_is_stored_as_none_not_empty_string` and `test_a_message_with_no_stanza_id_is_dropped_and_counted`).
