from datetime import UTC, date, datetime

import pytest

from clients import docs_api
from services import outreach
from services import outreach_doc_write as writer
from services.outreach_doc import DOC_MESSAGES, Sections

TODAY = date(2026, 10, 15)
TS = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)
PERSON = {"id": 7, "email": "alex@example.com", "display_name": "Alex Example"}
URL = "https://docs.google.com/document/d/doc7/edit"
EXISTING = "# Outreach — Alex Example\n\n## Ben's notes\n\nkeep me\n"
SECTIONS = Sections(
    who="Founder at a clinic-software startup.",
    history="Worked together in 2019.",
    next_step="brief",
    talking_points="- ask how the launch went",
    log=("- 2026-10-14 · prepped brief (iMessage)",),
)


class Conn:
    def __init__(self):
        self.log = []

    def commit(self):
        self.log.append("commit")


class FakeDocs:
    VersionConflict = docs_api.VersionConflict

    def __init__(self, content="", version="", on_get=(), conflicts=0):
        self.content, self.version = content, version
        self.on_get = list(on_get)  # one entry per GET: None, or a callable that edits the doc
        self.conflicts = conflicts
        self.gets, self.created, self.replaced, self.folders = 0, [], [], []

    def folder_id(self, entry_id):
        self.folders.append(entry_id)
        return "folder-1"

    def create_doc(self, title, markdown, parent):
        self.created.append({"title": title, "markdown": markdown, "parent": parent})
        return {"web_url": "https://docs.google.com/document/d/new1/edit"}

    def get_doc(self, doc_id):
        self.gets += 1
        edit = self.on_get.pop(0) if self.on_get else None
        if edit:
            edit(self)
        return {"content": self.content, "version": self.version}

    def replace_doc(self, doc_id, markdown, if_version):
        if self.conflicts:
            self.conflicts -= 1
            raise self.VersionConflict(doc_id)
        self.replaced.append((doc_id, markdown, if_version))
        self.content = markdown
        return {}


@pytest.fixture
def env(monkeypatch):
    state = {"doc_url": None, "recorded": [], "set_doc_fails": False}

    def snapshot(conn, pid, limit):
        assert limit == DOC_MESSAGES
        if pid != 7:
            return None
        return {
            "item": {"person": PERSON, "stage": "to_contact", "doc_url": state["doc_url"]},
            "messages": {
                "imessage": [
                    {"sent_at": TS, "from_me": False, "text": "hi", "has_attachments": False}
                ],
                "whatsapp": [],
                "linkedin": [],
            },
        }

    def set_doc(conn, pid, url):
        if state["set_doc_fails"]:
            raise RuntimeError("db unavailable")
        state["recorded"].append((pid, url))
        return {}

    monkeypatch.setattr(writer.outreach_session, "snapshot", snapshot)
    monkeypatch.setattr(writer.outreach, "set_doc", set_doc)
    return state


def test_first_write_creates_the_doc_in_the_catalog_folder_and_records_it(env):
    docs, conn = FakeDocs(), Conn()
    result = writer.write(conn, 7, SECTIONS, today=TODAY, docs=docs)
    assert result == "created https://docs.google.com/document/d/new1/edit"
    assert docs.folders == ["consulting-outreach"]
    [created] = docs.created
    assert created["title"] == "Outreach — Alex Example" and created["parent"] == "folder-1"
    assert "## Talking points (next step: brief)" in created["markdown"]
    assert "- 2026-10-13 11:30 Alex: hi" in created["markdown"]
    assert env["recorded"] == [(7, "https://docs.google.com/document/d/new1/edit")]
    assert conn.log == ["commit"]


def test_update_merges_with_a_second_get_and_if_version(env):
    env["doc_url"] = URL
    docs = FakeDocs(content=EXISTING, version="v1")
    assert writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs) == f"updated {URL}"
    assert docs.gets == 2 and docs.created == []
    [(doc_id, markdown, if_version)] = docs.replaced
    assert doc_id == "doc7" and if_version == "v1"
    assert (
        "## Ben's notes\n\nkeep me" in markdown and "people id: 7 · stage: to_contact" in markdown
    )


def test_an_edit_between_the_two_gets_is_re_merged(env):
    env["doc_url"] = URL

    def ben_edits(d):
        d.content = d.content.replace("keep me", "keep me, and call Tuesday")

    docs = FakeDocs(content=EXISTING, on_get=[None, ben_edits])  # version stays "" throughout
    writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs)
    assert docs.gets == 4
    [(_, markdown, _)] = docs.replaced
    assert "keep me, and call Tuesday" in markdown


def test_a_409_is_retried_once_after_a_fresh_merge(env):
    env["doc_url"] = URL
    docs = FakeDocs(content=EXISTING, version="v1", conflicts=1)
    assert writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs) == f"updated {URL}"
    assert docs.gets == 4 and len(docs.replaced) == 1


def test_two_conflicts_stop_without_writing(env):
    env["doc_url"] = URL
    docs = FakeDocs(content=EXISTING, version="v1", conflicts=2)
    with pytest.raises(writer.DocConflict, match="nothing written"):
        writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=docs)
    assert docs.replaced == []


def test_someone_outside_the_pipeline_is_refused(env):
    with pytest.raises(outreach.NotInPipeline):
        writer.write(Conn(), 9, SECTIONS, today=TODAY, docs=FakeDocs())


def test_a_created_doc_that_cannot_be_recorded_names_its_url(env):
    env["set_doc_fails"] = True
    with pytest.raises(writer.DocNotRecorded, match="new1") as excinfo:
        writer.write(Conn(), 7, SECTIONS, today=TODAY, docs=FakeDocs())
    assert "outreach doc 7" in str(excinfo.value)
