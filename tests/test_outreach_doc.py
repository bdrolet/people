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
    return doc.merge(
        existing, update, person=PERSON, stage="briefed", messages=messages, today=TODAY
    )


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
            "linkedin": [
                {"sent_at": TS, "from_me": True, "text": "x" * 500, "has_attachments": False}
            ],
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
    assert (
        doc.parse_sections(
            SECTIONS.replace("## Log\n\n- 2026-10-14 · prepped brief (iMessage)\n\n", "")
        ).log
        == ()
    )


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
    order = [
        "## Who they are",
        "## History",
        "## Log",
        "## Talking points (next step: nudge 1)",
        "## Ben's notes",
        "## Messages",
    ]
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
    assert (
        "- 2026\\-10\\-14 · prepped brief \\(iMessage\\)\n- 2026-10-15 · briefed by iMessage" in out
    )
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
