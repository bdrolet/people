from clients import otel


def test_set_rows_observed():
    otel.set_rows("outreach_cohort", [(3, {"cohort_week": "2026-W42", "stage": "briefed"})])
    obs = otel._observe_rows("outreach_cohort")(None)  # type: ignore[arg-type]
    assert [(o.value, dict(o.attributes)) for o in obs] == [
        (3, {"cohort_week": "2026-W42", "stage": "briefed"})
    ]


def test_unset_key_observes_nothing():
    assert otel._observe_rows("never_set_key")(None) == []  # type: ignore[arg-type]


def test_every_dashboard_gauge_key_has_a_people_name():
    for key, (name, _) in otel.DASHBOARD_GAUGES.items():
        assert name == f"people.{key}"
