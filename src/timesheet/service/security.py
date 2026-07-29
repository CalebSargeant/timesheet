"""Shared-secret ingest auth + signed, time-limited download tokens.

The site itself sits behind Cloudflare Access (email-OTP allowlist: Caleb + Marc).
These add two things Access can't: a secret the Power Automate flow presents on
ingest, and a link that lets an emailed .xlsx be fetched for a bounded window."""
from __future__ import annotations

import hashlib
import hmac
import os
import time


def _key() -> bytes:
    return (os.environ.get("DOWNLOAD_SIGNING_KEY")
            or os.environ.get("INGEST_TOKEN") or "dev-insecure-key").encode()


def check_ingest_token(presented: str | None) -> bool:
    expected = os.environ.get("INGEST_TOKEN", "")
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def sign_download(name: str, ttl_seconds: int) -> str:
    exp = int(time.time()) + int(ttl_seconds)
    sig = hmac.new(_key(), f"{name}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def verify_download(name: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    good = hmac.new(_key(), f"{name}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, good)
