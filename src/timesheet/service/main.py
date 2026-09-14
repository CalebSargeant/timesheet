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
  POST /deliver               send this week to the configured manager, now
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
from datetime import date, datetime, timedelta
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
from ..pipeline import Sources, build_from_ingest, collect_week
from ..reconstruct import logical_date
from ..render import html as render_html
from ..render import xlsx as render_xlsx
from ..render.theme import esc
from . import auth, crypto, delivery, mcp_connect, security, users, web
from .store import GITHUB, MICROSOFT, make_store

log = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

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
            cred = _store.get_credential(user.id, GITHUB)
            token = (cred or {}).get("access_token", "")
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


def _week_days(user: users.User, cfg: Config, monday: date) -> list[Day]:
    """Days for one week. Past weeks come from the store (reconstructed + cached on a
    miss). The current (in-progress) week is always rebuilt live and never cached:
    today grows through the day and future days must stay hidden, so a frozen
    snapshot would be wrong."""
    sources = _sources_for(user)
    if monday >= _current_monday(cfg):
        try:
            return collect_week(monday, cfg, sources)
        except Exception:
            log.warning("live week %s failed for %s", monday, user.login, exc_info=True)
            return []
    cached = _store.get_days(user.id, monday)
    if cached is not None:
        return cached
    try:
        days = collect_week(monday, cfg, sources)
    except Exception:
        log.warning("week %s failed for %s", monday, user.login, exc_info=True)
        return []
    _store.save(user.id, monday, {}, days)
    return days


def _period_days(user: users.User, cfg: Config, period: Period) -> list[Day]:
    out: list[Day] = []
    for monday in mondays_covering(period.start, period.end):
        out.extend(_week_days(user, cfg, monday))
    out = [d for d in out if period.start <= d.date.date() <= period.end]
    out.sort(key=lambda d: d.date)
    return out


def _resolve(cfg: Config, period: str, frm: str | None, to: str | None) -> Period:
    f, t = parse_date(frm), parse_date(to)
    if f and t:
        return custom_period(f, t, cfg.locale)
    return resolve_period(period, _today(cfg), cfg.locale)


def _guard(user: users.User, csrf: str | None) -> None:
    if not auth.check_csrf(user.id, csrf):
        raise HTTPException(status_code=403, detail="stale form — reload the page")


def _redirect(path: str, *, status: int = 303) -> RedirectResponse:
    return RedirectResponse(path, status_code=status)


# --- sign in ---------------------------------------------------------------


@app.get("/auth/login")
def login(next: str = Query(default="/")):
    if not _provider.configured:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")
    return _redirect(_provider.authorize_url(auth.make_state(next)), status=307)


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
        token = auth.exchange(_provider, code)
        profile, email, orgs = auth.identify(_provider, token)
        user = auth.upsert(_store, _provider, _policy, profile, email, orgs, tz=_env_cfg.tz)
    except auth.NotAllowed as e:
        return HTMLResponse(web.landing(policy_line=_policy.describe(), configured=True,
                                        error=str(e)), status_code=403)
    except auth.AuthError as e:
        return HTMLResponse(web.landing(policy_line=_policy.describe(), configured=True,
                                        error=str(e)), status_code=502)

    # The OAuth token reads the user's own commits, PRs, issues and reviews — and
    # on a private-repo scope that is real access, so it is stored the same way
    # the Microsoft one is.
    try:
        _store.put_credential(user.id, GITHUB, {"access_token": token}, account=user.login)
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
          ts_session: str | None = Cookie(default=None)):
    user = _current_user(ts_session)
    if user is None:
        return HTMLResponse(web.landing(policy_line=_policy.describe(),
                                        configured=_provider.configured))
    cfg = _config_for(user)
    p = _resolve(cfg, period, frm, to)
    days = _period_days(user, cfg, p)
    dl = (f"timesheet.xlsx?from={p.start.isoformat()}&to={p.end.isoformat()}"
          if p.key == "custom" else f"timesheet.xlsx?period={p.key}")
    generated = (_store.latest_meta(user.id).get("generated_at", "") or "")[:16].replace("T", " ")
    return HTMLResponse(render_html.build_week(
        days, locale=cfg.strings, subtitle=_subtitle(p, days), download_url=dl,
        nav_html=_nav_html(cfg, p.key if p.key in PERIOD_KEYS else "", p.start, p.end),
        generated=generated or None,
        account_html=web.account_chrome(user, auth.csrf_token(user.id))))


def _period_xlsx(user: users.User, cfg: Config, period: Period) -> Response:
    days = _period_days(user, cfg, period)
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
    channel, target = delivery.target_for(user)
    if channel == "none":
        return "Nothing is sent automatically"
    return f"Sends by {channel} to {target or 'nobody — no recipient set'}"


@app.get("/connections", response_class=HTMLResponse)
def connections(ts_session: str | None = Cookie(default=None),
                message: str = Query(default=""), error: str = Query(default="")):
    user = _require(ts_session)
    meta = None
    with contextlib.suppress(crypto.CryptoUnavailable):
        meta = _store.credential_meta(user.id, MICROSOFT)
    channel, _ = delivery.target_for(user)
    blocked = delivery.unavailable(channel, session=_sources_for(user).m365)
    return HTMLResponse(web.connections(
        user, csrf=auth.csrf_token(user.id), github_account=user.login, microsoft=meta,
        can_store=crypto.available(), delivery_line=_delivery_line(user),
        message=message[:200], error=error[:200], delivery_blocked=blocked))


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
        return _redirect(f"/connections?error={esc(str(e)[:120])}")
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
        return _redirect(f"/connections?error={esc(str(e)[:120])}")
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
                                     message=message[:200]))


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
                                         message="Saved, apart from:", errors=errors),
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


@app.post("/deliver")
def deliver_now(csrf: str = Form(default=""), ts_session: str | None = Cookie(default=None)):
    user = _require(ts_session)
    _guard(user, csrf)
    cfg = _config_for(user)
    monday = _current_monday(cfg)
    days = _week_days(user, cfg, monday)
    if not days:
        return _redirect("/connections?error=Nothing+to+send+for+this+week+yet")
    meta = _store.save(user.id, monday, {}, days)
    result = delivery.send(user, days, meta, session=_sources_for(user).m365,
                           public_url=_public_url)
    key = "message" if result.sent else "error"
    return _redirect(f"/connections?{key}={esc(result.describe())}")


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
    user = _require(ts_session)
    return {"ok": True, "account": user.login, **_store.latest_meta(user.id)}


@app.get("/healthz")
def healthz():
    # Liveness/readiness: cheap and DB-free. A DB call here turns a slow database
    # into a crash-loop, which is precisely when you need the pod to stay up.
    return {"ok": True}
