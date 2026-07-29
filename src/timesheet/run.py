"""Cron entrypoint: reconstruct the current (or given) week, persist it, and
pre-warm recent weeks so the date filter's month views are already cached.

    python -m timesheet.run                # this week (+ backfill recent weeks)
    python -m timesheet.run 2026-07-20     # a specific week (Monday)
    python -m timesheet.run --email        # also email the manager

Calendar comes from M365_ICS_URL (published ICS); commits from GHE (PAT). Run
nightly by the Kubernetes CronJob; the FastAPI web pod reads the same store.
BACKFILL_WEEKS (default 6) recent weeks are reconstructed once if not yet stored."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .pipeline import collect_week
from .render import xlsx as render_xlsx
from .service import email as mailer
from .service.store import make_store


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def main(argv: list[str]) -> int:
    cfg = Config.from_env()
    want_email = "--email" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    week = date.fromisoformat(args[0]) if args else datetime.now(ZoneInfo(cfg.tz)).date()
    store = make_store(tz=cfg.tz)

    # Current week — always refreshed.
    days = collect_week(week, cfg)
    monday = days[0].date.date() if days else _monday(week)
    meta = store.save(monday, {}, days)
    print(f"[run] week {meta['week_start']}: {meta['total_hm']} across {meta['days']} days")

    # Backfill recent weeks once (skip already-cached ones) so month filters are fast.
    for i in range(1, int(os.environ.get("BACKFILL_WEEKS", "6")) + 1):
        m = _monday(week) - timedelta(days=7 * i)
        if store.get_days(m) is not None:
            continue
        bd = collect_week(m, cfg)
        store.save(m, {}, bd)
        print(f"[run] backfilled week {m.isoformat()}: {len(bd)} days")

    if want_email:
        ok = mailer.send_weekly(render_xlsx.build_week(days), meta)
        print(f"[run] email: {'sent' if ok else 'skipped/failed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
