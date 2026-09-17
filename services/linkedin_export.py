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
