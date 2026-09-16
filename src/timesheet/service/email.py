"""Sending a timesheet by email.

Mail settings belong to the deployment, not to the user: an account holder gives
a *recipient*, never a mail server's credentials, so nobody can turn this into an
open relay by filling in a settings form. The message is sent from the
deployment's own address with the user's address as `Reply-To`, which is what
makes a manager's reply land with the person whose hours these are.

Two transports, one message:

  * **SMTP** — `SMTP_HOST` and friends. Works with any provider, Mailgun's SMTP
    endpoint included.
  * **Mailgun's HTTP API** — `MAILGUN_API_KEY` and `MAILGUN_DOMAIN`. Same
    message, posted over 443. This exists because plenty of clusters and hosts
    block outbound 25/465/587 outright, and there the SMTP path cannot be made
    to work no matter how correct the credentials are.

The message is built once, as MIME, and handed to whichever transport is
configured — so an attachment, a Reply-To or a From that works on one works on
the other.

Failure raises `MailError` with a reason fit to show a human. Callers isolate:
`delivery.send` turns it into a reported failure for one account, and a scheduled
run for twenty people is never lost because one of them has a typo in a manager's
address.
"""
from __future__ import annotations

import base64
import logging
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from ..net import InsecureUrl, open_url
from ..timeutil import hm

log = logging.getLogger(__name__)

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")

MAILGUN_DEFAULT_BASE = "https://api.mailgun.net"


class MailError(RuntimeError):
    """The message could not be sent, with a reason fit to show the user."""


def _bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Mailer:
    """The SMTP transport."""
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    starttls: bool = True
    timeout: int = 30

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def missing(self) -> str:
        """What a deployment still has to set, in the words of its own settings."""
        gaps = [name for name, value in (("SMTP_HOST", self.host),
                                         ("REPORT_EMAIL_FROM", self.sender)) if not value]
        return ("set " + " and ".join(gaps)) if gaps else ""

    def describe(self) -> str:
        return f"SMTP {self.host}:{self.port} as {self.sender}"

    def _open(self) -> smtplib.SMTP:
        s = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        if self.starttls:
            s.starttls(context=ssl.create_default_context())
        if self.user and self.password:
            s.login(self.user, self.password)
        return s

    def check(self) -> None:
        """Connect, negotiate TLS, sign in — then hang up without sending.

        Everything that usually breaks (a blocked port, a wrong password, a
        certificate nobody trusts) breaks here, and nobody receives an email to
        find that out.
        """
        if not self.configured:
            raise MailError(f"no SMTP server configured — {self.missing()}")
        try:
            with self._open():
                pass
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(f"{self.describe()}: {e}") from e

    def send(self, message: EmailMessage) -> None:
        if not self.configured:
            raise MailError(f"no SMTP server configured — {self.missing()}")
        try:
            with self._open() as s:
                s.send_message(message)
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(f"could not send the message: {e}") from e


@dataclass(frozen=True)
class MailgunMailer:
    """Mailgun's HTTP API, for hosts where outbound SMTP is not an option."""
    api_key: str = ""
    domain: str = ""
    sender: str = ""
    base_url: str = MAILGUN_DEFAULT_BASE
    timeout: int = 30

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.domain and self.sender)

    def missing(self) -> str:
        gaps = [name for name, value in (("MAILGUN_API_KEY", self.api_key),
                                         ("MAILGUN_DOMAIN", self.domain),
                                         ("REPORT_EMAIL_FROM", self.sender)) if not value]
        return ("set " + " and ".join(gaps)) if gaps else ""

    def describe(self) -> str:
        return f"Mailgun {self.domain} via {self.base_url} as {self.sender}"

    def _endpoint(self, name: str) -> str:
        return f"{self.base_url.rstrip('/')}/v3/{urllib.parse.quote(self.domain)}/{name}"

    def _post(self, url: str, fields: list[tuple[str, str]],
              files: list[tuple[str, str, bytes]] = ()) -> str:
        """One multipart POST. Returns Mailgun's reply, raises MailError otherwise."""
        boundary = f"----timesheet{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for name, value in fields:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode())
        for name, filename, blob in files:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n".encode())
            parts.append(blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())

        token = base64.b64encode(f"api:{self.api_key}".encode()).decode()
        try:
            with open_url(url, data=b"".join(parts), timeout=self.timeout, headers={
                "Authorization": f"Basic {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }) as r:
                return r.read().decode(errors="replace")[:300]
        except urllib.error.HTTPError as e:
            # Mailgun puts the reason in the body — an unverified domain, a
            # sandbox recipient that was never authorised, a key for the wrong
            # region. Dropping it leaves only "HTTP 401", which explains nothing.
            detail = e.read().decode(errors="replace")[:200] if e.fp else ""
            raise MailError(f"Mailgun refused the message (HTTP {e.code}): {detail}"
                            f"{_region_hint(e.code)}") from e
        except urllib.error.URLError as e:
            raise MailError(f"cannot reach {self.base_url}: {e.reason}") from e
        except InsecureUrl as e:
            raise MailError(str(e)) from e

    def check(self) -> None:
        """Have Mailgun accept a message in test mode: validated, never delivered.

        A send is the only request a domain *sending* key may make — Mailgun
        scopes those keys to POST /messages and /messages.mime, which is what
        makes them the right key for this service. Asking the domains API
        instead, as this first did, reported a perfectly good sending key as
        rejected. `o:testmode` makes the request real in every respect but
        delivery: the key, the domain and the region are all checked. Mailgun
        bills it like any other message, which for one press of a button is the
        price of an answer that is true.
        """
        if not self.configured:
            raise MailError(f"Mailgun is not fully configured — {self.missing()}")
        address = parseaddr(self.sender)[1] or self.sender
        self._post(self._endpoint("messages"), [
            ("from", self.sender),
            ("to", address),
            ("subject", "timesheet: mail server check"),
            ("text", "Sent in Mailgun test mode: accepted, validated, never delivered."),
            ("o:testmode", "yes"),
        ])

    def send(self, message: EmailMessage) -> None:
        if not self.configured:
            raise MailError(f"Mailgun is not fully configured — {self.missing()}")
        recipients = [a for a in (message.get_all("To") or []) if a]
        if not recipients:
            raise MailError("no recipient on the message")
        self._post(self._endpoint("messages.mime"), [("to", ", ".join(recipients))],
                   [("message", "timesheet.eml", bytes(message))])


def _region_hint(code: int) -> str:
    """What a 401 or 404 from Mailgun most often means when the key is right.

    Domains and keys are global but a domain's data lives in one region, so a
    valid key sent to the other region's API fails exactly like a wrong key.
    """
    if code not in (401, 403, 404):
        return ""
    return (" — if the key and domain are right, check MAILGUN_BASE_URL: a domain "
            "answers only in its own region (US https://api.mailgun.net, "
            "EU https://api.eu.mailgun.net)")


def from_env(env: dict | None = None) -> Mailer | MailgunMailer:
    """The transport this deployment configured.

    Mailgun's API wins when it is configured, because somebody who set an API key
    did so precisely to stop using SMTP.
    """
    e = env if env is not None else os.environ
    sender = e.get("REPORT_EMAIL_FROM", "")
    if e.get("MAILGUN_API_KEY") or e.get("MAILGUN_DOMAIN"):
        return MailgunMailer(
            api_key=e.get("MAILGUN_API_KEY", ""),
            domain=e.get("MAILGUN_DOMAIN", ""),
            sender=sender,
            base_url=e.get("MAILGUN_BASE_URL", "") or MAILGUN_DEFAULT_BASE,
        )
    return Mailer(
        host=e.get("SMTP_HOST", ""),
        port=int(e.get("SMTP_PORT", "587")),
        user=e.get("SMTP_USER", ""),
        password=e.get("SMTP_PASSWORD", ""),
        sender=sender,
        starttls=_bool(e.get("SMTP_STARTTLS", ""), True),
    )


def _envelope(*, to: str, mailer, subject: str, body: str, sender_name: str = "",
              reply_to: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name or "Timesheet", parseaddr(mailer.sender)[1]))
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg


def build_message(xlsx: bytes, meta: dict, *, to: str, mailer, strings,
                  manager_name: str = "", sender_name: str = "",
                  reply_to: str = "", public_url: str = "") -> EmailMessage:
    week = meta.get("week_start", "")
    total = hm(meta.get("total_minutes", 0))
    greeting = f" {manager_name.split()[0]}" if manager_name.strip() else ""

    msg = _envelope(to=to, mailer=mailer, sender_name=sender_name, reply_to=reply_to,
                    subject=strings.mail_subject.format(week=week, total=total),
                    body=strings.mail_body.format(
                        greeting=greeting, week=week, total=total,
                        url=public_url or "", sender=sender_name or ""))
    msg.add_attachment(xlsx, maintype=XLSX_MIME[0], subtype=XLSX_MIME[1],
                       filename=f"timesheet-{week}.xlsx")
    return msg


def send_weekly(xlsx: bytes, meta: dict, *, to: str, strings, mailer=None,
                manager_name: str = "", sender_name: str = "", reply_to: str = "",
                public_url: str = "") -> None:
    """Send one period's sheet. Raises `MailError` with the reason if it doesn't go."""
    mailer = mailer or from_env()
    if not to:
        raise MailError("no recipient configured")
    mailer.send(build_message(xlsx, meta, to=to, mailer=mailer, strings=strings,
                              manager_name=manager_name, sender_name=sender_name,
                              reply_to=reply_to, public_url=public_url))


def send_test(to: str, *, strings, mailer=None, sender_name: str = "", reply_to: str = "",
              public_url: str = "", xlsx: bytes = b"") -> None:
    """A test message to the account holder, shaped like the real one."""
    mailer = mailer or from_env()
    msg = _envelope(
        to=to, mailer=mailer, sender_name=sender_name, reply_to=reply_to,
        subject=f"[test] {strings.mail_subject.format(week='—', total='0:00')}",
        body=("This is a test from your timesheet.\n\n"
              "If it arrived, delivery works: the same server, the same From "
              "address and the same attachment are used for the real thing.\n"
              f"{public_url}\n"))
    if xlsx:
        msg.add_attachment(xlsx, maintype=XLSX_MIME[0], subtype=XLSX_MIME[1],
                           filename="timesheet-test.xlsx")
    mailer.send(msg)
