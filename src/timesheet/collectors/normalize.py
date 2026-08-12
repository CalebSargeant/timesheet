"""Raw collector output -> normalized Meeting/Commit.

The *shape* consumed here is exactly what the Microsoft Graph calendar collector
and the GHE commit collector emit (and what tests/fixtures capture from live
data), so the reconstructor never sees a provider-specific field."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..model import Commit, FullDayEvent, Meeting

_UTC = ZoneInfo("UTC")


def _clean_subject(s: str) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"[!]+", "", s)).strip()


def _as_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt


def normalize_meetings(raw: list[dict], cfg: Config, tz: ZoneInfo) -> list[Meeting]:
    out: list[Meeting] = []
    for m in raw:
        if cfg.drop_all_day and m.get("all_day"):
            continue
        if (m.get("show_as") or "").lower() in cfg.drop_show_as:
            continue
        subj = m.get("subject", "") or ""
        low = subj.lower()
        if any(mk in low for mk in cfg.drop_subject_markers):
            continue
        start = _as_utc(m["start_utc"]).astimezone(tz)
        end = _as_utc(m["end_utc"]).astimezone(tz)
        if end <= start:
            continue
        if any(mk in low for mk in cfg.standup_markers):
            project, taak = cfg.standup_project, _clean_subject(subj)
        else:
            project, taak = cfg.meeting_project, _clean_subject(subj)
        out.append(Meeting(start=start, end=end, project=project, taak=taak))
    out.sort(key=lambda x: x.start)
    return out


def normalize_full_days(raw: list[dict], cfg: Config, tz: ZoneInfo) -> list[FullDayEvent]:
    """All-day BUSY/OOF calendar events, expanded to the local dates they cover.

    Leave lands in Outlook (via AFAS) as an all-day event marked busy — "Leave /
    Verlof". Both calendar sources give an all-day event midnight bounds with an
    *exclusive* end, whether that midnight is expressed in UTC (a bare ICS DATE) or
    local; converting to local first makes the covered dates right either way.
    All-day items marked 'free' (desk bookings) are not absences and never match."""
    out: list[FullDayEvent] = []
    if not cfg.full_day_owns_day:
        return out
    for m in raw:
        if not m.get("all_day"):
            continue
        if (m.get("show_as") or "busy").lower() not in cfg.full_day_show_as:
            continue
        subj = m.get("subject", "") or ""
        low = subj.lower()
        if any(mk in low for mk in cfg.drop_subject_markers):
            continue
        first = _as_utc(m["start_utc"]).astimezone(tz).date()
        last = _as_utc(m["end_utc"]).astimezone(tz).date()      # exclusive
        span = max(1, min((last - first).days, cfg.full_day_max_span))
        cleaned = _clean_subject(subj)
        blank = cleaned.lower() in cfg.leave_blank_subjects     # 'availability only' ICS
        is_leave = blank or any(mk in low for mk in cfg.leave_markers)
        project = cfg.leave_project if is_leave else cfg.meeting_project
        taak = cfg.leave_taak if blank else cleaned
        for i in range(span):
            out.append(FullDayEvent(date=first + timedelta(days=i), project=project,
                                    taak=taak, kind="leave" if is_leave else "meeting"))
    out.sort(key=lambda e: e.date)
    return out


def normalize_commits(raw: list[dict], tz: ZoneInfo) -> list[Commit]:
    out: list[Commit] = []
    for c in raw:
        ts = datetime.fromisoformat(c["ts_local"]).astimezone(tz)
        kind = c.get("kind", "commit")
        msg = (c.get("message") or "").split("\n")[0].strip()
        if kind == "commit" and msg.lower().startswith("merge "):   # merges aren't authored work
            continue
        out.append(Commit(ts=ts, repo=c.get("repo", "?"), message=msg, kind=kind))
    out.sort(key=lambda x: x.ts)
    return out
