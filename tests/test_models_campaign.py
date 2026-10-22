import dataclasses
import re
from pathlib import Path

from models import campaign

SCHEMA = (Path(__file__).resolve().parent.parent / "repo" / "schema.sql").read_text()


def _table(name: str) -> str:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {name} \((.*?)\n\);", SCHEMA, re.S)
    assert m, name
    return m.group(1)


def _check_set(table: str, column: str) -> tuple:
    m = re.search(rf"{column}\s+IN \(([^)]*)\)", _table(table))
    assert m, (table, column)
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def test_tuples_match_check_constraints():
    assert campaign.ROUNDS == _check_set("campaign_companies", "round")
    assert campaign.COMPANY_STATUSES == _check_set("campaign_companies", "status")
    assert campaign.CONTACT_STATUSES == _check_set("campaign_contacts", "status")
    assert campaign.VERIFICATIONS == _check_set("campaign_contacts", "verification")
    assert campaign.EMAIL_SOURCES == _check_set("campaign_contacts", "email_source")
    assert set(campaign.COMPANY_STATUSES) | set(campaign.CONTACT_STATUSES) == set(
        _check_set("campaign_events", "status")
    )
    assert campaign.EVENT_SOURCES == _check_set("campaign_events", "source")


def test_opt_out_phrases():
    assert campaign.OPT_OUT_PHRASES == (
        "no",
        "unsubscribe",
        "remove",
        "not interested",
        "no thanks",
        "please remove me",
        "stop",
        "opt out",
        "opt-out",
    )


def test_run_counts_has_one_field_per_contact_status_plus_two():
    names = [f.name for f in dataclasses.fields(campaign.RunCounts)]
    assert set(names) == set(campaign.CONTACT_STATUSES) | {"companies_found", "above_bar"}
    assert all(getattr(campaign.RunCounts(), n) == 0 for n in names)


def test_advance_fields():
    a = campaign.Advance(row={"id": 1}, changed=False, event_id=None)
    assert (a.row, a.changed, a.event_id) == ({"id": 1}, False, None)
