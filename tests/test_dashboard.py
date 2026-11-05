"""dashboards/outreach.py: the committed JSON is what the builder produces, and the
dashboard reads the gauges the design names, newest run first, with nothing personal."""

import json
import re

from dashboards import outreach as dash

NEW_GAUGES = (
    "people_outreach_cohort",
    "people_outreach_activity",
    "people_outreach_lost_reasons",
    "people_outreach_target",
    "people_linkedin_outbox",
    "people_linkedin_daily",
)


def _model() -> dict:
    return json.loads(dash.render())


def _exprs(model: dict) -> list[str]:
    return [t["expr"] for p in model["panels"] for t in p.get("targets", [])]


def test_committed_json_is_current():
    assert dash.OUTPUT.read_text() == dash.render(), "run: python -m dashboards.outreach"


def test_latest_keeps_only_the_newest_run():
    q = dash.latest('people_outreach_cohort{stage="briefed"}', "cohort_week")
    assert q == (
        "max by (cohort_week) (last_over_time(people_outreach_cohort"
        '{stage="briefed"}[26h]) and on(job, instance) '
        "topk(1, last_over_time(people_outreach_refreshed[26h])))"
    )


def test_identity_and_folderless_model():
    m = _model()
    assert m["uid"] == "people-outreach" and m["title"] == "Referral outreach"


def test_rows_in_order():
    rows = [p["title"] for p in _model()["panels"] if p["type"] == "row"]
    assert rows == [
        "Is outreach producing results?",
        "Am I putting in the effort?",
        "Is the LinkedIn sender healthy?",
    ]


def test_every_new_gauge_is_queried():
    text = "\n".join(_exprs(_model()))
    for metric in NEW_GAUGES:
        assert metric in text, metric


def test_outreach_gauges_read_the_newest_run():
    """Every query on a record_population gauge goes through latest(), so two runs inside
    the window never add up or show a stale 'today'."""
    for expr in _exprs(_model()):
        if re.search(
            r"people_outreach_(cohort|activity|lost_reasons|stage_population|follow_ups_due|qualified_leads)",
            expr,
        ):
            assert "topk(1, last_over_time(people_outreach_refreshed[26h]))" in expr, expr


def test_no_bare_instant_gauge_reads():
    for expr in _exprs(_model()):
        if "_total" in expr or "_bucket" in expr:
            continue
        assert "last_over_time" in expr, expr


def test_no_personal_data():
    text = dash.render()
    assert "@" not in text and "drolet" not in text.lower()


def test_targets_use_the_datasource_variable():
    for p in _model()["panels"]:
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == "${datasource}", p["title"]
