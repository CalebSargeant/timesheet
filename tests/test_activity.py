"""Correspondence -> minutes.

This is the part that most easily turns into fiction, so the numbers are pinned
down in both directions: a burst of chat must not become an afternoon, and a
morning's real back-and-forth must not become nothing."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from timesheet import activity as act
from timesheet.config import Config, SignalSpec
from timesheet.model import ActivityEvent

TZ = ZoneInfo("Europe/Amsterdam")
SPEC = SignalSpec("email_sent", gap_minutes=30, lead_minutes=6, cap_minutes=150)


def at(h, m=0, kind="email_sent", subject="") -> ActivityEvent:
    return ActivityEvent(ts=datetime(2026, 7, 20, h, m, tzinfo=TZ), kind=kind, subject=subject)


def span(h1, m1, h2, m2):
    return (datetime(2026, 7, 20, h1, m1, tzinfo=TZ), datetime(2026, 7, 20, h2, m2, tzinfo=TZ))


def test_nothing_is_worth_nothing():
    assert act.session_minutes([], 30, 6) == 0


def test_one_message_is_worth_its_lead_in():
    """The work that led up to pressing send is real, and invisible."""
    assert act.session_minutes([at(10).ts], 30, 6) == 6


def test_a_burst_is_worth_its_span_plus_one_lead_in():
    """Forty messages in ten minutes is ten minutes of work, not forty
    messages' worth."""
    stamps = [at(10, m).ts for m in range(10)]
    assert act.session_minutes(stamps, 30, 6) == 6 + 9


def test_a_long_gap_starts_a_second_sitting():
    """One email at 10:00 and one at 16:00 is two sittings, not six hours."""
    assert act.session_minutes([at(10).ts, at(16).ts], 30, 6) == 12


def test_sittings_split_on_the_gap():
    groups = act.cluster([at(9), at(9, 20), at(11), at(11, 5)], 30)
    assert [len(g) for g in groups] == [2, 2]


def test_a_sitting_inside_a_meeting_is_the_meeting():
    """Replying to mail during a call is the call. Counting both inflates every
    meeting-heavy day."""
    found = act.sessions([at(10), at(10, 10)], SPEC, busy=[span(9, 30, 11, 0)])
    assert found == []


def test_a_lone_message_inside_a_meeting_is_also_dropped():
    assert act.sessions([at(10)], SPEC, busy=[span(9, 30, 11, 0)]) == []


def test_a_lone_message_outside_a_meeting_survives():
    """A sitting of one has no span at all. Filtering zero-length intervals out
    would silently drop every isolated email there is."""
    found = act.sessions([at(12)], SPEC, busy=[span(9, 30, 11, 0)])
    assert [s.minutes for s in found] == [6]


def test_a_partly_covered_sitting_is_credited_pro_rata():
    """A lunchtime inbox sweep that runs into a 14:00 stand-up still counts for
    the half of it that happened first."""
    whole = act.sessions([at(13), at(13, 20)], SPEC)[0].minutes
    clipped = act.sessions([at(13), at(13, 20)], SPEC, busy=[span(13, 10, 14, 0)])[0]
    assert 0 < clipped.minutes < whole


def test_a_days_worth_is_capped():
    """A mailbox that takes 400 messages says more about a distribution list
    than about the reader's hours."""
    small = SignalSpec("email_sent", gap_minutes=5, lead_minutes=10, cap_minutes=25)
    hourly = [at(h) for h in range(8, 18)]
    assert act.total_minutes(act.sessions(hourly, small)) == 25


def test_the_cap_drops_whole_sittings_rather_than_shaving_every_one():
    """What survives still lines up with real moments in the day."""
    small = SignalSpec("email_sent", gap_minutes=5, lead_minutes=10, cap_minutes=25)
    found = act.sessions([at(h) for h in range(8, 18)], small)
    assert [s.start.hour for s in found] == [8, 9, 10]
    assert [s.minutes for s in found] == [10, 10, 5]


def test_received_mail_is_worth_far_less_than_sent():
    """Mail arriving proves a sender was at their desk, not the reader."""
    cfg = Config()
    sent = act.sessions([at(10, kind="email_sent")], cfg.email_sent)
    got = act.sessions([at(10, kind="email_received")], cfg.email_received)
    assert sent[0].minutes > got[0].minutes


def test_a_disabled_source_contributes_nothing():
    cfg = Config(include_chat=False)
    found = act.day_sessions([at(10, kind="chat"), at(11, kind="email_sent")], cfg)
    assert {s.kind for s in found} == {"email_sent"}


def test_a_block_is_labelled_with_the_real_subjects():
    """"Re: incident 4412" is a far better line on a timesheet than
    "Correspondence"."""
    cfg = Config()
    found = act.sessions([at(10, subject="Re: incident 4412"),
                          at(10, 5, subject="Re: rollout window")], cfg.email_sent)
    project, task = act.label(found[0], cfg)
    assert project == "Correspondence"
    assert task == "Re: incident 4412, Re: rollout window"


def test_a_block_with_no_subjects_still_says_something_true():
    cfg = Config()
    found = act.sessions([at(10, kind="chat"), at(10, 3, kind="chat")], cfg.chat)
    project, task = act.label(found[0], cfg)
    assert project == "Chat / coordination"
    assert task == "2 chat messages"


def test_labels_follow_the_locale():
    cfg = Config(locale="nl")
    found = act.sessions([at(10, kind="chat")], cfg.chat)
    assert act.label(found[0], cfg)[0] == "Teams / overleg"


def test_spread_merges_touching_sittings():
    found = act.sessions([at(10), at(10, 10)], SPEC) + act.sessions([at(10, 11)], SPEC)
    merged = act.spread(found)
    assert len(merged) == 1
    assert merged[0][1] - merged[0][0] == timedelta(minutes=11)
