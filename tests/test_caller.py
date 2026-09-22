import base64
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import caller


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"hdr.{payload}.sig"


def test_caller_email_reads_email_claim():
    assert caller.caller_email(f"Bearer {_jwt({'email': 'a@b.c'})}") == "a@b.c"


def test_caller_email_falls_back_to_sub():
    assert caller.caller_email(f"Bearer {_jwt({'sub': '123'})}") == "123"


def test_caller_email_dash_when_missing_or_garbage():
    assert caller.caller_email(None) == "-"
    assert caller.caller_email("Basic abc") == "-"
    assert caller.caller_email("Bearer not-a-jwt") == "-"
    assert caller.caller_email("Bearer a.!!!.c") == "-"
    assert caller.caller_email(f"Bearer hdr.{base64.urlsafe_b64encode(b'[1]').decode()}.sig") == "-"


def test_install_is_noop_off_cloud_run(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    app = FastAPI()
    caller.install(app)
    assert app.user_middleware == []


def test_install_logs_caller_on_cloud_run(monkeypatch, caplog):
    monkeypatch.setenv("K_SERVICE", "x-api")
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    caller.install(app)
    with caplog.at_level(logging.INFO, logger="api.caller"):
        resp = TestClient(app).get(
            "/ping", headers={"Authorization": f"Bearer {_jwt({'email': 'a@b.c'})}"}
        )
    assert resp.status_code == 200
    assert "GET /ping 200 caller=a@b.c" in caplog.text
