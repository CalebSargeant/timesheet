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


def test_email_says_why_it_could_not_send_without_a_server():
    """The reason travels with the failure. A bare False left the caller to guess,
    and every guess it made was "the server rejected it"."""
    with pytest.raises(mailer.MailError, match="SMTP_HOST"):
        mailer.send_weekly(b"PK\x03\x04", META, to="boss@example.invalid",
                           strings=i18n.EN, mailer=mailer.Mailer())


def test_email_says_why_it_could_not_send_without_a_recipient(smtp):
    with pytest.raises(mailer.MailError, match="no recipient"):
        mailer.send_weekly(b"PK", META, to="", strings=i18n.EN, mailer=smtp)


def test_email_builds_and_sends(smtp):
    mailer.send_weekly(b"PK\x03\x04data", META, to="boss@example.invalid",
                       strings=i18n.EN, mailer=smtp, manager_name="Dana Boss",
                       sender_name="Alice Smith", reply_to="alice@example.invalid",
                       public_url="https://timesheet.example.invalid")
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


def test_chat_delivery_needs_a_connected_microsoft_account(chat_on):
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    out = delivery.send(user, _days(), META, session=None)
    assert out.sent is False and "not connected" in out.reason


@pytest.fixture
def chat_on(monkeypatch):
    """A deployment that has switched Teams delivery on. Off is the default —
    see test_teams_delivery_is_off_until_a_deployment_switches_it_on."""
    monkeypatch.setenv(delivery.CHAT_ENV, "true")


@pytest.fixture
def can_chat(monkeypatch, chat_on):
    """Pretend the connector grants a send scope. It does not in reality — see
    test_chat_needs_a_write_scope_the_connector_does_not_grant."""
    monkeypatch.setattr("timesheet.collectors.m365_mcp.can_send",
                        lambda kind, session=None: "")


def test_chat_delivery_sends_through_the_users_own_connection(monkeypatch, can_chat):
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


def test_a_failed_chat_send_is_reported_not_swallowed(monkeypatch, can_chat):
    def _boom(*_a, **_k):
        raise mcp_client.McpError("the tenant blocks app messages")

    monkeypatch.setattr("timesheet.collectors.m365_mcp.send_chat", _boom)
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_chat="boss@example.invalid")
    out = delivery.send(user, _days(), META, session=McpSession(tokens={"refresh_token": "r"}))
    assert out.sent is False and "blocks app messages" in out.reason


# --- telling "not configured" apart from "rejected" -------------------------


def test_no_mail_server_is_reported_as_such_not_as_a_rejection():
    """These are different problems with different fixes. Reporting both as a
    rejection sends people hunting a mail server that was never there."""
    user = _user(delivery_channel="email", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    out = delivery.send(user, _days(), META, mailer=mailer.Mailer())
    assert out.sent is False
    assert "no mail server configured" in out.reason
    assert "rejected" not in out.reason


def test_a_configured_server_that_refuses_is_reported_in_its_own_words(monkeypatch, smtp):
    """What the server said is the whole diagnosis — a relay denial, an
    unverified sending domain, a rejected recipient. Flattening every one of them
    to "rejected" throws away the only sentence that tells you what to fix."""
    def _boom(self, message):
        raise mailer.MailError("550 relay denied")

    monkeypatch.setattr(mailer.Mailer, "send", _boom)
    user = _user(delivery_channel="email", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    out = delivery.send(user, _days(), META, mailer=smtp)
    assert out.sent is False and "550 relay denied" in out.reason


def test_unavailable_names_the_blocker_before_anything_is_sent(chat_on):
    assert "no mail server" in delivery.unavailable("email", mailer=mailer.Mailer())
    assert delivery.unavailable("email", mailer=mailer.Mailer(
        host="smtp.example.invalid", sender="a@b.invalid")) == ""
    assert "not connected" in delivery.unavailable("chat", session=None)
    assert delivery.unavailable("none") == ""


def test_chat_needs_a_write_scope_the_connector_does_not_grant(monkeypatch, chat_on):
    """The connector's delegated permissions are read-only — Chat.Read,
    Mail.Read and friends, with no ChatMessage.Send and no Mail.Send. Sending
    therefore fails with "FORBIDDEN: Missing scope" at the moment somebody
    presses the button, so it is checked up front instead."""
    from timesheet.collectors import m365_mcp
    read_only = ["Calendars.Read", "Chat.Read", "ChatMessage.Read", "Mail.Read", "User.Read"]
    monkeypatch.setattr(m365_mcp, "granted_scopes", lambda session=None: read_only)
    why = delivery.unavailable("chat", session=McpSession(tokens={"refresh_token": "r"}))
    assert "read-only" in why and "ChatMessage.Send" in why


def test_a_connection_that_does_grant_sending_is_allowed(monkeypatch, chat_on):
    from timesheet.collectors import m365_mcp
    monkeypatch.setattr(m365_mcp, "granted_scopes",
                        lambda session=None: ["Chat.Read", "ChatMessage.Send"])
    assert delivery.unavailable("chat",
                                session=McpSession(tokens={"refresh_token": "r"})) == ""


def test_an_unreachable_connector_is_not_reported_as_a_permissions_problem(
        monkeypatch, chat_on):
    from timesheet.collectors import m365_mcp

    def _down(session=None):
        raise mcp_client.McpError("cannot reach the MCP connector: dns")

    monkeypatch.setattr(m365_mcp, "granted_scopes", _down)
    why = delivery.unavailable("chat", session=McpSession(tokens={"refresh_token": "r"}))
    assert "could not ask Microsoft" in why and "read-only" not in why


# --- Teams delivery is off unless a deployment says otherwise ---------------


def test_teams_delivery_is_off_until_a_deployment_switches_it_on(monkeypatch):
    """The connector's app registration carries no ChatMessage.Send, so on almost
    every tenant this channel cannot work. Offering it anyway produced a green
    line on the Connections page and a 403 at the moment somebody used it."""
    monkeypatch.delenv(delivery.CHAT_ENV, raising=False)
    assert delivery.channels() == ("none", "email")
    why = delivery.unavailable("chat", session=McpSession(tokens={"refresh_token": "r"}))
    assert "switched off on this deployment" in why


def test_switching_teams_on_puts_it_back_on_offer(monkeypatch):
    monkeypatch.setenv(delivery.CHAT_ENV, "true")
    assert delivery.channels() == ("none", "email", "chat")


def test_a_chat_send_on_a_deployment_with_it_off_is_refused_not_attempted(monkeypatch):
    monkeypatch.delenv(delivery.CHAT_ENV, raising=False)
    monkeypatch.setattr("timesheet.collectors.m365_mcp.send_chat",
                        lambda *a, **k: pytest.fail("must not reach the connector"))
    user = _user(delivery_channel="chat", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    out = delivery.send(user, _days(), META, session=McpSession(tokens={"refresh_token": "r"}))
    assert out.sent is False and "switched off" in out.reason


# --- the manual send -------------------------------------------------------


def test_pressing_send_works_even_with_automatic_delivery_off():
    user = _user(delivery_channel="email", manager_email="boss@example.invalid",
                 delivery_enabled=False)
    assert delivery.target_for(user) == ("none", "")
    assert delivery.target_for(user, manual=True) == ("email", "boss@example.invalid")


def test_the_link_in_a_message_opens_the_period_that_was_sent(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-delivery-tests-long-enough")
    url = delivery.signed_link(_user(), "https://timesheet.example.invalid",
                               "from=2026-07-20&to=2026-07-26")
    assert url.endswith("/timesheet.xlsx?from=2026-07-20&to=2026-07-26")
    assert "/d/github.com:1/" in url


# --- proving the send works, without a manager finding out ------------------


def test_a_test_message_goes_to_the_account_holder(smtp):
    out = delivery.send_test(_user(), mailer=smtp)
    assert out.sent is True and out.target == "alice@example.invalid"
    assert FakeSMTP.sent["to"] == "alice@example.invalid"
    assert FakeSMTP.sent["subject"].startswith("[test]")


def test_a_test_message_carries_the_attachment_the_real_one_would(smtp):
    delivery.send_test(_user(), mailer=smtp, xlsx=b"PK\x03\x04data")
    assert FakeSMTP.sent["has_xlsx"] is True


def test_a_test_with_nowhere_to_send_says_so_rather_than_guessing():
    user = _user()
    user.email = ""
    out = delivery.send_test(user, mailer=mailer.Mailer(
        host="smtp.example.invalid", sender="a@b.invalid"))
    assert out.sent is False and "no address to test with" in out.reason


def test_checking_the_server_opens_a_connection_and_sends_nothing(monkeypatch, smtp):
    assert delivery.check_email(mailer=smtp) == ""
    assert "subject" not in FakeSMTP.sent          # nothing was sent to find that out


def test_a_check_reports_what_the_server_said(monkeypatch, smtp):
    def _refused(self):
        raise mailer.MailError("[Errno 61] Connection refused")

    monkeypatch.setattr(mailer.Mailer, "check", _refused)
    assert "Connection refused" in delivery.check_email(mailer=smtp)


def test_an_unconfigured_deployment_is_named_as_such_not_checked():
    assert "SMTP_HOST" in delivery.mail_server_line(mailer=mailer.Mailer())
    assert "smtp.example.invalid" in delivery.mail_server_line(
        mailer=mailer.Mailer(host="smtp.example.invalid", sender="a@b.invalid"))


# --- Mailgun over HTTPS, for hosts that block SMTP --------------------------


def test_mailgun_is_chosen_when_it_is_configured():
    env = {"MAILGUN_API_KEY": "key-1", "MAILGUN_DOMAIN": "mg.example.invalid",
           "REPORT_EMAIL_FROM": "timesheet@example.invalid",
           "SMTP_HOST": "smtp.example.invalid"}
    m = mailer.from_env(env)
    assert isinstance(m, mailer.MailgunMailer) and m.configured


def test_smtp_stays_the_default():
    m = mailer.from_env({"SMTP_HOST": "smtp.example.invalid",
                         "REPORT_EMAIL_FROM": "timesheet@example.invalid"})
    assert isinstance(m, mailer.Mailer) and m.configured


def test_mailgun_posts_the_whole_mime_message(monkeypatch):
    posted = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"id": "<x@mg>"}'

    def _open(url, *, data=None, headers=None, timeout=30):
        posted.update(url=url, data=data, headers=headers)
        return _Response()

    monkeypatch.setattr(mailer, "open_url", _open)
    m = mailer.MailgunMailer(api_key="key-1", domain="mg.example.invalid",
                             sender="timesheet@example.invalid")
    mailer.send_weekly(b"PK\x03\x04", META, to="boss@example.invalid", strings=i18n.EN,
                       mailer=m, sender_name="Alice Smith")
    assert posted["url"] == "https://api.mailgun.net/v3/mg.example.invalid/messages.mime"
    assert posted["headers"]["Authorization"].startswith("Basic ")
    assert b"boss@example.invalid" in posted["data"]
    assert b"Timesheet week 2026-07-20" in posted["data"]
    # The attachment travels in the MIME message, so one message serves both
    # transports and an .xlsx that works on SMTP works here.
    assert b"timesheet-2026-07-20.xlsx" in posted["data"]


def test_an_eu_key_pointed_at_the_us_region_says_which_setting_is_wrong(monkeypatch):
    import urllib.error

    def _unauthorised(url, *, data=None, headers=None, timeout=30):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(mailer, "open_url", _unauthorised)
    m = mailer.MailgunMailer(api_key="key-1", domain="mg.example.invalid",
                             sender="timesheet@example.invalid")
    with pytest.raises(mailer.MailError, match=r"api\.eu\.mailgun\.net"):
        m.check()


def test_mailgun_refusing_a_message_carries_its_own_explanation(monkeypatch):
    import io
    import urllib.error

    def _refused(url, *, data=None, headers=None, timeout=30):
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", {},
            io.BytesIO(b'{"message": "Domain mg.example.invalid is not allowed to send"}'))

    monkeypatch.setattr(mailer, "open_url", _refused)
    m = mailer.MailgunMailer(api_key="key-1", domain="mg.example.invalid",
                             sender="timesheet@example.invalid")
    user = _user(delivery_channel="email", delivery_enabled=True,
                 manager_email="boss@example.invalid")
    out = delivery.send(user, _days(), META, mailer=m)
    assert out.sent is False and "is not allowed to send" in out.reason
