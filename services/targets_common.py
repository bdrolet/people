"""TOML helpers and the error type shared by the targets loaders."""

from pathlib import Path


class TargetsError(ValueError):
    pass


def table(data: dict, name: str, path: Path) -> dict:
    t = data.get(name)
    if not isinstance(t, dict):
        raise TargetsError(f"{path}: missing [{name}]")
    return t


def value(t: dict, table: str, key: str, path: Path) -> object:
    if key not in t:
        raise TargetsError(f"{path}: [{table}] lacks {key}")
    return t[key]


def positive(t: dict, table: str, key: str, path: Path) -> int:
    v = value(t, table, key, path)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise TargetsError(f"{path}: [{table}] {key} must be a positive whole number, got {v!r}")
    return v
