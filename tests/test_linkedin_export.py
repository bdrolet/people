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
        ("Mary-Jane O’Brien", "mary-jane o'brien"),  # U+2019 right single quotation mark
        ("O’Brien", "o'brien"),  # U+2019
        ("OʼBrien", "o'brien"),  # U+02BC modifier letter apostrophe
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
    assert lx.parse_timestamp("2026-09-16 17:03:12 UTC") == datetime(
        2026, 9, 16, 17, 3, 12, tzinfo=UTC
    )
    assert lx.parse_timestamp("not-a-date") is None
    assert lx.parse_timestamp("") is None
