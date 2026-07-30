"""Reconstruction invariants, checked against the captured real week.

These are the properties that make a sheet defensible to a manager: the day is
built from real anchors, nothing double-books, no unnatural mega-blocks, and the
calendar standup lands where it really was."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timesheet.collectors import normalize_commits, normalize_meetings
from timesheet.config import Config
from timesheet.model import Commit
from timesheet.reconstruct import reconstruct_day, reconstruct_week
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
    # a fixed 'now' well after the captured week, so no day is future / in-progress
    now = datetime(2026, 8, 1, 12, 0, tzinfo=tz)
    return reconstruct_week(meetings, commits, ws, cfg, now=now), cfg


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
        start = d.blocks[0].start.strftime("%H:%M")
        # opens at 08:30, or earlier if the day's activity shows it, never before the floor
        assert cfg.earliest_start_floor <= start <= cfg.day_start, f"{d.date:%a} opens {start}"
        for b in d.blocks:
            assert b.minutes > 0


def test_day_opens_at_default_or_earlier_by_activity():
    cfg = Config()
    tz = ZoneInfo(cfg.tz)
    day = datetime(2026, 7, 20, tzinfo=tz)
    # an early commit pulls the start back to when work actually began
    early = [Commit(ts=datetime(2026, 7, 20, 7, 40, tzinfo=tz), repo="r", message="fix: vroeg")]
    d = reconstruct_day(day, [], early, cfg, tz)
    assert d.blocks[0].start.strftime("%H:%M") == "07:40"
    # a late-only day still opens at the normal 08:30, not at the commit time
    late = [Commit(ts=datetime(2026, 7, 20, 11, 0, tzinfo=tz), repo="r", message="fix: laat")]
    d = reconstruct_day(day, [], late, cfg, tz)
    assert d.blocks[0].start.strftime("%H:%M") == cfg.day_start   # 08:30


def test_reviews_count_as_effort_and_are_labelled():
    """A review-only day (no commits, no meetings) still reads as real work, and
    the block is labelled Code review, not generic admin."""
    cfg = Config()
    tz = ZoneInfo(cfg.tz)
    day = datetime(2026, 7, 20, tzinfo=tz)
    reviews = [
        Commit(ts=datetime(2026, 7, 20, 10, 0, tzinfo=tz), repo="infra", message="Fix ingress", kind="review"),
        Commit(ts=datetime(2026, 7, 20, 10, 40, tzinfo=tz), repo="apps", message="Bump chart", kind="review"),
    ]
    d = reconstruct_day(day, [], reviews, cfg, tz)
    assert d.minutes > 0
    assert any(b.project == cfg.review_project for b in d.blocks), "no Code review block"


def test_prs_and_issues_show_as_labelled_work():
    """A day of PRs opened / issues filed (few commits) reads as real work, not admin."""
    cfg = Config()
    tz = ZoneInfo(cfg.tz)
    day = datetime(2026, 7, 22, tzinfo=tz)
    events = [
        Commit(ts=datetime(2026, 7, 22, 9, 0, tzinfo=tz), repo="infra", message="Upgrade ESO", kind="pr"),
        Commit(ts=datetime(2026, 7, 22, 13, 0, tzinfo=tz), repo="infra", message="Fix ingress VIP", kind="issue"),
    ]
    d = reconstruct_day(day, [], events, cfg, tz)
    projects = {b.project for b in d.blocks}
    assert cfg.pr_project in projects or cfg.issue_project in projects


def test_admin_labels_vary_so_quiet_days_are_not_identical():
    cfg = Config()
    tz = ZoneInfo(cfg.tz)
    d = reconstruct_day(datetime(2026, 7, 24, tzinfo=tz), [], [], cfg, tz)
    admin_taaks = {b.taak for b in d.blocks if b.kind == "admin"}
    assert len(admin_taaks) >= 2, "a quiet day still renders a column of identical admin rows"


def test_no_mega_blocks(week):
    """The bug in the first POC: a thin-meeting day rendered one 6h block."""
    days, cfg = week
    cap = max(cfg.max_focus_minutes, cfg.max_admin_minutes)
    for d in days:
        for b in d.blocks:
            if b.kind in ("focus", "admin"):
                assert b.minutes <= cap, f"{b.kind} block {b.minutes}m on {d.date:%a}"


def test_no_template_rota(week):
    """The example sheet's 07:30 'Checks en standby' was a placeholder, not real
    work; rota is off by default so it must never appear."""
    days, _ = week
    for d in days:
        assert all(b.kind != "rota" for b in d.blocks), f"phantom rota on {d.date:%a}"


def test_standup_present(week):
    days, cfg = week
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
