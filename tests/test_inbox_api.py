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
