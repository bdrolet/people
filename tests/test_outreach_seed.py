from datetime import UTC, datetime, timedelta

from scripts import outreach_seed as script
from services import outreach_seed as seed

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(days=30)
OLD = NOW - timedelta(days=800)


def email(pid, **kw):
    return {
        "id": pid,
        "email": f"p{pid}@example.com",
        "display_name": f"P{pid}",
        "eligible": True,
        "automated": False,
        "message_count": 3,
        "my_response_count": 2,
        "last_interaction": RECENT,
    } | kw


def linkedin(pid, **kw):
    return {
        "person_id": pid,
        "full_name": f"L{pid}",
        "company": "Co",
        "position": "Engineer",
        "profile_url": f"https://www.linkedin.com/in/l{pid}",
        "message_count": 4,
        "my_message_count": 2,
        "last_message_at": RECENT,
    } | kw


def handle(pid, **kw):
    return {
        "person_id": pid,
        "handle": f"+1555010000{pid or 9}",
        "display_name": f"H{pid}",
        "message_count": 12,
        "my_message_count": 5,
        "last_message_at": RECENT,
    } | kw


def _plan(**kw):
    rows = {
        "email_rows": [],
        "linkedin_rows": [],
        "imessage_rows": [],
        "whatsapp_rows": [],
        "existing": set(),
    }
    return seed.plan(**(rows | kw), now=NOW, window_days=730, min_messages=10)


def test_email_bar():
    p = _plan(
        email_rows=[
            email(1),
            email(2, eligible=False),
            email(3, automated=True),
            email(4, my_response_count=0),  # Ben never wrote
            email(5, message_count=0),  # they never wrote
            email(6, last_interaction=OLD),
        ]
    )
    assert p.candidates == {1: {"email"}}


def test_linkedin_bar_excludes_recruiters_and_one_sided_threads():
    p = _plan(
        linkedin_rows=[
            linkedin(1),
            linkedin(2, position="Senior Technical Recruiter"),
            linkedin(3, position="Head of Talent"),
            linkedin(4, my_message_count=4),  # all four messages were Ben's
            linkedin(5, last_message_at=OLD),
            linkedin(6, position=None),
        ]
    )
    assert set(p.candidates) == {1, 6}


def test_text_bars_need_volume_and_a_reply_from_ben():
    p = _plan(
        imessage_rows=[handle(1), handle(2, message_count=9), handle(3, my_message_count=0)],
        whatsapp_rows=[handle(4), handle(5, last_message_at=None)],
    )
    assert p.candidates == {1: {"imessage"}, 4: {"whatsapp"}}


def test_sources_merge_per_person_and_by_source_counts():
    p = _plan(email_rows=[email(1)], imessage_rows=[handle(1)], linkedin_rows=[linkedin(2)])
    assert p.candidates == {1: {"email", "imessage"}, 2: {"linkedin"}}
    assert p.by_source() == {"email": 1, "imessage": 1, "linkedin": 1, "whatsapp": 0}


def test_rows_without_a_person_are_listed_not_inserted():
    p = _plan(linkedin_rows=[linkedin(None)], whatsapp_rows=[handle(None)])
    assert p.candidates == {}
    assert [(u.source, u.detail) for u in p.unlinked] == [
        ("linkedin", "https://www.linkedin.com/in/lNone"),
        ("whatsapp", "+15550100009"),
    ]


def test_people_already_in_the_pipeline_are_never_proposed_again():
    p = _plan(email_rows=[email(1), email(2)], existing={1})
    assert p.candidates == {2: {"email"}} and p.already_in == {1}


class Conn:
    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch):
    repo = script.outreach_repo
    monkeypatch.setattr(repo, "email_seed_rows", lambda c: [email(1), email(2)])
    monkeypatch.setattr(repo, "linkedin_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "imessage_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "whatsapp_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "pipeline_person_ids", lambda c: {2})
    inserted = []
    monkeypatch.setattr(
        script.outreach,
        "ensure_contact",
        lambda c, pid, *, at, source: inserted.append((pid, source)),
    )
    return inserted


def test_dry_run_inserts_nothing(monkeypatch):
    inserted = _wire(monkeypatch)
    conn = Conn()
    p = script.run(lambda: conn, now=NOW, window_days=730, min_messages=10, dry_run=True)
    assert inserted == [] and conn.rolled_back and set(p.candidates) == {1}


def test_a_real_run_inserts_candidates_as_seed(monkeypatch):
    inserted = _wire(monkeypatch)
    script.run(lambda: Conn(), now=NOW, window_days=730, min_messages=10, dry_run=False)
    assert inserted == [(1, "seed")]


def test_summary_counts_and_lists_unlinked_people():
    p = seed.SeedPlan(
        candidates={1: {"email"}},
        already_in={2},
        unlinked=[seed.Unlinked("linkedin", "Lee Example", "https://www.linkedin.com/in/lee")],
    )
    text = script.summary(p, dry_run=True)
    assert "DRY RUN" in text and "candidates 1" in text and "already in the pipeline 1" in text
    assert "Lee Example" in text and "creating-person" in text
