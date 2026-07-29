"""FastAPI service: ingest (Power Automate) -> reconstruct -> live view + download.

Routes:
  POST /api/ingest       calendar/email/Teams JSON from the flow (X-Ingest-Token)
  GET  /                 the live 'Uren' page Marc opens (behind Cloudflare Access)
  GET  /uren.xlsx        download the current sheet (behind Access)
  GET  /d/{token}/uren.xlsx   signed, time-limited download (for the emailed link)
  GET  /healthz          liveness
"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import Config
from ..pipeline import build_from_ingest
from . import security
from .store import make_store

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="github-timesheet", docs_url=None, redoc_url=None)
_cfg = Config.from_env()
_store = make_store(tz=_cfg.tz)

_PLACEHOLDER = ("<!doctype html><meta charset=utf-8><title>Uren</title>"
                "<body style='font:15px system-ui;padding:3rem;max-width:40rem;margin:auto'>"
                "<h1>🕑 Uren — Team Cloud</h1><p>Nog geen gegevens ontvangen. "
                "De Power Automate-flow vult dit dagelijks.</p>")


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


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_store.latest_html() or _PLACEHOLDER)


def _xlsx_response() -> Response:
    data = _store.latest_xlsx()
    if data is None:
        raise HTTPException(status_code=404, detail="no timesheet yet")
    meta = _store.latest_meta()
    name = f"Uren-{meta.get('week_start', 'week')}.xlsx"
    return Response(data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/uren.xlsx")
def download():
    return _xlsx_response()


@app.get("/d/{token}/uren.xlsx")
def signed_download(token: str):
    if not security.verify_download("uren.xlsx", token):
        raise HTTPException(status_code=403, detail="link expired or invalid")
    return _xlsx_response()


@app.get("/healthz")
def healthz():
    return {"ok": True, **_store.latest_meta()}
