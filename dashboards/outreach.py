"""The `Referral outreach` dashboard, as code (outreach dashboard design). Builds the
dashboard with the Grafana Foundation SDK and writes dashboards/build/outreach.json,
which terraform/grafana.tf applies. Run `python -m dashboards.outreach` after any change;
CI fails when the committed JSON is stale.

Every number here must match `outreach today` / `outreach report`, so the outreach panels
read the DB-derived gauges, never the event counters. The gauges only exist while a
process reports them, so every read takes the last value over a window, and the outreach
ones keep only the newest run's series (see `latest`). Nothing personal belongs here:
the targets come from the `people_outreach_target` gauge, never from this file."""

import json
from pathlib import Path

from grafana_foundation_sdk.builders import (
    barchart,
    bargauge,
    dashboard,
    prometheus,
    stat,
    timeseries,
)
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import common
from grafana_foundation_sdk.models.dashboard import (
    DataSourceRef,
    DataTransformerConfig,
    VariableRefresh,
    VariableSort,
)
from grafana_foundation_sdk.models.prometheus import PromQueryFormat

OUTPUT = Path(__file__).resolve().parent / "build" / "outreach.json"
DS = DataSourceRef(type_val="prometheus", uid="${datasource}")
COHORT = 'cohort_week=~"$cohort"'
WINDOW = "26h"  # people-sync runs nightly; a day plus slack
TARGET_WINDOW = "8d"  # only `outreach today` publishes targets
FRESHEST = f"on(job, instance) topk(1, last_over_time(people_outreach_refreshed[{WINDOW}]))"


def latest(selector: str, by: str = "") -> str:
    """The newest run's value of an outreach gauge. Every people-sync run reports under a
    new `instance` and the CLI under its own `job`, so a window holds several runs'
    series; keep only those of the run with the newest `people_outreach_refreshed`."""
    agg = f"max by ({by})" if by else "max"
    return f"{agg} (last_over_time({selector}[{WINDOW}]) and {FRESHEST})"


def last(selector: str, window: str = WINDOW) -> str:
    """The last value of a single-publisher gauge (the LinkedIn sender)."""
    return f"max(last_over_time({selector}[{window}]))"


def q(ref: str, expr: str, legend: str = "", *, instant: bool = True, table: bool = False):
    query = prometheus.Dataquery().ref_id(ref).expr(expr).datasource(DS)
    if legend:
        query = query.legend_format(legend)
    if instant:
        query = query.instant()
    else:
        query = query.range()
    if table:
        query = query.format(PromQueryFormat.TABLE)
    return query


def t(id_val: str, options: dict) -> DataTransformerConfig:
    return DataTransformerConfig(id_val=id_val, options=options)


def _cohort(stage: str) -> str:
    return f"sum({latest(f'people_outreach_cohort{{stage="{stage}",{COHORT}}}', 'cohort_week')})"


def _stat(title: str, w: int = 4, h: int = 5) -> stat.Panel:
    return stat.Panel().title(title).datasource(DS).span(w).height(h)


def _per_week_bars() -> barchart.Panel:
    sel = f'people_outreach_cohort{{stage=~"intro|qualified",cohort_week!="none",{COHORT}}}'
    return (
        barchart.Panel()
        .title("Qualified leads and introductions per brief week")
        .description("Last 12 brief cohorts. Qualified = current verdict yes.")
        .datasource(DS)
        .span(12)
        .height(8)
        .with_target(q("A", latest(sel, "cohort_week, stage"), table=True))
        .transformations(
            [
                t("sortBy", {"sort": [{"field": "cohort_week", "desc": True}]}),
                t(
                    "groupingToMatrix",
                    {"rowField": "cohort_week", "columnField": "stage", "valueField": "Value"},
                ),
                t("limit", {"limitField": 12}),
                t("sortBy", {"sort": [{"field": "cohort_week\\stage", "desc": False}]}),
            ]
        )
    )


def _chain_counts() -> bargauge.Panel:
    p = (
        bargauge.Panel()
        .title("Conversion chain")
        .description(
            "People in the selected cohorts who ever reached each stage. "
            "With all cohorts selected this is `outreach report`'s All row."
        )
        .datasource(DS)
        .span(6)
        .height(8)
        .orientation(common.VizOrientation.HORIZONTAL)
    )
    for ref, stage, label in (
        ("A", "briefed", "Briefed"),
        ("B", "replied", "Replied"),
        ("C", "intro", "Intro"),
        ("D", "conversation", "Conversation"),
        ("E", "qualified", "Qualified"),
    ):
        p = p.with_target(q(ref, _cohort(stage), label))
    return p


def _chain_rates() -> stat.Panel:
    p = (
        _stat("Conversion rates (per briefed)", w=6, h=8)
        .description("`outreach report`'s Reply%, Intro%, Conv% and Qual%.")
        .unit("percentunit")
        .decimals(1)
    )
    for ref, stage, label in (
        ("A", "replied", "Reply"),
        ("B", "intro", "Intro"),
        ("C", "conversation", "Conversation"),
        ("D", "qualified", "Qualified"),
    ):
        p = p.with_target(q(ref, f"{_cohort(stage)} / {_cohort('briefed')}", label))
    return p


def _results(d: dashboard.Dashboard) -> dashboard.Dashboard:
    verdicts = _stat("Qualified verdicts", w=12, h=5).with_target(
        q(
            "A",
            f"sum by (verdict) ({latest('people_outreach_qualified_leads', 'verdict, cohort_week')})",
            "{{verdict}}",
        )
    )
    lost = (
        bargauge.Panel()
        .title("Lost reasons")
        .datasource(DS)
        .span(12)
        .height(5)
        .orientation(common.VizOrientation.HORIZONTAL)
        .with_target(
            q("A", latest("people_outreach_lost_reasons", "lost_reason"), "{{lost_reason}}")
        )
    )
    return (
        d.with_row(dashboard.Row("Is outreach producing results?"))
        .with_panel(_per_week_bars())
        .with_panel(_chain_counts())
        .with_panel(_chain_rates())
        .with_panel(verdicts)
        .with_panel(lost)
    )


def _briefs(title: str, window: str, target: str) -> stat.Panel:
    return (
        _stat(title)
        .description("First briefs, as `outreach today` counts them, against the target.")
        .with_target(
            q(
                "A",
                latest(f'people_outreach_activity{{kind="first_briefs",window="{window}"}}'),
                "Done",
            )
        )
        .with_target(
            q("B", last(f'people_outreach_target{{target="{target}"}}', TARGET_WINDOW), "Target")
        )
    )


def _linkedin_per_day(title: str, what: str) -> barchart.Panel:
    expr = f'sum by (day, kind) (last_over_time(people_linkedin_daily{{what="{what}"}}[{WINDOW}]))'
    return (
        barchart.Panel()
        .title(title)
        .description("Pacific days, from the outbox table, split by kind.")
        .datasource(DS)
        .span(12)
        .height(8)
        .stacking(common.StackingMode.NORMAL)
        .with_target(q("A", expr, table=True))
        .transformations(
            [
                t(
                    "groupingToMatrix",
                    {"rowField": "day", "columnField": "kind", "valueField": "Value"},
                ),
                t("sortBy", {"sort": [{"field": "day\\kind", "desc": False}]}),
            ]
        )
    )


def _effort(d: dashboard.Dashboard) -> dashboard.Dashboard:
    nudges = _stat("Nudges this week").with_target(
        q("A", latest('people_outreach_activity{kind="nudges",window="week"}'), "Nudges")
    )
    due = _stat("Follow-ups due").with_target(
        q("A", latest("people_outreach_follow_ups_due"), "Due")
    )
    pool = (
        _stat("Warm pool left")
        .description("Referrers at to_contact: who is left to brief.")
        .with_target(
            q(
                "A",
                "sum("
                + latest(
                    'people_outreach_stage_population{pipeline="referrer",stage="to_contact"}',
                    "cohort_week",
                )
                + ")",
                "To contact",
            )
        )
    )
    return (
        d.with_row(dashboard.Row("Am I putting in the effort?"))
        .with_panel(_briefs("First briefs today", "today", "briefs_per_day"))
        .with_panel(_briefs("First briefs this week", "week", "briefs_per_week"))
        .with_panel(nudges)
        .with_panel(due)
        .with_panel(pool.span(8))
        .with_panel(_linkedin_per_day("LinkedIn approved per day", "approved"))
        .with_panel(_linkedin_per_day("LinkedIn sent per day", "sent"))
    )


def _sender(d: dashboard.Dashboard) -> dashboard.Dashboard:
    depth = _stat("Queue depth").with_target(q("A", last("people_linkedin_queue_depth"), "Queued"))
    oldest = (
        _stat("Oldest queued")
        .unit("s")
        .with_target(q("A", last("people_linkedin_queue_oldest_age_seconds"), "Age"))
        .with_target(
            q("B", last("people_linkedin_queue_oldest_window_seconds"), "Waited in window")
        )
    )
    today = (
        _stat("Sends today vs ceiling")
        .with_target(q("A", last("people_linkedin_sends_today"), "Sent"))
        .with_target(q("B", last("people_linkedin_cap_today"), "Ceiling"))
    )
    week = (
        _stat("Sends this week vs ceiling")
        .with_target(q("A", last("people_linkedin_sends_week"), "Sent"))
        .with_target(q("B", last("people_linkedin_cap_week"), "Ceiling"))
    )
    heartbeat = (
        _stat("Since last heartbeat")
        .description("The sender runs every 2 minutes while the Mac is awake.")
        .unit("s")
        .with_target(q("A", f"time() - {last('people_linkedin_sender_heartbeat', '7d')}", "Age"))
    )
    paused = (
        _stat("Paused")
        .description("1 while the queue is paused; the label is the reason.")
        .no_value("sender not reporting")
        .with_target(
            q(
                "A",
                "max by (reason) (last_over_time(people_linkedin_sender_paused[15m]))",
                "{{reason}}",
            )
        )
    )
    outbox = (
        bargauge.Panel()
        .title("Outbox by status")
        .datasource(DS)
        .span(8)
        .height(8)
        .orientation(common.VizOrientation.HORIZONTAL)
        .with_target(
            q(
                "A",
                f"max by (status) (last_over_time(people_linkedin_outbox[{WINDOW}]))",
                "{{status}}",
            )
        )
    )
    outcomes = (
        timeseries.Panel()
        .title("Send outcomes per day (rough)")
        .description(
            "From the people_linkedin_sends counter. Short-lived sender runs make it "
            "undercount; the outbox panels are the source of truth."
        )
        .datasource(DS)
        .span(8)
        .height(8)
        .with_target(
            q(
                "A",
                "sum by (outcome) (increase(people_linkedin_sends_total[1d]))",
                "{{outcome}}",
                instant=False,
            )
        )
    )
    latency = stat.Panel().title("Approval to send").datasource(DS).span(8).height(8).unit("s")
    for ref, quantile in (("A", "0.5"), ("B", "0.9")):
        latency = latency.with_target(
            q(
                ref,
                f"histogram_quantile({quantile}, sum by (le) "
                "(increase(people_linkedin_approval_to_send_seconds_bucket[7d])))",
                f"p{int(float(quantile) * 100)}",
            )
        )
    return (
        d.with_row(dashboard.Row("Is the LinkedIn sender healthy?"))
        .with_panel(depth)
        .with_panel(oldest)
        .with_panel(today)
        .with_panel(week)
        .with_panel(heartbeat)
        .with_panel(paused)
        .with_panel(outbox)
        .with_panel(outcomes)
        .with_panel(latency)
    )


def build() -> dashboard.Dashboard:
    d = (
        dashboard.Dashboard("Referral outreach")
        .uid("people-outreach")
        .tags(["people", "outreach"])
        .description(
            "Is referral outreach working? Results on top, effort in the middle, the "
            "LinkedIn sender at the bottom. Built by dashboards/outreach.py in the people repo."
        )
        .time("now-30d", "now")
        .timezone("America/New_York")
        .refresh("5m")
        .editable()
        .with_variable(
            dashboard.DatasourceVariable("datasource").label("Data source").type("prometheus")
        )
        .with_variable(
            dashboard.QueryVariable("cohort")
            .label("Brief cohort")
            .datasource(DS)
            .query("label_values(people_outreach_cohort, cohort_week)")
            .refresh(VariableRefresh.ON_TIME_RANGE_CHANGED)
            .sort(VariableSort.ALPHABETICAL_ASC)
            .multi(True)
            .include_all(True)
            .all_value(".*")
        )
    )
    return _sender(_effort(_results(d)))


def render() -> str:
    return json.dumps(build().build(), cls=JSONEncoder, indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
