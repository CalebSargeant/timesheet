"""Signed, time-limited download links, and the shared secret for pushed ingest.

A download link is the one way data leaves this service without a session: a
manager clicks a link in an email and gets the .xlsx, having never signed in. So
the signature covers **who the sheet belongs to** as well as what and until when.
Without the user id in the signed material, one valid link plus a guessed
account id would read anybody's timesheet.

The ingest token is per user for the same reason. One deployment-wide secret
would let any automation that holds it write into every account.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

VERSION = "1"


def _key() -> bytes:
    raw = (os.environ.get("DOWNLOAD_SIGNING_KEY") or os.environ.get("SECRET_KEY") or "")
    if not raw:
        # Refusing outright is the only safe answer: a default key is a public
        # key, and these tokens are what stand between a link and someone's week.
        raise RuntimeError("SECRET_KEY (or DOWNLOAD_SIGNING_KEY) must be set to sign links")
    return hashlib.sha256(("download:" + raw).encode()).digest()


def _mac(material: str) -> str:
    return hmac.new(_key(), material.encode(), hashlib.sha256).hexdigest()[:32]


def sign_download(user_id: str, name: str, ttl_seconds: int) -> str:
    exp = int(time.time()) + int(ttl_seconds)
    return f"{VERSION}.{exp}.{_mac(f'{VERSION}:{user_id}:{name}:{exp}')}"


def verify_download(user_id: str, name: str, token: str) -> bool:
    try:
        version, exp_s, sig = token.split(".", 2)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if version != VERSION or exp < time.time():
        return False
    return hmac.compare_digest(sig, _mac(f"{version}:{user_id}:{name}:{exp}"))


def ingest_token(user_id: str) -> str:
    """A stable per-user secret for the push API. Derived rather than stored: it
    cannot leak from the database, and rotating SECRET_KEY rotates every one."""
    return hmac.new(_key(), f"ingest:{user_id}".encode(), hashlib.sha256).hexdigest()


def check_ingest_token(user_id: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented, ingest_token(user_id))
