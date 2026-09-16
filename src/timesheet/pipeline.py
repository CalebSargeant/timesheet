"""Tie collectors -> reconstruction for one person's week.

`build_week` is pure — it takes already-collected raw data — so the whole
reconstruction stays unit-testable offline with no network and no account.
`collect` does the live pulls, and takes a `Sources` holding one person's
credentials rather than reading the environment, which is what lets a single
process serve many users without any chance of crossing them over. It returns a
`WeekBuild`: the days, and a line for every source that could not be read.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .collectors import (
    normalize_activity,
    normalize_commits,
    normalize_full_days,
    normalize_meetings,
)
from .collectors.github import GitHub
from .collectors.mcp_client import McpSession
from .config import Config
from .model import Day
from .reconstruct import reconstruct_week

log = logging.getLogger(__name__)

CALENDAR_SOURCES = ("mcp", "ics", "graph", "none")


@dataclass(frozen=True)
class WeekBuild:
    """One week's days, plus what could not be read while building them.

    `problems` exists so a thin week can say why it is thin. A page that shows
    nothing and explains nothing is indistinguishable from a week in which
    nothing happened, and that is precisely the case where somebody needs to be
    told to reconnect an account.
    """
    days: list[Day]
    problems: tuple[str, ...] = ()


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class Sources:
    """One person's connected accounts.

    Every field is optional: a user who has linked GitHub but not Microsoft gets
    a sheet built from commits alone, which is thin but honest. Nothing here
    falls back to a different account's credentials, and nothing reads the
    environment — a missing connection produces a missing signal, not somebody
    else's data.
    """
    github: GitHub | None = None
    m365: McpSession | None = None
    ics_url: str = ""
    calendar: str = ""              # mcp | ics | graph | none; blank = infer

    @property
    def calendar_source(self) -> str:
        """Which calendar collector runs.

        Chosen explicitly rather than by falling back on failure. Silently
        swapping the source a week is reconstructed from would make a broken
        connector look like a week with no meetings, and that reconstructs into a
        plausible, wrong, entirely admin-filled sheet.
        """
        chosen = (self.calendar or "").strip().lower()
        if chosen:
            if chosen not in CALENDAR_SOURCES:
                raise ValueError(
                    f"calendar source must be one of {', '.join(CALENDAR_SOURCES)}, "
                    f"not {chosen!r}")
            return chosen
        if self.m365 and self.m365.connected:
            return "mcp"
        return "ics" if self.ics_url else "none"

    @staticmethod
    def from_env(env: dict | None = None) -> Sources:
        """The single-user / CLI path: credentials from the environment."""
        e = env or os.environ
        gh = GitHub(user=e.get("GITHUB_USER", ""), token=e.get("GITHUB_TOKEN", ""),
                    host=e.get("GITHUB_HOST", "github.com"))
        session = None
        if (e.get("M365_SOURCE") or "").lower() in ("", "mcp"):
            from .collectors import mcp_client
            session = mcp_client.default_session()
        return Sources(github=gh if gh.user else None, m365=session,
                       ics_url=e.get("M365_ICS_URL", ""),
                       calendar=(e.get("M365_SOURCE") or "").strip().lower())


def build_week(week_start: date, raw_meetings: list[dict], raw_commits: list[dict],
               cfg: Config, llm=None, raw_activity: list[dict] | None = None) -> list[Day]:
    tz = ZoneInfo(cfg.tz)
    meetings = normalize_meetings(raw_meetings, cfg, tz)
    full_days = normalize_full_days(raw_meetings, cfg, tz)   # leave: all-day + busy
    commits = normalize_commits(raw_commits, tz)
    events = normalize_activity(raw_activity or [], tz)
    ws = datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz)
    return reconstruct_week(meetings, commits, ws, cfg, llm,
                            events=events, full_days=full_days)


def _reason(what: str, exc: Exception) -> str:
    """One failed source, in a sentence fit to show the person whose week it is."""
    text = str(exc).strip() or exc.__class__.__name__
    return f"{what}: {text[:200]}"


def _calendar(sources: Sources, win_start: datetime, win_end: datetime) -> list[dict]:
    source = sources.calendar_source
    if source == "mcp":
        from .collectors import m365_mcp
        return m365_mcp.fetch_events(win_start, win_end, session=sources.m365)
    if source == "ics":
        from .collectors import m365_ics
        return m365_ics.fetch_events(sources.ics_url, win_start, win_end)
    if source == "graph":
        from .collectors import m365_graph
        return m365_graph.fetch_events(m365_graph.get_token(), win_start, win_end)
    return []


def _correspondence(sources: Sources, cfg: Config, win_start: datetime,
                    win_end: datetime) -> tuple[list[dict], list[str]]:
    """Mail and chat for the window, from whichever of them is switched on.

    Each source is attempted independently and a failure in one is reported and
    skipped rather than raised. Correspondence adds hours to a day; losing it
    understates a week, which is recoverable. Letting it abort the run loses the
    week entirely, which is not.
    """
    if sources.calendar_source != "mcp" or not sources.m365:
        return [], []
    from .collectors import m365_mcp

    out: list[dict] = []
    problems: list[str] = []
    wanted: list[tuple[str, callable]] = []
    if cfg.include_email:
        wanted.append(("sent mail",
                       lambda: m365_mcp.fetch_mail(win_start, win_end, sent=True,
                                                   session=sources.m365)))
        wanted.append(("received mail",
                       lambda: m365_mcp.fetch_mail(win_start, win_end, sent=False,
                                                   session=sources.m365)))
    if cfg.include_chat:
        wanted.append(("Teams chat",
                       lambda: m365_mcp.fetch_chat(win_start, win_end, session=sources.m365)))

    for what, pull in wanted:
        try:
            out.extend(pull())
        # Deliberately broad: a collector can fail in as many ways as the network
        # and a third-party schema allow, and a thin week beats no week.
        except Exception as e:
            log.warning("could not read %s; continuing without it", what, exc_info=True)
            problems.append(_reason(what, e))
    return out, problems


def _github(sources: Sources, cfg: Config, since: str, until: str, *,
            full: bool) -> tuple[list[dict], list[str]]:
    """Commits, and optionally the items a commit search misses.

    Commits are wrapped like everything else. They are the backbone of the sheet,
    which is exactly why an unwrapped failure here used to take the calendar down
    with it and leave the whole week blank — a rate limit or an expired token on
    one source must cost that source and nothing more.
    """
    if not sources.github:
        return [], []
    from .collectors import github as gh

    rows: list[dict] = []
    problems: list[str] = []
    try:
        rows += gh.fetch_commits(since, until, client=sources.github)
    except Exception as e:
        log.warning("commit fetch failed; continuing without it", exc_info=True)
        problems.append(_reason("GitHub commits", e))
    # PRs opened + issues authored: one search each, cheap enough for a web request.
    if cfg.include_authored:
        try:
            rows += gh.fetch_authored(since, until, client=sources.github)
        except Exception as e:
            log.debug("authored-item fetch failed", exc_info=True)
            problems.append(_reason("GitHub pull requests and issues", e))
    # Reviews: a per-PR REST fan-out, so background refresh only.
    if full and cfg.include_reviews:
        try:
            rows += gh.fetch_reviews(since, until, client=sources.github)
        except Exception as e:
            log.debug("review fetch failed", exc_info=True)
            problems.append(_reason("GitHub reviews", e))
    return rows, problems


def collect(week_start: date, cfg: Config, sources: Sources | None = None, llm=None, *,
            full: bool = False) -> WeekBuild:
    """Live pull for one person's week, then build — reporting what failed.

    `full` adds the heavier signals — PR reviews, and the mail and chat reads —
    that a page view can't afford. The web path calls this without it (calendar
    and commits only, fast); the scheduled refresh calls it with.

    Every source is read independently. One that fails costs its own signal and
    says so in `problems`; it does not cost the week. Before that was true, a
    Microsoft connection that had quietly expired produced an empty page with no
    explanation on it, which reads exactly like a week in which nobody worked.
    """
    sources = sources or Sources.from_env()
    tz = ZoneInfo(cfg.tz)
    monday = _monday(week_start)
    win_start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    win_end = win_start + timedelta(days=7)

    problems: list[str] = []
    try:
        raw_meetings = _calendar(sources, win_start, win_end)
    except Exception as e:
        log.warning("calendar read failed; continuing without it", exc_info=True)
        raw_meetings = []
        problems.append(_reason("calendar", e))

    since = monday.isoformat()
    until = (monday + timedelta(days=6)).isoformat()
    raw_commits, gh_problems = _github(sources, cfg, since, until, full=full)
    problems += gh_problems

    raw_activity: list[dict] = []
    if full:
        raw_activity, comms_problems = _correspondence(sources, cfg, win_start, win_end)
        problems += comms_problems

    days = build_week(monday, raw_meetings, raw_commits, cfg, llm, raw_activity)
    return WeekBuild(days=days, problems=tuple(problems))


def collect_week(week_start: date, cfg: Config, sources: Sources | None = None, llm=None, *,
                 full: bool = False) -> list[Day]:
    """`collect`, for callers that only want the days."""
    return collect(week_start, cfg, sources, llm, full=full).days


def build_from_ingest(payload: dict, cfg: Config, sources: Sources | None = None, llm=None, *,
                      fetch_commits=None) -> tuple[date, list[Day]]:
    """The push path: calendar, mail and chat arrive as JSON from an external
    automation; commits are pulled server-side. Returns (week_start_monday, days).

    Still supported for anyone whose tenant offers no other way in, and for
    sources this project has no collector for at all. `fetch_commits` is
    injectable so the path can be tested with no network.
    """
    sources = sources or Sources.from_env()
    if fetch_commits is None:
        from .collectors import github as gh
        def fetch_commits(a, b):
            return gh.fetch_commits(a, b, client=sources.github) if sources.github else []

    tz = ZoneInfo(cfg.tz)
    if payload.get("week_start"):
        ws = date.fromisoformat(payload["week_start"])
    else:
        ws = datetime.now(tz).date()
    monday = _monday(ws)

    raw_commits = fetch_commits(monday.isoformat(), (monday + timedelta(days=6)).isoformat())
    activity = _ingest_activity(payload)
    days = build_week(monday, payload.get("meetings", []), raw_commits, cfg, llm, activity)
    return monday, days


def _ingest_activity(payload: dict) -> list[dict]:
    """Correspondence out of a pushed payload, in the collectors' own shape.

    Accepts both the current keys and the older flat `emails` list, which older
    automations still send and which had no sent/received distinction — those are
    read as sent, the interpretation they were built under.
    """
    out: list[dict] = []
    for row in payload.get("emails", []) or []:
        out.append({**row, "kind": row.get("kind") or "email_sent"})
    for row in payload.get("emails_received", []) or []:
        out.append({**row, "kind": "email_received"})
    for row in (payload.get("chat") or payload.get("teams") or []):
        out.append({**row, "kind": "chat"})
    return out
