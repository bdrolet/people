"""Read-only access to WhatsApp's local store
(docs/superpowers/specs/2026-09-29-whatsapp-snapshot-design.md §5.1, §6.1). Local
script use only — people's Cloud Functions never read WhatsApp, exactly as
clients/imessage_local.py and clients/graph_local.py are import-only."""

import logging
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
        "Z_PK",
        "ZCONTACTJID",
        "ZSESSIONTYPE",
        "ZPARTNERNAME",
        "ZLASTMESSAGEDATE",
        "ZGROUPINFO",
    ),
    "ZWAMESSAGE": (
        "Z_PK",
        "ZSTANZAID",
        "ZCHATSESSION",
        "ZISFROMME",
        "ZMESSAGEDATE",
        "ZTEXT",
        "ZMESSAGETYPE",
        "ZGROUPMEMBER",
        "ZMEDIAITEM",
    ),
    "ZWAGROUPMEMBER": (
        "Z_PK",
        "ZCHATSESSION",
        "ZMEMBERJID",
        "ZISADMIN",
        "ZISACTIVE",
        "ZCONTACTNAME",
    ),
    "ZWAGROUPINFO": ("Z_PK", "ZCREATIONDATE"),
    "ZWAMEDIAITEM": ("Z_PK", "ZMEDIALOCALPATH", "ZMEDIAURL", "ZVCARDSTRING", "ZTITLE"),
    "ZWAPROFILEPUSHNAME": ("ZJID", "ZPUSHNAME"),
}

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
       m.ZMESSAGETYPE, m.ZGROUPMEMBER, m.ZMEDIAITEM,
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
    push_names: dict[str, str] = field(default_factory=dict)
    address_book: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def _copied(path: Path) -> Iterator[Path]:
    """Copy the store and its sidecars to a temp directory and yield the copy.

    The copy holds the full message history in plaintext, so the directory is
    removed in a finally — including on error (§7)."""
    tmp = Path(tempfile.mkdtemp(prefix="whatsapp-import-"))
    try:
        try:
            shutil.copy2(path, tmp / path.name)
            for suffix in SIDECARS:
                side = path.with_name(path.name + suffix)
                if side.exists():
                    shutil.copy2(side, tmp / side.name)
        except OSError as e:
            raise StoreUnreadableError(str(e)) from e
        yield tmp / path.name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_schema(
    conn: sqlite3.Connection,
    required: Mapping[str, tuple[str, ...]] = REQUIRED_COLUMNS,
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


def read(
    path: Path,
    *,
    since_pk: int = 0,
    window_start: datetime | None = None,
    full: bool = False,
) -> RawStore:
    """Copy the store, then read sessions, messages and group members from the copy.

    Sessions and members are always read in full; messages honour the Z_PK
    watermark plus an optional re-scan window (§6.2). `full` ignores both.

    Also reads profile push names, and the sibling ContactsV2.sqlite address book
    in full (spec 2026-10-14 §5.1)."""
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
            push_names = {r["ZJID"]: r["ZPUSHNAME"].strip() for r in conn.execute(PUSH_NAME_SQL)}
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
