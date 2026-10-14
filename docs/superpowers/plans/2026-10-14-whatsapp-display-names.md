# WhatsApp Display Names and LID Phone Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give WhatsApp handles real display names instead of protobuf blobs, and phone-match LID handles and members through WhatsApp's address book.

**Architecture:** `clients/whatsapp_local.py` also reads `ZWAPROFILEPUSHNAME` (from `ChatStorage.sqlite`) and `ZWAADDRESSBOOKCONTACT` (from the sibling `ContactsV2.sqlite`), and stops reading `ZWAMESSAGE.ZPUSHNAME`. The pure layer `services/whatsapp_export.py` resolves names by a fixed precedence and resolves `lid:` handles to E.164 phones, and `match_handles` links those phones as `'lid_phone'`. `repo/whatsapp.py` stores a new `phone` column. On a `--full` run it overwrites `display_name`/`phone` verbatim, which cleans the existing garbled rows; on an incremental run it coalesces.

**Tech Stack:** Python 3.13, sqlite3 (local store), Postgres via psycopg/pg8000, FastAPI/pydantic, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-14-whatsapp-display-names-design.md`. It amends `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md`, so read both.

## Global Constraints

- Repo is **public**: fixtures use only invented names, `+1555010…` numbers and `999…` LIDs; no real name, number or JID anywhere in code, tests or output.
- Script output and logs are **counts only**: never a JID, number, name or message text.
- The import writes **only** `whatsapp_*` tables. Never `people`, Google Contacts or HubSpot.
- Layer rules: `clients/` I/O only; `services/` pure; `repo/` takes an open connection and never commits; `models/` pure types.
- `handle` keys never change: a LID handle stays `lid:<id>` even when its phone is known.
- `match_method` values: `'phone' | 'lid_phone' | 'name' | NULL`.
- Blob guard regex: `^[A-Za-z0-9+/]+={1,2}$` with `len % 4 == 0`. It's never shape alone without padding, so `"Mike"` must pass.
- A missing `ContactsV2.sqlite` degrades, it doesn't fail. A `ContactsV2.sqlite` missing a required column is a `SchemaError`.
- Verify with: `.venv/bin/pytest tests/ -q`, `.venv/bin/ruff check . && .venv/bin/ruff format --check .`, `.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`.

## Review Focus

1. **No `ContactsV2.sqlite` beside the store.** The import should run as before, with names from the profile table only. Pinned in Task 1 (`test_read_without_contacts_file_returns_an_empty_address_book`).
2. **An address-book row whose `ZWHATSAPPID` won't normalize, or whose `ZLID` is NULL.** It should resolve no phone, and must not crash or create a bogus link. Pinned in Task 2 (`test_lid_phone_ignores_unnormalizable_and_lidless_rows`).
3. **A LID handle whose resolved phone matches two `people` rows.** It should link nothing, with no name fallback even if its partner name is unique. Pinned in Task 3 (`test_lid_phone_ambiguity_links_nothing_and_skips_the_name_match`).
4. **An incremental run that sees a handle with no name or phone this time.** The stored values should survive. Pinned in Task 4 (`test_upsert_handles_incremental_coalesces_name_and_phone`).
5. **Real names that look like base64 ("Mike", "Anna", CJK, emoji).** They should pass the guard. Pinned in Task 2 (`test_blob_guard_never_drops_a_real_name`).

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `tests/fixtures/whatsapp.py` | Adds `ZWAPROFILEPUSHNAME` to the synthetic store, makes message push names blobs, and adds `build_contacts()` | 1 |
| `clients/whatsapp_local.py` | Reads push names and the address book; drops `ZPUSHNAME` | 1 |
| `models/whatsapp.py` | `WhatsAppHandle.phone`; batch counters and `lid_phones` | 2 |
| `services/whatsapp_export.py` | `looks_like_blob`, `resolve_display_name`, `lid_phone`, `build_batch` wiring | 2 |
| `services/whatsapp_export.py` | `match_handles` `'lid_phone'` rule | 3 |
| `repo/schema.sql`, `repo/whatsapp.py`, `api/routers/whatsapp.py` | `phone` column, mode-dependent upsert, rematch, read/out | 4 |
| `scripts/import_whatsapp.py` | Passes `full` through; summary and metrics | 5 |
| `.claude/skills/importing-whatsapp/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`, `CLAUDE.md`, both specs | Docs | 5 |

---

### Task 1: Read profile push names and the address book

**Files:**
- Modify: `tests/fixtures/whatsapp.py`
- Modify: `clients/whatsapp_local.py`
- Test: `tests/test_whatsapp_local.py`

**Interfaces:**
- Produces: `RawStore.push_names: dict[str, str]` (raw JID → push name), `RawStore.address_book: list[dict[str, Any]]` (keys `ZLID`, `ZWHATSAPPID`, `ZFULLNAME`), `whatsapp_local.CONTACTS_FILENAME = "ContactsV2.sqlite"`. Fixture: `build_contacts(tmp_path, rows=None) -> Path`, plus constants `MEMBER_LID_PHONE_JID = "15550100005@s.whatsapp.net"` and `MEMBER_4_LID_JID = "99900000000004@lid"`.
- `MESSAGE_SQL` no longer selects `m.ZPUSHNAME`, and `REQUIRED_COLUMNS["ZWAMESSAGE"]` no longer lists it.

- [ ] **Step 1: Extend the fixture**

In `tests/fixtures/whatsapp.py`, append to `DDL`:

```python
CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZJID VARCHAR, ZPUSHNAME VARCHAR);
```

Add constants under `MEMBER_4_JID`:

```python
# The address book maps the @lid member to this phone JID (spec 2026-10-14 §5.3).
MEMBER_LID_PHONE_JID = "15550100005@s.whatsapp.net"
# Frank's LID, so the address book can key him both ways.
MEMBER_4_LID_JID = "99900000000004@lid"

CONTACTS_DDL = """
CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZFULLNAME VARCHAR, ZLID VARCHAR, ZPHONENUMBER VARCHAR, ZWHATSAPPID VARCHAR);
"""
```

In `build_store`, change message row 14's `ZPUSHNAME` from `"Frank"` to `"CJaiyNAGIAA="`, which is what the real store holds (§1 of the spec). Change every other inbound row's `ZPUSHNAME` (`"Alice"`, `"Ghost"`, `"Bob"`, `"Carmen"`) to `"IAA="`. Before `db.commit()`, insert profile push names:

```python
    db.executemany(
        "INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) VALUES (?, ?, ?)",
        [
            (1, ALICE_JID, "Alice Push"),  # loses to ZPARTNERNAME
            (2, MEMBER_4_JID, "Frank Push"),
            (3, MEMBER_LID_JID, "Eve Push"),
            (4, MEMBER_2_JID, "   "),  # blank: filtered out by the read
        ],
    )
```

Add at the end of the module:

```python
def build_contacts(tmp_path: Path, rows: list[tuple] | None = None) -> Path:
    """Synthetic ContactsV2.sqlite beside ChatStorage.sqlite. rows are
    (ZLID, ZWHATSAPPID, ZFULLNAME)."""
    path = tmp_path / "ContactsV2.sqlite"
    db = sqlite3.connect(path)
    db.executescript(CONTACTS_DDL)
    if rows is None:
        rows = [
            (MEMBER_LID_JID, MEMBER_LID_PHONE_JID, "Eve Saved"),
            (MEMBER_4_LID_JID, MEMBER_4_JID, "Frank Saved"),
        ]
    db.executemany(
        "INSERT INTO ZWAADDRESSBOOKCONTACT (ZLID, ZWHATSAPPID, ZFULLNAME) VALUES (?, ?, ?)", rows
    )
    db.commit()
    db.close()
    return path
```

Update the module docstring's shape summary: add `profile push names 4` and `address book (build_contacts) 2`.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_whatsapp_local.py` (extend its fixture import with `ALICE_JID, MEMBER_4_JID, MEMBER_LID_JID, MEMBER_2_JID, build_contacts`):

```python
def test_read_returns_profile_push_names_and_skips_blank_ones(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert raw.push_names[MEMBER_4_JID] == "Frank Push"
    assert raw.push_names[MEMBER_LID_JID] == "Eve Push"
    assert raw.push_names[ALICE_JID] == "Alice Push"
    assert MEMBER_2_JID not in raw.push_names


def test_read_returns_the_address_book_from_the_sibling_file(tmp_path):
    path = build_store(tmp_path)
    build_contacts(tmp_path)
    raw = whatsapp_local.read(path)
    assert {r["ZFULLNAME"] for r in raw.address_book} == {"Eve Saved", "Frank Saved"}
    assert set(raw.address_book[0]) == {"ZLID", "ZWHATSAPPID", "ZFULLNAME"}


def test_read_without_contacts_file_returns_an_empty_address_book(tmp_path, caplog):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert raw.address_book == []
    assert "address book" in caplog.text
    assert str(tmp_path) not in caplog.text  # counts/notes only, never a path


def test_contacts_file_missing_a_column_raises_naming_it(tmp_path):
    import sqlite3

    path = build_store(tmp_path)
    contacts = build_contacts(tmp_path)
    db = sqlite3.connect(contacts)
    db.executescript("ALTER TABLE ZWAADDRESSBOOKCONTACT DROP COLUMN ZLID;")
    db.commit()
    db.close()
    with pytest.raises(whatsapp_local.SchemaError, match="ContactsV2.sqlite.*ZLID"):
        whatsapp_local.read(path)


def test_messages_no_longer_carry_zpushname(tmp_path):
    raw = whatsapp_local.read(build_store(tmp_path))
    assert all("ZPUSHNAME" not in m for m in raw.messages)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_whatsapp_local.py -q`
Expected: the five new tests FAIL (`AttributeError: 'RawStore' object has no attribute 'push_names'`, …). Existing tests still pass.

- [ ] **Step 4: Implement in `clients/whatsapp_local.py`**

Add `import logging` and `logger = logging.getLogger(__name__)`. Change `from dataclasses import dataclass` to `from dataclasses import dataclass, field`.

In `REQUIRED_COLUMNS`, remove `"ZPUSHNAME"` from the `"ZWAMESSAGE"` tuple and add:

```python
    "ZWAPROFILEPUSHNAME": ("ZJID", "ZPUSHNAME"),
```

Below `REQUIRED_COLUMNS`:

```python
# WhatsApp's copy of the phone's address book, beside the message store in the same
# group container (spec 2026-10-14 §5.1). Optional: names and LID phones degrade
# without it, the import does not fail.
CONTACTS_FILENAME = "ContactsV2.sqlite"
CONTACTS_REQUIRED_COLUMNS = {"ZWAADDRESSBOOKCONTACT": ("ZLID", "ZWHATSAPPID", "ZFULLNAME")}
ADDRESS_BOOK_SQL = "SELECT ZLID, ZWHATSAPPID, ZFULLNAME FROM ZWAADDRESSBOOKCONTACT"

# The real push names (spec 2026-10-14 §1). ZWAMESSAGE.ZPUSHNAME is a base64 protobuf
# on this store, never a name, and is no longer read.
PUSH_NAME_SQL = """
SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME
WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL AND trim(ZPUSHNAME) <> ''
"""
```

In `MESSAGE_SQL`, change `m.ZMESSAGETYPE, m.ZPUSHNAME, m.ZGROUPMEMBER, m.ZMEDIAITEM,` to `m.ZMESSAGETYPE, m.ZGROUPMEMBER, m.ZMEDIAITEM,`.

Extend `RawStore`:

```python
@dataclass
class RawStore:
    sessions: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    members: list[dict[str, Any]]
    max_pk: int
    push_names: dict[str, str] = field(default_factory=dict)
    address_book: list[dict[str, Any]] = field(default_factory=list)
```

Generalize `_check_schema`:

```python
def _check_schema(
    conn: sqlite3.Connection,
    required: dict[str, tuple[str, ...]] = REQUIRED_COLUMNS,
    label: str = "ChatStorage.sqlite",
) -> None:
    missing: list[str] = []
    for table, columns in required.items():
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            missing.append(table)
            continue
        missing += [f"{table}.{c}" for c in columns if c not in present]
    if missing:
        raise SchemaError(f"{label} is missing: {', '.join(missing)}")
```

Add the address-book reader:

```python
def _read_address_book(path: Path) -> list[dict[str, Any]]:
    """Every ZWAADDRESSBOOKCONTACT row, from a temp copy (spec 2026-10-14 §5.1).
    A missing file is an empty book, logged without the path (public repo)."""
    if not path.exists():
        logger.warning("whatsapp address book not found; names and LID phones degrade")
        return []
    with _copied(path) as copy:
        try:
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        except sqlite3.Error as e:
            raise StoreUnreadableError(str(e)) from e
        conn.row_factory = sqlite3.Row
        try:
            _check_schema(conn, CONTACTS_REQUIRED_COLUMNS, CONTACTS_FILENAME)
            return [dict(r) for r in conn.execute(ADDRESS_BOOK_SQL)]
        finally:
            conn.close()
```

In `read()`, after `max_pk = …` and before the `return`, add:

```python
            push_names = {r["ZJID"]: r["ZPUSHNAME"].strip() for r in conn.execute(PUSH_NAME_SQL)}
```

Then pass `push_names=push_names` into `RawStore(...)`, and replace the `return RawStore(...)` with:

```python
            raw = RawStore(
                sessions=sessions,
                messages=messages,
                members=members,
                max_pk=int(max_pk),
                push_names=push_names,
            )
        finally:
            conn.close()
    raw.address_book = _read_address_book(path.with_name(CONTACTS_FILENAME))
    return raw
```

(The address book is read after the message-store copy is removed, from its own temp copy, so two plaintext copies never coexist longer than needed.) Update the `read()` docstring: "Also reads profile push names, and the sibling ContactsV2.sqlite address book in full (spec 2026-10-14 §5.1)."

- [ ] **Step 5: Run the client tests**

Run: `.venv/bin/pytest tests/test_whatsapp_local.py -q`
Expected: all PASS.

- [ ] **Step 6: Run the whole suite to see the expected downstream break**

Run: `.venv/bin/pytest tests/test_whatsapp_export.py -q`
Expected: `test_a_group_only_sender_earns_a_handle_from_zmemberjid_not_zfromjid` FAILS. Its `display_name` is now `None`, because `build_batch` still reads `row.get("ZPUSHNAME")`, which is gone. Task 2 fixes this. Everything else passes.

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures/whatsapp.py clients/whatsapp_local.py tests/test_whatsapp_local.py
git commit -m "whatsapp_local: read profile push names and the address book, drop ZPUSHNAME"
```

---

### Task 2: Resolve display names and LID phones in the batch

**Files:**
- Modify: `models/whatsapp.py`
- Modify: `services/whatsapp_export.py`
- Test: `tests/test_whatsapp_export.py`

**Interfaces:**
- Consumes: `RawStore.push_names`, `RawStore.address_book` (Task 1).
- Produces:
  - `WhatsAppHandle.phone: str | None` (E.164; equals `handle` for a phone handle).
  - `WhatsAppBatch.lid_phones: dict[str, str]` (`lid:` handle → E.164, for handles **and** members).
  - Batch counters `names_partner`, `names_address_book`, `names_profile`, `names_member`, `names_none`, `matched_by_lid_phone: int`.
  - `whatsapp_export.looks_like_blob(value: str) -> bool`.
  - `whatsapp_export.resolve_display_name(*, partner: str | None, address_book: str | None, profile: str | None, member: str | None) -> tuple[str | None, str]`, where the second element is one of `"partner" | "address_book" | "profile" | "member" | "none"`.
  - `whatsapp_export.lid_phone(handle: str, phone_jid_by_lid: dict[str, str]) -> str | None`.

- [ ] **Step 1: Extend the models**

In `models/whatsapp.py`:

```python
@dataclass
class WhatsAppHandle:
    handle: str  # E.164, or 'lid:<id>' (§6.4)
    jid: str | None = None
    display_name: str | None = None
    person_id: int | None = None
    match_method: str | None = None  # 'phone' | 'lid_phone' | 'name' | None
    # E.164: the handle itself, or a LID's phone from the address book (2026-10-14 §5.3)
    phone: str | None = None
```

In `WhatsAppBatch`, after `partner_names`:

```python
    # 'lid:<id>' -> E.164 via the address book, for handles and members alike
    # (spec 2026-10-14 §5.3). match_handles reads this.
    lid_phones: dict[str, str] = field(default_factory=dict)
```

and after `matched_by_name: int = 0`:

```python
    matched_by_lid_phone: int = 0
    names_partner: int = 0
    names_address_book: int = 0
    names_profile: int = 0
    names_member: int = 0
    names_none: int = 0
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_whatsapp_export.py` (extend its fixture import with `MEMBER_LID_JID, MEMBER_LID_PHONE_JID, MEMBER_4_LID_JID, build_contacts`):

```python
# --- Display names and LID phones (spec 2026-10-14 §5.2, §5.3) ----------------


def batch_with_contacts(tmp_path, rows=None):
    path = build_store(tmp_path)
    build_contacts(tmp_path, rows)
    return wa.build_batch(whatsapp_local.read(path), mode="full")


def test_blob_guard_catches_padded_protobufs():
    for blob in ("IAA=", "CJaiyNAGIAA=", "IAA6FDNCQkYzMzE5NDFDRTc1MDdDNjRDQMCN59zdM3iKuf654jM="):
        assert wa.looks_like_blob(blob), blob


def test_blob_guard_never_drops_a_real_name():
    for name in ("Mike", "Anna", "Jo", "Alice Example", "王小明", "Zoë 🎉", "Bob", "A+B"):
        assert not wa.looks_like_blob(name), name


def test_resolve_display_name_precedence():
    r = wa.resolve_display_name
    assert r(partner="P", address_book="A", profile="F", member="M") == ("P", "partner")
    assert r(partner=None, address_book="A", profile="F", member="M") == ("A", "address_book")
    assert r(partner=None, address_book=None, profile="F", member="M") == ("F", "profile")
    assert r(partner=None, address_book=None, profile=None, member="M") == ("M", "member")
    assert r(partner=None, address_book=None, profile=None, member=None) == (None, "none")


def test_resolve_display_name_skips_blank_and_blob_candidates():
    r = wa.resolve_display_name
    assert r(partner="  ", address_book="IAA=", profile="Frank", member=None) == ("Frank", "profile")
    assert r(partner=None, address_book=None, profile="CJaiyNAGIAA=", member=None) == (None, "none")


def test_lid_phone_resolves_through_the_address_book():
    book = {MEMBER_LID_JID: MEMBER_LID_PHONE_JID}
    assert wa.lid_phone("lid:99900000000002", book) == "+15550100005"
    assert wa.lid_phone("lid:99900000000099", book) is None
    assert wa.lid_phone("+15550100001", book) is None  # not a LID handle


def test_lid_phone_ignores_unnormalizable_and_lidless_rows(tmp_path):
    b = batch_with_contacts(
        tmp_path,
        rows=[
            (MEMBER_LID_JID, "5@s.whatsapp.net", "Eve Saved"),  # won't normalize
            (None, MEMBER_4_JID, "Frank Saved"),  # no LID: name still usable
        ],
    )
    assert b.lid_phones == {}
    assert {h.handle: h for h in b.handles}["+15550100004"].display_name == "Frank Saved"


def test_group_only_sender_takes_the_profile_name_without_an_address_book(tmp_path):
    frank = {h.handle: h for h in batch(tmp_path).handles}["+15550100004"]
    assert frank.display_name == "Frank Push"


def test_address_book_name_beats_profile_name(tmp_path):
    frank = {h.handle: h for h in batch_with_contacts(tmp_path).handles}["+15550100004"]
    assert frank.display_name == "Frank Saved"


def test_partner_name_still_wins_for_direct_chats(tmp_path):
    alice = {h.handle: h for h in batch_with_contacts(tmp_path).handles}["+15550100001"]
    assert alice.display_name == "Alice Example"


def test_no_handle_ever_carries_a_message_push_name_blob(tmp_path):
    for h in batch_with_contacts(tmp_path).handles:
        assert h.display_name is None or not wa.looks_like_blob(h.display_name), h.handle


def test_phone_handles_carry_their_own_phone_and_lid_members_resolve(tmp_path):
    b = batch_with_contacts(tmp_path)
    handles = {h.handle: h for h in b.handles}
    assert handles["+15550100001"].phone == "+15550100001"
    assert handles["lid:99900000000001"].phone is None  # Bob: not in the address book
    assert b.lid_phones == {"lid:99900000000002": "+15550100005"}  # Eve, a member only


def test_name_source_counters_add_up(tmp_path):
    b = batch_with_contacts(tmp_path)
    total = b.names_partner + b.names_address_book + b.names_profile + b.names_member + b.names_none
    assert total == len(b.handles)
    assert b.names_address_book == 1  # Frank
```

Update the existing `test_a_group_only_sender_earns_a_handle_from_zmemberjid_not_zfromjid`: change its last line to `assert frank.display_name == "Frank Push"`, and add `ZPUSHNAME` to its docstring's list of things that are not read.

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_whatsapp_export.py -q`
Expected: new tests FAIL with `AttributeError: module 'services.whatsapp_export' has no attribute 'looks_like_blob'` and similar.

- [ ] **Step 4: Implement the pure helpers**

In `services/whatsapp_export.py`, add `import re`, and below `SESSION_TYPES`:

```python
# A padded base64 string: what ZWAMESSAGE.ZPUSHNAME actually holds (spec 2026-10-14
# §1). Padding is required so a real name like "Mike" — valid, unpadded base64 —
# is never mistaken for one (§5.2).
_BLOB = re.compile(r"[A-Za-z0-9+/]+={1,2}")


def looks_like_blob(value: str) -> bool:
    return bool(_BLOB.fullmatch(value)) and len(value) % 4 == 0


def resolve_display_name(
    *, partner: str | None, address_book: str | None, profile: str | None, member: str | None
) -> tuple[str | None, str]:
    """First usable name by the spec 2026-10-14 §5.2 precedence, and its source.

    ZPARTNERNAME, then Ben's address-book name, then the contact's own profile push
    name, then ZCONTACTNAME. Blank and blob-shaped candidates are skipped."""
    for value, source in (
        (partner, "partner"),
        (address_book, "address_book"),
        (profile, "profile"),
        (member, "member"),
    ):
        name = (value or "").strip()
        if name and not looks_like_blob(name):
            return name, source
    return None, "none"


def lid_phone(handle: str, phone_jid_by_lid: dict[str, str]) -> str | None:
    """E.164 for a 'lid:<id>' handle whose LID is in the address book, else None
    (spec 2026-10-14 §5.3). Normalized exactly as any phone JID is (§6.4, MX/AR rule
    included) — never from ZPHONENUMBER, which carries locale formatting."""
    if not handle.startswith(LID_PREFIX):
        return None
    phone_jid = phone_jid_by_lid.get(handle[len(LID_PREFIX) :] + LID_SUFFIX)
    if not phone_jid:
        return None
    phone, _kind = jid_to_handle(phone_jid)
    return phone if phone and not phone.startswith(LID_PREFIX) else None
```

Move the existing `LID_PREFIX = "lid:"` line (currently just above `match_handles`) up next to `LID_SUFFIX`, so `lid_phone` can use it.

- [ ] **Step 5: Wire them into `build_batch`**

In `build_batch`, the group-sender branch currently sets `display_name=row.get("ZPUSHNAME") or member_names.get(sender_handle)`. Remove that argument so the handle is created with `handle` and `jid` only. Then replace the final `batch.handles = list(handles.values())` with:

```python
    # Address book, keyed both ways: a LID or a phone JID -> Ben's saved name, and a
    # LID -> its phone JID (spec 2026-10-14 §5.1-§5.3). Rows missing a key are skipped.
    book_name_by_jid: dict[str, str] = {}
    phone_jid_by_lid: dict[str, str] = {}
    for entry in raw.address_book:
        lid_jid, phone_jid, full_name = entry.get("ZLID"), entry.get("ZWHATSAPPID"), entry.get("ZFULLNAME")
        for key in (lid_jid, phone_jid):
            if key and full_name:
                book_name_by_jid.setdefault(key, full_name)
        if lid_jid and phone_jid:
            phone_jid_by_lid.setdefault(lid_jid, phone_jid)

    for handle_row in handles.values():
        jid = handle_row.jid or ""
        name, source = resolve_display_name(
            partner=batch.partner_names.get(handle_row.handle),
            address_book=book_name_by_jid.get(jid),
            profile=raw.push_names.get(jid),
            member=member_names.get(handle_row.handle),
        )
        handle_row.display_name = name
        counter = f"names_{source}"
        setattr(batch, counter, getattr(batch, counter) + 1)
        if handle_row.handle.startswith(LID_PREFIX):
            handle_row.phone = lid_phone(handle_row.handle, phone_jid_by_lid)
        else:
            handle_row.phone = handle_row.handle

    for handle in set(handles) | {m.handle for m in batch.members}:
        phone = lid_phone(handle, phone_jid_by_lid)
        if phone:
            batch.lid_phones[handle] = phone

    batch.handles = list(handles.values())
    return batch
```

The direct-session branch that builds `WhatsAppHandle(handle=handle, jid=jid, display_name=name)` can stay. The loop above overwrites `display_name` with the same partner name, since it has top precedence.

- [ ] **Step 6: Run the export tests**

Run: `.venv/bin/pytest tests/test_whatsapp_export.py tests/test_whatsapp_local.py -q`
Expected: all PASS.

- [ ] **Step 7: Lint and type-check**

Run: `.venv/bin/ruff check services/ models/ && .venv/bin/ruff format services/ models/ tests/ && .venv/bin/mypy services/ models/ clients/`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add models/whatsapp.py services/whatsapp_export.py tests/test_whatsapp_export.py
git commit -m "whatsapp_export: resolve display names by precedence and LID phones via the address book"
```

---

### Task 3: Phone-match LID handles and members (`'lid_phone'`)

**Files:**
- Modify: `services/whatsapp_export.py` (`match_handles`)
- Test: `tests/test_whatsapp_export.py`

**Interfaces:**
- Consumes: `WhatsAppBatch.lid_phones`, `WhatsAppBatch.matched_by_lid_phone` (Task 2).
- Produces: `match_handles` sets `match_method = 'lid_phone'` and increments `batch.matched_by_lid_phone`. `matched_by_phone` and `matched_by_name` keep their meaning.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_export.py`:

```python
def test_a_lid_member_links_by_its_address_book_phone(tmp_path):
    b = batch_with_contacts(tmp_path)
    wa.match_handles(b, [{"id": 11, "display_name": "Someone", "phone_numbers": ["+15550100005"]}])
    eve = next(m for m in b.members if m.handle == "lid:99900000000002")
    assert eve.person_id == 11


def test_a_lid_handle_links_as_lid_phone():
    b = WhatsAppBatch(mode="full")
    b.handles = [WhatsAppHandle(handle="lid:1", phone="+15550100005")]
    b.lid_phones = {"lid:1": "+15550100005"}
    wa.match_handles(b, [{"id": 11, "display_name": "X", "phone_numbers": ["+15550100005"]}])
    assert (b.handles[0].person_id, b.handles[0].match_method) == (11, "lid_phone")
    assert (b.matched_by_lid_phone, b.matched_by_phone, b.matched_by_name) == (1, 0, 0)


def test_lid_phone_ambiguity_links_nothing_and_skips_the_name_match():
    """§5.3 step 3: a known phone that does not resolve to one row is "no match" —
    the unique partner name must not be tried."""
    b = WhatsAppBatch(mode="full")
    b.handles = [WhatsAppHandle(handle="lid:1", phone="+15550100005")]
    b.lid_phones = {"lid:1": "+15550100005"}
    b.partner_names = {"lid:1": "Zoe Example"}
    rows = [
        {"id": 11, "display_name": "A", "phone_numbers": ["+15550100005"]},
        {"id": 12, "display_name": "B", "phone_numbers": ["+15550100005"]},
        {"id": 13, "display_name": "Zoe Example", "phone_numbers": []},
    ]
    wa.match_handles(b, rows)
    assert (b.handles[0].person_id, b.handles[0].match_method) == (None, None)
    assert b.matched_by_name == 0


def test_a_lid_handle_without_a_phone_still_name_matches():
    b = WhatsAppBatch(mode="full")
    b.handles = [WhatsAppHandle(handle="lid:1")]
    b.partner_names = {"lid:1": "Zoe Example"}
    wa.match_handles(b, [{"id": 13, "display_name": "Zoe Example", "phone_numbers": []}])
    assert (b.handles[0].person_id, b.handles[0].match_method) == (13, "name")
```

Add `WhatsAppBatch, WhatsAppHandle` to the test module's imports (`from models.whatsapp import WhatsAppBatch, WhatsAppHandle`).

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_whatsapp_export.py -q -k "lid_phone or lid_member or lid_handle"`
Expected: `test_a_lid_member_links_by_its_address_book_phone`, `test_a_lid_handle_links_as_lid_phone` and `test_lid_phone_ambiguity_links_nothing_and_skips_the_name_match` FAIL. The name-match test passes already.

- [ ] **Step 3: Implement**

In `match_handles`, replace the candidate loop and the counter block:

```python
    links: dict[str, tuple[int, str]] = {}
    targets = {h.handle for h in batch.handles} | {m.handle for m in batch.members}
    for handle in sorted(targets):
        if handle.startswith(LID_PREFIX):
            resolved = batch.lid_phones.get(handle)
            if resolved is not None:
                # A known phone that does not resolve to one row is "no match";
                # never fall back to the name (spec 2026-10-14 §5.3 step 3).
                candidates = ids_by_phone.get(resolved, set())
                method = "lid_phone"
            else:
                name = normalize_name(batch.partner_names.get(handle))
                candidates = ids_by_name.get(name, set()) if name else set()
                method = "name"
        else:
            candidates = ids_by_phone.get(handle, set())
            method = "phone"
        if len(candidates) == 1:
            links[handle] = (next(iter(candidates)), method)

    batch.matched_by_phone = batch.matched_by_name = batch.matched_by_lid_phone = 0
    for handle_row in batch.handles:
        link = links.get(handle_row.handle)
        handle_row.person_id, handle_row.match_method = link if link else (None, None)
        if not link:
            continue
        if link[1] == "phone":
            batch.matched_by_phone += 1
        elif link[1] == "lid_phone":
            batch.matched_by_lid_phone += 1
        else:
            batch.matched_by_name += 1
```

Update the docstring: "Three rules … By LID phone: a LID handle or member whose LID the address book maps to a phone is matched by that phone, as `'lid_phone'`; the name rule then never runs for it (spec 2026-10-14 §5.3)."

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_whatsapp_export.py tests/test_import_whatsapp.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/whatsapp_export.py tests/test_whatsapp_export.py
git commit -m "whatsapp_export: phone-match LID handles and members through the address book"
```

---

### Task 4: Store `phone`, clean names on `--full`, re-match stored LID phones, serve `phone`

**Files:**
- Modify: `repo/schema.sql`
- Modify: `repo/whatsapp.py` (`_HANDLE_*`, `upsert_handles`, `rematch_stored_handles`, `summary_for_person`, `_HANDLE_OUT`)
- Modify: `api/routers/whatsapp.py` (`WhatsAppHandleOut`)
- Test: `tests/test_repo_whatsapp.py`, `tests/test_schema.py`, `tests/test_import_whatsapp.py` (one marker)

**Interfaces:**
- Consumes: `WhatsAppHandle.phone` (Task 2).
- Produces: `repo.whatsapp.upsert_handles(conn, handles, chunk=500, *, full: bool = False) -> int`. Column `whatsapp_handles.phone TEXT`. `WhatsAppHandleOut.phone: str | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_repo_whatsapp.py`, replace `test_upsert_handles_writes_link_fields_only`'s INSERT assertion with:

```python
    assert (
        "INSERT INTO whatsapp_handles (handle, jid, display_name, person_id, match_method, phone)"
        in sql
    )
```

Replace `test_upsert_handles_keeps_a_known_display_name` with:

```python
def test_upsert_handles_incremental_coalesces_name_and_phone():
    """An incremental batch may not see a handle's 1:1 session or address-book row,
    so a missing name or phone there means "not seen", not "has none"."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001")])
    sql, _ = conn.calls[0]
    assert "display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)" in sql
    assert "phone = COALESCE(EXCLUDED.phone, whatsapp_handles.phone)" in sql


def test_upsert_handles_full_overwrites_name_and_phone():
    """A full batch is authoritative, NULL included — this is what cleans the
    stored protobuf-blob names (spec 2026-10-14 §5.4)."""
    conn = FakeConn()
    whatsapp.upsert_handles(conn, [WhatsAppHandle(handle="+15550100001")], full=True)
    sql, params = conn.calls[0]
    assert "display_name = EXCLUDED.display_name" in sql
    assert "COALESCE(EXCLUDED.display_name" not in sql
    assert "phone = EXCLUDED.phone" in sql
    assert params == ("+15550100001", None, None, None, None, None)
```

Replace the three `rematch_stored_handles` tests with:

```python
def test_rematch_stored_handles_covers_phone_handles_and_lids_with_a_phone():
    """A LID with a stored phone is re-matched by it (spec 2026-10-14 §5.3); a LID
    without one keeps its name link, which this SQL cannot recompute."""
    conn = FakeConn()
    whatsapp.rematch_stored_handles(conn)
    sql, params = conn.calls[0]
    assert "WHERE handle NOT LIKE 'lid:%' OR phone IS NOT NULL" in sql
    assert "COALESCE(phone, handle) AS phone" in sql
    assert params is None


def test_rematch_stored_handles_links_on_an_exact_unique_phone_match():
    conn = FakeConn()
    whatsapp.rematch_stored_handles(conn)
    sql, _ = conn.calls[0]
    assert "JOIN people p ON k.phone = ANY(p.phone_numbers)" in sql
    assert "COUNT(DISTINCT person_id) AS n" in sql
    assert "WHEN counts.n = 1 THEN counts.only_id ELSE NULL END" in sql
    assert (
        "WHEN counts.n = 1 AND k.is_lid THEN 'lid_phone' WHEN counts.n = 1 THEN 'phone' ELSE NULL END"
        in sql
    )


def test_rematch_stored_handles_nulls_on_zero_or_ambiguous_matches():
    conn = FakeConn()
    whatsapp.rematch_stored_handles(conn)
    sql, _ = conn.calls[0]
    assert "LEFT JOIN counts ON counts.handle = k.handle" in sql
    assert "UPDATE whatsapp_handles h SET" in sql
    assert "updated_at = now()" in sql
```

Add:

```python
def test_summary_for_person_ranks_lid_phone_between_phone_and_name():
    conn = FakeConn()
    whatsapp.summary_for_person(conn, 1)
    sql, _ = conn.calls[0]
    assert sql.index("'phone') THEN 'phone'") < sql.index("'lid_phone') THEN 'lid_phone'")
    assert sql.index("'lid_phone') THEN 'lid_phone'") < sql.index("'name') THEN 'name'")


def test_handle_reads_serve_phone():
    conn = FakeConn()
    whatsapp.handles(conn)
    sql, _ = conn.calls[0]
    assert "h.phone" in sql
```

In `tests/test_import_whatsapp.py::test_a_real_run_writes_in_the_documented_order`, change the rematch marker `"FROM whatsapp_handles WHERE handle NOT LIKE"` to `"WHERE handle NOT LIKE 'lid:%' OR phone IS NOT NULL"`.

In `tests/test_schema.py`, after `test_whatsapp_tables_exist`:

```python
def test_whatsapp_handles_has_a_phone_column(conn):
    cols = {
        r[0]
        for r in conn.execute(
            "select column_name from information_schema.columns"
            " where table_name = 'whatsapp_handles'"
        ).fetchall()
    }
    assert "phone" in cols
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_whatsapp.py tests/test_import_whatsapp.py -q`
Expected: the new and changed tests FAIL (`TypeError: upsert_handles() got an unexpected keyword argument 'full'`, missing SQL fragments). `test_schema.py` skips unless `TEST_DATABASE_URL` is set.

- [ ] **Step 3: Schema**

In `repo/schema.sql`, change the `whatsapp_handles` comments: `display_name` to `-- partner, address-book, profile or member name (2026-10-14 §5.2)`, and `match_method` to `-- 'phone' | 'lid_phone' | 'name' | NULL`. Directly after the `whatsapp_handles_name_trgm_idx` index, add:

```sql
-- E.164: the handle itself, or a LID's phone from WhatsApp's address book
-- (spec 2026-10-14 §5.3). Additive, so existing databases migrate in place.
ALTER TABLE whatsapp_handles ADD COLUMN IF NOT EXISTS phone TEXT;
```

- [ ] **Step 4: `repo/whatsapp.py`**

Replace the handle constants:

```python
_HANDLE_COLUMNS = ("handle", "jid", "display_name", "person_id", "match_method", "phone")
_HANDLE_UPDATE = ("jid", "display_name", "person_id", "match_method", "phone")
# person_id/match_method are rewritten verbatim, including to NULL — the match is
# redone from scratch every run (§6.5). display_name and phone depend on the mode
# (spec 2026-10-14 §5.4): a full batch is authoritative and overwrites, NULL
# included, which is what cleans stored protobuf-blob names; an incremental batch
# may simply not have seen a handle's session or address-book row, so it coalesces.
_HANDLE_INCREMENTAL_OVERRIDES = {
    "display_name": (
        "display_name = COALESCE(EXCLUDED.display_name, whatsapp_handles.display_name)"
    ),
    "phone": "phone = COALESCE(EXCLUDED.phone, whatsapp_handles.phone)",
}
```

Replace `upsert_handles`:

```python
def upsert_handles(
    conn: Any, handles: list[WhatsAppHandle], chunk: int = 500, *, full: bool = False
) -> int:
    """Link fields only (§6.7 step 2) — the stats columns belong exclusively to
    recompute_handle_stats and must never be touched here, or an ON CONFLICT would
    reset a counter to its insert default."""
    return _upsert_many(
        conn,
        "whatsapp_handles",
        _HANDLE_COLUMNS,
        "handle",
        _HANDLE_UPDATE,
        [
            (h.handle, h.jid, h.display_name, h.person_id, h.match_method, h.phone)
            for h in handles
        ],
        chunk,
        set_overrides=None if full else _HANDLE_INCREMENTAL_OVERRIDES,
    )
```

Replace the SQL in `rematch_stored_handles`:

```python
    conn.execute(
        """
        WITH keyed AS (
            SELECT handle, COALESCE(phone, handle) AS phone, handle LIKE 'lid:%' AS is_lid
            FROM whatsapp_handles
            WHERE handle NOT LIKE 'lid:%' OR phone IS NOT NULL
        ), candidates AS (
            SELECT k.handle, p.id AS person_id
            FROM keyed k
            JOIN people p ON k.phone = ANY(p.phone_numbers)
        ), counts AS (
            SELECT handle, COUNT(DISTINCT person_id) AS n, MIN(person_id) AS only_id
            FROM candidates GROUP BY handle
        )
        UPDATE whatsapp_handles h SET
            person_id = CASE WHEN counts.n = 1 THEN counts.only_id ELSE NULL END,
            match_method = CASE WHEN counts.n = 1 AND k.is_lid THEN 'lid_phone'
                                WHEN counts.n = 1 THEN 'phone' ELSE NULL END,
            updated_at = now()
        FROM keyed k
        LEFT JOIN counts ON counts.handle = k.handle
        WHERE h.handle = k.handle
        """
    )
```

The test asserts on whitespace-collapsed SQL (`FakeConn` joins on whitespace), so the multi-line `CASE` matches `"WHEN counts.n = 1 AND k.is_lid THEN 'lid_phone' WHEN counts.n = 1 THEN 'phone' ELSE NULL END"`. Update the docstring's last paragraph: "A `lid:` handle is re-matched only when it has a stored `phone` (spec 2026-10-14 §5.3), labelled `'lid_phone'`. A LID without one keeps its name link, which this SQL cannot recompute."

In `summary_for_person`, change the `CASE` to:

```sql
                   CASE
                       WHEN bool_or(match_method = 'phone') THEN 'phone'
                       WHEN bool_or(match_method = 'lid_phone') THEN 'lid_phone'
                       WHEN bool_or(match_method = 'name') THEN 'name'
                   END AS match_method,
```

In `_HANDLE_OUT`, change the first line to `h.handle, h.jid, h.phone, h.display_name, h.person_id, p.email AS person_email, h.match_method,`.

- [ ] **Step 5: API**

In `api/routers/whatsapp.py`, `WhatsAppHandleOut`, after `jid: str | None`:

```python
    # E.164: the handle itself, or a LID's phone from WhatsApp's address book.
    phone: str | None = None
```

- [ ] **Step 6: Wire the mode through the script (needed for the run-order test)**

In `scripts/import_whatsapp.py::run`, change `whatsapp_repo.upsert_handles(conn, batch.handles)` to `whatsapp_repo.upsert_handles(conn, batch.handles, full=full)`.

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS (schema tests skip without `TEST_DATABASE_URL`). If a local Postgres is available: `TEST_DATABASE_URL=… .venv/bin/pytest tests/test_schema.py -q` passes.

- [ ] **Step 8: Commit**

```bash
git add repo/schema.sql repo/whatsapp.py api/routers/whatsapp.py scripts/import_whatsapp.py \
  tests/test_repo_whatsapp.py tests/test_schema.py tests/test_import_whatsapp.py
git commit -m "whatsapp: store phone, overwrite names on --full, re-match LID phones, serve phone"
```

---

### Task 5: Summary, metrics and docs

**Files:**
- Modify: `scripts/import_whatsapp.py` (`summary`, `_record_metrics`)
- Modify: `.claude/skills/importing-whatsapp/SKILL.md`, `.claude/skills/querying-people-db/SKILL.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md`, `docs/superpowers/specs/2026-10-14-whatsapp-display-names-design.md`
- Test: `tests/test_import_whatsapp.py`

**Interfaces:**
- Consumes: `batch.matched_by_lid_phone`, `batch.names_*` (Tasks 2–3).
- Produces: the handles summary line `handles N (phone-matched N, name-matched N, lid-phone-matched N, unmatched N, unnormalized N)`, and a new line `names: partner N, address book N, profile N, member N, none N`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_import_whatsapp.py`:

```python
def test_summary_reports_lid_phone_matches_and_name_sources():
    batch = WhatsAppBatch(mode="full", watermark=1)
    batch.handles = [WhatsAppHandle(handle="lid:1", phone="+15550100005")]
    batch.lid_phones = {"lid:1": "+15550100005"}
    match_handles(batch, [{"id": 9, "display_name": "X", "phone_numbers": ["+15550100005"]}])
    batch.names_address_book = 1
    out = import_whatsapp.summary(batch, dry_run=False, deleted=0)
    assert "phone-matched 0, name-matched 0, lid-phone-matched 1, unmatched 0" in out
    assert "names: partner 0, address book 1, profile 0, member 0, none 0" in out
    assert "+15550100005" not in out  # counts only, never a number


def test_metrics_count_lid_phone_matches(monkeypatch):
    seen = []

    class FakeCounter:
        def add(self, n, attrs):
            seen.append((n, attrs))

    monkeypatch.setattr(import_whatsapp.otel, "whatsapp_import_handles", FakeCounter())
    batch = WhatsAppBatch(mode="full", watermark=1)
    batch.matched_by_lid_phone = 3
    import_whatsapp._record_metrics(batch, deleted=0, started_at=datetime.now(UTC))
    assert (3, {"match_method": "lid_phone"}) in seen
```

Add `from datetime import UTC, datetime` to the test module's imports if it isn't there already.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_import_whatsapp.py -q`
Expected: the two new tests FAIL.

- [ ] **Step 3: Implement**

In `scripts/import_whatsapp.py`, in both `_record_metrics` and `summary`, change the unmatched computation to:

```python
    unmatched = (
        len(batch.handles)
        - batch.matched_by_phone
        - batch.matched_by_name
        - batch.matched_by_lid_phone
    )
```

In `_record_metrics`, after the `"name"` line:

```python
    otel.whatsapp_import_handles.add(batch.matched_by_lid_phone, {"match_method": "lid_phone"})
```

In `summary`, replace the handles line and add the names line after it:

```python
        f"handles {len(batch.handles):,} (phone-matched {batch.matched_by_phone:,}, "
        f"name-matched {batch.matched_by_name:,}, "
        f"lid-phone-matched {batch.matched_by_lid_phone:,}, unmatched {unmatched:,}, "
        f"unnormalized {batch.handles_unnormalized:,})",
        f"names: partner {batch.names_partner:,}, address book {batch.names_address_book:,}, "
        f"profile {batch.names_profile:,}, member {batch.names_member:,}, "
        f"none {batch.names_none:,}",
```

In `main_with`, change the `SchemaError` message prefix from `"ChatStorage.sqlite schema error: "` to `"WhatsApp store schema error: "`. The error text itself now names the file. `test_a_schema_error_exits_2_naming_the_column` asserts only on `ZSTANZAID`, so it still passes.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Docs**

- `.claude/skills/importing-whatsapp/SKILL.md`:
  - Document the `names:` summary line and `lid-phone-matched`.
  - Note that `ContactsV2.sqlite` is read when present and that its absence only degrades names and LID phones.
  - Note that after this ships, **one `--full` run** cleans every stored garbled name.
  - Add the check query from the spec §9 step 4.
- `.claude/skills/querying-people-db/SKILL.md`, in the `whatsapp_handles` row:
  - Add `phone` (E.164; the handle itself, or a LID's phone from WhatsApp's address book).
  - Change `match_method` to `phone`/`lid_phone`/`name`/null, and say `lid_phone` is as trustworthy as `phone`.
  - Change `display_name`'s source to partner name > address book > profile push name.
- `CLAUDE.md`, code layout: `whatsapp_local.py  read-only ChatStorage.sqlite + ContactsV2.sqlite access for scripts/import_whatsapp.py only`.
- `docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md`:
  - Under the header, add `**Amended by:** 2026-10-14-whatsapp-display-names-design.md (§5.5, §6.5, §6.6)`.
  - In §6.6, add one sentence: "Superseded: `ZWAMESSAGE.ZPUSHNAME` is a protobuf blob, not a name. See the 2026-10-14 design."
- `docs/superpowers/specs/2026-10-14-whatsapp-display-names-design.md`: set `**Status:** Implemented`.

- [ ] **Step 6: Full verification**

Run:
```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
```
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add scripts/import_whatsapp.py tests/test_import_whatsapp.py .claude/skills CLAUDE.md docs/superpowers/specs
git commit -m "import_whatsapp: report LID phone matches and name sources; docs"
```

---

## After the plan (operator steps, not tasks)

These need Ben's Mac, the real store and the real DB, so they run after merge (spec §9):

1. `.venv/bin/python scripts/migrate_db.py` adds `whatsapp_handles.phone`.
2. `.venv/bin/python scripts/import_whatsapp.py --full --dry-run`. Expect about 152 names recovered, `lid-phone-matched` > 0, and member matches up by about 103.
3. `.venv/bin/python scripts/import_whatsapp.py --full`.
4. Verify that `SELECT count(*) FROM whatsapp_handles WHERE display_name ~ '^[A-Za-z0-9+/]{4,}={1,2}$'` returns 0.
