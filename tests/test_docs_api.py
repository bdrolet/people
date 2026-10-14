import httpx
import pytest

from clients import docs_api

BASE = "https://docs.example.test"
AUTH = {"Authorization": f"Bearer tok-for-{BASE}"}


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
    monkeypatch.setenv("DOCS_API_URL", BASE + "/")
    monkeypatch.setattr(docs_api.gcp_auth, "id_token_for", lambda aud: f"tok-for-{aud}")


def test_get_doc(monkeypatch, env):
    seen = {}

    def get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return Resp(200, {"content": "x", "version": ""})

    monkeypatch.setattr(docs_api.httpx, "get", get)
    assert docs_api.get_doc("doc7") == {"content": "x", "version": ""}
    assert seen == {"url": f"{BASE}/docs/gdrive/doc7", "headers": AUTH}


def test_create_doc_posts_title_markdown_and_parent(monkeypatch, env):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return Resp(201, {"web_url": "https://docs.google.com/document/d/new1/edit"})

    monkeypatch.setattr(docs_api.httpx, "post", post)
    ref = docs_api.create_doc("Outreach — Alex Example", "# x\n", "folder-1")
    assert ref["web_url"].endswith("/new1/edit")
    assert seen["url"] == f"{BASE}/docs" and seen["headers"] == AUTH
    assert seen["json"] == {
        "title": "Outreach — Alex Example",
        "markdown": "# x\n",
        "parent": "folder-1",
    }


def test_replace_doc_sends_if_version_and_maps_409(monkeypatch, env):
    calls = []

    def put(url, json, headers, timeout):
        calls.append((url, json))
        return Resp(409 if len(calls) == 2 else 200, {"version": "v2"})

    monkeypatch.setattr(docs_api.httpx, "put", put)
    assert docs_api.replace_doc("doc7", "m", "v1") == {"version": "v2"}
    assert calls[0] == (f"{BASE}/docs/gdrive/doc7", {"markdown": "m", "if_version": "v1"})
    with pytest.raises(docs_api.VersionConflict):
        docs_api.replace_doc("doc7", "m", "")


def test_folder_id_reads_the_catalog(monkeypatch, env):
    body = {
        "folders": [
            {"id": "inbox", "folder_id": "f0"},
            {"id": "consulting-outreach", "folder_id": "f1"},
        ]
    }
    monkeypatch.setattr(docs_api.httpx, "get", lambda url, headers, timeout: Resp(200, body))
    assert docs_api.folder_id("consulting-outreach") == "f1"
    with pytest.raises(docs_api.FolderNotFound, match="nope"):
        docs_api.folder_id("nope")


def test_an_unset_url_is_unavailable(monkeypatch):
    monkeypatch.delenv("DOCS_API_URL", raising=False)
    assert not docs_api.configured()
    with pytest.raises(docs_api.DocsApiUnavailable):
        docs_api.get_doc("doc7")


def test_http_errors_raise(monkeypatch, env):
    monkeypatch.setattr(docs_api.httpx, "get", lambda url, headers, timeout: Resp(413))
    with pytest.raises(httpx.HTTPStatusError):
        docs_api.get_doc("doc7")
