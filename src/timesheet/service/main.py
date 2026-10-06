"""The web service: sign in with GitHub, connect Microsoft, see and send your week.

Routes:
  GET  /                      the live timesheet (or the landing page)
  GET  /auth/login            start GitHub OAuth      GET /auth/callback   finish it
  POST /auth/logout
  GET  /connections           what this account is linked to
  POST /connect/microsoft     start the device-code sign-in
  GET  /connect/microsoft/poll        JSON: has the human finished yet?
  POST /connect/microsoft/finish      the no-JavaScript version of the same
  POST /connect/microsoft/disconnect
  GET/POST /settings          the account's own reconstruction settings
  POST /deliver               send the period on screen to the configured manager
  POST /deliver/test          send a test message to the signed-in account
  POST /deliver/check         ask the mail server if it would take one
  POST /account/delete
  GET  /timesheet.xlsx        download the selected period
  GET  /d/{uid}/{token}/timesheet.xlsx    signed, time-limited (the emailed link)
  POST /api/ingest            pushed calendar/mail/chat JSON (X-Ingest-Token)
  GET  /status                this account's last refresh   GET /healthz  liveness

Every data route resolves the signed-in user first and passes that user's id into
the store. There is no route that reads a week without one.
"""
from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import Cookie, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..collectors.github import GitHub
from ..collectors.mcp_client import McpAuthError, McpError, McpSession
from ..config import Config
from ..model import Day
from ..periods import (
    PERIOD_KEYS,
    Period,
    custom_period,
    mondays_covering,
    parse_date,
    resolve_period,
)
from ..pipeline import Sources, WeekBuild, build_from_ingest, collect
from ..reconstruct import logical_date
from ..render import html as render_html
from ..render import xlsx as render_xlsx
from ..render.theme import esc
from ..timeutil import hm
from . import auth, crypto, delivery, mcp_connect, security, users, web
from .store import GITHUB, MICROSOFT, make_store

log = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# How long the scheduled refresh's copy of the in-progress week is served before
# the page rebuilds it live. It is the page's staleness bound, so it wants to be
# about the refresh interval: shorter and every view pays for a live rebuild that
# the CronJob was about to do anyway; much longer and "this week" lags behind the
# morning it is describing.
LIVE_MAX_AGE = int(os.environ.get("LIVE_MAX_AGE_SECONDS", "900"))

# Where a form may send somebody afterwards. An open redirect is one `next=`
# parameter away in any service that takes one, so the set is closed.
_BACK = ("/", "/connections")

app = FastAPI(title="timesheet", docs_url=None, redoc_url=None)
_env_cfg = Config.from_env()
_store = make_store(tz=_env_cfg.tz)
_provider = auth.Provider.from_env()
_policy = auth.Policy.from_env()
_public_url = os.environ.get("PUBLIC_URL", "http://localhost:8000")
_secure_cookies = _public_url.startswith("https://")


# --- per-request plumbing --------------------------------------------------


def _current_user(cookie: str | None) -> users.User | None:
    """The signed-in account, or None.

    The access policy is re-checked here and not only at sign-in. Dropping
    someone from the allow-list has to end the session they are already holding,
    or the setting is decorative.
    """
    uid = auth.read_session(cookie)
    if not uid:
        return None
    user = _store.get_user(uid)
    if user is None:
        return None
    try:
        auth.check_access(_policy, user.login, uid, existing=True,
                          user_count=_store.count_users(), orgs=[])
    except auth.NotAllowed:
        # An org policy can't be re-checked without calling GitHub on every
        # request, so an org deployment trusts the check made at sign-in for the
        # life of the session. Every other mode is cheap and is enforced here.
        if _policy.mode != "org":
            log.info("session for %s rejected by policy", user.login)
            return None
    return user


def _require(cookie: str | None) -> users.User:
    user = _current_user(cookie)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return user


def _config_for(user: users.User) -> Config:
    """The deployment's config, overlaid with this user's own settings."""
    return _env_cfg.with_settings(users.for_config(user.settings))


def _sources_for(user: users.User) -> Sources:
    """One user's connected accounts, with their own credentials and nobody else's."""
    gh = None
    login = user.setting("github_user") or user.login
    if login:
        token = ""
        try:
            token = auth.github_token(_store, _provider, user.id)
        except crypto.CryptoUnavailable:
            log.warning("cannot read the stored GitHub token for %s", user.login)
        gh = GitHub(user=login, token=token, host=user.setting("github_host") or user.host)

    session = None
    try:
        tokens = _store.get_credential(user.id, MICROSOFT)
    except crypto.CryptoUnavailable:
        tokens = None
    if tokens:
        session = McpSession(
            tokens=tokens,
            # Entra hands back a replacement refresh token on every refresh.
            # Persisting it here is what keeps the connection alive past the
            # first rotation; dropping it would work for exactly one more read.
            on_rotate=lambda fresh, uid=user.id: _store.put_credential(
                uid, MICROSOFT, fresh, account=(_store.credential_meta(uid, MICROSOFT)
                                                or {}).get("account", "")),
        )
    return Sources(github=gh, m365=session)


def _today(cfg: Config) -> date:
    return datetime.now(ZoneInfo(cfg.tz)).date()


def _current_monday(cfg: Config) -> date:
    d = logical_date(datetime.now(ZoneInfo(cfg.tz)), cfg.day_rollover_hour)
    return d - timedelta(days=d.weekday())


def _age_seconds(stamp: str | None) -> float | None:
    """How old a stored week is, or None if it carries no usable timestamp."""
    if not stamp:
        return None
    try:
        built = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if built.tzinfo is None:
        built = built.replace(tzinfo=UTC)
    return (datetime.now(UTC) - built).total_seconds()


def _collect(user: users.User, cfg: Config, monday: date, sources: Sources):
    """A live build that never raises. The reasons come back as `problems`."""
    try:
        return collect(monday, cfg, sources)
    # A collector can fail in as many ways as the network and a third party's
    # schema allow; one bad week must not 500 the whole page.
    except Exception as e:
        log.warning("week %s failed for %s", monday, user.login, exc_info=True)
        return WeekBuild(days=[], problems=(f"could not build this week: {str(e)[:200]}",))


def _current_week(user: users.User, cfg: Config, monday: date,
                  sources: Sources) -> tuple[list[Day], list[str]]:
    """The in-progress week: as fresh as it can be, and never silently blank.

    The scheduled refresh stores this week with the signals a page view cannot
    afford — mail, chat, reviews — so while that copy is fresh it is both richer
    and cheaper than anything this request could build, and it is served as is.
    Once it ages past `LIVE_MAX_AGE` the page rebuilds live (calendar and commits)
    rather than showing yesterday's hours.

    If that rebuild comes back empty the stored copy is served instead, with a
    line saying how old it is. An hour-old week is a worse answer than a live one
    and a far better answer than a blank page, which is what an expired Microsoft
    token used to produce — with nothing on the page to say so.
    """
    stored = _store.get_days(user.id, monday)
    meta = _store.meta_of(user.id, monday) or {}
    age = _age_seconds(meta.get("generated_at"))
    if stored and meta.get("full") and age is not None and age <= LIVE_MAX_AGE:
        return stored, []

    build = _collect(user, cfg, monday, sources)
    if build.days:
        return build.days, list(build.problems)
    if stored:
        when = (meta.get("generated_at", "") or "")[:16].replace("T", " ")
        return stored, [*build.problems,
                        f"Nothing could be read just now — showing the copy stored at {when}."]
    return [], list(build.problems)


def _week_days(user: users.User, cfg: Config, monday: date,
               sources: Sources | None = None) -> tuple[list[Day], list[str]]:
    """Days for one week, and anything that could not be read while building them.

    Past weeks come from the store (reconstructed + cached on a miss). The current
    week is handled separately: it grows through the day, so it is either fresh or
    rebuilt, never frozen.
    """
    sources = sources if sources is not None else _sources_for(user)
    if monday >= _current_monday(cfg):
        return _current_week(user, cfg, monday, sources)
    cached = _store.get_days(user.id, monday)
    if cached is not None:
        return cached, []
    build = _collect(user, cfg, monday, sources)
    if build.days:
        # Never cache an empty week. Reaching here with nothing usually means the
        # account has not connected anything yet, not that the week was empty —
        # and a cached blank would outlive the connection that fixes it.
        _store.save(user.id, monday, {}, build.days)
    return build.days, list(build.problems)


def _period_days(user: users.User, cfg: Config, period: Period) -> tuple[list[Day], list[str]]:
    out: list[Day] = []
    notes: list[str] = []
    # One set of credentials for the whole period: a month is five weeks, and
    # decrypting the same two tokens five times is five times the work for the
    # same answer.
    sources = _sources_for(user)
    for monday in mondays_covering(period.start, period.end):
        days, problems = _week_days(user, cfg, monday, sources)
        out.extend(days)
        notes.extend(n for n in problems if n not in notes)
    out = [d for d in out if period.start <= d.date.date() <= period.end]
    out.sort(key=lambda d: d.date)
    return out, notes


def _period_query(period: Period) -> str:
    """The query string that names this period on any route that takes one."""
    if period.key == "custom":
        return f"from={period.start.isoformat()}&to={period.end.isoformat()}"
    return f"period={period.key}"


def _period_meta(period: Period, days: list[Day], loc) -> dict:
    """The meta a delivered message is written from.

    `week_start` is a whole Monday-to-Sunday week's Monday where that is what is
    being sent, and the plain date range otherwise — so a month lands in the
    subject line as the month it is, not as "week This month".
    """
    total = sum(d.minutes for d in days)
    whole_week = period.start.weekday() == 0 and (period.end - period.start).days == 6
    label = (period.start.isoformat() if whole_week else
             f"{period.start.isoformat()} {loc.period_to} {period.end.isoformat()}")
    return {"week_start": label, "total_minutes": total, "total_hm": hm(total),
            "days": len(days)}


def _resolve(cfg: Config, period: str, frm: str | None, to: str | None) -> Period:
    f, t = parse_date(frm), parse_date(to)
    if f and t:
        return custom_period(f, t, cfg.locale)
    return resolve_period(period, _today(cfg), cfg.locale)


def _guard(user: users.User, csrf: str | None) -> None:
    if not auth.check_csrf(user.id, csrf):
        raise HTTPException(status_code=403, detail="stale form — reload the page")


def _redirect(path: str, *, status: int = 303) -> RedirectResponse:
    """Every redirect this service issues goes through here, so it is where the
    "never off-site" rule lives. Callers already pass local paths; this makes that
    a property of the service rather than of each caller remembering to.

    `auth.local_path` does the real work. The backslash strip and the scheme/host
    test after it change nothing that function lets through — they are the shape
    CodeQL's url-redirection query recognises as a check, and it cannot see inside
    `local_path`. An alert nobody can clear is one everybody learns to ignore.
    """
    target = auth.local_path(path).replace("\\", "")
    if not urlparse(target).netloc and not urlparse(target).scheme:
        return RedirectResponse(target, status_code=status)
    return RedirectResponse("/", status_code=status)


# --- sign in ---------------------------------------------------------------


@app.get("/auth/login")
def login(next: str = Query(default="/")):
    if not _provider.configured:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")
    # The one redirect that must leave this host, to GitHub's own authorize page.
    # Built from deployment config, never from the request.
    return RedirectResponse(_provider.authorize_url(auth.make_state(next)), status_code=307)


@app.get("/auth/callback")
def callback(code: str = Query(default=""), state: str = Query(default="")):
    return_to = auth.read_state(state)
    if return_to is None:
        return HTMLResponse(web.landing(
            policy_line=_policy.describe(), configured=_provider.configured,
            error="That sign-in link expired or was not issued here. Try again."),
            status_code=400)
    if not code:
        return _redirect("/")
    try:
        tokens = auth.exchange(_provider, code)
        profile, email, orgs = auth.identify(_provider, tokens["access_token"])
        user = auth.upsert(_store, _provider, _policy, profile, email, orgs, tz=_env_cfg.tz)
    except auth.NotAllowed as e:
        return HTMLResponse(web.landing(policy_line=_policy.describe(), configured=True,
                                        error=str(e)), status_code=403)
    except auth.AuthError as e:
        return HTMLResponse(web.landing(policy_line=_policy.describe(), configured=True,
                                        error=str(e)), status_code=502)

    # The OAuth token reads the user's own commits, PRs, issues and reviews — and
    # on a private-repo scope that is real access, so it is stored the same way
    # the Microsoft one is. The whole set, refresh token included: without it an
    # expiring token leaves the account with no GitHub eight hours from now.
    try:
        _store.put_credential(user.id, GITHUB, tokens, account=user.login)
    except crypto.CryptoUnavailable as e:
        log.error("cannot store the GitHub token: %s", e)

    response = _redirect(return_to)
    response.headers["Set-Cookie"] = auth.cookie_header(auth.make_session(user.id),
                                                        secure=_secure_cookies)
    return response


@app.post("/auth/logout")
def logout(csrf: str = Form(default=""), ts_session: str | None = Cookie(default=None)):
    user = _current_user(ts_session)
    if user is not None:
        _guard(user, csrf)
    response = _redirect("/")
    response.headers["Set-Cookie"] = auth.clear_cookie(secure=_secure_cookies)
    return response


# --- the timesheet ---------------------------------------------------------


def _nav_html(cfg: Config, active: str, start, end) -> str:
    today = _today(cfg)
    loc = cfg.strings
    pills = "".join(
        f'<a class="{"pill active" if k == active else "pill"}" href="/?period={k}">'
        f"{esc(resolve_period(k, today, loc).label)}</a>"
        for k in PERIOD_KEYS)
    form = (
        '<form class="range" method="get" action="/">'
        f'<input type="date" name="from" value="{start.isoformat()}">'
        f'<span class="sep">{esc(loc.period_to)}</span>'
        f'<input type="date" name="to" value="{end.isoformat()}">'
        f'<button class="pill go" type="submit">{esc(loc.show)}</button>'
        "</form>")
    settings_link = ('<a class="pill" href="/connections">Connections</a>'
                     '<a class="pill" href="/settings">Settings</a>')
    return pills + settings_link + form


def _subtitle(period: Period, days: list[Day]) -> str:
    if not days:
        return period.label
    a, b = days[0].date, days[-1].date
    return f"{period.label} · {a.day:02d}-{a.month:02d} to {b.day:02d}-{b.month:02d}"


@app.get("/", response_class=HTMLResponse)
def index(period: str = Query(default="this-week"),
          frm: str | None = Query(default=None, alias="from"),
          to: str | None = Query(default=None),
          message: str = Query(default=""), error: str = Query(default=""),
          ts_session: str | None = Cookie(default=None)):
    user = _current_user(ts_session)
    if user is None:
        return HTMLResponse(web.landing(policy_line=_policy.describe(),
                                        configured=_provider.configured))
    cfg = _config_for(user)
    p = _resolve(cfg, period, frm, to)
    days, notes = _period_days(user, cfg, p)
    query = _period_query(p)
    generated = (_store.latest_meta(user.id).get("generated_at", "") or "")[:16].replace("T", " ")
    channel, target = delivery.target_for(user, manual=True)
    return HTMLResponse(render_html.build_week(
        days, locale=cfg.strings, subtitle=_subtitle(p, days),
        download_url=f"timesheet.xlsx?{query}",
        nav_html=_nav_html(cfg, p.key if p.key in PERIOD_KEYS else "", p.start, p.end),
        generated=generated or None,
        # What is on screen is what gets sent: the button carries the period the
        # page is showing, so "send this" means this and not "send this week".
        send_html=(web.send_form(query, csrf=auth.csrf_token(user.id), target=target)
                   if channel != "none" else ""),
        notes=[message[:500]] if message else [],
        bad_notes=[*notes, *([error[:500]] if error else [])],
        account_html=web.account_chrome(user, auth.csrf_token(user.id))))


def _period_xlsx(user: users.User, cfg: Config, period: Period) -> Response:
    days, _ = _period_days(user, cfg, period)
    if not days:
        raise HTTPException(status_code=404, detail="no timesheet for this period")
    name = f"timesheet-{period.start.isoformat()}_{period.end.isoformat()}.xlsx"
    return Response(render_xlsx.build_week(days, locale=cfg.strings), media_type=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/timesheet.xlsx")
def download(period: str = Query(default="this-week"),
             frm: str | None = Query(default=None, alias="from"),
             to: str | None = Query(default=None),
             ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    cfg = _config_for(user)
    return _period_xlsx(user, cfg, _resolve(cfg, period, frm, to))


@app.get("/d/{uid}/{token}/timesheet.xlsx")
def signed_download(uid: str, token: str, period: str = Query(default="this-week"),
                    frm: str | None = Query(default=None, alias="from"),
                    to: str | None = Query(default=None)):
    """The emailed link. No session: the signature names the account, so a valid
    link for one person cannot be pointed at another's week by editing the id."""
    if not security.verify_download(uid, "timesheet.xlsx", token):
        raise HTTPException(status_code=403, detail="link expired or invalid")
    user = _store.get_user(uid)
    if user is None:
        raise HTTPException(status_code=404, detail="no such account")
    cfg = _config_for(user)
    return _period_xlsx(user, cfg, _resolve(cfg, period, frm, to))


# --- connections -----------------------------------------------------------


def _delivery_line(user: users.User) -> str:
    """What this account does with its finished week, in one sentence.

    Automatic and manual are said separately because they are different: a
    recipient with the switch off means the Send button works and nothing leaves
    here on its own, and reading that as "nothing is sent" sent people looking
    for a broken scheduler.
    """
    channel, target = delivery.target_for(user, manual=True)
    if channel == "none":
        return "No channel chosen — nothing is sent"
    who = target or "nobody — no recipient set"
    if not user.setting("delivery_enabled"):
        return f"Sends by {channel} to {who} when you press Send (never on its own)"
    return f"Sends by {channel} to {who}"


@app.get("/connections", response_class=HTMLResponse)
def connections(ts_session: str | None = Cookie(default=None),
                message: str = Query(default=""), error: str = Query(default="")):
    user = _require(ts_session)
    meta = None
    with contextlib.suppress(crypto.CryptoUnavailable):
        meta = _store.credential_meta(user.id, MICROSOFT)
    channel, _ = delivery.target_for(user, manual=True)
    sources = _sources_for(user)
    blocked = delivery.unavailable(channel, session=sources.m365)
    github_problem = auth.github_problem(_provider,
                                         sources.github.token if sources.github else "")
    return HTMLResponse(web.connections(
        user, csrf=auth.csrf_token(user.id), github_account=user.login, microsoft=meta,
        github_problem=github_problem,
        can_store=crypto.available(), delivery_line=_delivery_line(user),
        message=message[:500], error=error[:500], delivery_blocked=blocked,
        mail_server=delivery.mail_server_line(), test_to=user.email))


@app.post("/connect/microsoft", response_class=HTMLResponse)
def connect_microsoft(csrf: str = Form(default=""),
                      ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    _guard(user, csrf)
    if not crypto.available():
        return _redirect("/connections?error=SECRET_KEY+is+not+configured")
    try:
        pending = mcp_connect.start(_store, user.id)
    except McpError as e:
        return _redirect(f"/connections?error={quote(str(e)[:300], safe='')}")
    return HTMLResponse(web.device_code(user, csrf=auth.csrf_token(user.id),
                                        **pending.public()))


@app.get("/connect/microsoft/poll")
def poll_microsoft(ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    try:
        done = mcp_connect.finish(_store, user.id)
    except McpAuthError as e:
        return JSONResponse({"done": False, "error": str(e)[:200]})
    except McpError as e:
        return JSONResponse({"done": False, "error": str(e)[:200]})
    return JSONResponse({"done": bool(done)})


@app.post("/connect/microsoft/finish")
def finish_microsoft(csrf: str = Form(default=""),
                     ts_session: str | None = Cookie(default=None)):
    """The no-JavaScript path: the user says they're done, we check once."""
    user = _require(ts_session)
    _guard(user, csrf)
    try:
        if mcp_connect.finish(_store, user.id):
            return _redirect("/connections?message=Microsoft+connected")
    except McpError as e:
        return _redirect(f"/connections?error={quote(str(e)[:300], safe='')}")
    return _redirect("/connections?error=Not+approved+yet+-+try+again")


@app.post("/connect/microsoft/disconnect")
def disconnect_microsoft(csrf: str = Form(default=""),
                         ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    _guard(user, csrf)
    _store.drop_credential(user.id, MICROSOFT)
    mcp_connect.cancel(_store, user.id)
    return _redirect("/connections?message=Microsoft+disconnected")


# --- settings --------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(ts_session: str | None = Cookie(default=None),
                  message: str = Query(default="")):
    user = _require(ts_session)
    return HTMLResponse(web.settings(user, csrf=auth.csrf_token(user.id),
                                     message=message[:200],
                                     channels=delivery.channels()))


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    form = await request.form()
    _guard(user, form.get("csrf"))

    submitted = {k: v for k, v in form.items() if k in users.SETTINGS_BY_NAME}
    # An unticked checkbox is simply absent from a form post, so every boolean
    # the page could have carried has to be read as false when it is missing.
    for spec in users.SETTINGS:
        if spec.kind == "bool" and spec.name not in submitted:
            submitted[spec.name] = False

    values, errors = users.clean(submitted)
    user.settings = {**user.settings, **values}
    _store.save_user(user)
    if errors:
        return HTMLResponse(web.settings(user, csrf=auth.csrf_token(user.id),
                                         message="Saved, apart from:", errors=errors,
                                         channels=delivery.channels()),
                            status_code=400)
    return _redirect("/settings?message=Saved")


@app.post("/account/delete")
def delete_account(csrf: str = Form(default=""), confirm: str = Form(default=""),
                   ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    _guard(user, csrf)
    if confirm.strip().upper() != "DELETE":
        return _redirect("/settings?message=Type+DELETE+to+confirm")
    _store.delete_user(user.id)
    response = _redirect("/")
    response.headers["Set-Cookie"] = auth.clear_cookie(secure=_secure_cookies)
    return response


# --- delivery --------------------------------------------------------------


def _back_url(back: str, query: str, key: str, text: str) -> str:
    """Where a send returns to, carrying its own outcome. Never anywhere else."""
    where = back if back in _BACK else "/connections"
    params = f"{query}&" if (query and where == "/") else ""
    return f"{where}?{params}{key}={quote(text, safe='')}"


@app.post("/deliver")
def deliver_now(csrf: str = Form(default=""), period: str = Form(default="this-week"),
                frm: str | None = Form(default=None, alias="from"),
                to: str | None = Form(default=None), back: str = Form(default="/connections"),
                ts_session: str | None = Cookie(default=None)):
    """Send what the sender is looking at, once, because they asked.

    The period comes from the form, so this sends the range on screen — a past
    week, a month, a custom span — rather than always this week, which was the
    only thing it could ever send.
    """
    user = _require(ts_session)
    _guard(user, csrf)
    cfg = _config_for(user)
    p = _resolve(cfg, period, frm, to)
    days, _ = _period_days(user, cfg, p)
    query = _period_query(p)
    if not days:
        return _redirect(_back_url(back, query, "error",
                                   f"Nothing to send for {p.label.lower()} yet"))
    meta = _period_meta(p, days, cfg.strings)
    result = delivery.send(user, days, meta, session=_sources_for(user).m365,
                           public_url=_public_url, manual=True, period=query)
    # quote(), not esc(): this lands in a QUERY STRING. HTML-escaping turns the
    # quotes in a connector's JSON error into &quot;, and the & then starts a new
    # parameter — every error was being cut off at its first quote, which is how
    # `FORBIDDEN: Missing scope ChatMessage.Send` reached the page as
    # "teams_create_chat: {".
    key = "message" if result.sent else "error"
    return _redirect(_back_url(back, query, key, result.describe()))


@app.post("/deliver/test")
def deliver_test(csrf: str = Form(default=""), to: str = Form(default=""),
                 ts_session: str | None = Cookie(default=None)):
    """Prove the mail path works — to the account holder, and to nobody else.

    A manager should never receive a test, so this one never asks where to send:
    it goes to the signed-in person's own address.
    """
    user = _require(ts_session)
    _guard(user, csrf)
    cfg = _config_for(user)
    days, _ = _period_days(user, cfg, _resolve(cfg, "this-week", None, None))
    xlsx = render_xlsx.build_week(days, locale=cfg.strings) if days else b""
    result = delivery.send_test(user, to=to.strip(), public_url=_public_url, xlsx=xlsx)
    key = "message" if result.sent else "error"
    text = (f"test message sent to {result.target} — check that it arrives"
            if result.sent else result.describe())
    return _redirect(f"/connections?{key}={quote(text, safe='')}")


@app.post("/deliver/check")
def deliver_check(csrf: str = Form(default=""),
                  ts_session: str | None = Cookie(default=None)):
    """Ask the mail server whether it would take a message, without sending one."""
    user = _require(ts_session)
    _guard(user, csrf)
    why = delivery.check_email()
    key = "error" if why else "message"
    text = why or "the mail server accepted the connection and the credentials"
    return _redirect(f"/connections?{key}={quote(text, safe='')}")


# --- pushed ingest ---------------------------------------------------------


@app.post("/api/ingest")
async def ingest(request: Request,
                 x_ingest_token: str | None = Header(default=None),
                 x_account: str | None = Header(default=None)):
    """Calendar, mail and chat pushed in by an external automation.

    Still here for tenants that offer no other way in. The token is per account
    and derived from the server secret, so one leaked automation cannot write
    into everybody's timesheet.
    """
    if not x_account or not security.check_ingest_token(x_account, x_ingest_token):
        raise HTTPException(status_code=401, detail="bad or missing X-Account/X-Ingest-Token")
    user = _store.get_user(x_account)
    if user is None:
        raise HTTPException(status_code=404, detail="no such account")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    cfg = _config_for(user)
    monday, days = build_from_ingest(payload, cfg, _sources_for(user))
    meta = _store.save(user.id, monday, payload, days)
    return JSONResponse({"ok": True, **meta})


@app.get("/status")
def status(ts_session: str | None = Cookie(default=None)):
    """This account's last refresh, and whether the current week is keeping up.

    Reads the store and nothing else: the question "is the refresh job running?"
    must be answerable without doing the refresh job's work in the request.
    """
    user = _require(ts_session)
    monday = _current_monday(_config_for(user))
    meta = _store.meta_of(user.id, monday) or {}
    age = _age_seconds(meta.get("generated_at"))
    return {"ok": True, "account": user.login,
            "this_week": {
                "week_start": monday.isoformat(),
                "stored_at": meta.get("generated_at"),
                "age_seconds": int(age) if age is not None else None,
                "full": bool(meta.get("full")),
                "total_hm": meta.get("total_hm"),
                # False means a page view rebuilds it live rather than serving
                # this copy — which is the intended fallback, not a fault.
                "fresh": bool(meta.get("full") and age is not None and age <= LIVE_MAX_AGE),
                "max_age_seconds": LIVE_MAX_AGE,
            },
            **_store.latest_meta(user.id)}


@app.get("/healthz")
def healthz():
    # Liveness/readiness: cheap and DB-free. A DB call here turns a slow database
    # into a crash-loop, which is precisely when you need the pod to stay up.
    return {"ok": True}
