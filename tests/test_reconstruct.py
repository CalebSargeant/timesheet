"""Reconstruction invariants, checked against the captured real week.

These are the properties that make a sheet defensible to a manager: the day is
built from real anchors, nothing double-books, no unnatural mega-blocks, and the
morning on-call + standup land where they really were."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timesheet.collectors import normalize_commits, normalize_meetings
from timesheet.config import Config
from timesheet.reconstruct import reconstruct_week
from timesheet.render import html, xlsx

FIXTURE = Path(__file__).parent / "fixtures" / "week_2026-07-20.json"


@pytest.fixture
def week():
    data = json.loads(FIXTURE.read_text())
    cfg = Config()
    tz = ZoneInfo(cfg.tz)
    meetings = normalize_meetings(data["meetings"], cfg, tz)
    commits = normalize_commits(data["commits"], tz)
    ws = datetime.fromisoformat(data["week_start"]).replace(tzinfo=tz)
    return reconstruct_week(meetings, commits, ws, cfg), cfg


def test_five_workdays(week):
    days, _ = week
    assert [d.date.weekday() for d in days] == [0, 1, 2, 3, 4]


def test_no_overlaps_and_ordered(week):
    days, _ = week
    for d in days:
        for a, b in zip(d.blocks, d.blocks[1:]):
            assert a.end <= b.start, f"overlap on {d.date:%a}: {a.taak} / {b.taak}"


def test_blocks_within_workday_and_positive(week):
    days, cfg = week
    for d in days:
        start = d.blocks[0].start
        assert start.strftime("%H:%M") == cfg.day_start        # day opens at 07:30
        for b in d.blocks:
            assert b.minutes > 0


def test_no_mega_blocks(week):
    """The bug in the first POC: a thin-meeting day rendered one 6h block."""
    days, cfg = week
    cap = max(cfg.max_focus_minutes, cfg.max_admin_minutes)
    for d in days:
        for b in d.blocks:
            if b.kind in ("focus", "admin"):
                assert b.minutes <= cap, f"{b.kind} block {b.minutes}m on {d.date:%a}"


def test_morning_is_oncall_and_standup_present(week):
    days, cfg = week
    for d in days:
        assert d.blocks[0].kind == "rota"
        assert d.blocks[0].project == cfg.rota_project
    # every day that week had a daily standup in the real calendar
    for d in days:
        assert any(b.taak == cfg.standup_taak for b in d.blocks), f"no standup on {d.date:%a}"


def test_standup_lands_at_local_0845(week):
    """Calendar says 06:45 UTC; the sheet (and the manager) expect 08:45 CEST."""
    days, _ = week
    monday = days[0]
    standup = next(b for b in monday.blocks if b.taak == "Daily's")
    assert standup.start.strftime("%H:%M") == "08:45"


def test_week_total_is_plausible(week):
    days, _ = week
    total = sum(d.minutes for d in days)
    assert 35 * 60 <= total <= 50 * 60          # a real working week, not 20 months


def test_renderers_produce_output(week):
    days, _ = week
    data = xlsx.build_week(days)
    assert data[:2] == b"PK" and len(data) > 2000
    page = html.build_week(days, download_url="uren.xlsx")
    # "Daily's" renders with the apostrophe HTML-escaped, so match the stem.
    assert "<table" in page and "TOTAAL" in page and "Daily" in page
