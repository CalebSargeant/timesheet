"""The live day-window rules: no future days, today capped at 'now', work past
midnight rolls back to the day it started, and weekends show only when worked."""
from datetime import datetime
from zoneinfo import ZoneInfo

from timesheet.config import Config
from timesheet.model import Commit, Meeting
from timesheet.reconstruct import reconstruct_week

TZ = ZoneInfo("Europe/Amsterdam")
CFG = Config(tz="Europe/Amsterdam")
WEEK = datetime(2026, 7, 27, tzinfo=TZ)          # a Monday


def _c(dt: datetime, msg: str = "fix: work", kind: str = "commit") -> Commit:
    return Commit(ts=dt, repo="r", message=msg, kind=kind)


def _dates(days) -> list[str]:
    return [d.date.date().isoformat() for d in days]


def test_future_days_are_not_shown():
    commits = [_c(datetime(2026, 7, 27 + i, 10, 0, tzinfo=TZ)) for i in range(5)]
    days = reconstruct_week([], commits, WEEK, CFG,
                            now=datetime(2026, 7, 29, 12, 0, tzinfo=TZ))   # Wednesday noon
    assert _dates(days) == ["2026-07-27", "2026-07-28", "2026-07-29"]      # Thu/Fri hidden


def test_today_is_capped_at_now():
    commits = [_c(datetime(2026, 7, 29, 9, 0, tzinfo=TZ))]
    now = datetime(2026, 7, 29, 11, 0, tzinfo=TZ)
    future_meeting_start = datetime(2026, 7, 29, 10, 30, tzinfo=TZ)
    future_meeting_end = datetime(2026, 7, 29, 12, 0, tzinfo=TZ)
    meetings = [Meeting(future_meeting_start, future_meeting_end, "proj", "standup")]
    days = reconstruct_week(meetings, commits, WEEK, CFG, now=now)
    today = next(d for d in days if d.date.date().isoformat() == "2026-07-29")
    assert max(b.end for b in today.blocks) <= now                        # nothing past now


def test_work_past_midnight_rolls_back_to_the_previous_day():
    commits = [_c(datetime(2026, 7, 27, 22, 0, tzinfo=TZ)),               # Mon 22:00
               _c(datetime(2026, 7, 28, 1, 0, tzinfo=TZ))]                # Tue 01:00 -> Monday
    days = reconstruct_week([], commits, WEEK, CFG,
                            now=datetime(2026, 7, 30, 12, 0, tzinfo=TZ))
    assert _dates(days) == ["2026-07-27"]                                 # no phantom Tuesday


def test_weekend_shown_when_worked_but_not_padded_to_8h():
    cfg = CFG
    commits = [_c(datetime(2026, 8, 1, 10, 0, tzinfo=TZ))]                # a Saturday
    days = reconstruct_week([], commits, WEEK, cfg,
                            now=datetime(2026, 8, 3, 12, 0, tzinfo=TZ))
    sat = next(d for d in days if d.date.date().isoformat() == "2026-08-01")
    assert sat.date.weekday() == 5
    assert sat.minutes < cfg.min_day_minutes                             # real effort, no 8h floor


def test_empty_weekend_is_skipped():
    commits = [_c(datetime(2026, 7, 27, 10, 0, tzinfo=TZ))]              # Monday only
    days = reconstruct_week([], commits, WEEK, CFG,
                            now=datetime(2026, 8, 3, 12, 0, tzinfo=TZ))
    assert _dates(days) == ["2026-07-27"]
    assert all(d.date.weekday() < 5 for d in days)
