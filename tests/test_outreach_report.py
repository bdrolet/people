from datetime import date

import pytest

from models.outreach import CohortCounts, FunnelCounts
from services import outreach_report as rpt

FLOORS_TOML = """
[floors]
replied = 0.40
intro = 0.08
conversation = 0.048
proposal = 0.016
signed = 0.0053

[readable_after]
replied = 6
intro = 36
conversation = 61
proposal = 186
signed = 564

[lag_weeks]
replied = 1
intro = 3
conversation = 5
proposal = 7
signed = 11
"""
TODAY = date(2026, 10, 20)


@pytest.fixture
def floors(tmp_path) -> rpt.Floors:
    path = tmp_path / "floors.toml"
    path.write_text(FLOORS_TOML)
    return rpt.load_floors(path)


@pytest.fixture
def counts() -> FunnelCounts:
    return FunnelCounts(
        cohorts=[
            CohortCounts(date(2026, 9, 7), briefed=20, replied=9, intro=2, conversation=1),
            CohortCounts(date(2026, 10, 12), briefed=10, replied=2),
        ],
        lost_reasons={"timing": 1, "no_budget": 1},
        follow_ups_due=9,
    )


def test_verdicts_use_only_lag_ripe_cohorts(counts, floors):
    v = {s.stage: s for s in rpt.stage_verdicts(counts, floors, today=TODAY)}
    # replied: both cohorts are a week old; floor 0.40 * 30 = 12; 11 < 12 and n >= 6
    assert (v["replied"].n, v["replied"].actual, v["replied"].floor_at_n) == (
        30,
        11,
        pytest.approx(12.0),
    )
    assert v["replied"].readable and v["replied"].verdict == "below floor"
    # intro: only the September cohort is three weeks old; n=20 < 36, so no verdict
    assert (v["intro"].n, v["intro"].actual) == (20, 2)
    assert not v["intro"].readable and v["intro"].verdict == "--"
    # proposal: no cohort is seven weeks old yet
    assert (v["proposal"].n, v["proposal"].floor_at_n) == (0, 0)


@pytest.mark.parametrize(
    "actual,verdict", [(12, "at floor"), (13, "above floor"), (11, "below floor")]
)
def test_verdict_rounds_the_floor_half_up(floors, actual, verdict):
    c = FunnelCounts([CohortCounts(date(2026, 9, 7), briefed=30, replied=actual)])
    assert rpt.stage_verdicts(c, floors, today=TODAY)[0].verdict == verdict


def test_render_without_floors_prints_counts_rates_lost_and_follow_ups(counts):
    text = rpt.render(counts)
    lines = text.splitlines()
    assert lines[0].split() == [
        "Cohort",
        "Briefed",
        "Replied",
        "Intro",
        "Conv",
        "Prop",
        "Signed",
        "Reply%",
        "Intro%",
        "Conv%",
    ]
    assert lines[1].split() == ["2026-09-07", "20", "9", "2", "1", "0", "0", "45%", "10%", "5.0%"]
    assert lines[3].split() == ["All", "30", "11", "2", "1", "0", "0", "37%", "6.7%", "3.3%"]
    assert "Floor@n" not in text
    assert "Lost reasons: no_budget 1, timing 1" in text and "Follow-ups due: 9" in text


def test_render_with_floors_adds_the_stage_table(counts, floors):
    text = rpt.render(counts, rpt.stage_verdicts(counts, floors, today=TODAY))
    assert "Readable?" in text
    assert "no (n<36)" in text
    replied = next(line for line in text.splitlines() if line.startswith("Replied"))
    assert replied.split() == ["Replied", "11", "12", "yes", "below", "floor"]


def test_render_with_nothing_yet():
    text = rpt.render(FunnelCounts([]))
    assert "All" in text and "Lost reasons: none" in text


@pytest.mark.parametrize(
    "num,den,out", [(0, 0, "--"), (0, 5, "0%"), (12, 30, "40%"), (1, 30, "3.3%")]
)
def test_pct(num, den, out):
    assert rpt.pct(num, den) == out


def test_floors_file_errors_are_specific(tmp_path):
    with pytest.raises(rpt.FloorsError, match="not found"):
        rpt.load_floors(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text(FLOORS_TOML.replace("signed = 564\n", ""))
    with pytest.raises(rpt.FloorsError, match=r"\[readable_after\] lacks signed"):
        rpt.load_floors(bad)
    bad.write_text("[floors]\nreplied = 0.4\n")
    with pytest.raises(rpt.FloorsError):
        rpt.load_floors(bad)
