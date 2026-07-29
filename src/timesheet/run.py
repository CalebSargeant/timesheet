"""Cron entrypoint: reconstruct the current (or given) week and persist it.

    python -m timesheet.run                # this week
    python -m timesheet.run 2026-07-20     # a specific week (Monday)
    python -m timesheet.run --email        # also email the manager

Calendar comes from M365_ICS_URL (published ICS); commits from GHE (PAT/gh).
Run nightly by the Kubernetes CronJob; the FastAPI web pod reads the same store."""
from __future__ import annotations

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Config
from .pipeline import collect_week
from .service import email as mailer
from .service.store import make_store


def main(argv: list[str]) -> int:
    cfg = Config.from_env()
    want_email = "--email" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    week = date.fromisoformat(args[0]) if args else datetime.now(ZoneInfo(cfg.tz)).date()

    days = collect_week(week, cfg)
    store = make_store(tz=cfg.tz)
    monday = days[0].date.date() if days else week
    meta = store.save(monday, {}, days)
    print(f"[run] week {meta['week_start']}: {meta['total_hm']} across {meta['days']} days")

    if want_email:
        ok = mailer.send_weekly(store.latest_xlsx() or b"", meta)
        print(f"[run] email: {'sent' if ok else 'skipped/failed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
