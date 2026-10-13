from datetime import UTC, date, datetime

from services import outreach_session as svc

TS = datetime(2025, 12, 1, 23, 0, tzinfo=UTC)

ROW = {
    "id": 388,
    "email": "jonah@example.com",
    "display_name": "Jonah Example",
    "company": "Workday",
    "job_title": "Director - Agent Platform",
    "notes": "Met at Eaze",
    "stage": "to_contact",
    "rank": 1,
    "follow_up_on": None,
    "has_lead": False,
    "outreach_notes": None,
    "briefed_at": None,
    "nudge_count": 0,
    "email_last": None,
    "imessage_last": TS,
    "has_imessage": True,
    "whatsapp_last": None,
    "has_whatsapp": False,
    "linkedin_url": "linkedin.com/in/jonah-example",
    "linkedin_last": None,
    "last_interaction": TS,
    "labels": ["consulting-outreach"],
}


class FakeRepos:
    def __init__(self, row=ROW):
        self.row = row

    def pipeline_row(self, conn, person_id):
        return self.row

    def get_by_id(self, conn, person_id):
        return {"id": person_id, "phone_numbers": ["+15550100001"]}

    def connection_for_person(self, conn, person_id):
        return {
            "profile_url": "linkedin.com/in/jonah-example",
            "company": "Workday",
            "position": "Director - Agent Platform",
            "connected_on": date(2023, 11, 11),
        }

    def messages_for(self, conn, profile_url, limit):  # newest first, like repo/linkedin.py
        return [
            {"sent_at": TS, "from_me": True, "content": "thanks again"},
            {"sent_at": datetime(2025, 1, 1, tzinfo=UTC), "from_me": False, "content": "hey"},
        ]

    def recent_direct_messages(self, conn, person_id, limit):
        return [
            {"sent_at": TS, "from_me": False, "text": "hi!\nhow are you", "has_attachments": False},
            {"sent_at": TS, "from_me": True, "text": None, "has_attachments": True},
        ]


def _patch(monkeypatch, fake):
    for name in ("outreach_repo", "people_repo", "linkedin_repo", "imessage_repo"):
        monkeypatch.setattr(svc, name, fake)

    class NoWhatsApp:
        def recent_direct_messages(self, conn, person_id, limit):
            return []

    monkeypatch.setattr(svc, "whatsapp_repo", NoWhatsApp())


def test_snapshot_is_none_outside_the_pipeline(monkeypatch):
    _patch(monkeypatch, FakeRepos(row=None))
    assert svc.snapshot(None, 388, limit=60) is None


def test_snapshot_gathers_every_channel_oldest_first(monkeypatch):
    _patch(monkeypatch, FakeRepos())
    snap = svc.snapshot(None, 388, limit=60)
    assert snap["item"]["person"]["id"] == 388 and snap["item"]["rank"] == 1
    assert snap["phone_numbers"] == ["+15550100001"]
    assert [m["text"] for m in snap["messages"]["linkedin"]] == ["hey", "thanks again"]
    assert len(snap["messages"]["imessage"]) == 2 and snap["messages"]["whatsapp"] == []


def test_context_block_carries_identity_pipeline_and_history(monkeypatch):
    _patch(monkeypatch, FakeRepos())
    block = svc.context_block(svc.snapshot(None, 388, limit=60), today=date(2026, 10, 13))
    assert block.startswith("# Active outreach person: Jonah Example")
    assert "people id `388`" in block and "working-outreach-queue" in block
    assert "jonah@example.com" in block and "+15550100001" in block
    assert "linkedin.com/in/jonah-example" in block and "connected 2023-11-11" in block
    assert "Director - Agent Platform at Workday" in block
    assert "to_contact · rank 1" in block
    assert "Met at Eaze" in block and "consulting-outreach" in block
    assert "2025-12-01 Jonah: hi! how are you" in block  # newlines flattened
    assert "2025-12-01 Ben: [attachment]" in block
    assert "### WhatsApp" not in block  # empty channels are omitted
    assert "2026-10-13" in block


def test_context_block_shows_briefed_state_and_truncates_long_messages(monkeypatch):
    row = ROW | {
        "stage": "briefed",
        "rank": None,
        "briefed_at": datetime(2026, 10, 6, 15, tzinfo=UTC),
        "nudge_count": 1,
        "follow_up_on": date(2026, 10, 20),
    }
    fake = FakeRepos(row=row)
    fake.recent_direct_messages = lambda conn, pid, limit: [
        {"sent_at": TS, "from_me": False, "text": "x" * 500, "has_attachments": False}
    ]
    _patch(monkeypatch, fake)
    block = svc.context_block(svc.snapshot(None, 388, limit=60), today=date(2026, 10, 13))
    assert "briefed 2026-10-06 · 1 nudge · follow-up due 2026-10-20" in block
    assert "x" * svc.TEXT_MAX + "…" in block and "x" * (svc.TEXT_MAX + 1) not in block


def test_context_block_without_any_messages(monkeypatch):
    fake = FakeRepos()
    fake.recent_direct_messages = lambda conn, pid, limit: []
    fake.connection_for_person = lambda conn, pid: None
    _patch(monkeypatch, fake)
    block = svc.context_block(svc.snapshot(None, 388, limit=60), today=date(2026, 10, 13))
    assert "No direct messages on file." in block and "LinkedIn: none linked" in block


def test_session_name_is_bounded():
    assert svc.session_name({"display_name": "Jonah Example"}) == "outreach: Jonah Example"
    long = svc.session_name({"display_name": "A" * 100, "id": 1})
    assert len(long) == svc.NAME_MAX and long.endswith("…")
    assert svc.session_name({"display_name": None, "email": None, "id": 7}) == "outreach: person 7"


def test_prep_prompt_points_at_the_skill_procedure():
    assert "working-outreach-queue" in svc.PREP_PROMPT and "Prep a brief" in svc.PREP_PROMPT


def test_people_api_never_reads_message_text():
    """Message text goes only into a local session's prompt; people-api must never
    reach the functions that select it (iMessage and WhatsApp §7)."""
    from pathlib import Path

    api = Path(__file__).resolve().parent.parent / "api"
    for path in api.rglob("*.py"):
        source = path.read_text()
        assert "recent_direct_messages" not in source, path
        assert "outreach_session" not in source, path
