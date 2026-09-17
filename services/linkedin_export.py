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
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote

from models.linkedin import (
    LinkedInConnection,
    LinkedInMessage,
    LinkedInRecommendation,
    LinkedInSnapshot,
)
from services.eligibility import normalize as normalize_email

# LinkedIn message bodies can exceed Python's default 131072-char csv field
# limit; raise it rather than crash on a long CONTENT cell. Not sys.maxsize —
# that overflows the C long the csv module casts it to on some platforms.
csv.field_size_limit(2**31 - 1)


class ExportError(Exception):
    """The export is unusable: a required file or column is missing."""


_URL_PREFIX = re.compile(r"^(https?://)?(www\.)?", re.IGNORECASE)
_DATE_FORMATS = ("%d %b %Y", "%m/%d/%y %I:%M %p", "%m/%d/%y, %I:%M %p", "%m/%d/%Y", "%Y-%m-%d")
_APOSTROPHE_MAP = str.maketrans(
    {
        "’": "\x27",  # U+2019 RIGHT SINGLE QUOTATION MARK -> ASCII apostrophe
        "‘": "\x27",  # U+2018 LEFT SINGLE QUOTATION MARK -> ASCII apostrophe
        "ʼ": "\x27",  # U+02BC MODIFIER LETTER APOSTROPHE -> ASCII apostrophe
    }
)


def normalize_profile_url(url: str | None) -> str | None:
    u = (url or "").strip()
    u = _URL_PREFIX.sub("", u).split("?", 1)[0].split("#", 1)[0].rstrip("/")
    u = unquote(u).lower()
    return u or None


def normalize_name(name: str | None) -> str:
    """Matching key only — stored names keep their original form (§5.2)."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    s = s.translate(_APOSTROPHE_MAP)
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
        recipients = (
            normalize_profile_url(u) for u in r.get("RECIPIENT PROFILE URLS", "").split(",")
        )
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
