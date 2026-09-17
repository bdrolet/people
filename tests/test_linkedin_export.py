import shutil
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

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
        ("Mary-Jane O’Brien", "mary-jane o\x27brien"),  # U+2019 right single quotation mark
        ("O’Brien", "o\x27brien"),  # U+2019
        ("O‘Brien", "o\x27brien"),  # U+2018 left single quotation mark
        ("OʼBrien", "o\x27brien"),  # U+02BC modifier letter apostrophe
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
    assert c2.recipient_profile_urls == [
        "linkedin.com/in/alice-example",
        "linkedin.com/in/bob-sample",
    ]
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
    assert s.missing_files == [
        "messages.csv",
        "Recommendations_Given.csv",
        "Recommendations_Received.csv",
    ]


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
