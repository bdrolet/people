"""`outreach report`: actuals against the funnel floors (outreach spec §9). Pure. The
floors, thresholds and lags are not in this public repo; they come from a TOML file in
the private consulting repo, passed with --floors."""

import math
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from models.outreach import REPORT_STAGES, CohortCounts, FunnelCounts

STAGE_LABELS = {
    "replied": "Replied",
    "intro": "Intro",
    "conversation": "Conversation",
    "proposal": "Proposal",
    "signed": "Signed",
}


class FloorsError(ValueError):
    pass


@dataclass
class Floors:
    rates: dict[str, float]  # rate per briefed contact
    readable_after: dict[str, int]  # briefed count before a zero means anything
    lag_weeks: dict[str, int]  # weeks a cohort needs to reach the stage


@dataclass
class StageVerdict:
    stage: str
    actual: int
    n: int  # briefed contacts in cohorts old enough to have reached the stage
    floor_at_n: float
    readable: bool
    threshold: int
    verdict: str  # "above floor" | "at floor" | "below floor" | "--"


def load_floors(path: Path) -> Floors:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise FloorsError(f"floors file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise FloorsError(f"{path}: {e}") from e
    tables: dict[str, dict] = {}
    for name in ("floors", "readable_after", "lag_weeks"):
        t = data.get(name)
        if not isinstance(t, dict):
            raise FloorsError(f"{path}: missing [{name}]")
        missing = [s for s in REPORT_STAGES if s not in t]
        if missing:
            raise FloorsError(f"{path}: [{name}] lacks {', '.join(missing)}")
        tables[name] = t
    return Floors(
        rates={s: float(tables["floors"][s]) for s in REPORT_STAGES},
        readable_after={s: int(tables["readable_after"][s]) for s in REPORT_STAGES},
        lag_weeks={s: int(tables["lag_weeks"][s]) for s in REPORT_STAGES},
    )


def stage_verdicts(counts: FunnelCounts, floors: Floors, *, today: date) -> list[StageVerdict]:
    """Actual and floor both come from the cohorts old enough to have reached the stage,
    so a cohort briefed yesterday neither drags a rate down nor props it up."""
    out = []
    for stage in REPORT_STAGES:
        lag = timedelta(weeks=floors.lag_weeks[stage])
        ripe = [
            c for c in counts.cohorts if c.cohort_week is not None and c.cohort_week + lag <= today
        ]
        n = sum(c.briefed for c in ripe)
        actual = sum(getattr(c, stage) for c in ripe)
        floor_at_n = floors.rates[stage] * n
        threshold = floors.readable_after[stage]
        readable = n >= threshold
        target = math.floor(floor_at_n + 0.5)
        if not readable:
            verdict = "--"
        elif actual > target:
            verdict = "above floor"
        elif actual == target:
            verdict = "at floor"
        else:
            verdict = "below floor"
        out.append(StageVerdict(stage, actual, n, floor_at_n, readable, threshold, verdict))
    return out


def pct(num: int, den: int) -> str:
    if den == 0:
        return "--"
    value = 100 * num / den
    return f"{value:.0f}%" if value == 0 or value >= 10 else f"{value:.1f}%"


def _floor(x: float) -> str:
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(h) for h in headers]] + [[str(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in cells)


def _total(counts: FunnelCounts) -> CohortCounts:
    t = CohortCounts(cohort_week=None)
    for c in counts.cohorts:
        for stage in ("briefed", *REPORT_STAGES):
            setattr(t, stage, getattr(t, stage) + getattr(c, stage))
    return t


def _cohort_row(label: str, c: CohortCounts) -> list:
    return [
        label,
        c.briefed,
        c.replied,
        c.intro,
        c.conversation,
        c.proposal,
        c.signed,
        pct(c.replied, c.briefed),
        pct(c.intro, c.briefed),
        pct(c.conversation, c.briefed),
    ]


def render(counts: FunnelCounts, verdicts: list[StageVerdict] | None = None) -> str:
    rows = [_cohort_row(str(c.cohort_week or "none"), c) for c in counts.cohorts]
    rows.append(_cohort_row("All", _total(counts)))
    parts = [
        table(
            [
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
            ],
            rows,
        )
    ]
    if verdicts is not None:
        parts.append(
            table(
                ["Stage", "Actual", "Floor@n", "Readable?", "Verdict"],
                [
                    [
                        STAGE_LABELS[v.stage],
                        v.actual,
                        _floor(v.floor_at_n),
                        "yes" if v.readable else f"no (n<{v.threshold})",
                        v.verdict,
                    ]
                    for v in verdicts
                ],
            )
        )
    lost = ", ".join(f"{k} {n}" for k, n in sorted(counts.lost_reasons.items())) or "none"
    parts.append(f"Lost reasons: {lost}\nFollow-ups due: {counts.follow_ups_due}")
    return "\n\n".join(parts)
