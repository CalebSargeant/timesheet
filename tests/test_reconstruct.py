"""Reconstruction invariants, checked against the captured week.

These are the properties that make a sheet defensible to whoever signs it off:
the day is built from real anchors, nothing double-books, no unnatural
mega-blocks, and a meeting lands where it really was."""
import json
from datetime import datetime, timedelta
from itertools import pairwise
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
        for a, b in pairwise(d.blocks):
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


# --- how the day's free time gets labelled ---------------------------------


def _commit(h, m, msg, kind="commit"):
    return Commit(ts=datetime(2026, 7, 20, h, m, tzinfo=TZ), repo="r", message=msg, kind=kind)


def _meeting(h1, m1, h2, m2, subject="Standup"):
    from timesheet.model import Meeting
    return Meeting(datetime(2026, 7, 20, h1, m1, tzinfo=TZ),
                   datetime(2026, 7, 20, h2, m2, tzinfo=TZ), "Internal", subject)


def _two_theme_day():
    """Two well-separated commit sessions: one security, one monitoring."""
    return reconstruct_day(datetime(2026, 7, 20, tzinfo=TZ), [], [
        _commit(9, 10, "fix(security): rotate the oauth client secret"),
        _commit(9, 40, "fix(security): tighten rbac on the admin role"),
        _commit(15, 0, "feat(monitoring): alert on endpoint probe failures"),
        _commit(15, 30, "feat(monitoring): ship thanos long-term storage"),
    ], CFG, TZ)


def test_themes_come_out_as_runs_not_interleaved():
    """The bug: Development/Security/Development/Security down the whole
    afternoon. No real day looks like that, and it made the sheet read as
    generated rather than recorded."""
    projects = [b.project for b in _two_theme_day().blocks if b.kind == "focus"]
    assert len(projects) >= 3, projects
    # every theme occupies one contiguous run
    runs = [p for i, p in enumerate(projects) if i == 0 or p != projects[i - 1]]
    assert len(runs) == len(set(runs)), f"a theme came back after another: {projects}"


def test_a_busier_theme_gets_more_of_the_day():
    day = reconstruct_day(datetime(2026, 7, 20, tzinfo=TZ), [], [
        *[_commit(9, m, f"fix(security): step {m}") for m in (0, 10, 20, 30, 40, 50)],
        _commit(15, 0, "feat(monitoring): one lone change"),
    ], CFG, TZ)
    minutes: dict[str, int] = {}
    for b in day.blocks:
        if b.kind == "focus":
            minutes[b.project] = minutes.get(b.project, 0) + b.minutes
    assert minutes["Security"] > minutes["Monitoring"], minutes


def test_successive_rows_of_one_theme_do_not_repeat_the_same_line():
    """A long run on one theme is split across rows by the focus cap. Repeating
    one sentence down all of them is the wallpaper the user saw."""
    day = reconstruct_day(datetime(2026, 7, 20, tzinfo=TZ), [], [
        _commit(9, 0, "fix(security): rotate the oauth client secret"),
        _commit(9, 20, "fix(security): tighten rbac on the admin role"),
        _commit(9, 40, "fix(security): drop the dangling service references"),
        _commit(10, 0, "fix(security): pin the vulnerable transitive dependency"),
    ], CFG, TZ)
    tasks = [b.taak for b in day.blocks if b.project == "Security"]
    assert len(tasks) >= 2
    assert len(set(tasks)) > 1, f"every row says the same thing: {tasks}"


def test_every_line_used_is_a_real_commit_subject():
    """The variety must not be invented — each line is a subject from that very
    session, or the sheet stops being defensible."""
    messages = [
        "fix(security): rotate the oauth client secret",
        "fix(security): tighten rbac on the admin role",
        "fix(security): drop the dangling service references",
    ]
    day = reconstruct_day(datetime(2026, 7, 20, tzinfo=TZ), [],
                          [_commit(9, i * 20, m) for i, m in enumerate(messages)], CFG, TZ)
    allowed = {m.split(": ", 1)[1] for m in messages}
    for b in day.blocks:
        if b.project == "Security":
            assert b.taak in allowed, b.taak


def test_a_short_gap_is_not_labelled_as_focus_work():
    """A fifteen-minute gap between two meetings coming out as a lone
    'Development' row is padding, and reads as padding."""
    day = reconstruct_day(datetime(2026, 7, 20, tzinfo=TZ),
                          [_meeting(8, 45, 9, 30), _meeting(9, 45, 11, 0, "Planning")],
                          [_commit(11, 30, "feat(security): a real change")], CFG, TZ)
    for b in day.blocks:
        if b.minutes < CFG.min_block_minutes:
            assert b.kind in ("admin", "meeting"), f"{b.minutes}m block labelled {b.project}"


def test_short_gaps_keep_their_minutes():
    """Dropping the sliver instead would make the day open later than the person
    actually started, and quietly lose the time."""
    day = reconstruct_day(datetime(2026, 7, 20, tzinfo=TZ), [_meeting(8, 45, 9, 30)],
                          [_commit(11, 0, "feat: a change")], CFG, TZ)
    assert day.blocks[0].start.strftime("%H:%M") == CFG.day_start


def test_adjacent_identical_blocks_are_fused():
    """Two rows saying exactly the same thing are one piece of work the chunker
    happened to cut in half."""
    from timesheet.model import Block
    from timesheet.reconstruct import _merge_repeats
    s = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    mid = datetime(2026, 7, 20, 9, 30, tzinfo=TZ)
    end = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
    merged = _merge_repeats([Block(s, mid, "Security", "same", "focus"),
                             Block(mid, end, "Security", "same", "focus")], CFG)
    assert len(merged) == 1 and merged[0].minutes == 60


def test_merging_cannot_recreate_a_mega_block():
    from timesheet.model import Block
    from timesheet.reconstruct import _merge_repeats
    s = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    blocks = [Block(s + timedelta(minutes=90 * i), s + timedelta(minutes=90 * (i + 1)),
                    "Security", "same", "focus") for i in range(4)]
    merged = _merge_repeats(blocks, CFG)
    assert all(b.minutes <= CFG.max_focus_minutes for b in merged)
