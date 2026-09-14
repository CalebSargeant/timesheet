"""Reconstruction invariants, checked against the captured week.

These are the properties that make a sheet defensible to whoever signs it off:
the day is built from real anchors, nothing double-books, no unnatural
mega-blocks, and a meeting lands where it really was."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timesheet.collectors import normalize_activity, normalize_commits, normalize_meetings
from timesheet.config import Config
from timesheet.model import Commit
from timesheet.reconstruct import reconstruct_day, reconstruct_week
from timesheet.render import html, xlsx

FIXTURE = Path(__file__).parent / "fixtures" / "week_2026-07-20.json"
CFG = Config(tz="Europe/Amsterdam")
TZ = ZoneInfo(CFG.tz)


@pytest.fixture
def week():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meetings = normalize_meetings(data["meetings"], CFG, TZ)
    commits = normalize_commits(data["commits"], TZ)
    events = normalize_activity(data["activity"], TZ)
    ws = datetime.fromisoformat(data["week_start"]).replace(tzinfo=TZ)
    # a fixed 'now' well after the captured week, so no day is future / in-progress
    now = datetime(2026, 8, 1, 12, 0, tzinfo=TZ)
    return reconstruct_week(meetings, commits, ws, CFG, now=now, events=events), CFG


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
    day = datetime(2026, 7, 20, tzinfo=TZ)
    # an early commit pulls the start back to when work actually began
    early = [Commit(ts=datetime(2026, 7, 20, 7, 40, tzinfo=TZ), repo="r", message="fix: early")]
    d = reconstruct_day(day, [], early, CFG, TZ)
    assert d.blocks[0].start.strftime("%H:%M") == "07:40"
    # a late-only day still opens at the normal 08:30, not at the commit time
    late = [Commit(ts=datetime(2026, 7, 20, 11, 0, tzinfo=TZ), repo="r", message="fix: late")]
    d = reconstruct_day(day, [], late, CFG, TZ)
    assert d.blocks[0].start.strftime("%H:%M") == CFG.day_start   # 08:30


def test_reviews_count_as_effort_and_are_labelled():
    """A review-only day (no commits, no meetings) still reads as real work, and
    the block is labelled Code review, not generic admin."""
    day = datetime(2026, 7, 20, tzinfo=TZ)
    reviews = [
        Commit(ts=datetime(2026, 7, 20, 10, 0, tzinfo=TZ), repo="infra",
               message="Fix ingress", kind="review"),
        Commit(ts=datetime(2026, 7, 20, 10, 40, tzinfo=TZ), repo="apps",
               message="Bump chart", kind="review"),
    ]
    d = reconstruct_day(day, [], reviews, CFG, TZ)
    assert d.minutes > 0
    assert any(b.project == CFG.labels.review_project for b in d.blocks), "no Code review block"


def test_prs_and_issues_show_as_labelled_work():
    """A day of PRs opened / issues filed (few commits) reads as real work, not admin."""
    day = datetime(2026, 7, 22, tzinfo=TZ)
    events = [
        Commit(ts=datetime(2026, 7, 22, 9, 0, tzinfo=TZ), repo="infra",
               message="Upgrade ESO", kind="pr"),
        Commit(ts=datetime(2026, 7, 22, 13, 0, tzinfo=TZ), repo="infra",
               message="Fix ingress VIP", kind="issue"),
    ]
    d = reconstruct_day(day, [], events, CFG, TZ)
    projects = {b.project for b in d.blocks}
    assert CFG.labels.pr_project in projects or CFG.labels.issue_project in projects


def test_admin_labels_vary_so_quiet_days_are_not_identical():
    """A day with only a little activity still rotates admin labels."""
    day = datetime(2026, 7, 24, tzinfo=TZ)
    commits = [Commit(ts=datetime(2026, 7, 24, 10, 0, tzinfo=TZ), repo="r", message="fix: thing")]
    d = reconstruct_day(day, [], commits, CFG, TZ)
    admin_tasks = {b.taak for b in d.blocks if b.kind == "admin"}
    assert len(admin_tasks) >= 2, "a quiet day still renders a column of identical admin rows"


def test_empty_day_with_no_activity_returns_no_blocks():
    """A day with no commits and no meetings produces no timesheet entry."""
    d = reconstruct_day(datetime(2026, 7, 25, tzinfo=TZ), [], [], CFG, TZ)
    assert d.blocks == []
    assert d.minutes == 0


def test_no_mega_blocks(week):
    """The bug in the first version: a thin-meeting day rendered one 6h block."""
    days, cfg = week
    cap = max(cfg.max_focus_minutes, cfg.max_admin_minutes)
    for d in days:
        for b in d.blocks:
            if b.kind in ("focus", "admin", "email", "chat"):
                assert b.minutes <= cap, f"{b.kind} block {b.minutes}m on {d.date:%a}"


def test_no_template_rota(week):
    """Rota is off by default, so it must never appear for someone who has none."""
    days, _ = week
    for d in days:
        assert all(b.kind != "rota" for b in d.blocks), f"phantom rota on {d.date:%a}"


def test_standup_present(week):
    days, cfg = week
    for d in days:
        assert any(b.project == cfg.labels.standup_project for b in d.blocks), \
            f"no standup on {d.date:%a}"


def test_standup_lands_at_local_0845(week):
    """The calendar says 06:45 UTC; the sheet must say 08:45 local."""
    days, cfg = week
    monday = days[0]
    standup = next(b for b in monday.blocks if b.project == cfg.labels.standup_project)
    assert standup.start.strftime("%H:%M") == "08:45"


def test_week_total_is_plausible(week):
    days, _ = week
    total = sum(d.minutes for d in days)
    assert 35 * 60 <= total <= 55 * 60          # a real working week, not 20 months


def test_renderers_produce_output(week):
    days, _ = week
    data = xlsx.build_week(days)
    assert data[:2] == b"PK" and len(data) > 2000
    page = html.build_week(days, download_url="timesheet.xlsx")
    assert "<table" in page and "TOTAL" in page and "standup" in page.lower()


def test_renderers_follow_the_locale(week):
    days, _ = week
    assert "TOTAAL" in html.build_week(days, locale="nl")
    assert "TOTAL" in html.build_week(days, locale="en")
