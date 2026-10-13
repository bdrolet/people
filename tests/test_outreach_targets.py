from datetime import date

import pytest

from services.outreach_targets import Targets, TargetsError, load_targets

GOOD = """
[briefs]
per_day  = 5
per_week = 25
start    = 2026-10-13
end      = 2026-11-21
total    = 150

[queue]
low_water_days = 5

[workdays]
days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
"""


def _write(tmp_path, text):
    path = tmp_path / "outreach_targets.toml"
    path.write_text(text)
    return path


def test_loads_the_spec_example(tmp_path):
    assert load_targets(_write(tmp_path, GOOD)) == Targets(
        per_day=5,
        per_week=25,
        start=date(2026, 10, 13),
        end=date(2026, 11, 21),
        total=150,
        low_water_days=5,
        workdays=frozenset({0, 1, 2, 3, 4}),
    )


def test_missing_file(tmp_path):
    with pytest.raises(TargetsError, match="not found"):
        load_targets(tmp_path / "nope.toml")


def test_bad_toml(tmp_path):
    with pytest.raises(TargetsError):
        load_targets(_write(tmp_path, "[briefs\nper_day = 5"))


@pytest.mark.parametrize("table", ["briefs", "queue", "workdays"])
def test_missing_table(tmp_path, table):
    text = GOOD.replace(f"[{table}]", "[other]")
    with pytest.raises(TargetsError, match=rf"\[{table}\]"):
        load_targets(_write(tmp_path, text))


@pytest.mark.parametrize("key", ["per_day", "per_week", "start", "end", "total"])
def test_missing_briefs_key(tmp_path, key):
    text = "\n".join(line for line in GOOD.splitlines() if not line.startswith(key))
    with pytest.raises(TargetsError, match=key):
        load_targets(_write(tmp_path, text))


@pytest.mark.parametrize(
    "line,replacement",
    [
        ("per_day  = 5", "per_day  = 0"),
        ("per_week = 25", "per_week = -1"),
        ("total    = 150", "total    = 1.5"),
        ("low_water_days = 5", "low_water_days = true"),
    ],
)
def test_non_positive_or_non_integer_numbers(tmp_path, line, replacement):
    with pytest.raises(TargetsError, match="positive whole number"):
        load_targets(_write(tmp_path, GOOD.replace(line, replacement)))


def test_start_after_end(tmp_path):
    text = GOOD.replace("start    = 2026-10-13", "start    = 2026-12-01")
    with pytest.raises(TargetsError, match="after"):
        load_targets(_write(tmp_path, text))


def test_a_date_must_be_a_bare_date(tmp_path):
    text = GOOD.replace("start    = 2026-10-13", 'start    = "2026-10-13"')
    with pytest.raises(TargetsError, match="date"):
        load_targets(_write(tmp_path, text))


def test_unknown_weekday(tmp_path):
    text = GOOD.replace('"Fri"]', '"Friday"]')
    with pytest.raises(TargetsError, match="Friday"):
        load_targets(_write(tmp_path, text))


def test_empty_weekday_list(tmp_path):
    text = GOOD.replace('days = ["Mon", "Tue", "Wed", "Thu", "Fri"]', "days = []")
    with pytest.raises(TargetsError, match="days"):
        load_targets(_write(tmp_path, text))
