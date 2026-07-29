"""Tie collectors -> reconstruction -> render for one week.

`build_week` is pure (takes already-collected raw data) so it stays unit-testable
offline. `collect_week` does the live pulls (Graph calendar + GHE commits) and is
what the nightly cron and the FastAPI refresh call."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .collectors import ghe, m365_graph, normalize_commits, normalize_meetings
from .config import Config
from .model import Day
from .reconstruct import reconstruct_week


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _as_local(s: str, tz: ZoneInfo) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)     # fromisoformat handles a trailing 'Z' on 3.11+
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz)


def enrich_admin(days: list[Day], emails: list[dict], teams: list[dict], tz: ZoneInfo) -> None:
    """Refine admin-block labels using when email/Teams activity actually happened.
    A generic 'Mail / GitHub / Teams' block becomes 'Mail', 'Teams / overleg', etc.
    when the window is dominated by one. Signal-only: never changes durations."""
    ev = [t for t in (_as_local(e.get("sent_utc") or e.get("ts_utc"), tz) for e in emails) if t]
    tv = [t for t in (_as_local(t_.get("ts_utc"), tz) for t_ in teams) if t]
    for d in days:
        for b in d.blocks:
            if b.kind != "admin":
                continue
            ne = sum(1 for t in ev if b.start <= t < b.end)
            nt = sum(1 for t in tv if b.start <= t < b.end)
            if not (ne or nt):
                continue
            parts = []
            if ne:
                parts.append("Mail")
            if nt:
                parts.append("Teams / overleg")
            b.taak = " / ".join(parts)


def build_week(week_start: date, raw_meetings: list[dict], raw_commits: list[dict],
               cfg: Config, llm=None) -> list[Day]:
    tz = ZoneInfo(cfg.tz)
    meetings = normalize_meetings(raw_meetings, cfg, tz)
    commits = normalize_commits(raw_commits, tz)
    ws = datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz)
    return reconstruct_week(meetings, commits, ws, cfg, llm)


def collect_week(week_start: date, cfg: Config, llm=None, *, full: bool = False) -> list[Day]:
    """Live pull for the cron: calendar + commits for the week, then build.

    Calendar source, in preference order (all sidestep an Azure app registration):
      1. M365_ICS_URL   — a published-calendar ICS link (no Power Automate)
      2. Power Automate — pushes to /api/ingest instead (see build_from_ingest)
      3. device-code Graph — only if the tenant allows it (it doesn't in LOCGOV)

    `full` adds the heavier signals (PR reviews) that a page view can't afford; the
    web path calls this without it (commits only, fast), the nightly refresh with it.
    """
    import os

    tz = ZoneInfo(cfg.tz)
    monday = _monday(week_start)
    win_start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    win_end = win_start + timedelta(days=len(cfg.workdays) + 2)   # cover the whole week

    ics_url = os.environ.get("M365_ICS_URL")
    if ics_url:
        from .collectors import m365_ics
        raw_meetings = m365_ics.fetch_events(ics_url, win_start, win_end)
    else:
        token = m365_graph.get_token()
        raw_meetings = m365_graph.fetch_events(token, win_start, win_end)

    since = monday.isoformat()
    until = (monday + timedelta(days=6)).isoformat()
    raw_commits = ghe.fetch_commits(since, until)
    if cfg.include_authored:   # PRs opened + issues authored: one search each, cheap enough for web
        from .collectors import ghe_authored
        try:
            raw_commits = raw_commits + ghe_authored.fetch_authored(since, until)
        except Exception:  # noqa: BLE001, S110 — a bonus signal, never block the week
            pass
    if full and cfg.include_reviews:   # reviews: per-PR REST fan-out, background refresh only
        from .collectors import ghe_reviews
        try:
            raw_commits = raw_commits + ghe_reviews.fetch_reviews(since, until)
        except Exception:  # noqa: BLE001, S110 — a bonus signal, never block the week
            pass

    return build_week(monday, raw_meetings, raw_commits, cfg, llm)


def build_from_ingest(payload: dict, cfg: Config, llm=None, *,
                      fetch_commits=None) -> tuple[date, list[Day]]:
    """The Power Automate path: calendar/email/Teams are pushed in; commits are
    pulled server-side with the GHE PAT. Returns (week_start_monday, days).
    `fetch_commits` defaults to the live GHE collector but is injectable for tests."""
    fetch_commits = fetch_commits or ghe.fetch_commits
    tz = ZoneInfo(cfg.tz)
    if payload.get("week_start"):
        ws = date.fromisoformat(payload["week_start"])
    else:
        ws = datetime.now(tz).date()
    monday = _monday(ws)

    raw_meetings = payload.get("meetings", [])
    raw_commits = fetch_commits(monday.isoformat(), (monday + timedelta(days=6)).isoformat())
    days = build_week(monday, raw_meetings, raw_commits, cfg, llm)
    enrich_admin(days, payload.get("emails", []), payload.get("teams", []), tz)
    return monday, days
