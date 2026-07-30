"""FastAPI service: ingest (Power Automate) -> reconstruct -> live view + download.

Routes:
  GET  /?period=            the live 'Uren' page (this/last week, this/last month)
  GET  /uren.xlsx?period=   download the selected period
  GET  /d/{token}/uren.xlsx signed, time-limited download (emailed link)
  POST /api/ingest          calendar/email/Teams JSON from the flow (X-Ingest-Token)
  GET  /status              last-refresh metadata (DB)   GET /healthz  liveness

A period is assembled from stored weeks; a week not yet cached is reconstructed
live (ICS + GHE) and stored, so GHE is only hit on a cache-miss — never on a plain
page view of already-cached data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

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
from ..pipeline import build_from_ingest, collect_week
from ..reconstruct import logical_date
from ..render import html as render_html
from ..render import xlsx as render_xlsx
from . import security
from .store import make_store

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="github-timesheet", docs_url=None, redoc_url=None)
_cfg = Config.from_env()
_store = make_store(tz=_cfg.tz)


def _today() -> date:
    return datetime.now(ZoneInfo(_cfg.tz)).date()


def _current_monday() -> date:
    d = logical_date(datetime.now(ZoneInfo(_cfg.tz)), _cfg.day_rollover_hour)
    return d - timedelta(days=d.weekday())


def _week_days(monday: date) -> list[Day]:
    """Days for one week. Past weeks come from the store (reconstructed + cached on a
    miss). The current (in-progress) week is always rebuilt live and never cached:
    today grows through the day and future days must stay hidden, so a frozen snapshot
    would be wrong. GHE is hit here, never on a cached page view of a past week."""
    if monday >= _current_monday():
        try:
            return collect_week(monday, _cfg)
        except Exception:  # noqa: BLE001 — one bad week must not 500 the whole page
            return []
    cached = _store.get_days(monday)
    if cached is not None:
        return cached
    try:
        days = collect_week(monday, _cfg)
    except Exception:  # noqa: BLE001 — one bad week must not 500 the whole page
        return []
    _store.save(monday, {}, days)
    return days


def _period_days(period: Period) -> list[Day]:
    out: list[Day] = []
    for monday in mondays_covering(period.start, period.end):
        out.extend(_week_days(monday))
    out = [d for d in out if period.start <= d.date.date() <= period.end]
    out.sort(key=lambda d: d.date)
    return out


def _resolve(period: str, frm: str | None, to: str | None) -> Period:
    f, t = parse_date(frm), parse_date(to)
    if f and t:
        return custom_period(f, t)
    return resolve_period(period, _today())


def _nav_html(active: str, start, end) -> str:
    today = _today()
    pills = "".join(
        f'<a class="{"pill active" if k == active else "pill"}" href="/?period={k}">'
        f'{resolve_period(k, today).label}</a>'
        for k in PERIOD_KEYS
    )
    form = (
        '<form class="range" method="get" action="/">'
        f'<input type="date" name="from" value="{start.isoformat()}">'
        '<span class="sep">tot</span>'
        f'<input type="date" name="to" value="{end.isoformat()}">'
        '<button class="pill go" type="submit">Toon</button>'
        '</form>'
    )
    return pills + form


def _subtitle(period: Period, days: list[Day]) -> str:
    if not days:
        return period.label
    a, b = days[0].date, days[-1].date
    return f"{period.label} · {a.day:02d}-{a.month:02d} tot {b.day:02d}-{b.month:02d}"


def _last_updated() -> str | None:
    g = _store.latest_meta().get("generated_at", "")
    return g[:16].replace("T", " ") if g else None


@app.get("/", response_class=HTMLResponse)
def index(period: str = Query(default="this-week"),
          frm: str | None = Query(default=None, alias="from"),
          to: str | None = Query(default=None)):
    p = _resolve(period, frm, to)
    days = _period_days(p)
    dl = (f"uren.xlsx?from={p.start.isoformat()}&to={p.end.isoformat()}"
          if p.key == "custom" else f"uren.xlsx?period={p.key}")
    active = p.key if p.key in PERIOD_KEYS else ""
    return HTMLResponse(render_html.build_week(
        days, title="Uren · Team Cloud", subtitle=_subtitle(p, days),
        download_url=dl, nav_html=_nav_html(active, p.start, p.end),
        generated=_last_updated()))


def _period_xlsx(period: Period) -> Response:
    days = _period_days(period)
    if not days:
        raise HTTPException(status_code=404, detail="no timesheet for this period")
    name = f"Uren-{period.start.isoformat()}_{period.end.isoformat()}.xlsx"
    return Response(render_xlsx.build_week(days), media_type=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/uren.xlsx")
def download(period: str = Query(default="this-week"),
             frm: str | None = Query(default=None, alias="from"),
             to: str | None = Query(default=None)):
    return _period_xlsx(_resolve(period, frm, to))


@app.get("/d/{token}/uren.xlsx")
def signed_download(token: str, period: str = Query(default="this-week"),
                    frm: str | None = Query(default=None, alias="from"),
                    to: str | None = Query(default=None)):
    if not security.verify_download("uren.xlsx", token):
        raise HTTPException(status_code=403, detail="link expired or invalid")
    return _period_xlsx(_resolve(period, frm, to))


@app.post("/api/ingest")
async def ingest(request: Request, x_ingest_token: str | None = Header(default=None)):
    if not security.check_ingest_token(x_ingest_token):
        raise HTTPException(status_code=401, detail="bad or missing X-Ingest-Token")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    monday, days = build_from_ingest(payload, _cfg)
    meta = _store.save(monday, payload, days)
    return JSONResponse({"ok": True, **meta})


@app.get("/status")
def status():
    # DB-backed last-refresh view (deliberately NOT what the probes hit).
    return {"ok": True, **_store.latest_meta()}


@app.get("/healthz")
def healthz():
    # Liveness/readiness: cheap and DB-free (a DB call here blew the 1s probe
    # timeout and crash-looped the pod). Pure "process is alive" check.
    return {"ok": True}
