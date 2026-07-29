"""Persist reconstructed weeks + serve them back by week.

Each Mon–Sun week is stored once (keyed by its Monday) with its reconstructed
`days`, so the service can assemble any period (this/last week, this/last month)
from stored weeks and render on demand — no GHE call on a page view. GHE is only
hit when a requested week isn't cached yet (see service._week_days).

Two backends behind one interface:
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


def _meta(days: list[Day], week_start: date, generated: datetime) -> dict:
    total = sum(d.minutes for d in days)
    return {"week_start": week_start.isoformat(), "generated_at": generated.isoformat(),
            "total_minutes": total, "total_hm": hm(total), "days": len(days)}


class FileStore:
    def __init__(self, data_dir: str, tz: str = "Europe/Amsterdam"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(tz)

    def _path(self, monday: date) -> Path:
        return self.dir / f"week-{monday.isoformat()}.json"

    def save(self, week_start: date, payload: dict, days: list[Day]) -> dict:
        meta = _meta(days, week_start, datetime.now(self.tz))
        self._path(week_start).write_text(json.dumps(
            {"meta": meta, "days": [d.to_dict() for d in days],
             "ingest": (payload or {})}))
        return meta

    def get_days(self, monday: date) -> list[Day] | None:
        p = self._path(monday)
        if not p.exists():
            return None
        return [Day.from_dict(x) for x in json.loads(p.read_text())["days"]]

    def latest_meta(self) -> dict:
        metas = []
        for p in self.dir.glob("week-*.json"):
            try:
                metas.append(json.loads(p.read_text())["meta"])
            except (OSError, ValueError, KeyError):
                continue
        return max(metas, key=lambda m: m.get("generated_at", ""), default={})


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
ALTER TABLE timesheet_week ADD COLUMN IF NOT EXISTS days jsonb;
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
        gen = datetime.now(self.tz)
        meta = _meta(days, week_start, gen)
        # html/xlsx columns are NOT NULL from the original schema; keep populating
        # them (single-week render) even though the web now renders periods on demand.
        page = render_html.build_week(days, title=f"Uren — week {week_start.isoformat()}")
        data = render_xlsx.build_week(days)
        days_json = json.dumps([d.to_dict() for d in days])
        with self._conn() as c:
            c.execute(
                """INSERT INTO timesheet_week
                       (week_start, generated_at, total_min, html, xlsx, meta, ingest, days)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (week_start) DO UPDATE SET
                       generated_at = EXCLUDED.generated_at, total_min = EXCLUDED.total_min,
                       html = EXCLUDED.html, xlsx = EXCLUDED.xlsx, meta = EXCLUDED.meta,
                       ingest = EXCLUDED.ingest, days = EXCLUDED.days""",
                (week_start, meta["generated_at"], meta["total_minutes"], page,
                 data, json.dumps(meta), json.dumps(payload or {})[:2_000_000], days_json),
            )
        return meta

    def get_days(self, monday: date) -> list[Day] | None:
        with self._conn() as c:
            row = c.execute("SELECT days FROM timesheet_week WHERE week_start = %s",
                            (monday,)).fetchone()
        if not row or row[0] is None:
            return None
        raw = row[0] if isinstance(row[0], list) else json.loads(row[0])
        return [Day.from_dict(x) for x in raw]

    def latest_meta(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT meta FROM timesheet_week ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return {}
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def make_store(tz: str = "Europe/Amsterdam"):
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return PgStore(dsn, tz=tz)
    return FileStore(os.environ.get("DATA_DIR", "./data"), tz=tz)
