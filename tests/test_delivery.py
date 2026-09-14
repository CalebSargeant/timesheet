"""Getting the week to whoever signs it off: email construction and channel
choice, with no real SMTP and no real connector."""
from typing import ClassVar

import pytest

from timesheet import i18n
from timesheet.collectors import mcp_client
from timesheet.collectors.mcp_client import McpSession
from timesheet.model import Block, Day
from timesheet.service import delivery
from timesheet.service import email as mailer
from timesheet.service.users import User, defaults

META = {"week_start": "2026-07-20", "total_minutes": 2400}


def _user(**settings) -> User:
    s = defaults()
    s.update(settings)
    return User(id="github.com:1", login="alice", name="Alice Smith",
                email="alice@example.invalid", settings=s)


def _days():
    from datetime import UTC, datetime
    s = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    return [Day(date=s, blocks=[Block(s, s.replace(hour=17), "Development", "work", "focus")])]


class FakeSMTP:
    sent: ClassVar[dict] = {}

    def __init__(self, host, port, timeout=0):
        FakeSMTP.sent = {"host": host, "port": port}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.sent["starttls"] = True

    def login(self, u, p):
        FakeSMTP.sent["login"] = (u, p)

    def send_message(self, msg):
        FakeSMTP.sent.update({
            "subject": msg["Subject"], "to": msg["To"], "from": msg["From"],
            "reply_to": msg["Reply-To"],
            "body": msg.get_body(("plain",)).get_content(),
            "has_xlsx": any(part.get_filename("").endswith(".xlsx")
                            for part in msg.iter_attachments()),
        })


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    return mailer.Mailer(host="smtp.example.invalid", user="svc", password="pw",
                         sender="timesheet@example.invalid")


def test_email_skips_without_smtp():
    assert mailer.send_weekly(b"PK\x03\x04", META, to="boss@example.invalid",
                              strings=i18n.EN, mailer=mailer.Mailer()) is False


def test_email_skips_without_a_recipient(smtp):
    assert mailer.send_weekly(b"PK", META, to="", strings=i18n.EN, mailer=smtp) is False


def test_email_builds_and_sends(smtp):
    ok = mailer.send_weekly(b"PK\x03\x04data", META, to="boss@example.invalid",
                            strings=i18n.EN, mailer=smtp, manager_name="Dana Boss",
                            sender_name="Alice Smith", reply_to="alice@example.invalid",
                            public_url="https://timesheet.example.invalid")
    assert ok is True
    assert FakeSMTP.sent["subject"] == "Timesheet week 2026-07-20 (40:00)"
    assert FakeSMTP.sent["to"] == "boss@example.invalid"
    assert FakeSMTP.sent["has_xlsx"] and FakeSMTP.sent["starttls"]
    assert FakeSMTP.sent["login"] == ("svc", "pw")
    assert "Hi Dana" in FakeSMTP.sent["body"]


def test_the_reply_goes_to_the_person_whose_hours_these_are(smtp):
    """Sent from the deployment's address so nobody has to hand it SMTP
    credentials; replied to the account holder so an answer reaches them."""
    mailer.send_weekly(b"PK", META, to="boss@example.invalid", strings=i18n.EN, mailer=smtp,
                       sender_name="Alice Smith", reply_to="alice@example.invalid")
    assert "timesheet@example.invalid" in FakeSMTP.sent["from"]
    assert FakeSMTP.sent["reply_to"] == "alice@example.invalid"


def test_the_message_follows_the_users_language(smtp):
    mailer.send_weekly(b"PK", META, to="boss@example.invalid", strings=i18n.NL, mailer=smtp)
    assert FakeSMTP.sent["subject"].startswith("Urenstaat")


# --- channel choice --------------------------------------------------------


def test_nothing_is_sent_until_the_user_switches_it_on():
    user = _user(delivery_channel="email", manager_email="boss@example.invalid",
                 delivery_enabled=False)
    assert delivery.target_for(user) == ("none", "")
    out = delivery.send(user, _days(), META)
    assert out.sent is False and "switched off" in out.reason


def test_an_enabled_channel_with_no_recipient_fails_loudly():
    user = _user(delivery_channel="email", delivery_enabled=True)
    out = delivery.send(user, _days(), META)
    assert out.sent is False and "no recipient" in out.reason


def test_chat_falls_back_to_the_email_address():
    """A Teams recipient IS an email address; asking for it twice is a good way
    to have one of them silently wrong."""
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    assert delivery.target_for(user) == ("chat", "boss@example.invalid")


def test_chat_delivery_needs_a_connected_microsoft_account():
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    out = delivery.send(user, _days(), META, session=None)
    assert out.sent is False and "not connected" in out.reason


def test_chat_delivery_sends_through_the_users_own_connection(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-delivery-tests-long-enough")
    sent = {}
    monkeypatch.setattr("timesheet.collectors.m365_mcp.send_chat",
                        lambda to, text, session=None: sent.update(to=to, text=text) or "chat-1")
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_chat="boss@example.invalid")
    out = delivery.send(user, _days(), META, session=McpSession(tokens={"refresh_token": "r"}),
                        public_url="https://timesheet.example.invalid")
    assert out.sent is True and out.target == "boss@example.invalid"
    assert "40:00" in sent["text"]
    assert "/d/github.com:1/" in sent["text"] and "timesheet.example.invalid" in sent["text"]


def test_a_failed_chat_send_is_reported_not_swallowed(monkeypatch):
    def _boom(*_a, **_k):
        raise mcp_client.McpError("the tenant blocks app messages")

    monkeypatch.setattr("timesheet.collectors.m365_mcp.send_chat", _boom)
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_chat="boss@example.invalid")
    out = delivery.send(user, _days(), META, session=McpSession(tokens={"refresh_token": "r"}))
    assert out.sent is False and "blocks app messages" in out.reason
