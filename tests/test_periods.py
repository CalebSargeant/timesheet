"""Named-period resolution, Day/Block JSON round-trip, and store get_days."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from timesheet.model import Block, Day
from timesheet.periods import mondays_covering, resolve_period

TZ = ZoneInfo("Europe/Amsterdam")
WED = date(2026, 7, 22)  # a Wednesday; its week is Mon 2026-07-20 .. Sun 2026-07-26


def test_this_and_last_week():
    tw = resolve_period("this-week", WED)
    assert (tw.start, tw.end, tw.is_month) == (date(2026, 7, 20), date(2026, 7, 26), False)
    lw = resolve_period("last-week", WED)
    assert (lw.start, lw.end) == (date(2026, 7, 13), date(2026, 7, 19))


def test_this_and_last_month():
    tm = resolve_period("this-month", WED)
    assert (tm.start, tm.end, tm.is_month) == (date(2026, 7, 1), date(2026, 7, 31), True)
    lm = resolve_period("last-month", WED)
    assert (lm.start, lm.end) == (date(2026, 6, 1), date(2026, 6, 30))


def test_month_year_boundaries():
    assert resolve_period("last-month", date(2026, 1, 15)).start == date(2025, 12, 1)
    assert resolve_period("this-month", date(2026, 12, 10)).end == date(2026, 12, 31)


def test_unknown_key_defaults_to_this_week():
    assert resolve_period("bogus", WED).key == "this-week"


def test_custom_period_and_parse():
    from timesheet.periods import custom_period, parse_date

    p = custom_period(date(2026, 7, 1), date(2026, 7, 15))
    assert (p.key, p.start, p.end, p.is_month) == ("custom", date(2026, 7, 1), date(2026, 7, 15), True)
    rev = custom_period(date(2026, 7, 15), date(2026, 7, 1))   # reversed -> normalized
    assert (rev.start, rev.end) == (date(2026, 7, 1), date(2026, 7, 15))
    assert parse_date("2026-07-01") == date(2026, 7, 1)
    assert parse_date("nope") is None and parse_date(None) is None


def test_mondays_covering_month():
    ms = mondays_covering(date(2026, 7, 1), date(2026, 7, 31))
    assert ms[0] == date(2026, 6, 29) and date(2026, 7, 27) in ms and len(ms) == 5


def _sample_days():
    b = Block(datetime(2026, 7, 20, 9, 0, tzinfo=TZ), datetime(2026, 7, 20, 10, 0, tzinfo=TZ),
              "Meeting", "Daily's", "meeting")
    return [Day(datetime(2026, 7, 20, 0, 0, tzinfo=TZ), [b], dropped_after_hours=1)]


def test_day_block_json_roundtrip():
    d = _sample_days()[0]
    r = Day.from_dict(d.to_dict())
    assert r.date == d.date and r.minutes == 60 and r.dropped_after_hours == 1
    assert r.blocks[0].project == "Meeting" and r.blocks[0].start == d.blocks[0].start


def test_filestore_get_days_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from timesheet.service.store import FileStore, make_store

    store = make_store()
    assert isinstance(store, FileStore)
    meta = store.save(date(2026, 7, 20), {}, _sample_days())
    assert meta["week_start"] == "2026-07-20"
    got = store.get_days(date(2026, 7, 20))
    assert got is not None and got[0].minutes == 60
    assert store.get_days(date(2026, 7, 13)) is None          # uncached week
    assert store.latest_meta()["week_start"] == "2026-07-20"
