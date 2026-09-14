"""The self-serve Microsoft connect flow, held together across two requests.

A device-code sign-in has a gap in the middle: the server issues a code, a human
goes off to a browser and approves it, and only then can the server claim the
tokens. That gap is minutes long, so it cannot be a held-open request, and the
pending code cannot live in process memory either — a second pod would not know
about it and a restart would lose it.

So it is stored like any other credential: encrypted, keyed by user, with its own
expiry. The row is deleted the instant it is redeemed, which is what keeps a
device code from being replayed.

Nothing here is shown to the user except `user_code` and `verification_uri`. The
`device_code` is the bearer secret of the pending sign-in and never leaves the
server.
"""
from __future__ import annotations

import logging
import time

from ..collectors.mcp_client import DeviceCode, McpSession, poll_device_code, start_device_code
from .store import MICROSOFT

log = logging.getLogger(__name__)

PENDING = "microsoft_pending"


def start(store, user_id: str, *, tenant: str | None = None) -> DeviceCode:
    """Issue a code for this user, replacing any half-finished attempt."""
    pending = start_device_code(tenant=tenant)
    store.put_credential(user_id, PENDING, {
        "device_code": pending.device_code,
        "user_code": pending.user_code,
        "verification_uri": pending.verification_uri,
        "expires_at": pending.expires_at,
        "interval": pending.interval,
        "tenant": tenant or "",
    }, account="pending")
    return pending


def current(store, user_id: str) -> DeviceCode | None:
    row = store.get_credential(user_id, PENDING)
    if not row:
        return None
    if float(row.get("expires_at", 0)) < time.time():
        store.drop_credential(user_id, PENDING)
        return None
    return DeviceCode(
        device_code=row["device_code"], user_code=row["user_code"],
        verification_uri=row["verification_uri"],
        expires_at=float(row["expires_at"]), interval=int(row.get("interval", 5)),
    )


def cancel(store, user_id: str) -> None:
    store.drop_credential(user_id, PENDING)


def finish(store, user_id: str, *, tenant: str | None = None) -> bool:
    """True once the sign-in is complete and the tokens are stored.

    False means only "the human has not approved it yet". Anything genuinely
    wrong — a declined sign-in, an expired code — raises, so the page can say so
    instead of spinning until the tab is closed.
    """
    pending = current(store, user_id)
    if pending is None:
        return bool(store.credential_meta(user_id, MICROSOFT))

    row = store.get_credential(user_id, PENDING) or {}
    tenant = tenant or (row.get("tenant") or None)
    try:
        tokens = poll_device_code(pending, tenant=tenant)
    except Exception:
        # A dead attempt must not stay on the account; leaving it would make
        # every later poll re-raise the same failure forever.
        store.drop_credential(user_id, PENDING)
        raise
    if tokens is None:
        return False

    # Ask the connector who just signed in, so the Connections page can name the
    # account rather than claiming an anonymous "connected". A failure here is
    # cosmetic and must not lose the tokens that were just obtained.
    account = ""
    try:
        account = (McpSession(tokens=tokens, tenant=tenant).me()
                   .get("userPrincipalName") or "")
    except Exception:  # noqa: BLE001
        log.debug("could not read the connected Microsoft identity", exc_info=True)

    store.put_credential(user_id, MICROSOFT, tokens, account=account)
    store.drop_credential(user_id, PENDING)
    return True
