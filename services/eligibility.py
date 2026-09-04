"""Who deserves a Google Contact (spec §5). Pure functions over env config so
nothing personal lives in code."""

import os
import re

DEFAULT_PATTERN = r"(no.?reply|noreply|do.?not.?reply|mailer.?daemon|notifications?@|alerts?@|support@|newsletter@)"


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def _csv_env(name: str) -> set[str]:
    return {v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()}


def is_own(email: str) -> bool:
    return normalize(email) in _csv_env("OWN_ADDRESSES")


def is_automated(email: str) -> bool:
    addr = normalize(email)
    if not addr or "@" not in addr:
        return True
    if is_own(addr):
        return True
    if addr.rsplit("@", 1)[1] in _csv_env("AUTOMATED_SENDER_DOMAINS"):
        return True
    pattern = os.environ.get("AUTOMATED_SENDER_PATTERN") or DEFAULT_PATTERN
    return re.search(pattern, addr, re.IGNORECASE) is not None


def inbound_eligible(email: str, category: str) -> bool:
    """An inbound email makes its sender eligible unless the sender is
    automated or the email was filed as ignore."""
    return not is_automated(email) and category != "ignore"
