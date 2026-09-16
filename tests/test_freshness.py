"""The in-progress week: fresh, degraded, or explained — but never a silent blank.

A page that shows nothing and says nothing is indistinguishable from a week in
which nobody worked, and that is exactly the state a broken Microsoft connection
used to produce. These are the tests for the three ways out of it: serve the
refresh job's copy while it is current, rebuild live when it is not, and fall
back to the stored copy — saying so — when the rebuild comes back with nothing.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from timesheet.config import Config
from timesheet.model import Block, Day
from timesheet.pipeline import Sources, WeekBuild
from timesheet.service.users import User, defaults

MONDAY = date(2026, 9, 14)


@pytest.fixture
def main(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-long-enough-to-be-accepted")
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    from timesheet.service import main as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_current_monday", lambda _cfg: MONDAY)
    return mod


def _user() -> User:
    return User(id="github.com:1", login="alice", settings=defaults())


def _days(hours: int = 8) -> list[Day]:
    start = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    return [Day(date=start, blocks=[Block(start, start + timedelta(hours=hours),
                                          "Development", "work", "focus")])]


def _store_week(main, user, days, *, age_seconds: int, full: bool = True):
    """A stored week, aged by rewriting the timestamp the store just wrote."""
    main._store.save_user(user)
    main._store.save(user.id, MONDAY, {}, days, full=full)
    path = main._store._path(user.id, MONDAY)
    import json
    doc = json.loads(path.read_text())
    when = datetime.now(UTC) - timedelta(seconds=age_seconds)
    doc["meta"]["generated_at"] = when.isoformat()
    path.write_text(json.dumps(doc))


def test_a_fresh_stored_week_is_served_without_a_live_rebuild(main, monkeypatch):
    """The refresh job's copy carries mail, chat and reviews. While it is current
    it is both richer and cheaper than anything a page view could build."""
    user = _user()
    _store_week(main, user, _days(8), age_seconds=60)
    monkeypatch.setattr(main, "collect", lambda *a, **k: pytest.fail("rebuilt a fresh week"))
    days, notes = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert sum(d.minutes for d in days) == 480
    assert notes == []


def test_a_stale_stored_week_is_rebuilt_live(main, monkeypatch):
    user = _user()
    _store_week(main, user, _days(8), age_seconds=main.LIVE_MAX_AGE + 60)
    monkeypatch.setattr(main, "collect", lambda *a, **k: WeekBuild(days=_days(6)))
    days, notes = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert sum(d.minutes for d in days) == 360      # the live one, not the stored one
    assert notes == []


def test_a_failed_rebuild_falls_back_to_the_stored_week_and_says_so(main, monkeypatch):
    """The regression this whole path exists for: an expired Microsoft token used
    to render as a week in which nothing happened."""
    user = _user()
    _store_week(main, user, _days(8), age_seconds=6 * 3600)
    monkeypatch.setattr(main, "collect", lambda *a, **k: WeekBuild(
        days=[], problems=("calendar: no usable Microsoft 365 token",)))
    days, notes = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert sum(d.minutes for d in days) == 480
    assert any("Microsoft 365 token" in n for n in notes)
    assert any("showing the copy stored at" in n for n in notes)


def test_with_nothing_stored_the_failure_is_still_reported(main, monkeypatch):
    user = _user()
    main._store.save_user(user)
    monkeypatch.setattr(main, "collect", lambda *a, **k: WeekBuild(
        days=[], problems=("GitHub commits: GitHub 401 on /search/commits",)))
    days, notes = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert days == []
    assert notes == ["GitHub commits: GitHub 401 on /search/commits"]


def test_a_collector_that_raises_is_reported_not_swallowed(main, monkeypatch):
    user = _user()
    main._store.save_user(user)

    def _boom(*_a, **_k):
        raise RuntimeError("the connector fell over")

    monkeypatch.setattr(main, "collect", _boom)
    days, notes = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert days == []
    assert any("fell over" in n for n in notes)


def test_a_thin_live_week_beats_a_stale_stored_one(main, monkeypatch):
    """Freshness wins while there is anything at all to show: the page is about
    today, and today is not in yesterday's copy."""
    user = _user()
    _store_week(main, user, _days(8), age_seconds=20 * 3600)
    monkeypatch.setattr(main, "collect", lambda *a, **k: WeekBuild(
        days=_days(2), problems=("Teams chat: FORBIDDEN",)))
    days, notes = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert sum(d.minutes for d in days) == 120
    assert notes == ["Teams chat: FORBIDDEN"]


def test_a_week_stored_without_the_heavy_signals_is_not_treated_as_fresh(main, monkeypatch):
    """A partial copy is not what the refresh job promises, so it never suppresses
    a rebuild — only a `full` week does."""
    user = _user()
    _store_week(main, user, _days(8), age_seconds=10, full=False)
    monkeypatch.setattr(main, "collect", lambda *a, **k: WeekBuild(days=_days(3)))
    days, _ = main._week_days(user, Config(tz="Europe/Amsterdam"), MONDAY)
    assert sum(d.minutes for d in days) == 180


# --- the collectors themselves --------------------------------------------


def test_one_broken_source_costs_that_source_and_not_the_week(monkeypatch):
    """The actual regression: an unwrapped calendar failure took the commits down
    with it, and the page showed a blank week for an account that had committed
    all day."""
    from timesheet import pipeline

    def _no_calendar(*_a, **_k):
        raise RuntimeError("cannot reach the MCP connector")

    monkeypatch.setattr(pipeline, "_calendar", _no_calendar)
    monkeypatch.setattr(pipeline, "_github", lambda *a, **k: (
        [{"ts_local": "2026-09-14T10:00:00+02:00", "repo": "infra", "message": "fix: a thing"}],
        []))
    build = pipeline.collect(MONDAY, Config(tz="Europe/Amsterdam"), Sources(),
                             full=False)
    assert build.days, "the commits should still have produced a day"
    assert any("cannot reach the MCP connector" in p for p in build.problems)


def test_a_broken_commit_search_does_not_cost_the_calendar(monkeypatch):
    from timesheet import pipeline
    from timesheet.collectors import github as gh

    monkeypatch.setattr(pipeline, "_calendar", lambda *a, **k: [
        {"subject": "Planning", "start_utc": "2026-09-14T08:00:00",
         "end_utc": "2026-09-14T09:00:00", "all_day": False, "show_as": "busy"}])

    def _rate_limited(*_a, **_k):
        raise gh.GitHubError("GitHub 403 on /search/commits")

    monkeypatch.setattr(gh, "fetch_commits", _rate_limited)
    build = pipeline.collect(MONDAY, Config(tz="Europe/Amsterdam"),
                             Sources(github=gh.GitHub(user="alice")), full=False)
    assert build.days
    assert any("403" in p for p in build.problems)


def test_status_says_whether_the_refresh_job_is_keeping_up(main):
    """The one question an operator has when a week looks thin, answered without
    doing the refresh job's work inside the request."""
    from fastapi.testclient import TestClient

    from timesheet.service import auth

    user = _user()
    _store_week(main, user, _days(8), age_seconds=30)
    client = TestClient(main.app)
    client.cookies.set(auth.COOKIE, auth.make_session(user.id))
    body = client.get("/status").json()
    assert body["this_week"]["fresh"] is True
    assert body["this_week"]["age_seconds"] < 60

    _store_week(main, user, _days(8), age_seconds=main.LIVE_MAX_AGE + 600)
    assert client.get("/status").json()["this_week"]["fresh"] is False
