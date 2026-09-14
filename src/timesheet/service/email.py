"""Sending a timesheet by email.

SMTP settings belong to the deployment, not to the user: an account holder gives
a *recipient*, never a mail server's credentials, so nobody can turn this into an
open relay by filling in a settings form. The message is sent from the
deployment's own address with the user's address as `Reply-To`, which is what
makes a manager's reply land with the person whose hours these are.

A missing or misconfigured mailer logs and returns False rather than raising, so
a scheduled run for twenty people is not lost because one of them has a typo in a
manager's address.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from ..timeutil import hm

log = logging.getLogger(__name__)

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class MailError(RuntimeError):
    """The message could not be sent, with a reason fit to show the user."""


@dataclass(frozen=True)
class Mailer:
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

    @staticmethod
    def from_env(env: dict | None = None) -> Mailer:
        e = env or os.environ
        return Mailer(
            host=e.get("SMTP_HOST", ""),
            port=int(e.get("SMTP_PORT", "587")),
            user=e.get("SMTP_USER", ""),
            password=e.get("SMTP_PASSWORD", ""),
            sender=e.get("REPORT_EMAIL_FROM", ""),
            starttls=(e.get("SMTP_STARTTLS", "true").lower() in ("1", "true", "yes", "on")),
        )

    def send(self, message: EmailMessage) -> None:
        if not self.configured:
            raise MailError("this deployment has no SMTP server configured")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                if self.starttls:
                    s.starttls(context=ssl.create_default_context())
                if self.user and self.password:
                    s.login(self.user, self.password)
                s.send_message(message)
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(f"could not send the message: {e}") from e


def build_message(xlsx: bytes, meta: dict, *, to: str, mailer: Mailer, strings,
                  manager_name: str = "", sender_name: str = "",
                  reply_to: str = "", public_url: str = "") -> EmailMessage:
    week = meta.get("week_start", "")
    total = hm(meta.get("total_minutes", 0))
    greeting = f" {manager_name.split()[0]}" if manager_name.strip() else ""

    msg = EmailMessage()
    msg["Subject"] = strings.mail_subject.format(week=week, total=total)
    msg["From"] = formataddr((sender_name or "Timesheet", parseaddr(mailer.sender)[1]))
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(strings.mail_body.format(
        greeting=greeting, week=week, total=total,
        url=public_url or "", sender=sender_name or ""))
    msg.add_attachment(xlsx, maintype=XLSX_MIME[0], subtype=XLSX_MIME[1],
                       filename=f"timesheet-{week}.xlsx")
    return msg


def send_weekly(xlsx: bytes, meta: dict, *, to: str, strings, mailer: Mailer | None = None,
                manager_name: str = "", sender_name: str = "", reply_to: str = "",
                public_url: str = "") -> bool:
    mailer = mailer or Mailer.from_env()
    if not to or not mailer.configured:
        log.info("email skipped: %s", "no recipient" if not to else "no SMTP configured")
        return False
    try:
        mailer.send(build_message(xlsx, meta, to=to, mailer=mailer, strings=strings,
                                  manager_name=manager_name, sender_name=sender_name,
                                  reply_to=reply_to, public_url=public_url))
    except MailError as e:
        log.warning("email to %s failed: %s", to, e)
        return False
    return True
