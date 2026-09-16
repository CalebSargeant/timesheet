"""Server-rendered pages for signing in, connecting accounts and changing settings.

Plain HTML forms, no client framework, no build step — the same decision as the
self-contained stylesheet. The one piece of JavaScript is the poller on the
Microsoft sign-in page, which exists because that flow genuinely needs to wait
for a human to finish in another tab, and the page degrades to a manual
"I've done it" button when scripting is off.

Everything interpolated here goes through `esc`. The values are a person's own
display name, a manager's address and a GitHub login — all attacker-controllable
in the sense that matters, since a login is chosen by whoever registers it.
"""
from __future__ import annotations

from .. import i18n
from ..render.theme import esc, page
from .users import SETTINGS_BY_NAME, User

NAV = (("/", "Timesheet"), ("/connections", "Connections"), ("/settings", "Settings"))


def account_chrome(user: User | None, csrf: str = "") -> str:
    """The signed-in-as badge and sign-out control shown in every header."""
    if user is None:
        return '<a class="btn primary" href="/auth/login">Sign in with GitHub</a>'
    avatar = (f'<img src="{esc(user.avatar_url)}" alt="" width="22" height="22">'
              if user.avatar_url else "")
    return (f'<span class="who">{avatar}{esc(user.display)}</span>'
            f'<form method="post" action="/auth/logout" style="display:inline">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<button class="btn" type="submit">Sign out</button></form>')


def send_form(query: str, *, csrf: str, target: str = "") -> str:
    """The Send control on the timesheet page.

    It carries the period the page is showing as hidden fields, so what leaves is
    what the sender can see. `back` brings them back to the same view with the
    outcome on it rather than dropping them on a settings page.
    """
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        for k, v in (pair.split("=", 1) for pair in query.split("&") if "=" in pair))
    who = f" to {target}" if target else ""
    return (f'<form method="post" action="/deliver" style="display:inline">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="back" value="/">{hidden}'
            f'<button class="btn" type="submit" '
            f'title="Send what is on screen{esc(who)}">✉ Send</button></form>')


def nav(active: str) -> str:
    links = "".join(
        f'<a class="{"pill active" if href == active else "pill"}" href="{esc(href)}">'
        f"{esc(text)}</a>"
        for href, text in NAV)
    return f'<nav class="periods">{links}</nav>'


def shell(title: str, user: User | None, active: str, body: str, *, csrf: str = "",
          subtitle: str = "", lang: str = "en", narrow: bool = False) -> str:
    sub = f'<div class="sub">{esc(subtitle)}</div>' if subtitle else ""
    head = (f'<header><div class="titles"><h1>🕑 {esc(title)}</h1>{sub}</div>'
            f"{account_chrome(user, csrf)}</header>")
    klass = "card narrow" if narrow else "card"
    return page(title, f'<div class="{klass}">{head}{nav(active) if user else ""}'
                       f'<div class="body">{body}</div></div>', lang=lang)


def note(text: str, kind: str = "") -> str:
    return f'<div class="note {esc(kind)}">{esc(text)}</div>' if text else ""


def landing(*, policy_line: str, configured: bool, error: str = "") -> str:
    if not configured:
        body = (
            note("This deployment has no GitHub OAuth app configured yet, so nobody can "
                 "sign in.", "bad")
            + "<p>Set <code>GITHUB_OAUTH_CLIENT_ID</code>, "
              "<code>GITHUB_OAUTH_CLIENT_SECRET</code>, <code>PUBLIC_URL</code> and "
              "<code>SECRET_KEY</code>, then restart. The README has the five-minute "
              "version, including the callback URL to register.</p>")
        return page("Timesheet", f'<div class="card narrow"><header><div class="titles">'
                                 f"<h1>🕑 Timesheet</h1></div></header>"
                                 f'<div class="body">{body}</div></div>')

    body = (
        note(error, "bad")
        + "<p>This builds your timesheet out of what you already did: the meetings in "
          "your calendar, the mail you sent and received, your chat messages, and your "
          "commits, pull requests, issues and reviews on GitHub.</p>"
          "<p>Sign in with GitHub, connect your Microsoft account, and tell it who to "
          "send the week to. Nothing is shared with anyone you don't name.</p>"
        + f'<p class="hint">Who can sign in here: {esc(policy_line)}.</p>'
          '<div class="actions">'
          '<a class="btn primary" href="/auth/login">Sign in with GitHub</a></div>')
    return page("Timesheet", '<div class="card narrow"><header><div class="titles">'
                             "<h1>🕑 Timesheet</h1>"
                             '<div class="sub">Your week, reconstructed</div></div></header>'
                             f'<div class="body">{body}</div></div>')


def _dot(ok: bool) -> str:
    return f'<span class="dot {"on" if ok else "off"}"></span>'


def connections(user: User, *, csrf: str, github_account: str, microsoft: dict | None,
                can_store: bool, delivery_line: str, message: str = "",
                error: str = "", delivery_blocked: str = "", mail_server: str = "",
                test_to: str = "") -> str:
    ms_ok = bool(microsoft)
    ms_line = (f"Connected as {microsoft.get('account') or 'your Microsoft account'}"
               if ms_ok else "Not connected")

    ms_actions = (
        f'<form method="post" action="/connect/microsoft/disconnect">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        f'<button class="btn" type="submit">Disconnect</button></form>'
        if ms_ok else
        f'<form method="post" action="/connect/microsoft">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        f'<button class="btn primary" type="submit"'
        f'{"" if can_store else " disabled"}>Connect Microsoft</button></form>')

    key_warning = (
        "" if can_store else
        note("SECRET_KEY is not set (or is too short), so this deployment cannot store "
             "credentials safely. Connecting Microsoft is disabled until it is.", "bad"))

    body = (
        note(message, "good") + note(error, "bad") + key_warning
        + "<h3 style='margin:0 0 6px'>GitHub</h3>"
          f'<p class="status">{_dot(True)} Connected as {esc(github_account)} '
          f'on {esc(user.host)}</p>'
          '<p class="hint">Used for your commits, pull requests, issues and reviews. This '
          "is the account you signed in with.</p>"
          "<h3 style='margin:22px 0 6px'>Microsoft 365</h3>"
          f'<p class="status">{_dot(ms_ok)} {esc(ms_line)}</p>'
          '<p class="hint">Read-only, delegated access to your own calendar, mail and '
          "chats — exactly what you can already open yourself. Sign-in is a device code, "
          "so it needs no admin and no app registration in your tenant.</p>"
          f'<div class="actions">{ms_actions}</div>'
          "<h3 style='margin:22px 0 6px'>Delivery</h3>"
          f'<p class="status">'
          f'{_dot(delivery_line.startswith("Sends") and not delivery_blocked)} '
          f'{esc(delivery_line)}</p>'
          # A channel that is switched on but cannot work must not read as
          # healthy; the failure would otherwise only surface on the button.
          + (note(f"This will not send: {delivery_blocked}.", "bad")
             if delivery_blocked else "")
          + f'<p class="hint">Mail server: {esc(mail_server)}. Change the recipient and '
          "the channel under Settings. Nothing leaves here on a schedule until you tick "
          "<em>Send my week automatically</em>; the buttons below send once, when you "
          "press them.</p>"
          '<div class="actions">'
          f'<form method="post" action="/deliver">'
          f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
          f'<input type="hidden" name="period" value="this-week">'
          f'<button class="btn" type="submit">Send this week now</button></form>'
          f'<form method="post" action="/deliver/check">'
          f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
          f'<button class="btn" type="submit">Check the mail server</button></form>'
          "</div>"
          # A test goes to the account holder, never to the manager: the point is
          # to find out whether delivery works without anyone else finding out
          # that you were unsure.
          "<h3 style='margin:22px 0 6px'>Test the send</h3>"
          '<p class="hint">Sends a real message through the real mail server to you, '
          "with this week's sheet attached. Your manager never sees it.</p>"
          f'<form method="post" action="/deliver/test" class="actions">'
          f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
          f'<input type="email" name="to" value="{esc(test_to)}" '
          'placeholder="your own address" style="max-width:260px">'
          '<button class="btn" type="submit">Send a test to me</button></form>')
    return shell("Connections", user, "/connections", body, csrf=csrf,
                 subtitle="What this account is linked to", lang=user.locale, narrow=True)


def device_code(user: User, *, csrf: str, user_code: str, verification_uri: str,
                expires_in: int, interval: int = 5) -> str:
    body = (
        "<p>Open the link below, enter this code, and approve the sign-in. This page "
        "finishes on its own once you're done.</p>"
        f'<p><span class="code">{esc(user_code)}</span></p>'
        f'<div class="actions"><a class="btn primary" href="{esc(verification_uri)}" '
        'target="_blank" rel="noopener noreferrer">Open Microsoft sign-in</a>'
        f'<form method="post" action="/connect/microsoft/finish">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        '<button class="btn" type="submit">I have done it</button></form></div>'
        f'<p class="hint" id="s">The code expires in about {max(1, expires_in // 60)} '
        "minutes.</p>"
        "<script>"
        f"(function(){{var n=0;var t=setInterval(function(){{n++;if(n>120){{clearInterval(t);"
        "return}}fetch('/connect/microsoft/poll',{{headers:{{'Accept':'application/json'}}}})"
        ".then(function(r){{return r.json()}}).then(function(d){{"
        "if(d.done){{clearInterval(t);location.href='/connections'}}"
        "else if(d.error){{clearInterval(t);"
        "document.getElementById('s').textContent=d.error}}}})"
        f".catch(function(){{}})}}, {max(2000, interval * 1000)})}})();"
        "</script>")
    return shell("Connect Microsoft", user, "/connections", body, csrf=csrf,
                 subtitle="One code, once", lang=user.locale, narrow=True)


def _field(name: str, label: str, value, hint: str = "",
           choices: tuple[str, ...] | None = None) -> str:
    """One labelled control, wrapped so a `.row` lays out fields and not fragments.

    `choices` narrows a choice field to what this deployment actually offers. The
    stored value is always included even when it is no longer on offer, so a
    setting somebody saved before a channel was switched off is shown to them
    instead of silently reading as something they never chose.
    """
    spec = SETTINGS_BY_NAME[name]
    fid = f"f-{name}"
    h = f'<div class="hint">{esc(hint)}</div>' if hint else ""

    if spec.kind == "bool":
        checked = " checked" if value else ""
        return (f'<div class="f check"><input type="checkbox" id="{fid}" '
                f'name="{esc(name)}" value="1"{checked}>'
                f'<label for="{fid}">{esc(label)}</label></div>{h}')

    if spec.kind in ("locale", "choice"):
        allowed = spec.choices if choices is None else tuple(
            dict.fromkeys([*choices, *([value] if value in spec.choices else [])]))
        options = i18n.choices() if spec.kind == "locale" else [(c, c) for c in allowed]
        opts = "".join(
            f'<option value="{esc(code)}"{" selected" if code == value else ""}>'
            f"{esc(display)}</option>"
            for code, display in options)
        control = f'<select id="{fid}" name="{esc(name)}">{opts}</select>'
    elif spec.kind == "days":
        text = ",".join(str(d) for d in (value or ()))
        control = f'<input type="text" id="{fid}" name="{esc(name)}" value="{esc(text)}">'
    else:
        kind = {"int": "number", "email": "email"}.get(spec.kind, "text")
        bounds = f' min="{spec.low}" max="{spec.high}"' if spec.kind == "int" else ""
        control = (f'<input type="{kind}" id="{fid}" name="{esc(name)}" '
                   f'value="{esc(value)}"{bounds}>')

    return f'<div class="f"><label for="{fid}">{esc(label)}</label>{control}{h}</div>'


def settings(user: User, *, csrf: str, message: str = "", errors: list[str] | None = None,
             channels: tuple[str, ...] | None = None) -> str:
    s = user.settings
    problems = "".join(note(e, "bad") for e in (errors or []))
    chat_hint = ("" if channels is None or "chat" in channels else
                 "Teams delivery is switched off on this deployment — the connector is "
                 "read-only, so it cannot send a message on your behalf.")
    body = (
        note(message, "good") + problems
        + '<form method="post" action="/settings">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'

        "<h3 style='margin:0 0 2px'>Presentation</h3>"
        '<div class="row">'
        + _field("locale", "Language", s.get("locale"),
                 "The language of the sheet your manager reads.")
        + _field("tz", "Time zone", s.get("tz"),
                 "An IANA name such as Europe/Amsterdam or Africa/Johannesburg.")
        + "</div>"

        "<h3 style='margin:24px 0 2px'>Your working day</h3>"
        '<div class="row">'
        + _field("day_start", "Normal start", s.get("day_start"),
                 "The day opens earlier if your own activity shows it did.")
        + _field("earliest_start_floor", "Never before", s.get("earliest_start_floor"))
        + "</div><div class='row'>"
        + _field("min_day_minutes", "Minimum day (minutes)", s.get("min_day_minutes"),
                 "480 is a normal eight-hour day. A busy day is allowed to run longer.")
        + _field("day_rollover_hour", "Past-midnight cutoff (hour)",
                 s.get("day_rollover_hour"),
                 "Work before this hour counts towards the previous day.")
        + "</div>"
        + _field("workdays", "Working days", s.get("workdays"),
                 "Comma-separated, 0 = Monday. Only these get the minimum-day floor.")
        + _field("rota_enabled", "I have a fixed morning on-call slot",
                 s.get("rota_enabled"))
        + _field("rota_minutes", "On-call minutes", s.get("rota_minutes"))

        + "<h3 style='margin:24px 0 2px'>What counts as time</h3>"
        + _field("include_email", "Email I sent and received", s.get("include_email"))
        + _field("include_chat", "Teams messages I sent", s.get("include_chat"))
        + _field("include_authored", "Pull requests I opened and issues I filed",
                 s.get("include_authored"))
        + _field("include_reviews", "Code reviews I submitted", s.get("include_reviews"))

        + "<h3 style='margin:24px 0 2px'>GitHub</h3>"
        '<div class="row">'
        + _field("github_host", "Host", s.get("github_host"),
                 "github.com, or your Enterprise Server hostname.")
        + _field("github_user", "Username", s.get("github_user"))
        + "</div>"

        + "<h3 style='margin:24px 0 2px'>Who gets the week</h3>"
        '<div class="row">'
        + _field("manager_name", "Manager's name", s.get("manager_name"))
        + _field("manager_email", "Manager's email", s.get("manager_email"))
        + "</div><div class='row'>"
        + _field("manager_chat", "Manager's Teams address", s.get("manager_chat"),
                 "Leave blank to use the email address above.")
        + _field("delivery_channel", "Channel", s.get("delivery_channel"),
                 chat_hint, choices=channels)
        + "</div>"
        + _field("delivery_enabled", "Send my week automatically",
                 s.get("delivery_enabled"),
                 "On a schedule. You can always send by hand from the timesheet.")

        + '<div class="actions"><button class="btn primary" type="submit">Save</button>'
          '<a class="btn" href="/connections">Connections</a></div>'
          "</form>"

        + "<h3 style='margin:28px 0 2px'>Delete this account</h3>"
          '<p class="hint">Removes your settings, your stored weeks and your Microsoft '
          "token from this deployment. It does not touch anything in GitHub or "
          "Microsoft.</p>"
          f'<form method="post" action="/account/delete" class="actions">'
          f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
          '<input type="text" name="confirm" placeholder="type DELETE to confirm" '
          'style="max-width:230px">'
          '<button class="btn" type="submit">Delete</button></form>')
    return shell("Settings", user, "/settings", body, csrf=csrf,
                 subtitle="Yours only — nobody else's sheet changes",
                 lang=user.locale)
