# LinkedIn Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load Ben's manual LinkedIn data export (connections, messages, recommendations) into four snapshot tables in the `people` DB with a local script, soft-link connections to `people` rows, and serve the snapshot through `people-api`.

**Architecture:** `services/linkedin_export.py` is a pure parser: export dir/zip → `LinkedInSnapshot` (typed records, normalized URLs, per-connection message stats, recommendation links), plus `match_people()` which links connections to `people` rows by exact email and then by a name that is unique on both sides. `scripts/import_linkedin.py` parses first, then opens one DB transaction: read people names → match → `repo/linkedin.py::replace_snapshot` (delete + batched insert + audit row). `api/routers/linkedin.py` adds `/linkedin/...` read endpoints; `PersonOut` and `POST /search` gain additive `linkedin` / `linkedin_results` fields.

**Tech Stack:** Python 3.13 stdlib (`csv`, `zipfile`, `unicodedata`), psycopg3 (local) / pg8000 via Cloud SQL connector (`clients/db.py`), Postgres `pg_trgm`, FastAPI + pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md` — read it alongside this plan. Section references below (§N) are to that spec.

## Global Constraints

- **Public repo.** No real names, profile URLs, or message text in code, tests, or commits. Fixtures use invented people and `example`-style slugs/emails only. Ben's own profile URL is inferred or passed with `--me`, never hardcoded. Export files are gitignored (`*LinkedInDataExport*`, `linkedin-export*/`).
- **Read-only relative to everything else (§1).** Nothing in this plan writes to `people`, Google Contacts, or HubSpot. The only people-table access is a `SELECT email, display_name`.
- **No message content in logs or script output (§6).** The script prints counts and Ben's inferred profile URL only.
- **Layer rules (CLAUDE.md):** `models/` pure types; `services/` pure logic, no DB; `repo/` takes an open conn, never opens or commits; `api/routers/` thin; scripts orchestrate.
- **Matching (§5.4, §12):** exact email, then exact normalized name. Wrong links are worse than missing ones. This plan adds one guard the spec implies but does not spell out: a name match also requires the connection's normalized name to be unique among connections, so one `people` row never gets two name-linked connections.
- **Snapshot semantics (§4):** `linkedin_connections`, `linkedin_messages`, `linkedin_recommendations` fully replaced per import in one transaction; `linkedin_imports` append-only; no foreign keys to `people`.
- **Backward compatibility (§7.1):** existing response fields unchanged; new fields are additive. List responses (`GET /people`, `POST /search` `results`) return `linkedin: null`.
- **No Terraform, secrets, Cloud Functions, or inbox changes (§11).**
- **Local CI** (all must pass before each commit):
  `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
- **Branch:** work continues on `linkedin-snapshot-spec` (already holds the spec commit), so spec, plan, and code ship as one PR. The PR is opened with `/pr-open` in Task 11 — never hand-rolled `git push` + `gh pr create`.
- **Formatting:** code snippets in this plan are correct but not guaranteed ruff-formatter-exact. Run `.venv/bin/ruff format .` before the Local CI command in every task.
- Style: line length 100, ruff `E,F,I`; match existing idiom (module docstrings stating the contract, `dict` rows from repo functions, `%s` params).

## File structure

```
repo/schema.sql                        + four linkedin_* tables (§4)
models/linkedin.py                     NEW  LinkedInConnection / LinkedInMessage / LinkedInRecommendation / LinkedInSnapshot
services/linkedin_export.py            NEW  normalize_profile_url, normalize_name, read_table, parse_date,
                                             parse_timestamp, parse_export, match_people, ExportError
repo/linkedin.py                       NEW  replace_snapshot + reads: connection_for_person, search_connections,
                                             list_connections, get_connection, messages_for,
                                             recommendations_for, latest_import
repo/people.py                         + names_for_matching
scripts/import_linkedin.py             NEW  CLI: run(), summary(), main()
api/routers/linkedin.py                NEW  response models + /linkedin/connections, /linkedin/connections/{slug},
                                             /linkedin/imports/latest
api/routers/people.py                  + PersonOut.linkedin, to_out(row, linkedin_row=None)
api/routers/search.py                  + SearchResponse.linkedin_results
api/main.py                            + include linkedin router
tests/fixtures/linkedin/               NEW  synthetic export (4 CSVs)
tests/test_linkedin_export.py          NEW
tests/test_repo_linkedin.py            NEW
tests/test_import_linkedin.py          NEW
tests/test_api.py                      + linkedin cases, autouse fixture patches repo.linkedin
.gitignore                             + export patterns
.claude/skills/importing-linkedin/     NEW  SKILL.md
.claude/skills/{searching-people,fetching-person,querying-people-db,people-architecture}/SKILL.md  docs
CLAUDE.md                              layout, source-of-truth row, local import command, tables, skills
```

## Task map

| # | Task | Depends on |
|---|---|---|
| 0 | Confirm the real export's shape (gate) | — |
| 1 | Schema, models, `.gitignore` | 0 |
| 2 | Export primitives: normalization, header-driven CSV reader, date parsing | 1 |
| 3 | `parse_export`: connections, messages + stats, recommendations | 2 |
| 4 | `match_people` | 3 |
| 5 | `repo/linkedin.py::replace_snapshot` + `people.names_for_matching` | 1 |
| 6 | `repo/linkedin.py` read queries | 5 |
| 7 | `scripts/import_linkedin.py` | 4, 5 |
| 8 | `api/routers/linkedin.py` | 6 |
| 9 | Additive `linkedin` fields on person and search responses | 8 |
| 10 | Skills and CLAUDE.md | 7, 9 |
| 11 | Migrate, verify against the real export, open PR | 10 |

---

### Task 0: Confirm the real export's shape (gate)

§5.1 says column names and date formats "must be confirmed against Ben's real export before implementation." This task records the actual headers and formats **without reading or printing any personal content**. No code, no commit.

**Files:** none.

- [ ] **Step 1: Get the export path from Ben**

If Ben has not requested the export yet: LinkedIn → Settings → Data privacy → Get a copy of your data → "Want something in particular?" → tick **Connections**, **Messages**, **Recommendations** → Request archive. The download arrives by email (usually minutes, can take up to 24h). Stop here until Ben gives you the unzipped directory or `.zip` path. Store it in a shell variable for this session only:

```bash
EXPORT=~/Downloads/Basic_LinkedInDataExport_MM-DD-YYYY   # the path Ben gives
```

- [ ] **Step 2: List file names and print only header rows and date-column shapes**

```bash
ls "$EXPORT"
.venv/bin/python - "$EXPORT" <<'PY'
import csv, io, re, sys
from pathlib import Path
root = Path(sys.argv[1])
targets = {"connections.csv", "messages.csv", "recommendations_given.csv", "recommendations_received.csv"}
for p in sorted(root.rglob("*.csv")):
    if p.name.lower() not in targets:
        continue
    rows = list(csv.reader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
    hdr_i = next(i for i, r in enumerate(rows) if len(r) > 3)
    hdr = [h.strip() for h in rows[hdr_i]]
    print(f"\n{p.relative_to(root)}  preamble_rows={hdr_i}  data_rows={len(rows) - hdr_i - 1}")
    print("  header:", hdr)
    for col in ("Connected On", "DATE", "Creation Date"):
        if col in hdr:
            j = hdr.index(col)
            shapes = {re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", r[j])) for r in rows[hdr_i + 1:hdr_i + 200] if len(r) > j}
            print(f"  {col} shapes:", sorted(shapes)[:5])
    for col in ("URL", "SENDER PROFILE URL"):
        if col in hdr:
            j = hdr.index(col)
            forms = {re.sub(r"/in/[^/?,]+", "/in/<slug>", r[j]) for r in rows[hdr_i + 1:hdr_i + 200] if len(r) > j}
            print(f"  {col} forms:", sorted(forms)[:5])
PY
```

The script prints digit/letter *shapes* (`99 aaa 9999`) and URL forms with the slug masked — never names, emails, or content.

- [ ] **Step 3: Reconcile with this plan**

Compare against `_DATE_FORMATS` and `parse_timestamp` (Task 2), the file names and column sets `CONNECTIONS`/`MESSAGES`/`RECS_GIVEN`/`RECS_RECEIVED`, `_CONNECTION_COLS`, `_MESSAGE_COLS`, `_REC_COLS` (Task 3), and the Task 3 fixtures. If anything differs (column renamed, date shape not covered, file name different, recipient URLs separated by something other than `,`), edit **this plan** — the constants, fixtures, and expected test values — before starting Task 1, and tell Ben what changed. If everything matches, note "export shape confirmed" in your task report and continue.

---

### Task 1: Schema, models, `.gitignore`

**Files:**
- Modify: `repo/schema.sql` (append)
- Create: `models/linkedin.py`
- Modify: `.gitignore` (append)

**Interfaces:**
- Produces: the four tables in §4 exactly; dataclasses below — every later task uses these field names verbatim.

- [ ] **Step 1: Append the four tables to `repo/schema.sql`**

Append after the `sync_state` table (the file already starts with `CREATE EXTENSION IF NOT EXISTS pg_trgm;`):

```sql

-- LinkedIn snapshot (docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4).
-- connections/messages/recommendations are fully replaced by scripts/import_linkedin.py;
-- linkedin_imports is an append-only audit. No foreign keys to people: the link is advisory.

CREATE TABLE IF NOT EXISTS linkedin_connections (
    profile_url         TEXT PRIMARY KEY,
    first_name          TEXT,
    last_name           TEXT,
    full_name           TEXT NOT NULL,
    email               TEXT,
    company             TEXT,
    position            TEXT,
    connected_on        DATE,
    person_email        TEXT,
    match_method        TEXT,
    message_count       INT  NOT NULL DEFAULT 0,
    my_message_count    INT  NOT NULL DEFAULT 0,
    last_message_at     TIMESTAMPTZ,
    last_my_message_at  TIMESTAMPTZ,
    snapshot_at         TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS linkedin_connections_person_email_idx ON linkedin_connections (person_email);
CREATE INDEX IF NOT EXISTS linkedin_connections_name_trgm_idx ON linkedin_connections USING gin (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS linkedin_connections_company_trgm_idx ON linkedin_connections USING gin (company gin_trgm_ops);
CREATE INDEX IF NOT EXISTS linkedin_connections_position_trgm_idx ON linkedin_connections USING gin (position gin_trgm_ops);

CREATE TABLE IF NOT EXISTS linkedin_messages (
    id                      BIGSERIAL PRIMARY KEY,
    conversation_id         TEXT NOT NULL,
    conversation_title      TEXT,
    sender_name             TEXT,
    sender_profile_url      TEXT,
    recipient_names         TEXT,
    recipient_profile_urls  TEXT[] NOT NULL DEFAULT '{}',
    sent_at                 TIMESTAMPTZ NOT NULL,
    subject                 TEXT,
    content                 TEXT,
    folder                  TEXT,
    from_me                 BOOLEAN NOT NULL,
    snapshot_at             TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS linkedin_messages_conversation_idx ON linkedin_messages (conversation_id, sent_at);
CREATE INDEX IF NOT EXISTS linkedin_messages_participants_idx ON linkedin_messages USING gin ((recipient_profile_urls || ARRAY[sender_profile_url]));

CREATE TABLE IF NOT EXISTS linkedin_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    direction       TEXT NOT NULL CHECK (direction IN ('given', 'received')),
    first_name      TEXT,
    last_name       TEXT,
    full_name       TEXT NOT NULL,
    company         TEXT,
    job_title       TEXT,
    text            TEXT,
    status          TEXT,
    created_on      DATE,
    profile_url     TEXT,
    snapshot_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS linkedin_imports (
    id                       BIGSERIAL PRIMARY KEY,
    snapshot_at              TIMESTAMPTZ NOT NULL,
    source                   TEXT NOT NULL,
    connections              INT NOT NULL,
    messages                 INT NOT NULL,
    recommendations_given    INT NOT NULL,
    recommendations_received INT NOT NULL,
    matched_by_email         INT NOT NULL,
    matched_by_name          INT NOT NULL
);
```

- [ ] **Step 2: Create `models/linkedin.py`**

```python
"""LinkedIn snapshot records (docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4).
Pure types: built by services/linkedin_export.py, written by repo/linkedin.py."""

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class LinkedInConnection:
    profile_url: str  # normalized: linkedin.com/in/<slug>
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    company: str | None
    position: str | None
    connected_on: date | None
    person_email: str | None = None  # soft link to people.email
    match_method: str | None = None  # 'email' | 'name' | None
    message_count: int = 0
    my_message_count: int = 0
    last_message_at: datetime | None = None
    last_my_message_at: datetime | None = None


@dataclass
class LinkedInMessage:
    conversation_id: str
    conversation_title: str | None
    sender_name: str | None
    sender_profile_url: str | None  # normalized
    recipient_names: str | None
    recipient_profile_urls: list[str]  # normalized
    sent_at: datetime
    subject: str | None
    content: str | None
    folder: str | None
    from_me: bool = False


@dataclass
class LinkedInRecommendation:
    direction: str  # 'given' | 'received'
    first_name: str | None
    last_name: str | None
    full_name: str
    company: str | None
    job_title: str | None
    text: str | None
    status: str | None
    created_on: date | None
    profile_url: str | None = None  # connection with the unique matching name


@dataclass
class LinkedInSnapshot:
    source: str  # export directory or zip basename
    snapshot_at: datetime
    me: str | None  # Ben's normalized profile URL, inferred or --me
    connections: list[LinkedInConnection]
    messages: list[LinkedInMessage]
    recommendations: list[LinkedInRecommendation]
    conversations: int = 0
    messages_by_url: int = 0
    messages_by_name: int = 0
    messages_unmatched: int = 0
    messages_skipped: int = 0  # rows with no conversation id or an unparseable DATE
    missing_files: list[str] = field(default_factory=list)
    matched_by_email: int = 0
    matched_by_name: int = 0
```

- [ ] **Step 3: Append export patterns to `.gitignore`**

```gitignore

# LinkedIn data exports — personal data, never committed (LinkedIn snapshot spec §6)
*LinkedInDataExport*
linkedin-export*/
```

- [ ] **Step 4: Verify**

Run: `.venv/bin/python -c "import models.linkedin as m; print(m.LinkedInSnapshot.__dataclass_fields__.keys())"` — prints the field names.
Run: `git check-ignore -v --no-index Basic_LinkedInDataExport_09-16-2026.zip linkedin-export-1/Connections.csv` — both paths print a matching `.gitignore` rule.
Run the Local CI command. Expected: all green (no behaviour change yet).

- [ ] **Step 5: Commit**

```bash
git add repo/schema.sql models/linkedin.py .gitignore
git commit -m "feat: linkedin snapshot schema and models"
```

---
### Task 2: Export primitives — normalization, header-driven CSV reader, dates

**Files:**
- Create: `services/linkedin_export.py`
- Test: `tests/test_linkedin_export.py`

**Interfaces:**
- Consumes: `services.eligibility.normalize` (email normalizer, `str -> str`).
- Produces (all in `services/linkedin_export.py`):
  - `class ExportError(Exception)`
  - `normalize_profile_url(url: str | None) -> str | None` — `linkedin.com/in/<slug>` or `None`
  - `normalize_name(name: str | None) -> str` — `""` for blank
  - `read_table(text: str, required: set[str], filename: str) -> list[dict[str, str]]`
  - `parse_date(value: str) -> date | None`
  - `parse_timestamp(value: str) -> datetime | None` (UTC-aware)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linkedin_export.py`:

```python
from datetime import UTC, date, datetime

import pytest

from services import linkedin_export as lx


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.linkedin.com/in/Alice-Example/", "linkedin.com/in/alice-example"),
        ("http://linkedin.com/in/bob?trk=abc", "linkedin.com/in/bob"),
        ("www.linkedin.com/in/carol#top", "linkedin.com/in/carol"),
        ("  linkedin.com/in/dana  ", "linkedin.com/in/dana"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_profile_url(raw, expected):
    assert lx.normalize_profile_url(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Élodie  Accent", "elodie accent"),
        ("Alice E.", "alice e"),
        ("Mary-Jane O'Brien", "mary-jane o'brien"),
        ("  BOB   sample ", "bob sample"),
        (None, ""),
    ],
)
def test_normalize_name(raw, expected):
    assert lx.normalize_name(raw) == expected


PREAMBLE_CSV = (
    "Notes:\n"
    '"When exporting your connection data, you may notice, that some emails are missing."\n'
    "\n"
    "First Name,Last Name,URL,Extra Column\n"
    "Alice,Example,https://www.linkedin.com/in/alice-example,x\n"
    ",,,\n"
    "Bob,Sample,https://www.linkedin.com/in/bob-sample\n"
)


def test_read_table_skips_preamble_blank_rows_and_keeps_extra_columns():
    rows = lx.read_table(PREAMBLE_CSV, {"First Name", "URL"}, "Connections.csv")
    assert len(rows) == 2
    assert rows[0] == {
        "First Name": "Alice",
        "Last Name": "Example",
        "URL": "https://www.linkedin.com/in/alice-example",
        "Extra Column": "x",
    }
    assert rows[1]["Extra Column"] == ""  # short row padded, not an IndexError


def test_read_table_missing_required_column_raises():
    with pytest.raises(lx.ExportError, match="Connections.csv.*Connected On"):
        lx.read_table(PREAMBLE_CSV, {"First Name", "Connected On"}, "Connections.csv")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("16 Sep 2026", date(2026, 9, 16)),
        ("06/01/25 12:00 PM", date(2025, 6, 1)),
        ("06/01/25, 12:00 PM", date(2025, 6, 1)),
        ("06/01/2025", date(2025, 6, 1)),
        ("2025-06-01", date(2025, 6, 1)),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_date(raw, expected):
    assert lx.parse_date(raw) == expected


def test_parse_timestamp():
    assert lx.parse_timestamp("2026-09-16 17:03:12 UTC") == datetime(2026, 9, 16, 17, 3, 12, tzinfo=UTC)
    assert lx.parse_timestamp("not-a-date") is None
    assert lx.parse_timestamp("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_linkedin_export.py -q`
Expected: collection error `ImportError: cannot import name 'linkedin_export' from 'services'`.

- [ ] **Step 3: Implement the primitives**

Create `services/linkedin_export.py`:

```python
"""Parse a LinkedIn data export into a LinkedInSnapshot
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §5). Pure: reads
only the CSV files it is handed (a directory or a .zip), never the DB.

Parsing is header-driven because LinkedIn changes these files without notice:
scan for the header row, map columns by name, ignore unknown extras. A missing
required column raises ExportError before anything is written."""

import csv
import io
import re
import unicodedata
from datetime import UTC, date, datetime


class ExportError(Exception):
    """The export is unusable: a required file or column is missing."""


_URL_PREFIX = re.compile(r"^(https?://)?(www\.)?")
_DATE_FORMATS = ("%d %b %Y", "%m/%d/%y %I:%M %p", "%m/%d/%y, %I:%M %p", "%m/%d/%Y", "%Y-%m-%d")


def normalize_profile_url(url: str | None) -> str | None:
    u = (url or "").strip().lower()
    u = _URL_PREFIX.sub("", u).split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return u or None


def normalize_name(name: str | None) -> str:
    """Matching key only — stored names keep their original form (§5.2)."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^\w\s'-]", " ", s)
    return " ".join(s.split())


def read_table(text: str, required: set[str], filename: str) -> list[dict[str, str]]:
    """Rows after the first row that contains every required column, keyed by
    header name. Tolerates a free-text preamble, extra columns, short rows, and
    blank rows."""
    reader = csv.reader(io.StringIO(text))
    for header in reader:
        names = [h.strip() for h in header]
        if required <= set(names):
            return [
                {name: (row[i].strip() if i < len(row) else "") for i, name in enumerate(names)}
                for row in reader
                if any(cell.strip() for cell in row)
            ]
    raise ExportError(f"{filename}: no header row with columns {sorted(required)}")


def parse_date(value: str) -> date | None:
    v = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_timestamp(value: str) -> datetime | None:
    v = (value or "").strip().removesuffix(" UTC")
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_linkedin_export.py -q`
Expected: all pass. Then run the Local CI command.

- [ ] **Step 5: Commit**

```bash
git add services/linkedin_export.py tests/test_linkedin_export.py
git commit -m "feat: linkedin export normalization and header-driven csv reader"
```

---

### Task 3: `parse_export` — connections, messages + stats, recommendations

**Files:**
- Create: `tests/fixtures/linkedin/Connections.csv`, `messages.csv`, `Recommendations_Given.csv`, `Recommendations_Received.csv`
- Modify: `services/linkedin_export.py`
- Test: `tests/test_linkedin_export.py` (append)

**Interfaces:**
- Consumes: Task 1 dataclasses; Task 2 primitives.
- Produces: `parse_export(path: Path, *, me: str | None = None, now: datetime | None = None) -> LinkedInSnapshot`. `me` is normalized with `normalize_profile_url`; when `None` it is inferred (§5.3). `now` defaults to `datetime.now(UTC)` and becomes `snapshot_at`. `source` is `path.name`. `matched_by_*` stay `0` (Task 4 fills them).

**Behaviour this task pins down** (beyond §5):
- Connection rows with a blank `URL` are dropped (it is the primary key); duplicate URLs keep the last row.
- `messages_skipped` counts message rows with no `CONVERSATION ID` or an unparseable `DATE`; they are not stored.
- "Me" = the normalized URL present in the most distinct conversations; ties break on the lexicographically smallest URL; `None` when there are no messages.
- A message is **by-url** if any participant URL is a connection. Otherwise it tries the **name fallback**: the sender name (unless `from_me`) and each comma-separated recipient name is looked up among connection names that are unique after `normalize_name`. A resolved sender replaces `sender_profile_url`; a resolved recipient URL is appended to `recipient_profile_urls`. Writing the URL back means the detail endpoint's participant query (Task 6) returns the same messages the stats counted. Anything else is **unmatched**.
- Each matched connection gets `message_count += 1`, `last_message_at = max(...)`; if `from_me`, also `my_message_count += 1`, `last_my_message_at = max(...)`.
- Recommendation `profile_url` = the connection whose unique normalized name equals the recommendation's normalized `full_name`.
- `missing_files` lists the canonical names (`messages.csv`, `Recommendations_Given.csv`, `Recommendations_Received.csv`) of absent optional files. A missing `Connections.csv` raises `ExportError`.
- File lookup is by case-insensitive basename anywhere under the directory (or in the zip), first match in sorted order.

- [ ] **Step 1: Create the fixture export**

All people are invented. Ben is `linkedin.com/in/me-example`.

`tests/fixtures/linkedin/Connections.csv`:

```csv
Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing. You will only see email addresses for connections who have allowed it."

First Name,Last Name,URL,Email Address,Company,Position,Connected On,Extra Column
Alice,Example,https://www.linkedin.com/in/alice-example,Alice@Example.com,Example Health,CTO,02 Apr 2021,x
Bob,Sample,https://www.linkedin.com/in/Bob-Sample/,,Sample Corp,VP Engineering,15 Jan 2019,
Carol,Test,https://www.linkedin.com/in/carol-test?trk=abc,,Test Labs,Founder,01 Mar 2020,
Dana,Dup,https://www.linkedin.com/in/dana-dup-1,,Dup Inc,Engineer,05 May 2022,
Dana,Dup,https://www.linkedin.com/in/dana-dup-2,,Other Inc,Designer,06 May 2022,
Élodie,Accent,https://www.linkedin.com/in/elodie-accent,,Accent SA,Partner,07 Jun 2023,
Nobody,Nourl,,,,,08 Jul 2023,
```

`tests/fixtures/linkedin/messages.csv`:

```csv
CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER
c1,,Alice Example,https://www.linkedin.com/in/alice-example,Me Example,https://www.linkedin.com/in/me-example,2026-01-10 09:00:00 UTC,,Hi there,INBOX
c1,,Me Example,https://www.linkedin.com/in/me-example,Alice Example,https://www.linkedin.com/in/alice-example,2026-01-11 10:00:00 UTC,,Hello back,INBOX
c1,,Alice Example,https://www.linkedin.com/in/alice-example,Me Example,https://www.linkedin.com/in/me-example,2026-02-01 08:30:00 UTC,,"Coffee, next week?",INBOX
c2,Group chat,Me Example,https://www.linkedin.com/in/me-example,"Alice Example,Bob Sample","https://www.linkedin.com/in/alice-example,https://www.linkedin.com/in/bob-sample",2025-06-01 12:00:00 UTC,,Intro,INBOX
c2,Group chat,Bob Sample,https://www.linkedin.com/in/bob-sample,"Me Example,Alice Example","https://www.linkedin.com/in/me-example,https://www.linkedin.com/in/alice-example",2025-06-02 12:00:00 UTC,,Thanks,INBOX
c3,,Rita Recruiter,https://www.linkedin.com/in/rita-recruiter,Me Example,https://www.linkedin.com/in/me-example,2026-03-01 15:00:00 UTC,Opportunity,Are you open?,INBOX
c4,,Carol Test,,Me Example,https://www.linkedin.com/in/me-example,2024-12-24 18:00:00 UTC,,Happy holidays,INBOX
c4,,Me Example,https://www.linkedin.com/in/me-example,Carol Test,,not-a-date,,bad row,INBOX
```

`tests/fixtures/linkedin/Recommendations_Given.csv`:

```csv
First Name,Last Name,Company,Job Title,Text,Creation Date,Status
Bob,Sample,Sample Corp,VP Engineering,Bob is great.,06/01/25 12:00 PM,VISIBLE
Dana,Dup,Dup Inc,Engineer,Dana ships.,05/06/22 09:00 AM,VISIBLE
```

`tests/fixtures/linkedin/Recommendations_Received.csv`:

```csv
First Name,Last Name,Company,Job Title,Text,Creation Date,Status
Élodie,Accent,Accent SA,Partner,A pleasure to work with.,07/08/23 10:00 AM,VISIBLE
```

Expected results the tests below encode (derive them yourself once to be sure you understand the rules):

| connection | message_count | my_message_count | last_message_at | last_my_message_at |
|---|---|---|---|---|
| alice-example | 5 (c1×3, c2×2) | 2 | 2026-02-01 08:30 | 2026-01-11 10:00 |
| bob-sample | 2 (c2×2) | 1 | 2025-06-02 12:00 | 2025-06-01 12:00 |
| carol-test | 1 (c4, by name) | 0 | 2024-12-24 18:00 | — |
| dana-dup-1/2, elodie-accent | 0 | 0 | — | — |

Messages: 7 stored, 1 skipped, 4 conversations; by-url 5, by-name 1, unmatched 1 (c3).

- [ ] **Step 2: Append the failing tests**

Append to `tests/test_linkedin_export.py` (add `import shutil`, `import zipfile`, `from pathlib import Path` to the imports at the top, keeping ruff's import order):

```python
FIXTURE = Path(__file__).parent / "fixtures" / "linkedin"
ME = "linkedin.com/in/me-example"
NOW = datetime(2026, 9, 16, 18, 4, 11, tzinfo=UTC)


def conns(snapshot):
    return {c.profile_url: c for c in snapshot.connections}


def test_parse_export_connections():
    s = lx.parse_export(FIXTURE, now=NOW)
    assert s.source == "linkedin" and s.snapshot_at == NOW
    by_url = conns(s)
    assert sorted(by_url) == [
        "linkedin.com/in/alice-example",
        "linkedin.com/in/bob-sample",
        "linkedin.com/in/carol-test",
        "linkedin.com/in/dana-dup-1",
        "linkedin.com/in/dana-dup-2",
        "linkedin.com/in/elodie-accent",
    ]
    alice = by_url["linkedin.com/in/alice-example"]
    assert alice.full_name == "Alice Example" and alice.email == "alice@example.com"
    assert alice.company == "Example Health" and alice.connected_on == date(2021, 4, 2)
    assert by_url["linkedin.com/in/bob-sample"].email is None
    assert by_url["linkedin.com/in/elodie-accent"].full_name == "Élodie Accent"


def test_parse_export_infers_me_and_counts_messages():
    s = lx.parse_export(FIXTURE, now=NOW)
    assert s.me == ME
    assert len(s.messages) == 7 and s.messages_skipped == 1 and s.conversations == 4
    assert (s.messages_by_url, s.messages_by_name, s.messages_unmatched) == (5, 1, 1)
    assert sum(m.from_me for m in s.messages) == 2
    c2 = [m for m in s.messages if m.conversation_id == "c2"][0]
    assert c2.recipient_profile_urls == ["linkedin.com/in/alice-example", "linkedin.com/in/bob-sample"]
    assert c2.conversation_title == "Group chat" and c2.subject is None


def test_parse_export_per_connection_stats():
    by_url = conns(lx.parse_export(FIXTURE, now=NOW))
    alice = by_url["linkedin.com/in/alice-example"]
    assert (alice.message_count, alice.my_message_count) == (5, 2)
    assert alice.last_message_at == datetime(2026, 2, 1, 8, 30, tzinfo=UTC)
    assert alice.last_my_message_at == datetime(2026, 1, 11, 10, 0, tzinfo=UTC)
    bob = by_url["linkedin.com/in/bob-sample"]
    assert (bob.message_count, bob.my_message_count) == (2, 1)
    assert bob.last_my_message_at == datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    dana = by_url["linkedin.com/in/dana-dup-1"]
    assert (dana.message_count, dana.last_message_at) == (0, None)


def test_name_fallback_counts_and_writes_url_back():
    s = lx.parse_export(FIXTURE, now=NOW)
    carol = conns(s)["linkedin.com/in/carol-test"]
    assert (carol.message_count, carol.my_message_count) == (1, 0)
    assert carol.last_message_at == datetime(2024, 12, 24, 18, 0, tzinfo=UTC)
    c4 = [m for m in s.messages if m.conversation_id == "c4"][0]
    assert c4.sender_profile_url == "linkedin.com/in/carol-test"


def test_me_override_is_normalized():
    s = lx.parse_export(FIXTURE, me="https://www.linkedin.com/in/Alice-Example/", now=NOW)
    assert s.me == "linkedin.com/in/alice-example"
    alice = conns(s)["linkedin.com/in/alice-example"]
    assert alice.my_message_count == 2  # the two c1 messages Alice sent


def test_recommendations_link_by_unique_name():
    s = lx.parse_export(FIXTURE, now=NOW)
    given = {r.full_name: r for r in s.recommendations if r.direction == "given"}
    received = [r for r in s.recommendations if r.direction == "received"]
    assert given["Bob Sample"].profile_url == "linkedin.com/in/bob-sample"
    assert given["Bob Sample"].created_on == date(2025, 6, 1)
    assert given["Dana Dup"].profile_url is None  # two Dana Dup connections: ambiguous
    assert received[0].profile_url == "linkedin.com/in/elodie-accent"


def test_missing_optional_files_are_empty_and_reported(tmp_path):
    shutil.copy(FIXTURE / "Connections.csv", tmp_path / "Connections.csv")
    s = lx.parse_export(tmp_path, now=NOW)
    assert len(s.connections) == 6 and s.messages == [] and s.recommendations == []
    assert s.me is None and s.conversations == 0
    assert s.missing_files == ["messages.csv", "Recommendations_Given.csv", "Recommendations_Received.csv"]


def test_missing_connections_file_raises(tmp_path):
    with pytest.raises(lx.ExportError, match="Connections.csv"):
        lx.parse_export(tmp_path, now=NOW)


def test_missing_required_message_column_raises(tmp_path):
    shutil.copy(FIXTURE / "Connections.csv", tmp_path / "Connections.csv")
    (tmp_path / "messages.csv").write_text("CONVERSATION ID,FROM\nc1,Alice\n")
    with pytest.raises(lx.ExportError, match="messages.csv"):
        lx.parse_export(tmp_path, now=NOW)


def test_zip_and_nested_directory_inputs(tmp_path):
    archive = tmp_path / "Basic_Export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for p in FIXTURE.iterdir():
            zf.write(p, f"nested/{p.name}")
    s = lx.parse_export(archive, now=NOW)
    assert s.source == "Basic_Export.zip" and len(s.connections) == 6 and len(s.messages) == 7
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_linkedin_export.py -q`
Expected: the new tests FAIL with `AttributeError: module 'services.linkedin_export' has no attribute 'parse_export'`; Task 2 tests still pass.

- [ ] **Step 4: Implement `parse_export`**

In `services/linkedin_export.py`, extend the imports to:

```python
import csv
import io
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from models.linkedin import (
    LinkedInConnection,
    LinkedInMessage,
    LinkedInRecommendation,
    LinkedInSnapshot,
)
from services.eligibility import normalize as normalize_email
```

Then append below `parse_timestamp`:

```python
CONNECTIONS = "Connections.csv"
MESSAGES = "messages.csv"
RECS_GIVEN = "Recommendations_Given.csv"
RECS_RECEIVED = "Recommendations_Received.csv"

_CONNECTION_COLS = {"First Name", "Last Name", "URL", "Company", "Position", "Connected On"}
_MESSAGE_COLS = {
    "CONVERSATION ID",
    "FROM",
    "SENDER PROFILE URL",
    "TO",
    "RECIPIENT PROFILE URLS",
    "DATE",
    "CONTENT",
}
_REC_COLS = {"First Name", "Last Name", "Text", "Creation Date", "Status"}


def parse_export(
    path: Path, *, me: str | None = None, now: datetime | None = None
) -> LinkedInSnapshot:
    files = _read_files(path)
    if CONNECTIONS not in files:
        raise ExportError(f"{path}: {CONNECTIONS} not found")
    # Parse every table before any derived work so a bad column fails fast.
    connection_rows = read_table(files[CONNECTIONS], _CONNECTION_COLS, CONNECTIONS)
    message_rows = read_table(files[MESSAGES], _MESSAGE_COLS, MESSAGES) if MESSAGES in files else []
    given_rows = read_table(files[RECS_GIVEN], _REC_COLS, RECS_GIVEN) if RECS_GIVEN in files else []
    received_rows = (
        read_table(files[RECS_RECEIVED], _REC_COLS, RECS_RECEIVED) if RECS_RECEIVED in files else []
    )

    connections = _parse_connections(connection_rows)
    messages, skipped = _parse_messages(message_rows)
    me_url = normalize_profile_url(me) if me else _infer_me(messages)
    counts = _attach_messages(connections, messages, me_url)
    names = _unique_by_name((c.full_name, c.profile_url) for c in connections)
    recommendations = _parse_recommendations(given_rows, "given", names)
    recommendations += _parse_recommendations(received_rows, "received", names)

    return LinkedInSnapshot(
        source=path.name,
        snapshot_at=now or datetime.now(UTC),
        me=me_url,
        connections=connections,
        messages=messages,
        recommendations=recommendations,
        conversations=len({m.conversation_id for m in messages}),
        messages_by_url=counts["by_url"],
        messages_by_name=counts["by_name"],
        messages_unmatched=counts["unmatched"],
        messages_skipped=skipped,
        missing_files=[name for name in (MESSAGES, RECS_GIVEN, RECS_RECEIVED) if name not in files],
    )


def _read_files(path: Path) -> dict[str, str]:
    """Canonical file name → text, matched by case-insensitive basename."""
    wanted = {name.lower(): name for name in (CONNECTIONS, MESSAGES, RECS_GIVEN, RECS_RECEIVED)}
    found: dict[str, str] = {}
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for member in sorted(zf.namelist()):
                name = wanted.get(Path(member).name.lower())
                if name and name not in found:
                    found[name] = zf.read(member).decode("utf-8-sig")
    elif path.is_dir():
        for p in sorted(path.rglob("*")):
            name = wanted.get(p.name.lower())
            if p.is_file() and name and name not in found:
                found[name] = p.read_text(encoding="utf-8-sig")
    else:
        raise ExportError(f"{path}: not a directory or .zip")
    return found


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(p for p in (first, last) if p)


def _parse_connections(rows: list[dict[str, str]]) -> list[LinkedInConnection]:
    by_url: dict[str, LinkedInConnection] = {}
    for r in rows:
        url = normalize_profile_url(r.get("URL"))
        if url is None:
            continue
        first, last = r.get("First Name") or None, r.get("Last Name") or None
        by_url[url] = LinkedInConnection(
            profile_url=url,
            first_name=first,
            last_name=last,
            full_name=_full_name(first, last),
            email=normalize_email(r.get("Email Address", "")) or None,
            company=r.get("Company") or None,
            position=r.get("Position") or None,
            connected_on=parse_date(r.get("Connected On", "")),
        )
    return list(by_url.values())


def _parse_messages(rows: list[dict[str, str]]) -> tuple[list[LinkedInMessage], int]:
    messages: list[LinkedInMessage] = []
    skipped = 0
    for r in rows:
        sent_at = parse_timestamp(r.get("DATE", ""))
        if sent_at is None or not r.get("CONVERSATION ID"):
            skipped += 1
            continue
        recipients = (normalize_profile_url(u) for u in r.get("RECIPIENT PROFILE URLS", "").split(","))
        messages.append(
            LinkedInMessage(
                conversation_id=r["CONVERSATION ID"],
                conversation_title=r.get("CONVERSATION TITLE") or None,
                sender_name=r.get("FROM") or None,
                sender_profile_url=normalize_profile_url(r.get("SENDER PROFILE URL")),
                recipient_names=r.get("TO") or None,
                recipient_profile_urls=[u for u in recipients if u],
                sent_at=sent_at,
                subject=r.get("SUBJECT") or None,
                content=r.get("CONTENT") or None,
                folder=r.get("FOLDER") or None,
            )
        )
    return messages, skipped


def _participants(m: LinkedInMessage) -> set[str]:
    return {u for u in (m.sender_profile_url, *m.recipient_profile_urls) if u}


def _infer_me(messages: list[LinkedInMessage]) -> str | None:
    """Ben is the participant present in the most conversations (§5.3)."""
    seen: dict[str, set[str]] = defaultdict(set)
    for m in messages:
        for url in _participants(m):
            seen[url].add(m.conversation_id)
    if not seen:
        return None
    return min(seen, key=lambda u: (-len(seen[u]), u))


def _unique_by_name(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """normalized name → value, keeping only names that occur exactly once."""
    counts: Counter[str] = Counter()
    first: dict[str, str] = {}
    for name, value in items:
        key = normalize_name(name)
        if key:
            counts[key] += 1
            first.setdefault(key, value)
    return {k: v for k, v in first.items() if counts[k] == 1}


def _later(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


def _attach_messages(
    connections: list[LinkedInConnection], messages: list[LinkedInMessage], me: str | None
) -> Counter[str]:
    by_url = {c.profile_url: c for c in connections}
    names = _unique_by_name((c.full_name, c.profile_url) for c in connections)
    counts: Counter[str] = Counter(by_url=0, by_name=0, unmatched=0)
    for m in messages:
        m.from_me = me is not None and m.sender_profile_url == me
        matched = [by_url[u] for u in sorted(_participants(m)) if u in by_url]
        if matched:
            counts["by_url"] += 1
        else:
            matched = _match_by_name(m, by_url, names)
            counts["by_name" if matched else "unmatched"] += 1
        for c in matched:
            c.message_count += 1
            c.last_message_at = _later(c.last_message_at, m.sent_at)
            if m.from_me:
                c.my_message_count += 1
                c.last_my_message_at = _later(c.last_my_message_at, m.sent_at)
    return counts


def _match_by_name(
    m: LinkedInMessage, by_url: dict[str, LinkedInConnection], names: dict[str, str]
) -> list[LinkedInConnection]:
    """URL fallback (§5.3). Writes resolved URLs back onto the message so the
    detail endpoint's participant query finds the same messages the stats counted."""
    matched: list[LinkedInConnection] = []
    if not m.from_me:
        url = names.get(normalize_name(m.sender_name))
        if url:
            m.sender_profile_url = url
            matched.append(by_url[url])
    for name in (m.recipient_names or "").split(","):
        url = names.get(normalize_name(name))
        if url and url not in m.recipient_profile_urls:
            m.recipient_profile_urls.append(url)
            matched.append(by_url[url])
    return matched


def _parse_recommendations(
    rows: list[dict[str, str]], direction: str, names: dict[str, str]
) -> list[LinkedInRecommendation]:
    out: list[LinkedInRecommendation] = []
    for r in rows:
        first, last = r.get("First Name") or None, r.get("Last Name") or None
        full = _full_name(first, last)
        out.append(
            LinkedInRecommendation(
                direction=direction,
                first_name=first,
                last_name=last,
                full_name=full,
                company=r.get("Company") or None,
                job_title=r.get("Job Title") or None,
                text=r.get("Text") or None,
                status=r.get("Status") or None,
                created_on=parse_date(r.get("Creation Date", "")),
                profile_url=names.get(normalize_name(full)),
            )
        )
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_linkedin_export.py -q`
Expected: all pass. Then run the Local CI command (`ruff format` may reflow long lines — run `.venv/bin/ruff format services/ tests/` and re-run the tests if so).

- [ ] **Step 6: Commit**

```bash
git add services/linkedin_export.py tests/test_linkedin_export.py tests/fixtures/linkedin/
git commit -m "feat: parse linkedin export into a snapshot with per-connection message stats"
```

---

### Task 4: `match_people`

**Files:**
- Modify: `services/linkedin_export.py` (append)
- Test: `tests/test_linkedin_export.py` (append)

**Interfaces:**
- Consumes: `LinkedInSnapshot` from Task 3; people rows shaped `{"email": str, "display_name": str | None}` (Task 5's `people.names_for_matching`).
- Produces: `match_people(snapshot: LinkedInSnapshot, people_rows: list[dict]) -> None` — sets `person_email` / `match_method` on every connection (clearing previous values) and `snapshot.matched_by_email` / `snapshot.matched_by_name`.

Rules (§5.4 plus the Global Constraints guard): (1) **email** — connection `email` equals a normalized `people.email`. (2) **name** — normalized connection `full_name` equals the normalized `display_name` of exactly one people row, that row's email is not already claimed, **and** the connection's normalized name is unique among connections.

- [ ] **Step 1: Append the failing tests**

```python
PEOPLE = [
    # Alice matches by email; her display_name collides with Élodie's to prove a
    # claimed email is never re-used by a name match.
    {"email": "alice@example.com", "display_name": "Elodie Accent"},
    {"email": "Bob@Work.example", "display_name": "Bob  Sample"},
    {"email": "dana@dup.example", "display_name": "Dana Dup"},
    {"email": "carol@a.example", "display_name": "Carol Test"},
    {"email": "carol@b.example", "display_name": "carol test"},
    {"email": "nameless@x.example", "display_name": None},
]


def test_match_people():
    s = lx.parse_export(FIXTURE, now=NOW)
    lx.match_people(s, PEOPLE)
    by_url = conns(s)
    links = {url: (c.person_email, c.match_method) for url, c in by_url.items()}
    assert links == {
        "linkedin.com/in/alice-example": ("alice@example.com", "email"),
        "linkedin.com/in/bob-sample": ("bob@work.example", "name"),
        "linkedin.com/in/carol-test": (None, None),  # two people named Carol Test
        "linkedin.com/in/dana-dup-1": (None, None),  # two connections named Dana Dup
        "linkedin.com/in/dana-dup-2": (None, None),
        "linkedin.com/in/elodie-accent": (None, None),  # only candidate already claimed by email
    }
    assert (s.matched_by_email, s.matched_by_name) == (1, 1)


def test_match_people_rerun_clears_stale_links():
    s = lx.parse_export(FIXTURE, now=NOW)
    lx.match_people(s, PEOPLE)
    lx.match_people(s, [])
    assert all(c.person_email is None and c.match_method is None for c in s.connections)
    assert (s.matched_by_email, s.matched_by_name) == (0, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_linkedin_export.py -q -k match_people`
Expected: FAIL with `AttributeError: ... has no attribute 'match_people'`.

- [ ] **Step 3: Implement**

Append to `services/linkedin_export.py`:

```python
def match_people(snapshot: LinkedInSnapshot, people_rows: list[dict]) -> None:
    """Soft-link connections to people rows (§5.4): exact email first, then a
    normalized name unique among people and among connections. Wrong links are
    worse than missing ones."""
    emails = {normalize_email(r["email"]) for r in people_rows}
    for c in snapshot.connections:
        c.person_email = c.match_method = None
        if c.email and c.email in emails:
            c.person_email, c.match_method = c.email, "email"
    claimed = {c.person_email for c in snapshot.connections if c.person_email}

    people_by_name = _unique_by_name(
        (r.get("display_name") or "", normalize_email(r["email"])) for r in people_rows
    )
    connection_names = _unique_by_name((c.full_name, c.profile_url) for c in snapshot.connections)
    for c in snapshot.connections:
        if c.person_email:
            continue
        key = normalize_name(c.full_name)
        email = people_by_name.get(key)
        if email and email not in claimed and connection_names.get(key) == c.profile_url:
            c.person_email, c.match_method = email, "name"
            claimed.add(email)

    snapshot.matched_by_email = sum(c.match_method == "email" for c in snapshot.connections)
    snapshot.matched_by_name = sum(c.match_method == "name" for c in snapshot.connections)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_linkedin_export.py -q` — all pass. Then the Local CI command.

- [ ] **Step 5: Commit**

```bash
git add services/linkedin_export.py tests/test_linkedin_export.py
git commit -m "feat: soft-link linkedin connections to people by email then unique name"
```

---

### Task 5: `repo/linkedin.py::replace_snapshot` and `people.names_for_matching`

**Files:**
- Create: `repo/linkedin.py`
- Modify: `repo/people.py` (append)
- Test: `tests/test_repo_linkedin.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces:
  - `repo.people.names_for_matching(conn) -> list[dict]` — every row's `email`, `display_name`.
  - `repo.linkedin.replace_snapshot(conn, snapshot: LinkedInSnapshot, chunk: int = 500) -> None` — the caller owns the transaction (commit/rollback); this function never commits.

Implementation notes:
- Neither `psycopg.Connection` nor `clients/db.py::_Pg8000Conn` exposes `executemany`, so batch with multi-row `VALUES` through `conn.execute`. 500 rows × 13 columns stays far below pg8000's 32,767-parameter limit.
- `recipient_profile_urls` uses the placeholder `%s::text[]` so an empty Python list binds as `'{}'` on pg8000 as well as psycopg3 (`_adapt_params` passes lists of strings through untouched; it only rewrites all-numeric lists).
- Skip the `INSERT` for an empty list (an empty `VALUES` is a syntax error).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repo_linkedin.py`:

```python
from datetime import UTC, date, datetime

from models.linkedin import (
    LinkedInConnection,
    LinkedInMessage,
    LinkedInRecommendation,
    LinkedInSnapshot,
)
from repo import linkedin, people
from tests.test_repo_people import FakeConn

NOW = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)


def connection(slug="alice-example", **kw):
    base = dict(
        profile_url=f"linkedin.com/in/{slug}",
        first_name="Alice",
        last_name="Example",
        full_name="Alice Example",
        email=None,
        company="Example Health",
        position="CTO",
        connected_on=date(2021, 4, 2),
    )
    base.update(kw)
    return LinkedInConnection(**base)


def snapshot(connections=None, messages=None, recommendations=None):
    return LinkedInSnapshot(
        source="Basic_Export",
        snapshot_at=NOW,
        me="linkedin.com/in/me-example",
        connections=[connection()] if connections is None else connections,
        messages=messages or [],
        recommendations=recommendations or [],
        matched_by_email=1,
        matched_by_name=0,
    )


MESSAGE = LinkedInMessage(
    conversation_id="c1",
    conversation_title=None,
    sender_name="Alice Example",
    sender_profile_url="linkedin.com/in/alice-example",
    recipient_names="Me Example",
    recipient_profile_urls=["linkedin.com/in/me-example"],
    sent_at=NOW,
    subject=None,
    content="Hi",
    folder="INBOX",
    from_me=False,
)
REC = LinkedInRecommendation(
    direction="given",
    first_name="Alice",
    last_name="Example",
    full_name="Alice Example",
    company=None,
    job_title=None,
    text="Great.",
    status="VISIBLE",
    created_on=date(2025, 6, 1),
    profile_url="linkedin.com/in/alice-example",
)


def test_names_for_matching():
    conn = FakeConn(results=[[{"email": "a@b.c", "display_name": "A"}]])
    assert people.names_for_matching(conn) == [{"email": "a@b.c", "display_name": "A"}]
    assert conn.calls[0] == ("SELECT email, display_name FROM people", None)


def test_replace_snapshot_order_and_params():
    conn = FakeConn()
    linkedin.replace_snapshot(conn, snapshot(messages=[MESSAGE], recommendations=[REC]))
    sqls = [sql for sql, _ in conn.calls]
    assert sqls[:3] == [
        "DELETE FROM linkedin_messages",
        "DELETE FROM linkedin_recommendations",
        "DELETE FROM linkedin_connections",
    ]
    assert sqls[3].startswith("INSERT INTO linkedin_connections (profile_url, first_name,")
    assert sqls[4].startswith("INSERT INTO linkedin_messages (conversation_id,")
    assert "%s::text[]" in sqls[4]
    assert sqls[5].startswith("INSERT INTO linkedin_recommendations (direction,")
    assert sqls[6].startswith("INSERT INTO linkedin_imports (snapshot_at, source,")
    assert len(sqls) == 7

    conn_params = conn.calls[3][1]
    assert conn_params[0] == "linkedin.com/in/alice-example" and conn_params[-1] == NOW
    msg_params = conn.calls[4][1]
    assert ["linkedin.com/in/me-example"] in msg_params and msg_params[-1] == NOW
    assert conn.calls[6][1] == (NOW, "Basic_Export", 1, 1, 1, 0, 1, 0)


def test_replace_snapshot_skips_empty_inserts():
    conn = FakeConn()
    linkedin.replace_snapshot(conn, snapshot(connections=[]))
    sqls = [sql for sql, _ in conn.calls]
    assert len(sqls) == 4 and sqls[3].startswith("INSERT INTO linkedin_imports")
    assert conn.calls[3][1] == (NOW, "Basic_Export", 0, 0, 0, 0, 1, 0)


def test_replace_snapshot_batches():
    conn = FakeConn()
    many = [connection(slug=f"p{i}") for i in range(1201)]
    linkedin.replace_snapshot(conn, snapshot(connections=many), chunk=500)
    inserts = [(sql, p) for sql, p in conn.calls if sql.startswith("INSERT INTO linkedin_connections")]
    assert [len(p) // 15 for _, p in inserts] == [500, 500, 201]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_linkedin.py -q`
Expected: collection error `ImportError: cannot import name 'linkedin' from 'repo'`.

- [ ] **Step 3: Add `names_for_matching` to `repo/people.py`**

Append:

```python
def names_for_matching(conn: Any) -> list[dict]:
    """Every row's email and display_name, for scripts/import_linkedin.py's soft link."""
    return conn.execute("SELECT email, display_name FROM people").fetchall()
```

- [ ] **Step 4: Create `repo/linkedin.py` with the write path**

```python
"""Reads and writes on the linkedin_* snapshot tables
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §4). Takes an open
connection; never opens or commits one."""

from typing import Any

from models.linkedin import LinkedInSnapshot

_CONNECTION_INSERT = (
    "profile_url", "first_name", "last_name", "full_name", "email", "company", "position",
    "connected_on", "person_email", "match_method", "message_count", "my_message_count",
    "last_message_at", "last_my_message_at", "snapshot_at",
)  # fmt: skip
_MESSAGE_INSERT = (
    "conversation_id", "conversation_title", "sender_name", "sender_profile_url",
    "recipient_names", "recipient_profile_urls", "sent_at", "subject", "content", "folder",
    "from_me", "snapshot_at",
)  # fmt: skip
_RECOMMENDATION_INSERT = (
    "direction", "first_name", "last_name", "full_name", "company", "job_title", "text",
    "status", "created_on", "profile_url", "snapshot_at",
)  # fmt: skip
# An empty Python list must bind as text[] on pg8000 too.
_CASTS = {"recipient_profile_urls": "::text[]"}


def _insert_many(
    conn: Any, table: str, columns: tuple[str, ...], rows: list[tuple], chunk: int
) -> None:
    placeholder = "(" + ", ".join(f"%s{_CASTS.get(c, '')}" for c in columns) + ")"
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join([placeholder] * len(batch))}",
            tuple(v for row in batch for v in row),
        )


def replace_snapshot(conn: Any, s: LinkedInSnapshot, chunk: int = 500) -> None:
    """Wholesale replace (§5.5). The caller's transaction makes it atomic: an
    error before commit leaves the previous snapshot intact."""
    conn.execute("DELETE FROM linkedin_messages")
    conn.execute("DELETE FROM linkedin_recommendations")
    conn.execute("DELETE FROM linkedin_connections")
    _insert_many(
        conn,
        "linkedin_connections",
        _CONNECTION_INSERT,
        [
            (
                c.profile_url, c.first_name, c.last_name, c.full_name, c.email, c.company,
                c.position, c.connected_on, c.person_email, c.match_method, c.message_count,
                c.my_message_count, c.last_message_at, c.last_my_message_at, s.snapshot_at,
            )  # fmt: skip
            for c in s.connections
        ],
        chunk,
    )
    _insert_many(
        conn,
        "linkedin_messages",
        _MESSAGE_INSERT,
        [
            (
                m.conversation_id, m.conversation_title, m.sender_name, m.sender_profile_url,
                m.recipient_names, m.recipient_profile_urls, m.sent_at, m.subject, m.content,
                m.folder, m.from_me, s.snapshot_at,
            )  # fmt: skip
            for m in s.messages
        ],
        chunk,
    )
    _insert_many(
        conn,
        "linkedin_recommendations",
        _RECOMMENDATION_INSERT,
        [
            (
                r.direction, r.first_name, r.last_name, r.full_name, r.company, r.job_title,
                r.text, r.status, r.created_on, r.profile_url, s.snapshot_at,
            )  # fmt: skip
            for r in s.recommendations
        ],
        chunk,
    )
    given = sum(r.direction == "given" for r in s.recommendations)
    conn.execute(
        """
        INSERT INTO linkedin_imports (snapshot_at, source, connections, messages,
            recommendations_given, recommendations_received, matched_by_email, matched_by_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            s.snapshot_at,
            s.source,
            len(s.connections),
            len(s.messages),
            given,
            len(s.recommendations) - given,
            s.matched_by_email,
            s.matched_by_name,
        ),
    )
```

If ruff's formatter rejects `# fmt: skip` on a multi-line tuple in your ruff version, drop the markers and accept ruff's one-item-per-line layout — correctness does not depend on it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_repo_linkedin.py -q` — all pass. Then the Local CI command.

- [ ] **Step 6: Commit**

```bash
git add repo/linkedin.py repo/people.py tests/test_repo_linkedin.py
git commit -m "feat: replace linkedin snapshot in one transaction with batched inserts"
```

---

### Task 6: `repo/linkedin.py` read queries

**Files:**
- Modify: `repo/linkedin.py` (append)
- Test: `tests/test_repo_linkedin.py` (append)

**Interfaces:**
- Produces (all return `dict` rows; connection rows carry exactly `_CONNECTION_COLUMNS`):
  - `connection_for_person(conn, email: str) -> dict | None`
  - `search_connections(conn, q: str, limit: int) -> list[dict]`
  - `list_connections(conn, *, q: str | None = None, company: str | None = None, position: str | None = None, min_messages: int | None = None, replied: bool | None = None, quiet_since: date | None = None, unmatched: bool | None = None, limit: int = 50) -> list[dict]`
  - `get_connection(conn, profile_url: str) -> dict | None`
  - `messages_for(conn, profile_url: str, limit: int) -> list[dict]` — newest first; keys `conversation_id, conversation_title, sender_name, sender_profile_url, recipient_names, sent_at, subject, content, folder, from_me`
  - `recommendations_for(conn, profile_url: str) -> list[dict]` — keys `direction, full_name, company, job_title, text, status, created_on`
  - `latest_import(conn) -> dict | None` — keys `snapshot_at, source, connections, messages, recommendations_given, recommendations_received, matched_by_email, matched_by_name`

Semantics: ordering for every connection list is `last_message_at DESC NULLS LAST, connected_on DESC NULLS LAST` (§7.3). `replied=True` → `my_message_count > 0`, `False` → `= 0`; `unmatched=True` → `person_email IS NULL`, `False` → `IS NOT NULL`; `quiet_since` → `last_message_at < quiet_since` (connections never messaged are *not* "quiet", they are excluded). Search (§7.2) is ILIKE substring or `similarity() > 0.3` on `full_name`, `company`, `position`. `messages_for` uses the exact participant expression from the GIN index so Postgres can use it.

- [ ] **Step 1: Append the failing tests**

```python
def test_connection_for_person():
    conn = FakeConn(results=[[{"profile_url": "linkedin.com/in/alice-example"}]])
    row = linkedin.connection_for_person(conn, "alice@example.com")
    sql, params = conn.calls[0]
    assert "FROM linkedin_connections WHERE person_email = %s" in sql
    assert "ORDER BY last_message_at DESC NULLS LAST" in sql and sql.endswith("LIMIT 1")
    assert params == ("alice@example.com",) and row["profile_url"].endswith("alice-example")


def test_search_connections():
    conn = FakeConn(results=[[]])
    linkedin.search_connections(conn, " Health ", 20)
    sql, params = conn.calls[0]
    for col in ("full_name", "company", "position"):
        assert f"{col} ILIKE %s" in sql and f"similarity({col}, %s) > 0.3" in sql
    assert params == ("%health%",) * 3 + ("Health",) * 3 + (20,)


def test_list_connections_no_filters():
    conn = FakeConn(results=[[]])
    linkedin.list_connections(conn)
    sql, params = conn.calls[0]
    assert "WHERE" not in sql
    assert sql.endswith("ORDER BY last_message_at DESC NULLS LAST, connected_on DESC NULLS LAST LIMIT %s")
    assert params == (50,)


def test_list_connections_all_filters():
    conn = FakeConn(results=[[]])
    linkedin.list_connections(
        conn,
        q="ali",
        company="Example",
        position="cto",
        min_messages=3,
        replied=False,
        quiet_since=date(2026, 1, 1),
        unmatched=True,
        limit=10,
    )
    sql, params = conn.calls[0]
    assert (
        "WHERE full_name ILIKE %s AND company ILIKE %s AND position ILIKE %s"
        " AND message_count >= %s AND my_message_count = 0 AND last_message_at < %s"
        " AND person_email IS NULL ORDER BY"
    ) in sql
    assert params == ("%ali%", "%Example%", "%cto%", 3, date(2026, 1, 1), 10)


def test_list_connections_true_false_variants():
    conn = FakeConn(results=[[]])
    linkedin.list_connections(conn, replied=True, unmatched=False)
    sql, _ = conn.calls[0]
    assert "my_message_count > 0 AND person_email IS NOT NULL" in sql


def test_get_connection():
    conn = FakeConn(results=[[]])
    assert linkedin.get_connection(conn, "linkedin.com/in/nobody") is None
    sql, params = conn.calls[0]
    assert "WHERE profile_url = %s" in sql and params == ("linkedin.com/in/nobody",)


def test_messages_for_uses_participant_index_expression():
    conn = FakeConn(results=[[]])
    linkedin.messages_for(conn, "linkedin.com/in/alice-example", 100)
    sql, params = conn.calls[0]
    assert "(recipient_profile_urls || ARRAY[sender_profile_url]) @> ARRAY[%s]::text[]" in sql
    assert sql.endswith("ORDER BY sent_at DESC LIMIT %s")
    assert params == ("linkedin.com/in/alice-example", 100)


def test_recommendations_for():
    conn = FakeConn(results=[[]])
    linkedin.recommendations_for(conn, "linkedin.com/in/alice-example")
    sql, params = conn.calls[0]
    assert "FROM linkedin_recommendations WHERE profile_url = %s" in sql
    assert params == ("linkedin.com/in/alice-example",)


def test_latest_import():
    conn = FakeConn(results=[[{"source": "Basic_Export"}]])
    assert linkedin.latest_import(conn) == {"source": "Basic_Export"}
    sql, _ = conn.calls[0]
    assert "FROM linkedin_imports ORDER BY id DESC LIMIT 1" in sql
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_linkedin.py -q`
Expected: the new tests FAIL with `AttributeError: module 'repo.linkedin' has no attribute ...`.

- [ ] **Step 3: Implement**

Add `from datetime import date` to the imports in `repo/linkedin.py`, then append:

```python
_CONNECTION_COLUMNS = """
    profile_url, full_name, email, company, position, connected_on, person_email,
    match_method, message_count, my_message_count, last_message_at, last_my_message_at,
    snapshot_at
"""
_ORDER = "ORDER BY last_message_at DESC NULLS LAST, connected_on DESC NULLS LAST"
# Must match linkedin_messages_participants_idx exactly for the planner to use it.
_PARTICIPANTS = "(recipient_profile_urls || ARRAY[sender_profile_url])"


def connection_for_person(conn: Any, email: str) -> dict | None:
    return conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections WHERE person_email = %s {_ORDER} LIMIT 1",
        (email,),
    ).fetchone()


def search_connections(conn: Any, q: str, limit: int) -> list[dict]:
    term = q.strip()
    like = f"%{term.lower()}%"
    return conn.execute(
        f"""
        SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections
        WHERE full_name ILIKE %s OR company ILIKE %s OR position ILIKE %s
           OR similarity(full_name, %s) > 0.3 OR similarity(company, %s) > 0.3
           OR similarity(position, %s) > 0.3
        {_ORDER} LIMIT %s
        """,
        (like, like, like, term, term, term, limit),
    ).fetchall()


def list_connections(
    conn: Any,
    *,
    q: str | None = None,
    company: str | None = None,
    position: str | None = None,
    min_messages: int | None = None,
    replied: bool | None = None,
    quiet_since: date | None = None,
    unmatched: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (("full_name", q), ("company", company), ("position", position)):
        if value:
            where.append(f"{column} ILIKE %s")
            params.append(f"%{value.strip()}%")
    if min_messages is not None:
        where.append("message_count >= %s")
        params.append(min_messages)
    if replied is not None:
        where.append("my_message_count > 0" if replied else "my_message_count = 0")
    if quiet_since is not None:
        where.append("last_message_at < %s")
        params.append(quiet_since)
    if unmatched is not None:
        where.append("person_email IS NULL" if unmatched else "person_email IS NOT NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections {clause} {_ORDER} LIMIT %s",
        (*params, limit),
    ).fetchall()


def get_connection(conn: Any, profile_url: str) -> dict | None:
    return conn.execute(
        f"SELECT {_CONNECTION_COLUMNS} FROM linkedin_connections WHERE profile_url = %s",
        (profile_url,),
    ).fetchone()


def messages_for(conn: Any, profile_url: str, limit: int) -> list[dict]:
    return conn.execute(
        f"""
        SELECT conversation_id, conversation_title, sender_name, sender_profile_url,
               recipient_names, sent_at, subject, content, folder, from_me
        FROM linkedin_messages
        WHERE {_PARTICIPANTS} @> ARRAY[%s]::text[]
        ORDER BY sent_at DESC LIMIT %s
        """,
        (profile_url, limit),
    ).fetchall()


def recommendations_for(conn: Any, profile_url: str) -> list[dict]:
    return conn.execute(
        """
        SELECT direction, full_name, company, job_title, text, status, created_on
        FROM linkedin_recommendations WHERE profile_url = %s
        ORDER BY created_on DESC NULLS LAST
        """,
        (profile_url,),
    ).fetchall()


def latest_import(conn: Any) -> dict | None:
    return conn.execute(
        """
        SELECT snapshot_at, source, connections, messages, recommendations_given,
               recommendations_received, matched_by_email, matched_by_name
        FROM linkedin_imports ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_repo_linkedin.py -q` — all pass. Then the Local CI command.

- [ ] **Step 5: Commit**

```bash
git add repo/linkedin.py tests/test_repo_linkedin.py
git commit -m "feat: linkedin snapshot read queries"
```

---

### Task 7: `scripts/import_linkedin.py`

**Files:**
- Create: `scripts/import_linkedin.py`
- Test: `tests/test_import_linkedin.py`

**Interfaces:**
- Consumes: `linkedin_export.parse_export`, `linkedin_export.match_people`, `linkedin_export.ExportError`, `repo.people.names_for_matching`, `repo.linkedin.replace_snapshot`, `clients.db.get_conn`.
- Produces:
  - `run(get_conn: Callable, path: Path, *, me: str | None, dry_run: bool) -> LinkedInSnapshot`
  - `summary(s: LinkedInSnapshot, *, dry_run: bool) -> str`
  - CLI: `import_linkedin.py <export> [--me URL] [--dry-run]`; exit code 2 with the error on stderr for `ExportError`.

Flow: parse the whole export **before** opening a connection (a malformed file never touches the DB) → open one connection → read people names → match → `replace_snapshot` unless `--dry-run` (then `rollback`). The `with get_conn() as conn` block commits on clean exit and rolls back on exception on both connection flavours (`psycopg.Connection.__exit__`, `_Pg8000Conn.__exit__`), which is what makes the replace atomic. `--dry-run` still *reads* `people` so match counts are real; it writes nothing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_import_linkedin.py`:

```python
import importlib.util
from pathlib import Path

import pytest

import repo.linkedin as linkedin_repo
import repo.people as people_repo
from services.linkedin_export import ExportError

spec = importlib.util.spec_from_file_location("import_linkedin", Path("scripts/import_linkedin.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin"


class FakeConn:
    """Mimics the commit-on-success / rollback-on-error context manager of both
    clients/db.py connection flavours."""

    def __init__(self):
        self.committed = self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *a):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def wired(monkeypatch):
    state = {"conns": [], "replaced": []}

    def get_conn():
        c = FakeConn()
        state["conns"].append(c)
        return c

    state["get_conn"] = get_conn
    monkeypatch.setattr(
        people_repo,
        "names_for_matching",
        lambda conn: [{"email": "alice@example.com", "display_name": "Alice Example"}],
    )
    monkeypatch.setattr(
        linkedin_repo, "replace_snapshot", lambda conn, s: state["replaced"].append(s)
    )
    return state


def test_run_matches_and_replaces(wired):
    s = mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=False)
    assert wired["replaced"] == [s]
    assert s.matched_by_email == 1
    assert wired["conns"][0].committed and not wired["conns"][0].rolled_back


def test_dry_run_writes_nothing(wired):
    s = mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=True)
    assert wired["replaced"] == []
    assert s.matched_by_email == 1  # people were still read for real match counts
    assert wired["conns"][0].rolled_back


def test_error_during_replace_rolls_back(wired, monkeypatch):
    def boom(conn, s):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(linkedin_repo, "replace_snapshot", boom)
    with pytest.raises(RuntimeError):
        mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=False)
    assert wired["conns"][0].rolled_back


def test_bad_export_never_opens_db(wired, tmp_path):
    (tmp_path / "Connections.csv").write_text("First Name,Last Name\nA,B\n")
    with pytest.raises(ExportError):
        mod.run(wired["get_conn"], tmp_path, me=None, dry_run=False)
    assert wired["conns"] == []


def test_summary_prints_counts_not_content(wired):
    s = mod.run(wired["get_conn"], FIXTURE, me=None, dry_run=True)
    out = mod.summary(s, dry_run=True)
    assert out.splitlines() == [
        "DRY RUN — nothing written",
        "connections 6 (email-matched 1, name-matched 0, unmatched 5)",
        "messages 7 in 4 conversations (me=linkedin.com/in/me-example; by-url 5, by-name 1, unmatched 1, skipped 1)",
        "recommendations given 2 (linked 1), received 1 (linked 1)",
        f"snapshot_at {s.snapshot_at:%Y-%m-%dT%H:%M:%SZ}",
    ]
    assert "Coffee" not in out and "Hi there" not in out


def test_summary_reports_missing_files(wired, tmp_path):
    (tmp_path / "Connections.csv").write_bytes((FIXTURE / "Connections.csv").read_bytes())
    s = mod.run(wired["get_conn"], tmp_path, me=None, dry_run=True)
    assert mod.summary(s, dry_run=True).splitlines()[-1] == (
        "missing (treated as empty): messages.csv, Recommendations_Given.csv, Recommendations_Received.csv"
    )
```

Note: `alice@example.com` matches by email here (the fixture's `Alice@Example.com` normalizes to it), so the name-match count is 0.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_import_linkedin.py -q`
Expected: collection error `FileNotFoundError: ... scripts/import_linkedin.py`.

- [ ] **Step 3: Implement**

Create `scripts/import_linkedin.py` (then `chmod +x scripts/import_linkedin.py`):

```python
#!/usr/bin/env python3
# scripts/import_linkedin.py
"""Load a LinkedIn data export into the linkedin_* snapshot tables
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §5). Local only.

  python scripts/import_linkedin.py <export-dir-or-zip> [--me <profile-url>] [--dry-run]

Runs against whatever DB clients/db.py resolves from env, like import_contacts.py.
Parses the whole export before opening the DB, so a malformed file writes
nothing. The replace is one transaction: an error leaves the previous snapshot
intact. --dry-run still reads people names so match counts are real, but
writes nothing. Output is counts only — never names or message content.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from models.linkedin import LinkedInSnapshot
from repo import linkedin as linkedin_repo
from repo import people as people_repo
from services import linkedin_export


def run(get_conn: Callable, path: Path, *, me: str | None, dry_run: bool) -> LinkedInSnapshot:
    snapshot = linkedin_export.parse_export(path, me=me)
    with get_conn() as conn:
        linkedin_export.match_people(snapshot, people_repo.names_for_matching(conn))
        if dry_run:
            conn.rollback()
        else:
            linkedin_repo.replace_snapshot(conn, snapshot)
    return snapshot


def summary(s: LinkedInSnapshot, *, dry_run: bool) -> str:
    n = len(s.connections)
    unmatched = n - s.matched_by_email - s.matched_by_name
    given = [r for r in s.recommendations if r.direction == "given"]
    received = [r for r in s.recommendations if r.direction == "received"]
    lines = ["DRY RUN — nothing written"] if dry_run else []
    lines += [
        f"connections {n:,} (email-matched {s.matched_by_email:,}, "
        f"name-matched {s.matched_by_name:,}, unmatched {unmatched:,})",
        f"messages {len(s.messages):,} in {s.conversations:,} conversations (me={s.me}; "
        f"by-url {s.messages_by_url:,}, by-name {s.messages_by_name:,}, "
        f"unmatched {s.messages_unmatched:,}, skipped {s.messages_skipped:,})",
        f"recommendations given {len(given):,} (linked {sum(1 for r in given if r.profile_url):,}), "
        f"received {len(received):,} (linked {sum(1 for r in received if r.profile_url):,})",
        f"snapshot_at {s.snapshot_at:%Y-%m-%dT%H:%M:%SZ}",
    ]
    if s.missing_files:
        lines.append("missing (treated as empty): " + ", ".join(s.missing_files))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("export", type=Path, help="unzipped export directory or the .zip")
    p.add_argument("--me", help="Ben's LinkedIn profile URL; inferred from messages if omitted")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    from clients.db import get_conn

    try:
        snapshot = run(get_conn, args.export.expanduser(), me=args.me, dry_run=args.dry_run)
    except linkedin_export.ExportError as e:
        print(f"export error: {e}", file=sys.stderr)
        sys.exit(2)
    print(summary(snapshot, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_import_linkedin.py -q` — all pass.
Run: `.venv/bin/python scripts/import_linkedin.py --help` — prints usage without touching the DB.
Run the Local CI command.

- [ ] **Step 5: Commit**

```bash
git add scripts/import_linkedin.py tests/test_import_linkedin.py
git commit -m "feat: import_linkedin.py loads a linkedin export snapshot"
```

---

### Task 8: `api/routers/linkedin.py`

**Files:**
- Create: `api/routers/linkedin.py`
- Modify: `api/main.py`
- Test: `tests/test_api.py` (modify autouse fixture, append tests)

**Interfaces:**
- Consumes: Task 6 read functions (called as `linkedin.<fn>` on the `repo.linkedin` module so tests can monkeypatch them).
- Produces (pydantic models Task 9 imports from `api.routers.linkedin`):
  - `LinkedInSummary` — `profile_url, company, position, connected_on, message_count, my_message_count, last_message_at, last_my_message_at, snapshot_at`
  - `LinkedInConnectionOut(LinkedInSummary)` — adds `full_name, email, person_email, match_method`
  - Routes: `GET /linkedin/connections`, `GET /linkedin/connections/{slug}`, `GET /linkedin/imports/latest` (§7.3).

Detail response = `LinkedInConnectionOut` fields + `recommendations: list[...]` + `conversations: list[{conversation_id, conversation_title, messages: [...]}]`. `messages_for` returns newest first, so conversations are ordered by their newest message and messages stay newest first inside each. `slug` → `linkedin.com/in/<slug lowercased, slashes stripped>`.

- [ ] **Step 1: Extend the autouse fixture and append failing tests in `tests/test_api.py`**

Add `import repo.linkedin as linkedin_repo` and `from datetime import date` to the imports (keep ruff order: `from datetime import UTC, date, datetime`). Add these helpers above `class Conn`:

```python
def li_row(slug="alice-example", **kw):
    base = {
        "profile_url": f"linkedin.com/in/{slug}",
        "full_name": "Alice Example",
        "email": None,
        "company": "Example Health",
        "position": "CTO",
        "connected_on": date(2021, 4, 2),
        "person_email": "alice@x.com",
        "match_method": "name",
        "message_count": 14,
        "my_message_count": 6,
        "last_message_at": TS,
        "last_my_message_at": TS,
        "snapshot_at": TS,
    }
    base.update(kw)
    return base


def msg_row(conversation_id, sent_at, content="hi", **kw):
    base = {
        "conversation_id": conversation_id,
        "conversation_title": None,
        "sender_name": "Alice Example",
        "sender_profile_url": "linkedin.com/in/alice-example",
        "recipient_names": "Me Example",
        "sent_at": sent_at,
        "subject": None,
        "content": content,
        "folder": "INBOX",
        "from_me": False,
    }
    base.update(kw)
    return base
```

Append to the body of the `_wire` fixture:

```python
    monkeypatch.setattr(linkedin_repo, "connection_for_person", lambda conn, email: None)
    monkeypatch.setattr(
        linkedin_repo, "search_connections", lambda conn, q, limit: [li_row()] if "ali" in q else []
    )
    monkeypatch.setattr(linkedin_repo, "list_connections", lambda conn, **kw: [li_row()])
    monkeypatch.setattr(
        linkedin_repo,
        "get_connection",
        lambda conn, url: li_row() if url == "linkedin.com/in/alice-example" else None,
    )
    monkeypatch.setattr(linkedin_repo, "messages_for", lambda conn, url, limit: [])
    monkeypatch.setattr(linkedin_repo, "recommendations_for", lambda conn, url: [])
    monkeypatch.setattr(linkedin_repo, "latest_import", lambda conn: None)
```

(`connection_for_person` / `search_connections` are only called from Task 9 onward; patching them now keeps the fixture in one place.)

Append tests:

```python
def test_linkedin_connections_passes_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        linkedin_repo, "list_connections", lambda conn, **kw: (seen.update(kw), [li_row()])[1]
    )
    r = client.get(
        "/linkedin/connections?company=Example&min_messages=3&replied=false"
        "&quiet_since=2026-01-01&unmatched=true&limit=10"
    )
    assert r.status_code == 200
    assert seen == {
        "q": None,
        "company": "Example",
        "position": None,
        "min_messages": 3,
        "replied": False,
        "quiet_since": date(2026, 1, 1),
        "unmatched": True,
        "limit": 10,
    }
    body = r.json()["results"][0]
    assert body["profile_url"] == "linkedin.com/in/alice-example"
    assert body["person_email"] == "alice@x.com" and body["message_count"] == 14


def test_linkedin_connections_limit_bounds():
    assert client.get("/linkedin/connections?limit=501").status_code == 422
    assert client.get("/linkedin/connections?limit=0").status_code == 422


def test_linkedin_connection_detail_groups_conversations(monkeypatch):
    t1, t2, t3 = (datetime(2026, 1, d, tzinfo=UTC) for d in (1, 2, 3))
    seen = {}

    def messages_for(conn, url, limit):
        seen["args"] = (url, limit)
        return [msg_row("c2", t3, "newest"), msg_row("c1", t2, "middle"), msg_row("c2", t1, "oldest")]

    monkeypatch.setattr(linkedin_repo, "messages_for", messages_for)
    monkeypatch.setattr(
        linkedin_repo,
        "recommendations_for",
        lambda conn, url: [
            {"direction": "given", "full_name": "Alice Example", "company": None,
             "job_title": None, "text": "Great.", "status": "VISIBLE", "created_on": date(2025, 6, 1)}
        ],
    )  # fmt: skip
    r = client.get("/linkedin/connections/Alice-Example?messages_limit=5")
    assert r.status_code == 200
    assert seen["args"] == ("linkedin.com/in/alice-example", 5)
    body = r.json()
    assert body["full_name"] == "Alice Example" and body["recommendations"][0]["text"] == "Great."
    assert [c["conversation_id"] for c in body["conversations"]] == ["c2", "c1"]
    assert [m["content"] for m in body["conversations"][0]["messages"]] == ["newest", "oldest"]


def test_linkedin_connection_detail_404():
    assert client.get("/linkedin/connections/nobody").status_code == 404


def test_linkedin_imports_latest(monkeypatch):
    assert client.get("/linkedin/imports/latest").status_code == 404
    monkeypatch.setattr(
        linkedin_repo,
        "latest_import",
        lambda conn: {
            "snapshot_at": TS,
            "source": "Basic_Export",
            "connections": 6,
            "messages": 7,
            "recommendations_given": 2,
            "recommendations_received": 1,
            "matched_by_email": 1,
            "matched_by_name": 1,
        },
    )
    r = client.get("/linkedin/imports/latest")
    assert r.status_code == 200 and r.json()["connections"] == 6
```

The detail test requests a mixed-case slug to prove the router lowercases it before the lookup.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -q`
Expected: new linkedin tests FAIL with 404 (routes don't exist); `test_linkedin_connection_detail_404` passes trivially — fine. Existing tests still pass.

- [ ] **Step 3: Create `api/routers/linkedin.py`**

```python
"""LinkedIn snapshot read endpoints
(docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md §7.3)."""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import verify_token
from clients import db
from repo import linkedin

router = APIRouter(dependencies=[Depends(verify_token)])


class LinkedInSummary(BaseModel):
    profile_url: str
    company: str | None
    position: str | None
    connected_on: date | None
    message_count: int
    my_message_count: int
    last_message_at: datetime | None
    last_my_message_at: datetime | None
    snapshot_at: datetime


class LinkedInConnectionOut(LinkedInSummary):
    full_name: str
    email: str | None
    person_email: str | None
    match_method: str | None


class LinkedInConnectionList(BaseModel):
    results: list[LinkedInConnectionOut]


class LinkedInMessageOut(BaseModel):
    sender_name: str | None
    sender_profile_url: str | None
    recipient_names: str | None
    sent_at: datetime
    subject: str | None
    content: str | None
    folder: str | None
    from_me: bool


class LinkedInConversationOut(BaseModel):
    conversation_id: str
    conversation_title: str | None
    messages: list[LinkedInMessageOut]


class LinkedInRecommendationOut(BaseModel):
    direction: str
    full_name: str
    company: str | None
    job_title: str | None
    text: str | None
    status: str | None
    created_on: date | None


class LinkedInConnectionDetail(LinkedInConnectionOut):
    recommendations: list[LinkedInRecommendationOut]
    conversations: list[LinkedInConversationOut]


class LinkedInImportOut(BaseModel):
    snapshot_at: datetime
    source: str
    connections: int
    messages: int
    recommendations_given: int
    recommendations_received: int
    matched_by_email: int
    matched_by_name: int


def group_conversations(rows: list[dict]) -> list[LinkedInConversationOut]:
    """rows arrive newest first; each conversation sits at its newest message."""
    groups: dict[str, LinkedInConversationOut] = {}
    for r in rows:
        group = groups.get(r["conversation_id"])
        if group is None:
            group = groups[r["conversation_id"]] = LinkedInConversationOut(
                conversation_id=r["conversation_id"],
                conversation_title=r.get("conversation_title"),
                messages=[],
            )
        group.messages.append(LinkedInMessageOut.model_validate(r))
    return list(groups.values())


@router.get("/linkedin/connections", response_model=LinkedInConnectionList)
def list_connections(
    q: str | None = None,
    company: str | None = None,
    position: str | None = None,
    min_messages: int | None = Query(default=None, ge=0),
    replied: bool | None = None,
    quiet_since: date | None = None,
    unmatched: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> LinkedInConnectionList:
    with db.get_conn() as conn:
        rows = linkedin.list_connections(
            conn,
            q=q,
            company=company,
            position=position,
            min_messages=min_messages,
            replied=replied,
            quiet_since=quiet_since,
            unmatched=unmatched,
            limit=limit,
        )
    return LinkedInConnectionList(results=[LinkedInConnectionOut.model_validate(r) for r in rows])


@router.get("/linkedin/connections/{slug}", response_model=LinkedInConnectionDetail)
def get_connection(
    slug: str, messages_limit: int = Query(default=100, ge=1, le=1000)
) -> LinkedInConnectionDetail:
    profile_url = f"linkedin.com/in/{slug.strip().strip('/').lower()}"
    with db.get_conn() as conn:
        row = linkedin.get_connection(conn, profile_url)
        if row is None:
            raise HTTPException(status_code=404)
        recommendations = linkedin.recommendations_for(conn, profile_url)
        messages = linkedin.messages_for(conn, profile_url, messages_limit)
    return LinkedInConnectionDetail(
        **LinkedInConnectionOut.model_validate(row).model_dump(),
        recommendations=[LinkedInRecommendationOut.model_validate(r) for r in recommendations],
        conversations=group_conversations(messages),
    )


@router.get("/linkedin/imports/latest", response_model=LinkedInImportOut)
def latest_import() -> LinkedInImportOut:
    with db.get_conn() as conn:
        row = linkedin.latest_import(conn)
    if row is None:
        raise HTTPException(status_code=404)
    return LinkedInImportOut.model_validate(row)
```

`model_validate` on a dict ignores extra keys (pydantic v2 default `extra="ignore"`), so repo rows can carry more columns than the model.

- [ ] **Step 4: Register the router in `api/main.py`**

```python
from api.routers import linkedin, people, search
```

and after `app.include_router(search.router)`:

```python
app.include_router(linkedin.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api.py -q` — all pass. Then the Local CI command.

- [ ] **Step 6: Commit**

```bash
git add api/routers/linkedin.py api/main.py tests/test_api.py
git commit -m "feat: people-api /linkedin endpoints"
```

---

### Task 9: Additive `linkedin` fields on person and search responses

**Files:**
- Modify: `api/routers/people.py`
- Modify: `api/routers/search.py`
- Test: `tests/test_api.py` (append)

**Interfaces:**
- Consumes: `LinkedInSummary`, `LinkedInConnectionOut` from `api.routers.linkedin`; `repo.linkedin.connection_for_person`, `repo.linkedin.search_connections`.
- Produces:
  - `PersonOut.linkedin: LinkedInSummary | None = None`
  - `to_out(row: dict, linkedin_row: dict | None = None) -> PersonOut`
  - `SearchResponse(PersonList)` with `linkedin_results: list[LinkedInConnectionOut]`

§7.1: filled on `GET /people/{email}` and `PATCH /people/{email}` only. `GET /people`, `POST /people/{email}/sync`, and `POST /search` `results` return `linkedin: null` (the sync route is not named in §7.1; leaving it null keeps it one lookup — the caller can `GET` the person). Import direction is one-way: `people.py` and `search.py` import from `linkedin.py`, never the reverse.

- [ ] **Step 1: Append the failing tests**

```python
def test_get_person_linkedin_absent_is_null():
    assert client.get("/people/alice@x.com").json()["linkedin"] is None


def test_get_person_linkedin_present(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        linkedin_repo,
        "connection_for_person",
        lambda conn, email: (seen.setdefault("email", email), li_row())[1],
    )
    body = client.get("/people/Alice@X.com").json()
    assert seen["email"] == "alice@x.com"
    assert body["linkedin"]["profile_url"] == "linkedin.com/in/alice-example"
    assert body["linkedin"]["my_message_count"] == 6
    assert "full_name" not in body["linkedin"] and "person_email" not in body["linkedin"]


def test_patch_person_includes_linkedin(monkeypatch):
    monkeypatch.setattr(person_edit, "update", lambda conn, email, **kw: row(notes="hi"))
    monkeypatch.setattr(linkedin_repo, "connection_for_person", lambda conn, email: li_row())
    body = client.patch("/people/alice@x.com", json={"notes": "hi"}).json()
    assert body["linkedin"]["company"] == "Example Health"


def test_list_responses_have_null_linkedin(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("list responses must not look up linkedin per row")

    monkeypatch.setattr(linkedin_repo, "connection_for_person", fail)
    assert client.get("/people?recent=1").json()["results"][0]["linkedin"] is None
    assert client.post("/search", json={"q": "ali"}).json()["results"][0]["linkedin"] is None


def test_search_linkedin_results():
    body = client.post("/search", json={"q": "ali", "limit": 5}).json()
    assert body["results"][0]["email"] == "alice@x.com"
    assert body["linkedin_results"][0]["full_name"] == "Alice Example"
    assert client.post("/search", json={"q": "zzz"}).json()["linkedin_results"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -q`
Expected: the five new tests FAIL with `KeyError: 'linkedin'` / `KeyError: 'linkedin_results'`.

- [ ] **Step 3: Update `api/routers/people.py`**

Imports — add:

```python
from api.routers.linkedin import LinkedInSummary
from repo import linkedin as linkedin_repo
```

`PersonOut` — add as the last field:

```python
    linkedin: LinkedInSummary | None = None
```

`to_out` — new signature and last argument:

```python
def to_out(row: dict, linkedin_row: dict | None = None) -> PersonOut:
    return PersonOut(
        ...existing fields unchanged...,
        in_hubspot=bool(row.get("hubspot_contact_id")),
        linkedin=LinkedInSummary.model_validate(linkedin_row) if linkedin_row else None,
    )
```

`get_person`:

```python
@router.get("/people/{email}", response_model=PersonOut)
def get_person(email: str) -> PersonOut:
    email = normalize(email)
    with db.get_conn() as conn:
        row = people.get(conn, email)
        linkedin_row = linkedin_repo.connection_for_person(conn, email) if row else None
    if row is None:
        raise HTTPException(status_code=404)
    return to_out(row, linkedin_row)
```

`patch_person`:

```python
@router.patch("/people/{email}", response_model=PersonOut)
def patch_person(email: str, body: PersonPatch) -> PersonOut:
    email = normalize(email)
    try:
        with db.get_conn() as conn:
            row = person_edit.update(
                conn, email, notes=body.notes, relationship_label=body.relationship_label
            )
            conn.commit()
            linkedin_row = linkedin_repo.connection_for_person(conn, email)
    except person_edit.NotFound:
        raise HTTPException(status_code=404)
    except person_edit.NotLinked:
        raise HTTPException(status_code=409, detail="person has no Google contact")
    return to_out(row, linkedin_row)
```

The lookup runs after `commit()` so a failure there can never roll back the Google-first edit.

- [ ] **Step 4: Update `api/routers/search.py`**

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.auth import verify_token
from api.routers.linkedin import LinkedInConnectionOut
from api.routers.people import PersonList, to_out
from clients import db
from repo import linkedin, people

router = APIRouter(dependencies=[Depends(verify_token)])


class SearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=100)


class SearchResponse(PersonList):
    linkedin_results: list[LinkedInConnectionOut]


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest) -> SearchResponse:
    with db.get_conn() as conn:
        results = [to_out(r) for r in people.search(conn, body.q, body.limit)]
        connections = linkedin.search_connections(conn, body.q, body.limit)
    return SearchResponse(
        results=results,
        linkedin_results=[LinkedInConnectionOut.model_validate(r) for r in connections],
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api.py -q` — all pass, including every pre-existing test (backward compatibility). Then the Local CI command.

- [ ] **Step 6: Commit**

```bash
git add api/routers/people.py api/routers/search.py tests/test_api.py
git commit -m "feat: linkedin summary on person responses and linkedin_results on search"
```

---

### Task 10: Skills and CLAUDE.md

**Files:**
- Create: `.claude/skills/importing-linkedin/SKILL.md`
- Modify: `.claude/skills/searching-people/SKILL.md`, `.claude/skills/fetching-person/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`, `.claude/skills/people-architecture/SKILL.md`, `CLAUDE.md`

No tests; verification is a read-through plus the Local CI command (ruff excludes `.claude` and `docs`). Do **not** add `importing-linkedin` to `scripts/link-skills.sh` (§8: it runs from this repo, not globally).

- [ ] **Step 1: Create `.claude/skills/importing-linkedin/SKILL.md`**

````markdown
---
name: importing-linkedin
description: Use when the user wants to load, refresh, or re-import their LinkedIn data export (connections, messages, recommendations) into the people database, run scripts/import_linkedin.py, or check how old the LinkedIn snapshot is.
---

# Importing a LinkedIn Snapshot

`scripts/import_linkedin.py` loads LinkedIn's official data export into the
`linkedin_connections`, `linkedin_messages`, and `linkedin_recommendations`
tables, replacing the previous snapshot in one transaction, and appends a row
to `linkedin_imports`. It never writes to `people`, Google Contacts, or
HubSpot. Spec: `docs/superpowers/specs/2026-09-16-linkedin-snapshot-design.md`.

## 1. Request the export (Ben, by hand)

LinkedIn → **Settings → Data privacy → Get a copy of your data** → "Want
something in particular?" → tick **Connections**, **Messages**,
**Recommendations** → **Request archive**. LinkedIn emails a download link
(usually minutes, up to 24h). Download and unzip it — the script also accepts
the `.zip` directly.

The export is personal data. Keep it outside the repo, or in a path matching
`*LinkedInDataExport*` / `linkedin-export*/` (both gitignored). Never commit it.

## 2. Dry run, then import

```bash
cd ~/src/people
scripts/fetch-env.sh            # if .env is missing or stale
.venv/bin/python scripts/import_linkedin.py ~/Downloads/Basic_LinkedInDataExport_MM-DD-YYYY --dry-run
.venv/bin/python scripts/import_linkedin.py ~/Downloads/Basic_LinkedInDataExport_MM-DD-YYYY
```

`--dry-run` parses everything and reads `people` for matching, but writes
nothing. Always run it first.

If the `me=` URL in the output is not Ben's profile, re-run with
`--me https://www.linkedin.com/in/<ben's-slug>` — it is inferred as the
participant in the most conversations and is never hardcoded (public repo).

## 3. Reading the counts

```
connections 1,284 (email-matched 37, name-matched 112, unmatched 1,135)
messages 4,902 in 611 conversations (me=linkedin.com/in/…; by-url 4,610, by-name 180, unmatched 112, skipped 0)
recommendations given 6 (linked 5), received 9 (linked 8)
snapshot_at 2026-09-16T18:04:11Z
```

- **email-matched / name-matched** — connections soft-linked to a `people`
  row. Name matches require the normalized name to be unique among both
  people and connections; ambiguous names stay unmatched on purpose.
- **by-url / by-name / unmatched messages** — how each message was tied to a
  connection. Unmatched is normal for InMail and recruiters (non-connections).
  A large by-name or unmatched share suggests LinkedIn changed URL formats.
- **skipped** — rows with no conversation id or an unparseable `DATE`. Non-zero
  means the date format changed: fix `parse_timestamp` in
  `services/linkedin_export.py`.
- **missing (treated as empty)** — an optional file was not in the export
  (usually a box left unticked when requesting it).

An `export error: ...` exit means a required file or column is missing — the
DB was not touched. LinkedIn renames columns without notice; update the column
sets at the top of `services/linkedin_export.py` and the fixtures in
`tests/fixtures/linkedin/`.

## 4. Check snapshot age

```bash
TOKEN=$(gcloud secrets versions access latest --secret people-api-token --project bens-project-462804)
curl -s https://people-api.drolet.cloud/linkedin/imports/latest -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

`404` = never imported.

Links from connections to people go stale as new people arrive by email;
re-running the import re-matches.
````

- [ ] **Step 2: Update `.claude/skills/searching-people/SKILL.md`**

After the "Search by name or email" paragraph that ends `limit defaults to 20, max 100.`, add:

````markdown
The response also carries `linkedin_results`: LinkedIn connections whose
name, company, or position match `q` (substring or trigram), ordered by last
LinkedIn message, same `limit`. `results` entries always have `linkedin: null`
— fetch the person for their LinkedIn summary.

## Search LinkedIn connections

```
GET $BASE/linkedin/connections
```

```bash
curl -s "$BASE/linkedin/connections?company=<company>&replied=true&quiet_since=2025-01-01&limit=50" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Filters (all optional, combined with AND): `q` (name substring), `company`,
`position` (case-insensitive substrings), `min_messages` (int),
`replied` (`true` = Ben has sent at least one message), `quiet_since`
(date — last message before it; never-messaged connections are excluded),
`unmatched` (`true` = not linked to a `people` row), `limit` (default 50,
max 500). Ordered by `last_message_at` desc (nulls last), then
`connected_on` desc.

Examples: "former colleagues at X who went quiet" →
`company=X&replied=true&quiet_since=<a year ago>`; "LinkedIn people I've never
emailed" → `unmatched=true&min_messages=1`.

Each result: `profile_url`, `full_name`, `email`, `company`, `position`,
`connected_on`, `person_email` (linked people row, if any), `match_method`
(`email`/`name`/null), `message_count`, `my_message_count`,
`last_message_at`, `last_my_message_at`, `snapshot_at`.

This is a snapshot from Ben's last manual export — check its age with
`GET $BASE/linkedin/imports/latest` before claiming something is current.
Open one with `GET $BASE/linkedin/connections/{slug}` (see **fetching-person**).
````

- [ ] **Step 3: Update `.claude/skills/fetching-person/SKILL.md`**

Append `linkedin` to the **Response fields** list:

```markdown
`in_google_contacts`, `in_hubspot`, `linkedin` (null, or the linked LinkedIn
connection's `profile_url`, `company`, `position`, `connected_on`,
`message_count`, `my_message_count`, `last_message_at`, `last_my_message_at`,
`snapshot_at`).
```

Add to the **Presenting** block, after the Google Contacts/HubSpot line:

```
LinkedIn: <company> · <position> · <message_count> msgs / <my_message_count> from Ben · last <last_message_at> (snapshot <snapshot_at date>)   ← only if linkedin is set
```

Append a section at the end:

````markdown
## LinkedIn detail

For conversation history and recommendations, take the slug after `/in/` in
`linkedin.profile_url` (or from a **searching-people** LinkedIn result, which
also covers connections with no `people` row):

```bash
curl -s "$BASE/linkedin/connections/<slug>?messages_limit=100" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Returns the connection fields plus `recommendations` (given/received) and
`conversations` (newest first, each with `messages` newest first). `404` =
not in the current snapshot. Message bodies are private — quote only what the
user asks for.
````

- [ ] **Step 4: Update `.claude/skills/querying-people-db/SKILL.md`**

Add rows to the **Key tables** table:

```markdown
| `linkedin_connections` | LinkedIn snapshot, PK `profile_url` (`linkedin.com/in/<slug>`): `full_name`, `email`, `company`, `position`, `connected_on`, `person_email` (soft link to `people.email`), `match_method`, `message_count`, `my_message_count`, `last_message_at`, `last_my_message_at`, `snapshot_at` |
| `linkedin_messages` | `conversation_id`, `sender_name`, `sender_profile_url`, `recipient_names`, `recipient_profile_urls` (text[]), `sent_at`, `subject`, `content`, `folder`, `from_me` |
| `linkedin_recommendations` | `direction` (`given`/`received`), `full_name`, `company`, `job_title`, `text`, `status`, `created_on`, `profile_url` (null unless the name matched one connection) |
| `linkedin_imports` | append-only audit of `scripts/import_linkedin.py` runs: `snapshot_at`, `source`, row counts, `matched_by_email`, `matched_by_name` |
```

Append to **Common queries**:

````markdown
**LinkedIn connections Ben talked with and let go quiet:**
```sql
SELECT full_name, company, position, message_count, my_message_count, last_message_at, person_email
FROM linkedin_connections
WHERE my_message_count > 0 AND last_message_at < now() - interval '1 year'
ORDER BY last_message_at DESC
LIMIT 50;
```

**Messages with one connection** (the expression matches the GIN index):
```sql
SELECT sent_at, from_me, sender_name, left(content, 200) AS content
FROM linkedin_messages
WHERE (recipient_profile_urls || ARRAY[sender_profile_url]) @> ARRAY['linkedin.com/in/<slug>']::text[]
ORDER BY sent_at DESC;
```

**Snapshot age and match rates:**
```sql
SELECT * FROM linkedin_imports ORDER BY id DESC LIMIT 5;
```
````

- [ ] **Step 5: Update `.claude/skills/people-architecture/SKILL.md`**

In the diagram, change `Cloud SQL db \`people\` (people, sync_state)` to `Cloud SQL db \`people\` (people, sync_state, linkedin_*)`, and below the Cloud Scheduler block add:

```
scripts/import_linkedin.py (local, manual) ──LinkedIn data export──▶ linkedin_* tables (snapshot, replaced per import)
```

In **Database**, after the `sync_state (...)` sentence, add:

```markdown
LinkedIn snapshot tables — `linkedin_connections` (PK `profile_url`, soft link
`person_email` → `people.email`, per-connection message stats),
`linkedin_messages`, `linkedin_recommendations` — are fully replaced by each
manual run of `scripts/import_linkedin.py`; `linkedin_imports` is its
append-only audit. No foreign keys to `people`; nothing flows from LinkedIn to
Google Contacts or HubSpot.
```

Wherever the file lists `people-api` routes, add `GET /linkedin/connections`, `GET /linkedin/connections/{slug}`, `GET /linkedin/imports/latest` (check with `grep -n "/search" .claude/skills/people-architecture/SKILL.md`).

- [ ] **Step 6: Update `CLAUDE.md`**

1. Stack table, **Database** row: `tables \`people\`, \`sync_state\`` → `tables \`people\`, \`sync_state\`, \`linkedin_connections\`, \`linkedin_messages\`, \`linkedin_recommendations\`, \`linkedin_imports\``.
2. Code layout block — add these lines in their sections:

```
  linkedin.py               LinkedInConnection/Message/Recommendation/Snapshot dataclasses      (under models/)
  linkedin.py               linkedin_* snapshot: replace_snapshot + API read queries            (under repo/)
  linkedin_export.py        parse a LinkedIn data export (dir/zip) → snapshot; match_people     (under services/)
    linkedin.py              GET /linkedin/connections[/{slug}], GET /linkedin/imports/latest   (under api/routers/)
  import_linkedin.py        load a LinkedIn export snapshot — --dry-run, --me                   (under scripts/)
```

(Drop the parenthesized section hints; they only tell you where each line goes. Match the existing column alignment.) Also add `importing-linkedin` to the `.claude/skills/` list.
3. After the "Source of truth" table, add the row from §4.5:

```markdown
| `linkedin_*` tables | LinkedIn data export | Export → DB on manual import (`scripts/import_linkedin.py`). Never written back anywhere; LinkedIn is not a source for any `people` field. |
```

4. Update the sentence under the table: "If the DB is lost, everything except the counters rebuilds…" → append " The LinkedIn snapshot rebuilds by re-running `scripts/import_linkedin.py` on the latest export."
5. **Local dev** block, after the API lines:

````markdown
LinkedIn snapshot (see `importing-linkedin`):
`.venv/bin/python scripts/import_linkedin.py <export-dir-or-zip> --dry-run`, then without `--dry-run`.
````

- [ ] **Step 7: Verify and commit**

Run: `grep -rn "linkedin" CLAUDE.md .claude/skills/*/SKILL.md | wc -l` — non-zero across all six files. Read each changed section once for accuracy against the code from Tasks 6–9 (parameter names, route paths). Run the Local CI command.

```bash
git add .claude/skills/ CLAUDE.md
git commit -m "docs: linkedin snapshot skills and CLAUDE.md"
```

---

### Task 11: Migrate, verify against the real export, open the PR

This task touches the **production** `people` DB (the only DB `.env` points at). Each write step below needs Ben's explicit go-ahead in the session — ask, don't assume.

**Files:** none new (fix-ups from verification get their own commits).

- [ ] **Step 1: Full local CI on the branch**

Run the Local CI command. Expected: all green. `git log --oneline main..HEAD` shows the spec commit, the plan commit (if committed separately), and Tasks 1–10.

- [ ] **Step 2: Apply the schema (§11 step 1) — ask Ben first**

Every statement is `IF NOT EXISTS`, so this is safe against the running service; without it, the new routes 500 after deploy.

```bash
scripts/fetch-env.sh   # if .env is missing
.venv/bin/python scripts/migrate_db.py
```

Expected: `Migration complete`. Confirm with the `querying-people-db` skill:

```sql
SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'linkedin_%' ORDER BY 1;
```

Expected: the four `linkedin_*` tables.

- [ ] **Step 3: Dry run against Ben's real export**

```bash
.venv/bin/python scripts/import_linkedin.py "$EXPORT" --dry-run
```

Check with Ben: `me=` is his profile; `skipped` is 0 or explained; by-name + unmatched messages are a small share; `missing` lists nothing unexpected. If anything is off, fix it in `services/linkedin_export.py` (with a fixture + test reproducing the shape, using invented data), commit, and re-run. Never paste real names or content into commits, tests, or the PR.

- [ ] **Step 4: Real import — ask Ben first**

```bash
.venv/bin/python scripts/import_linkedin.py "$EXPORT"        # add --me <url> if Step 3 needed it
```

Then, via `querying-people-db`:

```sql
SELECT count(*) FROM linkedin_connections;
SELECT count(*) FROM linkedin_messages;
SELECT match_method, count(*) FROM linkedin_connections GROUP BY 1;
SELECT * FROM linkedin_imports ORDER BY id DESC LIMIT 1;
```

Expected: counts equal the script's summary.

- [ ] **Step 5: Run the branch's API locally and spot-check**

```bash
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080)   # run_in_background
.venv/bin/python scripts/test-api-local.py
```

Then (no auth locally when `PEOPLE_API_TOKEN` is unset in the shell; otherwise add the bearer header):

```bash
curl -s localhost:8080/linkedin/imports/latest | python3 -m json.tool
curl -s "localhost:8080/linkedin/connections?replied=true&limit=5" | python3 -m json.tool
curl -s "localhost:8080/linkedin/connections/<slug from the previous result>?messages_limit=5" | python3 -m json.tool
curl -s "localhost:8080/people/<person_email of an email- or name-matched connection>" | python3 -m json.tool
curl -s -X POST localhost:8080/search -H 'Content-Type: application/json' -d '{"q":"<a known company>","limit":5}' | python3 -m json.tool
```

Ask Ben to spot-check 3 connections he knows: company/position right, message counts plausible, the `people` link correct (or correctly absent). Also confirm the pre-existing responses still have every old field (backward compatibility for inbox's classify-time consumer). Stop uvicorn afterwards.

- [ ] **Step 6: Open the PR with `/pr-open`**

Invoke the `pr-open` skill (never hand-roll `git push` + `gh pr create`). The description must note: the schema was already migrated on prod (§11 step 1); merging deploys `people-api` only (no Cloud Functions change — `deploy.yml` will still run because `repo/`, `services/`, `models/` changed, and is a no-op for the functions' behaviour); no Terraform/secrets/inbox changes; the one matching guard added beyond the spec (connection-side name uniqueness). No real names, counts that could identify people, or message content in the PR body — aggregate counts are fine.

- [ ] **Step 7: Post local verification to the PR**

Invoke `verifying-pr-locally` to record Steps 3–5 on the PR (aggregate counts and pass/fail only — no personal data; the repo is public).

---

## Self-review against the spec

| Spec | Covered by |
|---|---|
| §1/§2 manual export, local import, no writes to people/Google/HubSpot | Global Constraints; Tasks 5, 7 (only `SELECT` on `people`) |
| §3 components | Tasks 1–10 (file structure table) |
| §4.1–4.4 tables + indexes | Task 1 |
| §4.5 source-of-truth row | Task 10 Step 6 |
| §5 CLI, dir or zip | Tasks 3 (`_read_files`), 7 |
| §5.1 header-driven parsing, required vs optional files, confirm real shape | Tasks 0, 2, 3 |
| §5.2 URL / name / timestamp normalization | Task 2 |
| §5.3 "me" inference + `--me`, per-connection stats, name fallback + reporting | Task 3, Task 7 summary |
| §5.4 matching | Task 4 (plus connection-side uniqueness guard) |
| §5.5 one transaction, rollback, dry-run, output format | Tasks 5, 7 |
| §6 privacy: gitignore, synthetic fixtures, no content in output | Tasks 1, 3, 7; Global Constraints |
| §7.1 `linkedin` on GET/PATCH person, null on lists | Task 9 |
| §7.2 `linkedin_results` on search | Tasks 6, 9 |
| §7.3 three endpoints, filters, ordering, 404s, `messages_limit` | Tasks 6, 8 |
| §8 skills and docs; not globally linked | Task 10 |
| §9 no new metrics; middleware covers routes | nothing to do — `api/main.py` middleware labels by route template |
| §10 testing list | Tasks 2–4 (`test_linkedin_export.py`), 5–6 (`test_repo_linkedin.py`), 7 (`test_import_linkedin.py`), 8–9 (`test_api.py`), 11 (local verification) |
| §11 rollout: migrate before merge, merge deploys API, import | Task 11 |
