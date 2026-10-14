from datetime import UTC, datetime, timedelta

import pytest

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
    assert [(u.source, u.detail) for u in p.unlinked] == [("whatsapp", "+15550100009")]
    assert [o.kind for o in p.linkedin] == ["create"]


def test_people_already_in_the_pipeline_are_never_proposed_again():
    p = _plan(email_rows=[email(1), email(2)], existing={1})
    assert p.candidates == {2: {"email"}} and p.already_in == {1}


def li(slug, pid=None, first="Jane", last="Example", **kw):
    return (
        linkedin(
            pid,
            profile_url=f"linkedin.com/in/{slug}",
            first_name=first,
            last_name=last,
            full_name=f"{first} {last}",
        )
        | kw
    )


def person_row(pid, name, url=None, rn=None):
    return {"id": pid, "display_name": name, "linkedin_url": url, "google_resource_name": rn}


def test_unlinked_linkedin_rows_get_one_outcome_each():
    p = _plan(
        linkedin_rows=[
            li("a", first="Ann", last="Alpha"),
            li("b", first="Bea", last="Google"),
            li("c", first="Jonathan", last="Smith"),
            li("d", first="Kimberly", last="Lee"),
            li("e", first="Dee", last="New"),
        ],
        people_rows=[
            person_row(5, "Pat Linked", url="linkedin.com/in/a", rn="people/c5"),
            person_row(6, "Jon Smith", rn="people/c6"),
        ],
        google_urls={"linkedin.com/in/b": [{"resource_name": "people/c9", "display_name": "B G"}]},
        google_contacts=[
            {"resource_name": "people/c9", "display_name": "B G"},
            {"resource_name": "people/c10", "display_name": "Kim Lee"},
            {
                "resource_name": "people/c6",
                "display_name": "Jon Smith",
            },  # has a row: not Google-only
        ],
    )
    got = {
        o.row["profile_url"]: (o.kind, o.person_id, o.candidates, o.google_names)
        for o in p.linkedin
    }
    assert got == {
        "linkedin.com/in/a": ("link", 5, [], []),
        "linkedin.com/in/b": ("in_google", None, [], []),
        "linkedin.com/in/c": ("possible_duplicate", None, [6], []),
        "linkedin.com/in/d": ("possible_duplicate", None, [], ["Kim Lee"]),
        "linkedin.com/in/e": ("create", None, [], []),
    }
    assert [o.row["profile_url"] for o in p.linkedin_of("create")] == ["linkedin.com/in/e"]
    assert p.candidates == {}


def test_a_url_link_wins_over_a_name_match():
    p = _plan(
        linkedin_rows=[li("a", first="Jon", last="Smith")],
        people_rows=[
            person_row(5, "Someone", url="linkedin.com/in/a"),
            person_row(6, "Jon Smith"),
        ],
    )
    assert [(o.kind, o.person_id) for o in p.linkedin] == [("link", 5)]


def test_cold_unlinked_rows_get_no_outcome():
    p = _plan(linkedin_rows=[li("a", last_message_at=OLD), li("b", position="Recruiter")])
    assert p.linkedin == []


def test_name_key_is_last_name_and_first_initial():
    assert seed.name_key("Jon Smith") == ("smith", "j")
    assert seed.name_key("Jonathan  Smith") == ("smith", "j")
    assert seed.name_key("Jane Q. Example") == ("example", "j")
    assert seed.name_key("Élodie Accent") == ("accent", "e")
    assert seed.name_key("Cher") is None
    assert seed.name_key(None) is None


def test_contact_for_builds_the_google_contact():
    assert seed.contact_for(li("jane-example", company="Example Health", position="CTO")) == {
        "names": [{"givenName": "Jane", "familyName": "Example"}],
        "organizations": [{"name": "Example Health", "title": "CTO"}],
        "urls": [{"value": "https://www.linkedin.com/in/jane-example", "type": "profile"}],
    }


def test_contact_for_without_names_or_company():
    row = li("x", first=None, last=None, company=None, position=None) | {"full_name": "X Example"}
    assert seed.contact_for(row) == {
        "names": [{"givenName": "X Example"}],
        "urls": [{"value": "https://www.linkedin.com/in/x", "type": "profile"}],
    }


class Conn:
    def __init__(self):
        self.rolled_back = 0
        self.commits = 0

    def rollback(self):
        self.rolled_back += 1

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, *, linkedin_rows=(), people_rows=(), google=None, create=None):
    repo = script.outreach_repo
    monkeypatch.setattr(repo, "email_seed_rows", lambda c: [email(1), email(2)])
    monkeypatch.setattr(repo, "linkedin_seed_rows", lambda c: list(linkedin_rows))
    monkeypatch.setattr(repo, "imessage_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "whatsapp_seed_rows", lambda c: [])
    monkeypatch.setattr(repo, "pipeline_person_ids", lambda c: {2})
    monkeypatch.setattr(repo, "people_seed_rows", lambda c: list(people_rows))
    monkeypatch.setattr(script.gc, "list_url_index", lambda: google or ({}, []))
    log = {"inserted": [], "linked": [], "created": []}
    monkeypatch.setattr(
        script.outreach,
        "ensure_contact",
        lambda c, pid, *, at, source: log["inserted"].append((pid, source)) or 1,
    )
    monkeypatch.setattr(
        script.linkedin_repo,
        "link_connection",
        lambda c, url, pid: log["linked"].append((url, pid)),
    )

    def fake_create(c, *, contact, labels):
        log["created"].append((contact["urls"][0]["value"], labels))
        if create:
            create(contact)
        return {"id": 100 + len(log["created"])}

    monkeypatch.setattr(script.person_create, "create", fake_create)
    return log


def _run(conn, *, dry_run=False):
    return script.run(lambda: conn, now=NOW, window_days=730, min_messages=10, dry_run=dry_run)


def test_dry_run_writes_nothing(monkeypatch):
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("a"), li("e", first="Dee", last="New")],
        people_rows=[person_row(5, "Pat Linked", url="linkedin.com/in/a")],
    )
    conn = Conn()
    r = _run(conn, dry_run=True)
    assert log == {"inserted": [], "linked": [], "created": []}
    assert conn.rolled_back and conn.commits == 0 and set(r.plan.candidates) == {1}
    text = script.summary(r, dry_run=True)
    assert "DRY RUN" in text
    assert "link 1, create 1, possible duplicate 0, in Google not synced 0" in text


def test_a_real_run_inserts_candidates_as_seed(monkeypatch):
    log = _wire(monkeypatch)
    r = _run(Conn())
    assert log["inserted"] == [(1, "seed")] and r.entered == 1


def test_link_enters_the_person_without_google(monkeypatch):
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("a")],
        people_rows=[person_row(5, "Pat Linked", url="linkedin.com/in/a")],
    )
    r = _run(Conn())
    assert log["linked"] == [("linkedin.com/in/a", 5)]
    assert (5, "seed") in log["inserted"]
    assert log["created"] == [] and r.linked == 1


def test_create_labels_links_and_commits_per_person(monkeypatch):
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New"), li("e", first="Eve", last="Other")],
    )
    conn = Conn()
    r = _run(conn)
    assert log["created"] == [
        ("https://www.linkedin.com/in/d", ["from-linkedin"]),
        ("https://www.linkedin.com/in/e", ["from-linkedin"]),
    ]
    assert log["linked"] == [("linkedin.com/in/d", 101), ("linkedin.com/in/e", 102)]
    assert (101, "seed") in log["inserted"] and (102, "seed") in log["inserted"]
    assert r.created == 2 and conn.commits == 3  # the candidates, then one per person


def test_one_failure_does_not_stop_the_run(monkeypatch):
    def create(contact):
        if contact["urls"][0]["value"].endswith("/d"):
            raise RuntimeError("quota")

    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New"), li("e", first="Eve", last="Other")],
        create=create,
    )
    conn = Conn()
    r = _run(conn)
    assert r.created == 1 and [name for name, _ in r.failures] == ["Dee New"]
    assert conn.rolled_back == 1
    assert log["linked"] == [("linkedin.com/in/e", 102)]
    out = script.summary(r, dry_run=False)
    assert "Failed:" in out and "a Google contact may now exist" in out


def test_a_rerun_links_instead_of_creating(monkeypatch):
    log = _wire(monkeypatch, linkedin_rows=[li("d", first="Dee", last="New")])
    _run(Conn())
    created_id = log["linked"][0][1]
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New")],
        people_rows=[person_row(created_id, "Dee New", url="linkedin.com/in/d")],
    )
    r = _run(Conn())
    assert log["created"] == [] and log["linked"] == [("linkedin.com/in/d", created_id)]
    assert r.linked == 1


def test_an_orphaned_google_contact_is_reported_not_recreated(monkeypatch):
    # Review Focus 3: the create reached Google but the commit did not.
    log = _wire(
        monkeypatch,
        linkedin_rows=[li("d", first="Dee", last="New")],
        google=(
            {"linkedin.com/in/d": [{"resource_name": "people/c9", "display_name": "Dee New"}]},
            [{"resource_name": "people/c9", "display_name": "Dee New"}],
        ),
    )
    r = _run(Conn())
    assert log["created"] == [] and [o.kind for o in r.plan.linkedin] == ["in_google"]
    assert "In Google, not synced" in script.summary(r, dry_run=False)


def test_summary_lists_possible_duplicates_and_unlinked_handles():
    p = seed.SeedPlan(
        candidates={1: {"email"}},
        already_in={2},
        unlinked=[seed.Unlinked("whatsapp", "Lee Example", "+15550100009")],
        linkedin=[
            seed.LinkedInOutcome(
                "possible_duplicate",
                li("c", first="Jonathan", last="Smith"),
                candidates=[6],
                google_names=["Jon Smith"],
            )
        ],
    )
    text = script.summary(script.SeedRun(p), dry_run=True)
    assert "candidates 1" in text and "already in the pipeline 1" in text
    assert "Possible duplicate" in text and "person 6" in text and "Jon Smith" in text
    assert "Lee Example" in text and "creating-person" in text


def test_main_exits_1_when_a_create_failed(monkeypatch):
    _wire(monkeypatch, linkedin_rows=[li("d", first="Dee", last="New")], create=_raise)
    monkeypatch.setattr(script.otel, "setup_telemetry", lambda name: None)
    with pytest.raises(SystemExit) as e:
        script.main_with([], lambda: Conn())
    assert e.value.code == 1


def _raise(contact):
    raise RuntimeError("quota")
