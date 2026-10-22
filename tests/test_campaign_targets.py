import pytest

from services.campaign_targets import CampaignTargets, TargetsError, load_targets

GOOD = """
[fit]
threshold = 6

[offer]
slots = 5
slots_through = "this fall"

[sending]
per_day = 20
follow_up_business_days = 5
lookback_days_first_run = 30
stale_after_days = 7
"""


def _write(tmp_path, text):
    path = tmp_path / "campaign_targets.toml"
    path.write_text(text)
    return path


def test_loads_the_spec_values(tmp_path):
    assert load_targets(_write(tmp_path, GOOD)) == CampaignTargets(
        threshold=6,
        slots=5,
        slots_through="this fall",
        per_day=20,
        follow_up_business_days=5,
        lookback_days_first_run=30,
        stale_after_days=7,
    )


def test_missing_file(tmp_path):
    with pytest.raises(TargetsError, match="not found"):
        load_targets(tmp_path / "nope.toml")


def test_bad_toml(tmp_path):
    with pytest.raises(TargetsError, match="campaign_targets.toml"):
        load_targets(_write(tmp_path, "[fit\nthreshold = 6"))


@pytest.mark.parametrize("table", ["fit", "offer", "sending"])
def test_missing_table(tmp_path, table):
    text = GOOD.replace(f"[{table}]", "[other]")
    with pytest.raises(TargetsError, match=rf"\[{table}\]"):
        load_targets(_write(tmp_path, text))


@pytest.mark.parametrize(
    "key",
    [
        "threshold",
        "slots",
        "slots_through",
        "per_day",
        "follow_up_business_days",
        "lookback_days_first_run",
        "stale_after_days",
    ],
)
def test_missing_key(tmp_path, key):
    text = "\n".join(line for line in GOOD.splitlines() if not line.startswith(f"{key} "))
    with pytest.raises(TargetsError, match=key):
        load_targets(_write(tmp_path, text))


@pytest.mark.parametrize(
    "line,replacement",
    [
        ("per_day = 20", "per_day = 0"),
        ("slots = 5", "slots = -1"),
        ("stale_after_days = 7", "stale_after_days = 1.5"),
        ("threshold = 6", "threshold = true"),
    ],
)
def test_non_positive_or_non_integer_numbers(tmp_path, line, replacement):
    with pytest.raises(TargetsError, match="positive whole number"):
        load_targets(_write(tmp_path, GOOD.replace(line, replacement)))


def test_slots_through_must_be_text(tmp_path):
    text = GOOD.replace('slots_through = "this fall"', "slots_through = 3")
    with pytest.raises(TargetsError, match="slots_through"):
        load_targets(_write(tmp_path, text))


def test_blank_slots_through(tmp_path):
    text = GOOD.replace('"this fall"', '"  "')
    with pytest.raises(TargetsError, match="non-empty text"):
        load_targets(_write(tmp_path, text))


def test_targets_error_is_shared_with_outreach():
    from services import campaign_targets, outreach_targets

    assert campaign_targets.TargetsError is outreach_targets.TargetsError
