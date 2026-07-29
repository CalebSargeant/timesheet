"""Store factory selection + weekly-email construction (no real SMTP/DB)."""

from timesheet.service import email as mailer
from timesheet.service.store import FileStore, make_store


def test_make_store_defaults_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert isinstance(make_store(), FileStore)


def test_email_skips_without_smtp(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert mailer.send_weekly(b"PK\x03\x04", {"week_start": "2026-07-20"}) is False


def test_email_builds_and_sends_via_fake_smtp(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"], sent["port"] = host, port
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context=None):
            sent["starttls"] = True
        def login(self, u, p):
            sent["login"] = (u, p)
        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]
            sent["has_xlsx"] = any(
                part.get_filename("").endswith(".xlsx") for part in msg.iter_attachments())

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("REPORT_EMAIL_TO", "marc.vergunst@pinkroccade.nl")
    monkeypatch.setenv("SMTP_USER", "caleb")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")

    ok = mailer.send_weekly(b"PK\x03\x04data", {"week_start": "2026-07-20", "total_minutes": 2400})
    assert ok is True
    assert sent["subject"] == "Urenstaat week 2026-07-20 — 40:00"
    assert sent["to"] == "marc.vergunst@pinkroccade.nl"
    assert sent["has_xlsx"] and sent["starttls"] and sent["login"] == ("caleb", "pw")
