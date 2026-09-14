"""Encryption for the credentials this service has to keep.

Two things end up in the database that would be a serious incident if the
database leaked: a Microsoft refresh token (standing delegated read access to
somebody's mailbox, calendar and chats, with no further MFA prompt) and a GitHub
token. Neither is stored in the clear.

AES-256-GCM via `cryptography`, with the key taken from `SECRET_KEY` and a fresh
random nonce per value. The stored form is versioned — `v1.<nonce>.<ciphertext>`
— so a future key rotation or cipher change can recognise what it is looking at
rather than guessing.

The failure mode is deliberate and blunt: with no key, or without `cryptography`
installed, storing a credential **raises**. Falling back to plaintext would make
a misconfigured deployment look identical to a correct one right up until the
day it matters.
"""
from __future__ import annotations

import base64
import hashlib
import os

MIN_KEY_LENGTH = 16
PREFIX = "v1"


class CryptoUnavailable(RuntimeError):
    """No usable key, or no cipher library — refuse rather than store plaintext."""


def _key(secret: str | None = None) -> bytes:
    raw = secret if secret is not None else os.environ.get("SECRET_KEY", "")
    if not raw or len(raw) < MIN_KEY_LENGTH:
        raise CryptoUnavailable(
            f"SECRET_KEY must be set and at least {MIN_KEY_LENGTH} characters before this "
            "service can store Microsoft or GitHub credentials. Generate one with "
            "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`.")
    # A passphrase is not a key. SHA-256 gives the 32 bytes AES-256 needs from
    # whatever length of secret the operator chose; the secret never varies per
    # value, so a KDF's salt would have nowhere to live and buys nothing here.
    return hashlib.sha256(raw.encode()).digest()


def available(secret: str | None = None) -> bool:
    """Can this process encrypt? Used to disable the connect flows in the UI
    rather than let someone start one that cannot possibly finish."""
    try:
        _key(secret)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except (CryptoUnavailable, ImportError):
        return False
    return True


def _aesgcm(secret: str | None):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise CryptoUnavailable(
            "the `cryptography` package is required to store credentials; "
            'install the service extra: pip install ".[service]"') from e
    return AESGCM(_key(secret))


def encrypt(plaintext: str, *, secret: str | None = None) -> str:
    nonce = os.urandom(12)
    blob = _aesgcm(secret).encrypt(nonce, plaintext.encode(), PREFIX.encode())
    return f"{PREFIX}.{base64.urlsafe_b64encode(nonce).decode()}." \
           f"{base64.urlsafe_b64encode(blob).decode()}"


def decrypt(stored: str, *, secret: str | None = None) -> str:
    try:
        version, nonce_b64, blob_b64 = stored.split(".", 2)
    except ValueError as e:
        raise CryptoUnavailable("stored credential is not in a recognised format") from e
    if version != PREFIX:
        raise CryptoUnavailable(f"stored credential has unknown version {version!r}")
    plain = _aesgcm(secret).decrypt(
        base64.urlsafe_b64decode(nonce_b64),
        base64.urlsafe_b64decode(blob_b64),
        version.encode(),
    )
    return plain.decode()
