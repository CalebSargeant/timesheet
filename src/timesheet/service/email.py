"""Weekly email to the manager — the minimum-expected delivery channel.

Attaches the .xlsx and links the live page. SMTP config from env; a missing/So
misconfigured mailer logs and returns False rather than raising, so a scheduled
run never crashes the job."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from ..timeutil import hm


def send_weekly(xlsx: bytes, meta: dict, *, public_url: str | None = None) -> bool:
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("REPORT_EMAIL_TO")
    frm = os.environ.get("REPORT_EMAIL_FROM", "caleb@calebsargeant.com")
    if not host or not to:
        return False

    week = meta.get("week_start", "")
    total = hm(meta.get("total_minutes", 0))
    url = public_url or os.environ.get("PUBLIC_URL", "https://uren.calebsargeant.com")

    msg = EmailMessage()
    msg["Subject"] = f"Urenstaat week {week} — {total}"
    msg["From"] = frm
    msg["To"] = to
    msg.set_content(
        f"Hoi Marc,\n\nBijgevoegd de urenstaat voor week {week} (totaal {total}).\n"
        f"Altijd actueel te bekijken op {url}\n\nGroet,\nCaleb\n")
    msg.add_attachment(xlsx, maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=f"Uren-{week}.xlsx")

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASSWORD")
    starttls = os.environ.get("SMTP_STARTTLS", "true").lower() in ("1", "true", "yes")
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if starttls:
                s.starttls(context=ssl.create_default_context())
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        print(f"[email] send failed: {e}")
        return False
