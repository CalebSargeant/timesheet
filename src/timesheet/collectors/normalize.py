"""Raw collector output -> normalized Meeting/Commit.

The *shape* consumed here is exactly what the Microsoft Graph calendar collector
and the GHE commit collector emit (and what tests/fixtures capture from live
data), so the reconstructor never sees a provider-specific field."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config
from ..model import Commit, Meeting

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
            project, taak = cfg.standup_project, cfg.standup_taak
        else:
            project, taak = cfg.meeting_project, _clean_subject(subj)
        out.append(Meeting(start=start, end=end, project=project, taak=taak))
    out.sort(key=lambda x: x.start)
    return out


def normalize_commits(raw: list[dict], tz: ZoneInfo) -> list[Commit]:
    out: list[Commit] = []
    for c in raw:
        ts = datetime.fromisoformat(c["ts_local"]).astimezone(tz)
        msg = (c.get("message") or "").split("\n")[0].strip()
        if msg.lower().startswith("merge "):     # merges aren't authored work
            continue
        out.append(Commit(ts=ts, repo=c.get("repo", "?"), message=msg))
    out.sort(key=lambda x: x.ts)
    return out
