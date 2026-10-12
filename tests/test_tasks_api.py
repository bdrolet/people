import httpx
import pytest

from clients import tasks_api


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TASKS_API_URL", "https://tasks.example.test/")
    monkeypatch.setattr(tasks_api.gcp_auth, "id_token_for", lambda aud: f"tok-for-{aud}")


def test_create_task_posts_name_tags_context_and_nothing_else(monkeypatch, env):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return Resp(201, {"task_gid": "123", "permalink_url": "x"})

    monkeypatch.setattr(tasks_api.httpx, "post", post)
    gid = tasks_api.create_task("Brief: A", ["outreach", "serves:consulting"], "ctx")
    assert gid == "123"
    assert seen["url"] == "https://tasks.example.test/tasks"
    assert seen["json"] == {
        "name": "Brief: A",
        "tags": ["outreach", "serves:consulting"],
        "context": "ctx",
    }
    # The audience is the base URL without the trailing slash: tasks-api's custom audience.
    assert seen["headers"] == {"Authorization": "Bearer tok-for-https://tasks.example.test"}


def test_complete_task_patches_completed_true(monkeypatch, env):
    seen = {}
    monkeypatch.setattr(
        tasks_api.httpx,
        "patch",
        lambda url, json, headers, timeout: seen.update(url=url, json=json) or Resp(),
    )
    tasks_api.complete_task("123")
    assert seen == {"url": "https://tasks.example.test/tasks/123", "json": {"completed": True}}


def test_errors_raise(monkeypatch, env):
    monkeypatch.setattr(tasks_api.httpx, "post", lambda *a, **k: Resp(502))
    with pytest.raises(httpx.HTTPStatusError):
        tasks_api.create_task("n", [], "c")


def test_unconfigured(monkeypatch):
    monkeypatch.delenv("TASKS_API_URL", raising=False)
    assert tasks_api.configured() is False
    with pytest.raises(tasks_api.TasksApiUnavailable):
        tasks_api.create_task("n", [], "c")
