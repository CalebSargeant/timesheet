"""A booked day off must report as leave, not as a reconstructed working day.

HR systems push leave into the work calendar as an all-day event marked busy.
The case this exists for: a Friday off where the recurring standup invite still
sat in the calendar, so the day was floored to a padded 8h of admin — on a day
nobody worked. The raw events below are the shapes every calendar collector emits.

The leave subject here is Dutch on purpose. Markers are matched across every
locale at once, because whoever runs the calendar decides what language the
subjects are in, and that is rarely the reader's.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from timesheet.collectors import normalize_commits, normalize_full_days, normalize_meetings
from timesheet.config import Config
from timesheet.reconstruct import reconstruct_week
from timesheet.render import html, xlsx

TZ = ZoneInfo("Europe/Amsterdam")
BASE = Config(tz="Europe/Amsterdam")
WEEK = datetime(2026, 8, 3, tzinfo=TZ)                 # Monday of the leave Friday
NOW = datetime(2026, 8, 12, 9, 0, tzinfo=TZ)           # well after that week

LEAVE = {"subject": "Leave / Verlof", "all_day": True, "show_as": "busy",
         "start_utc": "2026-08-07T00:00:00", "end_utc": "2026-08-08T00:00:00"}
STANDUP = {"subject": "!! Daily standup !!", "all_day": False,
           "show_as": "busy", "start_utc": "2026-08-07T06:45:00",
           "end_utc": "2026-08-07T07:30:00"}
DESK = {"subject": "Booking (Desk 5.17)", "all_day": True, "show_as": "free",
        "start_utc": "2026-08-06T00:00:00", "end_utc": "2026-08-07T00:00:00"}
COMMIT = {"ts_local": "2026-08-07T10:00:00+02:00", "repo": "infra", "message": "fix: a thing"}


def _week(raw_meetings, raw_commits=(), cfg=None):
    cfg = cfg or BASE
    return reconstruct_week(
        normalize_meetings(raw_meetings, cfg, TZ),
        normalize_commits(list(raw_commits), TZ),
        WEEK, cfg, now=NOW,
        full_days=normalize_full_days(raw_meetings, cfg, TZ),
    ), cfg


def _friday(days):
    return next(d for d in days if d.date.date() == date(2026, 8, 7))


def test_all_day_busy_event_is_detected_as_leave():
    cfg = BASE
    evs = normalize_full_days([LEAVE], cfg, TZ)
    assert [e.date for e in evs] == [date(2026, 8, 7)]      # not the 6th: it's a date, not UTC midnight
    assert evs[0].project == cfg.labels.leave_project and evs[0].kind == "leave"
    assert evs[0].taak == "Leave / Verlof"


def test_leave_day_reports_leave_instead_of_a_padded_workday():
    days, cfg = _week([LEAVE, STANDUP], [COMMIT])
    friday = _friday(days)
    assert [b.project for b in friday.blocks] == [cfg.labels.leave_project]
    assert friday.minutes == cfg.full_day_minutes          # one honest 8h leave row
    assert not any(b.kind in ("admin", "focus", "meeting") for b in friday.blocks)
    assert not any("standup" in b.taak.lower() for b in friday.blocks)


def test_leave_day_is_reported_even_with_no_other_signal():
    """No meetings, no commits — the day would otherwise be skipped entirely."""
    days, cfg = _week([LEAVE])
    assert _friday(days).blocks[0].project == cfg.labels.leave_project


def test_free_all_day_bookings_are_not_leave():
    """A desk booking is all-day but 'free' — Thursday stays a normal working day."""
    cfg = BASE
    assert normalize_full_days([DESK], cfg, TZ) == []
    days, _ = _week([DESK, STANDUP], [COMMIT])
    assert all(b.project != cfg.labels.leave_project for d in days for b in d.blocks)


def test_detail_free_all_day_block_still_reads_as_leave():
    """A calendar published as 'availability only' hides the subject behind "Busy";
    a whole day blocked out with nothing said about it is still an absence."""
    cfg = BASE
    evs = normalize_full_days([dict(LEAVE, subject="Busy")], cfg, TZ)
    assert [(e.project, e.taak, e.kind) for e in evs] == [
        (cfg.labels.leave_project, cfg.labels.leave_task, "leave")]


def test_timed_busy_meetings_are_not_leave():
    assert normalize_full_days([STANDUP], BASE, TZ) == []


def test_multi_day_leave_expands_all_dates_weekends_filtered_by_reconstruct():
    # Fri 7 Aug through Mon 10 Aug inclusive (Outlook's end date is exclusive)
    leave = dict(LEAVE, end_utc="2026-08-11T00:00:00")
    cfg = BASE
    assert [e.date for e in normalize_full_days([leave], cfg, TZ)] == [
        date(2026, 8, 7), date(2026, 8, 8), date(2026, 8, 9), date(2026, 8, 10)]
    days, _ = _week([leave])
    assert [d.date.date() for d in days] == [date(2026, 8, 7)]        # Sat/Sun not reported
    later = reconstruct_week([], [], datetime(2026, 8, 10, tzinfo=TZ), cfg, now=NOW,
                             full_days=normalize_full_days([leave], cfg, TZ))
    assert [d.date.date() for d in later] == [date(2026, 8, 10)]      # Monday of the next week


def test_future_leave_is_not_reported_yet():
    cfg = BASE
    assert _week([LEAVE])[0], "sanity: past leave shows"
    early = reconstruct_week([], [], WEEK, cfg,
                             now=datetime(2026, 8, 5, 12, 0, tzinfo=TZ),   # the Wednesday before
                             full_days=normalize_full_days([LEAVE], cfg, TZ))
    assert early == []


def test_leave_renders_in_both_outputs():
    days, cfg = _week([LEAVE, STANDUP])
    page = html.build_week(days, locale=cfg.strings)
    assert cfg.labels.leave_project in page and "tag leave" in page
    assert xlsx.build_week(days, locale=cfg.strings)[:2] == b"PK"


def test_a_dutch_leave_subject_is_understood_by_an_english_reader():
    """Markers are matched across every locale. Reading "Verlof" as an ordinary
    meeting would reconstruct a day off into eight hours of invented work."""
    english = Config(tz="Europe/Amsterdam", locale="en")
    evs = normalize_full_days([dict(LEAVE, subject="Verlof")], english, TZ)
    assert [e.kind for e in evs] == ["leave"]
    assert evs[0].project == "Leave"
