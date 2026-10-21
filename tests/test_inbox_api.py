import pytest

from clients import inbox_api


def _capture_post(monkeypatch):
    monkeypatch.setenv("INBOX_API_URL", "https://inbox.example.test")
    monkeypatch.setattr(inbox_api.gcp_auth, "id_token_for", lambda aud: "tok")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "d1"}

    def post(url, json, headers, timeout):
        seen.update(json=json)
        return Resp()

    monkeypatch.setattr(inbox_api.httpx, "post", post)
    return seen


def test_create_draft_sends_the_from_address_as_an_alias(monkeypatch):
    seen = _capture_post(monkeypatch)
    inbox_api.create_draft(["alice@example.com"], subject="", from_address="ben@example.com")
    assert seen["json"]["from"] == {"address": "ben@example.com", "shared": False}


def test_create_draft_without_from_address_sends_no_from_block(monkeypatch):
    monkeypatch.setenv("INBOX_API_URL", "https://inbox.example.test")
    monkeypatch.setattr(inbox_api.gcp_auth, "id_token_for", lambda aud: "tok")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "d1", "web_link": "https://outlook.example.test/d1"}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return Resp()

    monkeypatch.setattr(inbox_api.httpx, "post", post)
    out = inbox_api.create_draft(["alice@example.com"], subject="")
    assert out["id"] == "d1"
    assert seen["url"] == "https://inbox.example.test/emails/drafts"
    assert seen["json"] == {"to": ["alice@example.com"], "subject": "", "body": ""}
    assert "from" not in seen["json"]


def _capture(monkeypatch, response):
    monkeypatch.setenv("INBOX_API_URL", "https://inbox.example.test/")
    monkeypatch.setattr(inbox_api.gcp_auth, "id_token_for", lambda aud: "tok")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return response

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return Resp()

    monkeypatch.setattr(inbox_api.httpx, "post", post)
    return seen


def test_send_draft_posts_the_from_block_and_returns_the_id(monkeypatch):
    seen = _capture(monkeypatch, {"status": "sent", "id": "m1"})
    out = inbox_api.send_draft("d1", from_address="ben@example.com")
    assert out == {"status": "sent", "id": "m1"}
    assert seen["url"] == "https://inbox.example.test/emails/drafts/d1/send"
    assert seen["json"] == {"from": {"address": "ben@example.com", "shared": False}}
    assert seen["headers"] == {"Authorization": "Bearer tok"}


def test_send_draft_tolerates_an_older_response_without_id(monkeypatch):
    seen = _capture(monkeypatch, {"status": "sent"})
    out = inbox_api.send_draft("d1", from_address=None)
    assert out.get("id") is None
    assert seen["json"] == {}


def test_reply_sends_to_the_named_recipients_only(monkeypatch):
    seen = _capture(monkeypatch, {"status": "sent"})
    inbox_api.reply("m1", body="nudge", to=["a@x.io"], from_address="ben@example.com")
    assert seen["url"] == "https://inbox.example.test/emails/m1/reply"
    assert seen["json"] == {
        "body": "nudge",
        "to": ["a@x.io"],
        "reply_all": False,
        "send": True,
        "from": {"address": "ben@example.com", "shared": False},
    }


def test_search_sent_queries_graph_without_inner_quotes(monkeypatch):
    hit = {
        "message_id": "m1",
        "subject": "S",
        "sender": "ben@example.com",
        "received_at": "t",
        "is_draft": False,
    }
    seen = _capture(monkeypatch, {"results": [hit]})
    assert inbox_api.search_sent(to="a@x.io", subject='Audit for "Acme"\nInc') == [hit]
    assert seen["url"] == "https://inbox.example.test/search"
    # inbox-api quotes the whole query, so an inner quoted phrase is a Graph 400.
    assert seen["json"] == {
        "query": "to:a@x.io subject:Audit for Acme Inc",
        "mode": "graph",
        "mailboxes": ["me"],
        "limit": 10,
    }


def test_search_sent_reports_is_draft_as_none_when_inbox_api_omits_it(monkeypatch):
    _capture(monkeypatch, {"results": [{"message_id": "m1", "subject": "S"}]})
    assert inbox_api.search_sent(to="a@x.io", subject="S")[0]["is_draft"] is None


def test_new_calls_need_the_url(monkeypatch):
    monkeypatch.delenv("INBOX_API_URL", raising=False)
    with pytest.raises(inbox_api.InboxApiUnavailable):
        inbox_api.send_draft("d1", from_address=None)
