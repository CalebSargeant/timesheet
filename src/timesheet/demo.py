"""Reconstruct a week from a captured fixture and render every format.

    python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/ [locale]

No network, no account, no AI — the deterministic path anyone can run offline to
see what the tool actually produces before connecting a single thing to it."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .collectors import (
    normalize_activity,
    normalize_commits,
    normalize_full_days,
    normalize_meetings,
)
from .config import Config
from .reconstruct import reconstruct_week
from .render import html, text, xlsx


def main(argv: list[str]) -> int:
    fixture = Path(argv[1]) if len(argv) > 1 else Path("tests/fixtures/week_2026-07-20.json")
    outdir = Path(argv[2]) if len(argv) > 2 else Path("out")
    locale = argv[3] if len(argv) > 3 else "en"
    outdir.mkdir(parents=True, exist_ok=True)

    data = json.loads(fixture.read_text(encoding="utf-8"))
    cfg = Config(locale=locale, tz=data.get("tz", "UTC"))
    tz = ZoneInfo(cfg.tz)
    raw_meetings = data.get("meetings", [])
    meetings = normalize_meetings(raw_meetings, cfg, tz)
    full_days = normalize_full_days(raw_meetings, cfg, tz)
    commits = normalize_commits(data.get("commits", []), tz)
    events = normalize_activity(data.get("activity", []), tz)
    week_start = datetime.fromisoformat(data["week_start"]).replace(tzinfo=tz)

    week = reconstruct_week(meetings, commits, week_start, cfg,
                            events=events, full_days=full_days)
    print(text.render_text(week, cfg.strings))

    (outdir / "timesheet.xlsx").write_bytes(xlsx.build_week(week, locale=cfg.strings))
    (outdir / "timesheet.html").write_text(
        html.build_week(week, locale=cfg.strings, download_url="timesheet.xlsx"),
        encoding="utf-8")
    print(f"\nwrote {outdir / 'timesheet.xlsx'} and {outdir / 'timesheet.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
