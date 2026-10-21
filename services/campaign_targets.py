"""`campaign`'s targets (funded-founder campaign spec §7.3). Pure. The numbers are not in
this public repo: they come from a TOML file in the private consulting repo, passed with
--targets or CAMPAIGN_TARGETS."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


class TargetsError(ValueError):
    pass


@dataclass(frozen=True)
class CampaignTargets:
    threshold: int
    slots: int
    slots_through: str
    per_day: int
    follow_up_business_days: int
    lookback_days_first_run: int
    stale_after_days: int


def _table(data: dict, name: str, path: Path) -> dict:
    t = data.get(name)
    if not isinstance(t, dict):
        raise TargetsError(f"{path}: missing [{name}]")
    return t


def _value(t: dict, table: str, key: str, path: Path) -> object:
    if key not in t:
        raise TargetsError(f"{path}: [{table}] lacks {key}")
    return t[key]


def _positive(t: dict, table: str, key: str, path: Path) -> int:
    v = _value(t, table, key, path)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise TargetsError(f"{path}: [{table}] {key} must be a positive whole number, got {v!r}")
    return v


def _text(t: dict, table: str, key: str, path: Path) -> str:
    v = _value(t, table, key, path)
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
    fit = _table(data, "fit", path)
    offer = _table(data, "offer", path)
    sending = _table(data, "sending", path)
    return CampaignTargets(
        threshold=_positive(fit, "fit", "threshold", path),
        slots=_positive(offer, "offer", "slots", path),
        slots_through=_text(offer, "offer", "slots_through", path),
        per_day=_positive(sending, "sending", "per_day", path),
        follow_up_business_days=_positive(sending, "sending", "follow_up_business_days", path),
        lookback_days_first_run=_positive(sending, "sending", "lookback_days_first_run", path),
        stale_after_days=_positive(sending, "sending", "stale_after_days", path),
    )
