"""Getting the finished timesheet to whoever has to sign it off.

Two channels, chosen per user:

  * **email** — the deployment's mail server (SMTP, or the Mailgun HTTP API
    where outbound SMTP is blocked), the user's chosen recipient, the .xlsx
    attached.
  * **chat** — a Teams message sent through that user's *own* Microsoft
    connection. Off unless a deployment switches it on, because the connector is
    granted read-only delegated permissions: without `ChatMessage.Send` in the
    tenant's app registration it cannot send at all, and a channel that is
    offered but cannot work is worse than one that is not offered.

Sending is an outward-facing act, so it happens only when a person asks for it:
the scheduled run delivers only to accounts that switched automatic delivery on,
and the buttons in the UI send exactly once, when pressed. The result is reported
honestly — `Delivery.sent` is false with a `reason` whenever anything at all went
wrong, and no caller is told a message went out that didn't.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..render import xlsx as render_xlsx
from ..timeutil import hm
from . import email as mailer_mod
from . import security
from .users import User

log = logging.getLogger(__name__)

# Delivering over Teams needs a write scope (ChatMessage.Send / Chat.ReadWrite)
# that the shared connector's app registration does not carry, and that plenty of
# tenants will not grant. It is therefore opt-in per deployment rather than an
# option every account is offered and none of them can use.
CHAT_ENV = "DELIVERY_CHAT_ENABLED"


@dataclass(frozen=True)
class Delivery:
    channel: str
    sent: bool
    target: str = ""
    reason: str = ""

    def describe(self) -> str:
        if self.sent:
            return f"sent to {self.target} by {self.channel}"
        return self.reason or f"not sent ({self.channel})"


def chat_enabled(env: dict | None = None) -> bool:
    e = env if env is not None else os.environ
    return (e.get(CHAT_ENV, "false") or "").strip().lower() in ("1", "true", "yes", "on")


def channels(env: dict | None = None) -> tuple[str, ...]:
    """The delivery channels this deployment offers, in the order they're shown."""
    return ("none", "email", "chat") if chat_enabled(env) else ("none", "email")


def unavailable(channel: str, *, mailer=None, session=None) -> str:
    """Why this channel cannot deliver right now, or "" if it can.

    Checked before anything is rendered or sent, so the Connections page can say
    plainly that a channel will not work instead of showing it green and failing
    only when somebody presses the button.
    """
    if channel == "email":
        m = mailer or mailer_mod.from_env()
        if not m.configured:
            return ("this deployment has no mail server configured "
                    f"({m.missing()})")
        return ""
    if channel == "chat":
        if not chat_enabled():
            return (f"Teams delivery is switched off on this deployment — set "
                    f"{CHAT_ENV}=true only if your tenant grants the connector "
                    f"ChatMessage.Send")
        if session is None or not session.connected:
            return "Microsoft is not connected for this account"
        from ..collectors import m365_mcp
        return m365_mcp.can_send("chat", session)
    return ""


def target_for(user: User, *, manual: bool = False) -> tuple[str, str]:
    """(channel, recipient) for a user, or ('none', '') if they haven't set one.

    `manual` is the difference between the scheduled run and somebody pressing
    Send. The automatic switch governs what leaves here unattended; it was never
    meant to stop the account holder sending their own week when they ask for it,
    and a Send button that silently does nothing is its own kind of dishonest.
    """
    channel = user.setting("delivery_channel")
    if channel == "none" or (not manual and not user.setting("delivery_enabled")):
        return "none", ""
    if channel == "email":
        return "email", (user.setting("manager_email") or "").strip()
    if channel == "chat":
        # Falls back to the email address when no separate chat address is given:
        # a Teams recipient IS an email address, and asking for the same value
        # twice is a good way to have one of them silently wrong.
        return "chat", ((user.setting("manager_chat") or user.setting("manager_email"))
                        or "").strip()
    return "none", ""


def send(user: User, days, meta: dict, *, session=None, mailer=None,
         public_url: str = "", manual: bool = False, period: str = "") -> Delivery:
    """Deliver one user's timesheet over their configured channel.

    `period` is the query string identifying what is being sent (`period=...` or
    `from=...&to=...`), so the link in the message opens the same range the
    sender was looking at rather than defaulting to this week.
    """
    channel, target = target_for(user, manual=manual)
    if channel == "none":
        return Delivery("none", False, reason="delivery is switched off for this account")
    if not target:
        return Delivery(channel, False, reason=f"no recipient configured for {channel}")

    strings = user.strings
    if channel == "email":
        # "Not configured" and "the server said no" are different problems with
        # different fixes; reporting both as a rejection sends people hunting a
        # mail server that was never there.
        why = unavailable("email", mailer=mailer)
        if why:
            return Delivery("email", False, target=target, reason=why)
        try:
            mailer_mod.send_weekly(
                render_xlsx.build_week(days, locale=strings), meta,
                to=target, strings=strings, mailer=mailer,
                manager_name=user.setting("manager_name"), sender_name=user.display,
                reply_to=user.email, public_url=signed_link(user, public_url, period))
        except mailer_mod.MailError as e:
            log.warning("email to %s failed: %s", target, e)
            return Delivery("email", False, target=target, reason=str(e)[:500])
        return Delivery("email", True, target=target)

    # chat
    why = unavailable("chat", session=session)
    if why:
        return Delivery("chat", False, target=target, reason=why)
    from ..collectors import m365_mcp
    from ..collectors.mcp_client import McpError

    text = strings.chat_body.format(
        week=meta.get("week_start", ""), total=hm(meta.get("total_minutes", 0)),
        url=signed_link(user, public_url, period))
    try:
        m365_mcp.send_chat(target, text, session=session)
    except McpError as e:
        log.warning("chat delivery to %s failed: %s", target, e)
        return Delivery("chat", False, target=target, reason=str(e)[:200])
    return Delivery("chat", True, target=target)


def signed_link(user: User, public_url: str, period: str = "") -> str:
    """The time-limited download link that goes in a message, or "" with no host.

    The signature names the account, so the link cannot be pointed at somebody
    else's week by editing the id in it.
    """
    if not public_url:
        return ""
    ttl = int(os.environ.get("DOWNLOAD_TTL_SECONDS", 7 * 24 * 3600))
    token = security.sign_download(user.id, "timesheet.xlsx", ttl)
    url = f"{public_url}/d/{user.id}/{token}/timesheet.xlsx"
    return f"{url}?{period}" if period else url


def send_test(user: User, *, to: str = "", mailer=None, public_url: str = "",
              xlsx: bytes = b"") -> Delivery:
    """A real message, through the real mail server, to the account holder.

    The only honest answer to "will the send work?" is a send. It goes to the
    person asking — never to the manager — so finding out costs nobody else an
    email, and it carries the same attachment and the same From/Reply-To as the
    real thing, because a test that skips the parts that usually fail proves
    nothing.
    """
    target = (to or user.email or "").strip()
    if not target:
        return Delivery("email", False,
                        reason="no address to test with — your GitHub account has no public "
                               "email, so type one in")
    m = mailer or mailer_mod.from_env()
    why = unavailable("email", mailer=m)
    if why:
        return Delivery("email", False, target=target, reason=why)
    try:
        mailer_mod.send_test(target, strings=user.strings, mailer=m,
                             sender_name=user.display, reply_to=user.email,
                             public_url=public_url, xlsx=xlsx)
    except mailer_mod.MailError as e:
        return Delivery("email", False, target=target, reason=str(e)[:500])
    return Delivery("email", True, target=target)


def mail_server_line(*, mailer=None) -> str:
    """The mail path this deployment is set up to use, for the Connections page.

    Named out loud because the commonest delivery fault is not a rejected
    message: it is a service that was never told where to send one, and looks
    identical from the outside.
    """
    m = mailer or mailer_mod.from_env()
    if not m.configured:
        return f"not configured — {m.missing()}"
    return m.describe()


def check_email(*, mailer=None) -> str:
    """"" if the mail server accepts a connection and our credentials, else why not.

    Opens the conversation and hangs up before any message: it answers the
    "is this thing plugged in" half of the question without sending anything at
    all, which is what you want when the recipient is a real manager.
    """
    m = mailer or mailer_mod.from_env()
    why = unavailable("email", mailer=m)
    if why:
        return why
    try:
        m.check()
    except mailer_mod.MailError as e:
        return str(e)[:500]
    return ""
