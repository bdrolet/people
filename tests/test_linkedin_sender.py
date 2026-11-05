import contextlib
import logging
from datetime import UTC, datetime

from scripts import linkedin_sender

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_main_runs_once_and_flushes(monkeypatch, caplog):
    seen = {}
    flushed = []
    monkeypatch.setattr(linkedin_sender.otel, "setup_telemetry", lambda name: seen.update(svc=name))
    monkeypatch.setattr(linkedin_sender.otel, "flush", lambda: flushed.append(True))

    def run_once(conn, factory, *, now, cfg, rng):
        seen.update(now=now, cfg=cfg, factory=factory)
        return "sent"

    monkeypatch.setattr(linkedin_sender.linkedin_outbox, "run_once", run_once)
    caplog.set_level(logging.INFO)
    code = linkedin_sender.main_with(
        lambda: Conn(),
        now=NOW,
        sender_factory=lambda: contextlib.nullcontext(None),
        env={"LINKEDIN_DAILY_CAP": "40", "LINKEDIN_RAMP_START": "20"},
    )
    assert code == 0 and flushed == [True]
    assert seen["svc"] == "people-linkedin-sender" and seen["now"] == NOW
    assert seen["cfg"].daily_cap == 40
    assert "sent" in caplog.text


def test_main_bad_config_exits_2_and_flushes(monkeypatch):
    flushed = []
    monkeypatch.setattr(linkedin_sender.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(linkedin_sender.otel, "flush", lambda: flushed.append(True))
    code = linkedin_sender.main_with(lambda: Conn(), now=NOW, env={"LINKEDIN_DAILY_CAP": "x"})
    assert code == 2 and flushed == [True]


def test_default_factory_pins_configured_version(monkeypatch):
    opened = []
    monkeypatch.setattr(linkedin_sender.otel, "setup_telemetry", lambda name: None)
    monkeypatch.setattr(linkedin_sender.otel, "flush", lambda: None)
    monkeypatch.setattr(linkedin_sender.linkedin_mcp, "open_sender", lambda v: opened.append(v))

    def run_once(conn, factory, *, now, cfg, rng):
        factory()
        return "sent"

    monkeypatch.setattr(linkedin_sender.linkedin_outbox, "run_once", run_once)
    linkedin_sender.main_with(lambda: Conn(), now=NOW, env={"LINKEDIN_MCP_VERSION": "4.30.1"})
    assert opened == ["4.30.1"]
