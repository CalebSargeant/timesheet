"""Reconstruct a week from a captured real-data fixture and render all formats.

    python -m timesheet.demo tests/fixtures/week_2026-07-20.json out/

Uses no network and no AI — the deterministic path a reviewer can run offline."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .collectors import normalize_commits, normalize_full_days, normalize_meetings
from .config import Config
from .reconstruct import reconstruct_week
from .render import html, text, xlsx


def main(argv: list[str]) -> int:
    fixture = Path(argv[1]) if len(argv) > 1 else Path("tests/fixtures/week_2026-07-20.json")
    outdir = Path(argv[2]) if len(argv) > 2 else Path("out")
    outdir.mkdir(parents=True, exist_ok=True)

    data = json.loads(fixture.read_text())
    cfg = Config()
    tz = ZoneInfo(cfg.tz)
    meetings = normalize_meetings(data.get("meetings", []), cfg, tz)
    full_days = normalize_full_days(data.get("meetings", []), cfg, tz)
    commits = normalize_commits(data.get("commits", []), tz)
    week_start = datetime.fromisoformat(data["week_start"]).replace(tzinfo=tz)

    week = reconstruct_week(meetings, commits, week_start, cfg, full_days=full_days)
    print(text.render_text(week))

    (outdir / "uren.xlsx").write_bytes(xlsx.build_week(week))
    (outdir / "uren.html").write_text(html.build_week(week, download_url="uren.xlsx"))
    print(f"\nwrote {outdir/'uren.xlsx'} and {outdir/'uren.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
