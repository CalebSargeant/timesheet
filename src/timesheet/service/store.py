"""Persist the latest reconstructed week + raw ingest.

Two backends behind one tiny interface:
  • PgStore   — shared firefly CNPG (prod): the CronJob writes, the web reads.
  • FileStore — a directory (local dev / tests).
`make_store()` picks Postgres when DATABASE_URL is set, else files.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..model import Day
from ..render import html as render_html
from ..render import xlsx as render_xlsx
from ..timeutil import hm


def _render(days: list[Day], week_start: date, generated: datetime) -> tuple[str, bytes, dict]:
    total = sum(d.minutes for d in days)
    title = f"Uren — Team Cloud — week {week_start.isoformat()}"
    page = render_html.build_week(
        days, title=title, download_url="uren.xlsx",
        generated=generated.strftime("%Y-%m-%d %H:%M"))
    data = render_xlsx.build_week(days)
    meta = {"week_start": week_start.isoformat(), "generated_at": generated.isoformat(),
            "total_minutes": total, "total_hm": hm(total), "days": len(days)}
    return page, data, meta


class FileStore:
    def __init__(self, data_dir: str, tz: str = "Europe/Amsterdam"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(tz)

    def save(self, week_start: date, payload: dict, days: list[Day]) -> dict:
        page, data, meta = _render(days, week_start, datetime.now(self.tz))
        (self.dir / "uren.html").write_text(page)
        (self.dir / "uren.xlsx").write_bytes(data)
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2))
        (self.dir / "ingest.json").write_text(json.dumps(payload)[:2_000_000])
        return meta

    def latest_html(self) -> str | None:
        p = self.dir / "uren.html"
        return p.read_text() if p.exists() else None

    def latest_xlsx(self) -> bytes | None:
        p = self.dir / "uren.xlsx"
        return p.read_bytes() if p.exists() else None

    def latest_meta(self) -> dict:
        p = self.dir / "meta.json"
        return json.loads(p.read_text()) if p.exists() else {}


_DDL = """
CREATE TABLE IF NOT EXISTS timesheet_week (
    week_start   date PRIMARY KEY,
    generated_at timestamptz NOT NULL,
    total_min    integer     NOT NULL,
    html         text        NOT NULL,
    xlsx         bytea       NOT NULL,
    meta         jsonb       NOT NULL,
    ingest       jsonb
);
"""


class PgStore:
    def __init__(self, dsn: str, tz: str = "Europe/Amsterdam"):
        self.dsn = dsn
        self.tz = ZoneInfo(tz)
        with self._conn() as c:
            c.execute(_DDL)

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True)

    def save(self, week_start: date, payload: dict, days: list[Day]) -> dict:
        page, data, meta = _render(days, week_start, datetime.now(self.tz))
        with self._conn() as c:
            c.execute(
                """INSERT INTO timesheet_week
                       (week_start, generated_at, total_min, html, xlsx, meta, ingest)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (week_start) DO UPDATE SET
                       generated_at = EXCLUDED.generated_at, total_min = EXCLUDED.total_min,
                       html = EXCLUDED.html, xlsx = EXCLUDED.xlsx, meta = EXCLUDED.meta,
                       ingest = EXCLUDED.ingest""",
                (week_start, meta["generated_at"], meta["total_minutes"], page,
                 data, json.dumps(meta), json.dumps(payload)[:2_000_000]),
            )
        return meta

    def _latest(self, col: str):
        with self._conn() as c:
            row = c.execute(
                f"SELECT {col} FROM timesheet_week ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def latest_html(self) -> str | None:
        return self._latest("html")

    def latest_xlsx(self) -> bytes | None:
        v = self._latest("xlsx")
        return bytes(v) if v is not None else None

    def latest_meta(self) -> dict:
        v = self._latest("meta")
        return v if isinstance(v, dict) else (json.loads(v) if v else {})


def make_store(tz: str = "Europe/Amsterdam"):
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return PgStore(dsn, tz=tz)
    return FileStore(os.environ.get("DATA_DIR", "./data"), tz=tz)
