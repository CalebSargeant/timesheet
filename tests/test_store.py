"""Version-stamped cache: a reconstruction-logic change invalidates old rows
automatically, so a deploy never serves stale numbers and needs no manual clear."""
from datetime import UTC, date, datetime

import timesheet.service.store as store_mod
from timesheet.model import Block, Day
from timesheet.service.store import FileStore


def _day(monday: date) -> Day:
    s = datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=UTC)
    return Day(date=s, blocks=[Block(s, s.replace(hour=17), "Development", "iets", "focus")])


def test_get_days_roundtrips_current_version(tmp_path):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(monday, {}, [_day(monday)])
    got = s.get_days(monday)
    assert got is not None and len(got) == 1


def test_stale_version_is_a_miss(tmp_path, monkeypatch):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(monday, {}, [_day(monday)])          # stamped with the current version
    # a later deploy bumps the reconstruction logic:
    monkeypatch.setattr(store_mod, "RECONSTRUCT_VERSION", store_mod.RECONSTRUCT_VERSION + 1)
    assert s.get_days(monday) is None           # the old row now counts as a cache miss


def test_is_full_reflects_saved_flag(tmp_path):
    s = FileStore(str(tmp_path))
    monday = date(2026, 7, 20)
    s.save(monday, {}, [_day(monday)], full=False)   # a fast web-warmed week
    assert s.is_full(monday) is False                # so the refresh will upgrade it
    s.save(monday, {}, [_day(monday)], full=True)    # the background refresh ran
    assert s.is_full(monday) is True
