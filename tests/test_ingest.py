"""The push path: calendar, mail and chat pushed in by an external automation,
plus commits pulled server-side, reconstructed into a week.

Still supported for tenants that offer no other way in, and for sources this
project has no collector for at all."""
import json
from pathlib import Path

from timesheet.config import Config
from timesheet.pipeline import build_from_ingest

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "week_2026-07-20.json").read_text(encoding="utf-8"))
CFG = Config(tz="Europe/Amsterdam")


def _commits_stub(since, until):
    # Stands in for the live collector: the same raw shape it emits.
    return FIXTURE["commits"]


def _payload(**over):
    payload = {
        "week_start": "2026-07-20",
        "meetings": FIXTURE["meetings"],
        # a burst of correspondence late on the Friday morning, well clear of
        # that day's meetings (the standup at 08:45 and a session at 14:00 local)
        "emails": [{"sent_utc": "2026-07-24T09:10:00Z", "subject": "Re: production"},
                   {"sent_utc": "2026-07-24T09:25:00Z", "subject": "Re: rollout"}],
        "chat": [{"ts_utc": "2026-07-24T09:40:00Z"}],
    }
    payload.update(over)
    return payload


def test_build_from_ingest_reconstructs_week():
    monday, days = build_from_ingest(_payload(), CFG, fetch_commits=_commits_stub)
    assert monday.isoformat() == "2026-07-20"
    assert [d.date.weekday() for d in days] == [0, 1, 2, 3, 4]
    total = sum(d.minutes for d in days)
    assert 35 * 60 <= total <= 55 * 60


def test_pushed_correspondence_becomes_labelled_blocks():
    _, days = build_from_ingest(_payload(), CFG, fetch_commits=_commits_stub)
    friday = next(d for d in days if d.date.weekday() == 4)
    # 09:10-09:40Z is 11:10-11:40 local; that window should read as correspondence
    # rather than as a generic admin block.
    kinds = {b.kind for b in friday.blocks}
    assert kinds & {"email", "chat"}, [b.taak for b in friday.blocks]
    assert any("rollout" in b.taak for b in friday.blocks)


def test_correspondence_inside_a_meeting_is_the_meeting():
    """Replying to mail during a call is the call. Counting both would inflate
    every meeting-heavy day."""
    during = _payload(
        # 12:10-12:40Z is 14:10-14:40 local, inside that Friday's 14:00 session
        emails=[{"sent_utc": "2026-07-24T12:10:00Z", "subject": "Re: production"},
                {"sent_utc": "2026-07-24T12:25:00Z", "subject": "Re: rollout"}],
        chat=[{"ts_utc": "2026-07-24T12:40:00Z"}])
    _, days = build_from_ingest(during, CFG, fetch_commits=_commits_stub)
    friday = next(d for d in days if d.date.weekday() == 4)
    assert not {b.kind for b in friday.blocks} & {"email", "chat"}


def test_pushed_correspondence_adds_time_not_just_a_label():
    """The point of reading mail and chat at all. A Saturday carries no 8h floor,
    so what the day is worth is exactly what the evidence says it is."""
    saturday = [{"sent_utc": f"2026-07-25T08:{m:02d}:00Z"} for m in (10, 25, 40)]
    _, without = build_from_ingest(_payload(emails=[], chat=[]), CFG,
                                   fetch_commits=lambda *_a: [])
    _, with_mail = build_from_ingest(_payload(emails=saturday, chat=[]), CFG,
                                     fetch_commits=lambda *_a: [])
    assert not [d for d in without if d.date.weekday() == 5]
    worked = next(d for d in with_mail if d.date.weekday() == 5)
    assert 0 < worked.minutes < CFG.min_day_minutes


def test_the_older_flat_emails_key_is_still_accepted():
    """An older automation sends `emails` with no sent/received distinction.
    Those are read as sent — the interpretation they were written under."""
    _, days = build_from_ingest(_payload(), CFG, fetch_commits=_commits_stub)
    assert days


def test_an_absent_week_start_uses_today():
    payload = _payload()
    payload.pop("week_start")
    monday, _ = build_from_ingest(payload, CFG, fetch_commits=lambda *_a: [])
    assert monday.weekday() == 0
