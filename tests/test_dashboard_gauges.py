from datetime import date

from models.outreach import LOST_REASONS, CohortCounts, FunnelCounts
from services import dashboard_gauges as g
from services import outreach_report
from services.outreach_targets import Targets

COLUMNS = ("briefed", "replied", "intro", "conversation", "qualified", "proposal", "signed")


def _funnel() -> FunnelCounts:
    return FunnelCounts(
        [
            CohortCounts(date(2026, 10, 12), briefed=9, replied=4, intro=2, conversation=1),
            CohortCounts(date(2026, 10, 19), briefed=5, replied=1, intro=1, qualified=1),
            CohortCounts(None, intro=3, conversation=2, qualified=1, proposal=1, signed=1),
        ],
        {"price": 1},
        2,
    )


def _printed(counts: FunnelCounts) -> dict[str, dict[str, int]]:
    """Cohort label → stage → number, parsed from the report's cohort table."""
    table = outreach_report.render(counts).split("\n\n")[0].splitlines()[1:]
    out = {}
    for line in table:
        cells = line.split()
        out[cells[0]] = dict(zip(COLUMNS, (int(v) for v in cells[1:8]), strict=True))
    return out


def test_cohort_rows_match_report_render():
    counts = _funnel()
    printed = _printed(counts)
    rows = g.cohort_rows(counts)
    published = {(a["cohort_week"], a["stage"]): v for v, a in rows}
    labels = {"2026-10-12": "2026-W42", "2026-10-19": "2026-W43", "none": "none"}
    for printed_label, week in labels.items():
        for stage in COLUMNS:
            assert published[(week, stage)] == printed[printed_label][stage], (week, stage)
    for stage in COLUMNS:
        total = sum(v for v, a in rows if a["stage"] == stage)
        assert total == printed["All"][stage], stage


def test_cohort_rows_zero_filled():
    rows = g.cohort_rows(FunnelCounts([CohortCounts(date(2026, 10, 12), briefed=3)]))
    assert len(rows) == 7
    assert [v for v, a in rows if v == 0] == [0] * 6
    assert (3, {"cohort_week": "2026-W42", "stage": "briefed"}) in rows


def test_activity_rows():
    rows = g.activity_rows(briefs_today=2, briefs_week=7, nudges_today=1, nudges_week=4)
    assert sorted(rows, key=lambda r: sorted(r[1].items())) == [
        (2, {"kind": "first_briefs", "window": "today"}),
        (7, {"kind": "first_briefs", "window": "week"}),
        (1, {"kind": "nudges", "window": "today"}),
        (4, {"kind": "nudges", "window": "week"}),
    ]


def test_lost_reason_rows_fill_known_reasons():
    rows = dict((a["lost_reason"], v) for v, a in g.lost_reason_rows({"price": 2, "weird": 1}))
    assert rows["price"] == 2 and rows["no_fit"] == 0 and rows["weird"] == 1
    assert set(LOST_REASONS) <= set(rows)


def test_target_rows():
    t = Targets(
        per_day=5,
        per_week=25,
        start=date(2026, 10, 13),
        end=date(2026, 11, 21),
        total=150,
        low_water_days=4,
        workdays=frozenset(range(5)),
    )
    assert sorted(g.target_rows(t), key=lambda r: r[1]["target"]) == [
        (5, {"target": "briefs_per_day"}),
        (25, {"target": "briefs_per_week"}),
        (150, {"target": "briefs_total"}),
        (4, {"target": "queue_low_water_days"}),
    ]
