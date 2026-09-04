import pytest

from services import eligibility


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AUTOMATED_SENDER_PATTERN", raising=False)
    monkeypatch.setenv("AUTOMATED_SENDER_DOMAINS", "group.calendar.google.com,bcc.na2.hubspot.com")
    monkeypatch.setenv("OWN_ADDRESSES", "Me@Example.com, alias@example.com")


def test_normalize_lowercases_and_strips():
    assert eligibility.normalize("  Alice@Example.COM ") == "alice@example.com"


@pytest.mark.parametrize(
    "addr",
    [
        "no-reply@x.com",
        "NoReply@x.com",
        "do_not_reply@x.com",
        "mailer-daemon@x.com",
        "notifications@github.com",
        "alerts@x.com",
        "support@x.com",
        "newsletter@x.com",
        "cal@group.calendar.google.com",
        "me@example.com",
    ],
)
def test_automated_addresses(addr):
    assert eligibility.is_automated(addr) is True


@pytest.mark.parametrize("addr", ["alice@example.com", "bob.smith@corp.io"])
def test_human_addresses(addr):
    assert eligibility.is_automated(addr) is False


def test_empty_is_automated():
    assert eligibility.is_automated("") is True


def test_is_own_case_insensitive():
    assert eligibility.is_own("ME@example.com") is True
    assert eligibility.is_own("alias@example.com") is True
    assert eligibility.is_own("alice@example.com") is False


def test_pattern_override(monkeypatch):
    monkeypatch.setenv("AUTOMATED_SENDER_PATTERN", r"^bot@")
    assert eligibility.is_automated("bot@x.com") is True
    assert eligibility.is_automated("no-reply@x.com") is False


def test_inbound_eligible():
    assert eligibility.inbound_eligible("alice@example.com", "review") is True
    assert eligibility.inbound_eligible("alice@example.com", "ignore") is False
    assert eligibility.inbound_eligible("no-reply@x.com", "urgent") is False
