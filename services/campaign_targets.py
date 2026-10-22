"""`campaign`'s targets (funded-founder campaign spec §7.3). Pure. The numbers are not in
this public repo: they come from a TOML file in the private consulting repo, passed with
--targets or CAMPAIGN_TARGETS."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from services.targets_common import TargetsError, positive, table, value


@dataclass(frozen=True)
class CampaignTargets:
    threshold: int
    slots: int
    slots_through: str
    per_day: int
    follow_up_business_days: int
    lookback_days_first_run: int
    stale_after_days: int


def _text(t: dict, table: str, key: str, path: Path) -> str:
    v = value(t, table, key, path)
    if not isinstance(v, str) or not v.strip():
        raise TargetsError(f"{path}: [{table}] {key} must be non-empty text, got {v!r}")
    return v


def load_targets(path: Path) -> CampaignTargets:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise TargetsError(f"targets file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise TargetsError(f"{path}: {e}") from e
    fit = table(data, "fit", path)
    offer = table(data, "offer", path)
    sending = table(data, "sending", path)
    return CampaignTargets(
        threshold=positive(fit, "fit", "threshold", path),
        slots=positive(offer, "offer", "slots", path),
        slots_through=_text(offer, "offer", "slots_through", path),
        per_day=positive(sending, "sending", "per_day", path),
        follow_up_business_days=positive(sending, "sending", "follow_up_business_days", path),
        lookback_days_first_run=positive(sending, "sending", "lookback_days_first_run", path),
        stale_after_days=positive(sending, "sending", "stale_after_days", path),
    )
