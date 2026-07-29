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
from .llm import make_llm
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
    # AI label polish runs here in the background, if configured — never in a web
    # request. Absent LITELLM_MODEL, this is None and summaries stay deterministic.
    llm = make_llm(cfg)
    if llm:
        print(f"[run] AI summaries on ({cfg.llm_model})")

    # Current week — always refreshed, with the full signal set (commits + reviews).
    days = collect_week(week, cfg, llm=llm, full=True)
    monday = days[0].date.date() if days else _monday(week)
    meta = store.save(monday, {}, days, full=True)
    print(f"[run] week {meta['week_start']}: {meta['total_hm']} across {meta['days']} days")

    # Backfill recent weeks so month filters are fast. Recompute a week unless it's
    # already cached at the current logic version AND with the full signal set — so
    # weeks a page view warmed with the fast commit-only pass get upgraded here.
    for i in range(1, int(os.environ.get("BACKFILL_WEEKS", "6")) + 1):
        m = _monday(week) - timedelta(days=7 * i)
        if store.is_full(m):
            continue
        bd = collect_week(m, cfg, llm=llm, full=True)
        store.save(m, {}, bd, full=True)
        print(f"[run] backfilled week {m.isoformat()}: {len(bd)} days")

    if want_email:
        ok = mailer.send_weekly(render_xlsx.build_week(days), meta)
        print(f"[run] email: {'sent' if ok else 'skipped/failed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
