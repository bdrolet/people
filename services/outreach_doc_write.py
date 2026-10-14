"""Create or merge one person's contact doc (contact-docs spec §3 write protocol). The
session supplies its sections; this module reads the DB snapshot, fetches the doc,
merges (services/outreach_doc.py), checks for a concurrent edit with a second GET (live
Google Docs usually report an empty version, so if_version alone protects nothing),
then replaces it, retrying once. It writes without asking: that's spec D4's exception
to editing-doc's confirm-every-write rule, made safe by the merge. Called only by
`outreach-sessions --write-doc`."""

from datetime import date
from typing import Any

from clients import docs_api
from services import outreach, outreach_doc, outreach_session

ATTEMPTS = 2


class DocConflict(RuntimeError):
    """The doc changed during every attempt; nothing was written (spec §3 step 5)."""


class DocNotRecorded(RuntimeError):
    """The doc was created but its link could not be stored in outreach_contacts."""


def write(
    conn: Any,
    person_id: int,
    sections: outreach_doc.Sections,
    *,
    today: date,
    docs: Any = docs_api,
) -> str:
    """`created <url>` or `updated <url>`. Raises outreach.NotInPipeline, DocConflict,
    DocNotRecorded, and docs-api errors."""
    snap = outreach_session.snapshot(conn, person_id, limit=outreach_doc.DOC_MESSAGES)
    if snap is None:
        raise outreach.NotInPipeline(str(person_id))
    item = snap["item"]
    person = item["person"]
    messages = outreach_doc.render_messages(
        snap["messages"], outreach_doc.them(person), today=today
    )

    def build(existing: str | None) -> str:
        return outreach_doc.merge(
            existing, sections, person=person, stage=item["stage"], messages=messages, today=today
        )

    url = item["doc_url"]
    if not url:
        parent = docs.folder_id(outreach_doc.FOLDER_ENTRY)
        ref = docs.create_doc(outreach_doc.title(person), build(None), parent)
        try:
            outreach.set_doc(conn, person_id, ref["web_url"])
            conn.commit()
        except Exception as e:
            raise DocNotRecorded(
                f"created {ref['web_url']} but could not record it ({e}); record it with "
                f"`outreach doc {person_id} {ref['web_url']}` before writing again"
            ) from e
        return f"created {ref['web_url']}"

    doc_id = outreach.doc_id(url)
    for _ in range(ATTEMPTS):
        before = docs.get_doc(doc_id)
        merged = build(before.get("content") or "")
        check = docs.get_doc(doc_id)
        if (check.get("content"), check.get("version")) != (
            before.get("content"),
            before.get("version"),
        ):
            continue
        try:
            docs.replace_doc(doc_id, merged, before.get("version") or "")
        except docs.VersionConflict:
            continue
        return f"updated {url}"
    raise DocConflict(f"the doc changed while it was being written, twice; nothing written: {url}")
