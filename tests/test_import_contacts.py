import importlib.util
from pathlib import Path

import pytest

import services.google_contacts_sync as gsync
import services.hubspot_mirror as mirror
import services.ingest as ingest
from models.types import IngestResult

spec = importlib.util.spec_from_file_location("import_contacts", Path("scripts/import_contacts.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def wired(monkeypatch):
    log = []
    monkeypatch.setattr(
        ingest,
        "record_inbound",
        lambda conn, **kw: (
            log.append(("in", kw["sender"])),
            IngestResult({"email": kw["sender"], "eligible": True}, True),
        )[1],
    )
    monkeypatch.setattr(
        ingest,
        "record_outbound",
        lambda conn, **kw: [
            IngestResult({"email": r, "eligible": True}, True)
            for r in kw["recipients"]
            if not log.append(("out", r))
        ],
    )
    monkeypatch.setattr(
        gsync, "ensure_contact", lambda c, r: (log.append(("google", r["email"])), r)[1]
    )
    monkeypatch.setattr(
        mirror, "ensure_contact", lambda c, r: (log.append(("hubspot", r["email"])), r)[1]
    )
    return log


def msg(folder, frm, to, ts="2026-09-03T12:00:00Z"):
    return {
        "folder": folder,
        "from": frm,
        "to": to,
        "cc": [],
        "from_name": None,
        "received_at": ts,
        "sent_at": ts,
        "category": "review",
    }


def test_run_routes_inbox_and_sent(wired):
    counts = mod.run(
        None,
        [msg("inbox", "alice@x.com", ["me@x.com"]), msg("sentitems", "me@x.com", ["bob@x.com"])],
        own={"me@x.com"},
        dry_run=False,
    )
    assert ("in", "alice@x.com") in wired and ("out", "bob@x.com") in wired
    assert counts["inbound"] == 1 and counts["outbound"] == 1
    assert ("google", "alice@x.com") in wired and ("google", "bob@x.com") in wired


def test_dry_run_skips_side_effects(wired):
    mod.run(None, [msg("inbox", "alice@x.com", ["me@x.com"])], own={"me@x.com"}, dry_run=True)
    assert not any(k in ("google", "hubspot") for k, _ in wired)
