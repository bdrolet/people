import os
import secrets


def is_authorized(header: str | None) -> bool:
    expected = os.environ.get("PEOPLE_SYNC_TOKEN", "")
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header.removeprefix("Bearer "), expected)
