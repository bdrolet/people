import base64
import json

import pytest

import main
from handlers import email_classified, email_sent
from handlers import sync as sync_handler


class Event:
    def __init__(self, payload):
        self.data = {
            "message": {
                "data": base64.b64encode(json.dumps(payload).encode()).decode(),
                "attributes": {},
            }
        }


@pytest.fixture
def seen(monkeypatch):
    log = []
    monkeypatch.setattr(
        email_classified, "handle", lambda e: log.append(("classified", e["message_id"]))
    )
    monkeypatch.setattr(email_sent, "handle", lambda e: log.append(("sent", e["graph_message_id"])))
    return log


def test_process_routes_classified(seen):
    main.process(Event({"event": "email_classified", "message_id": "m1"}))
    assert seen == [("classified", "m1")]


def test_process_routes_sent(seen):
    main.process(Event({"event": "email_sent", "graph_message_id": "g1"}))
    assert seen == [("sent", "g1")]


def test_process_ignores_unknown(seen):
    main.process(Event({"event": "label_applied"}))
    assert seen == []


class Req:
    def __init__(self, method="POST", auth=None):
        self.method = method
        self.headers = {"Authorization": auth} if auth else {}


def test_sync_requires_bearer(monkeypatch):
    monkeypatch.setenv("PEOPLE_SYNC_TOKEN", "t0k")
    monkeypatch.setattr(sync_handler, "run", lambda: {"google": {}, "hubspot": {}})
    assert main.sync(Req(auth="Bearer nope"))[1] == 401
    body, status, _ = main.sync(Req(auth="Bearer t0k"))
    assert status == 200 and json.loads(body) == {"google": {}, "hubspot": {}}


def test_sync_rejects_get(monkeypatch):
    monkeypatch.setenv("PEOPLE_SYNC_TOKEN", "t0k")
    assert main.sync(Req(method="GET", auth="Bearer t0k"))[1] == 405
