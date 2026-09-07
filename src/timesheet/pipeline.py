"""Tie collectors -> reconstruction -> render for one week.

`build_week` is pure (takes already-collected raw data) so it stays unit-testable
offline. `collect_week` does the live pulls (Graph calendar + GHE commits) and is
what the nightly cron and the FastAPI refresh call."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .collectors import (
    ghe,
    m365_graph,
    normalize_commits,
    normalize_full_days,
    normalize_meetings,
)
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
    full_days = normalize_full_days(raw_meetings, cfg, tz)   # leave: all-day + busy
    commits = normalize_commits(raw_commits, tz)
    ws = datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz)
    return reconstruct_week(meetings, commits, ws, cfg, llm, full_days=full_days)


def calendar_source(env: dict | None = None) -> str:
    """Which calendar collector runs. `M365_SOURCE` decides; otherwise the
    historical behaviour — an ICS link if one is configured, else Graph.

    Chosen explicitly rather than by falling back on failure. Silently swapping
    the source a week is reconstructed from would make a broken connector look
    like a week with no meetings, which reconstructs into a plausible, wrong,
    entirely admin-filled sheet.
    """
    e = env if env is not None else os.environ
    chosen = (e.get("M365_SOURCE") or "").strip().lower()
    if chosen:
        if chosen not in {"mcp", "ics", "graph"}:
            raise ValueError(f"M365_SOURCE must be mcp, ics or graph, not {chosen!r}")
        return chosen
    return "ics" if e.get("M365_ICS_URL") else "graph"


def collect_week(week_start: date, cfg: Config, llm=None, *, full: bool = False) -> list[Day]:
    """Live pull for the cron: calendar + commits for the week, then build.

    Calendar source, set by `M365_SOURCE` (all sidestep an Azure app registration):
      mcp    — Claude's Microsoft 365 MCP connector. Real subjects, no rolling
               three-month window, and email + Teams activity as well, which is
               what /api/ingest and the Power Automate flow were built to push in.
      ics    — a published-calendar ICS link (the default when M365_ICS_URL is set)
      graph  — device-code Graph, only if the tenant allows it (LOCGOV does not)

    `full` adds the heavier signals (PR reviews, and the email/Teams enrichment)
    that a page view can't afford; the web path calls this without it (commits
    only, fast), the nightly refresh with it.
    """
    tz = ZoneInfo(cfg.tz)
    monday = _monday(week_start)
    win_start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    win_end = win_start + timedelta(days=len(cfg.workdays) + 2)   # cover the whole week

    source = calendar_source()
    if source == "mcp":
        from .collectors import m365_mcp
        raw_meetings = m365_mcp.fetch_events(win_start, win_end)
    elif source == "ics":
        from .collectors import m365_ics
        raw_meetings = m365_ics.fetch_events(os.environ["M365_ICS_URL"], win_start, win_end)
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

    days = build_week(monday, raw_meetings, raw_commits, cfg, llm)

    # Email/Teams enrichment: two more searches, so background refresh only.
    # It relabels admin blocks and never touches a duration, which is why a
    # failure here is swallowed — a generic "Mail / GitHub / Teams" label is a
    # perfectly honest fallback, and a missing label is not worth losing a week
    # of otherwise-correct reconstruction over.
    if full and source == "mcp":
        from .collectors import m365_mcp
        try:
            enrich_admin(days, m365_mcp.fetch_emails(win_start, win_end),
                         m365_mcp.fetch_teams(win_start, win_end), tz)
        except Exception:
            logging.getLogger(__name__).warning("enrichment skipped", exc_info=True)

    return days


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
