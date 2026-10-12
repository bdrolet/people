"""tasks-api (Cloud Run, IAM-authenticated): one completed Asana task per countable
outreach stage event (outreach spec §8). Every call raises on failure;
services/outreach_sync.py owns the retry policy. The URL is read per call so tests and
the CLI can set it late; it is also the ID-token audience (tasks-api's custom audience)."""

import os

import httpx

from clients import gcp_auth

TIMEOUT_S = 10


class TasksApiUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("TASKS_API_URL", "").strip())


def _base() -> str:
    url = os.environ.get("TASKS_API_URL", "").strip().rstrip("/")
    if not url:
        raise TasksApiUnavailable("TASKS_API_URL unset")
    return url


def _headers(base: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {gcp_auth.id_token_for(base)}"}


def create_task(name: str, tags: list[str], context: str) -> str:
    """POST /tasks into tasks-api's default project; no priority, no assignee."""
    base = _base()
    resp = httpx.post(
        f"{base}/tasks",
        json={"name": name, "tags": tags, "context": context},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return str(resp.json()["task_gid"])


def complete_task(gid: str) -> None:
    """A second call because POST /tasks has no `completed` field (spec §8)."""
    base = _base()
    resp = httpx.patch(
        f"{base}/tasks/{gid}",
        json={"completed": True},
        headers=_headers(base),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
