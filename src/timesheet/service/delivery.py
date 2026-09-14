"""Getting the finished timesheet to whoever has to sign it off.

Two channels, chosen per user:

  * **email** — the deployment's SMTP server, the user's chosen recipient, the
    .xlsx attached.
  * **chat** — a Teams message sent through that user's *own* Microsoft
    connection, so it arrives from them and reaches exactly the people they can
    already message. A chat message carries no attachment, so it carries the link
    instead; the page behind it is the same one the manager would open anyway.

Sending is an outward-facing act, so it happens only when the user has switched
it on, and the result is reported honestly: `Delivery.sent` is false with a
`reason` whenever anything at all went wrong, and no caller is told a message
went out that didn't.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..render import xlsx as render_xlsx
from ..timeutil import hm
from . import email as mailer_mod
from . import security
from .users import User

log = logging.getLogger(__name__)


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


def unavailable(channel: str, *, mailer=None, session=None) -> str:
    """Why this channel cannot deliver right now, or "" if it can.

    Checked before anything is rendered or sent, so the Connections page can say
    plainly that a channel will not work instead of showing it green and failing
    only when somebody presses the button.
    """
    if channel == "email":
        m = mailer or mailer_mod.Mailer.from_env()
        if not m.configured:
            return ("this deployment has no mail server configured "
                    "(set SMTP_HOST and REPORT_EMAIL_FROM, or deliver over chat instead)")
        return ""
    if channel == "chat":
        if session is None or not session.connected:
            return "Microsoft is not connected for this account"
        from ..collectors import m365_mcp
        return m365_mcp.can_send("chat", session)
    return ""


def target_for(user: User) -> tuple[str, str]:
    """(channel, recipient) for a user, or ('none', '') if they haven't set one."""
    channel = user.setting("delivery_channel")
    if not user.setting("delivery_enabled") or channel == "none":
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
         public_url: str = "") -> Delivery:
    """Deliver one user's week over their configured channel."""
    channel, target = target_for(user)
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
        ok = mailer_mod.send_weekly(
            render_xlsx.build_week(days, locale=strings), meta,
            to=target, strings=strings, mailer=mailer,
            manager_name=user.setting("manager_name"), sender_name=user.display,
            reply_to=user.email, public_url=public_url)
        return Delivery("email", ok, target=target,
                        reason="" if ok else "the mail server rejected the message")

    # chat
    why = unavailable("chat", session=session)
    if why:
        return Delivery("chat", False, target=target, reason=why)
    from ..collectors import m365_mcp
    from ..collectors.mcp_client import McpError

    signed_url = ""
    if public_url:
        token = security.sign_download(user.id, "timesheet.xlsx", 7 * 24 * 3600)
        signed_url = f"{public_url}/d/{user.id}/{token}/timesheet.xlsx"
    text = strings.chat_body.format(
        week=meta.get("week_start", ""), total=hm(meta.get("total_minutes", 0)),
        url=signed_url)
    try:
        m365_mcp.send_chat(target, text, session=session)
    except McpError as e:
        log.warning("chat delivery to %s failed: %s", target, e)
        return Delivery("chat", False, target=target, reason=str(e)[:200])
    return Delivery("chat", True, target=target)
