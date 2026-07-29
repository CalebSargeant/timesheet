"""Time formatting, snapping, and interval arithmetic — no external deps."""
from __future__ import annotations

from datetime import datetime, timedelta

NL_DAYS = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
NL_MONTHS = ["", "januari", "februari", "maart", "april", "mei", "juni", "juli",
             "augustus", "september", "oktober", "november", "december"]

Interval = tuple[datetime, datetime]


def hm(minutes: int) -> str:
    """420 -> '7:00'. Hours are NOT zero-padded, matching the manager's sheet.
    Totals may exceed 24h (e.g. a week), which is fine: '42:27'."""
    minutes = max(0, int(minutes))
    return f"{minutes // 60}:{minutes % 60:02d}"


def parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def snap(dt: datetime, step: int) -> datetime:
    """Round to the nearest `step` minutes (:00/:15/:30/:45 for step=15)."""
    floor = dt.replace(minute=0, second=0, microsecond=0)
    q = round((dt - floor).total_seconds() / 60 / step) * step
    return floor + timedelta(minutes=q)


def mins(iv: Interval) -> int:
    return int((iv[1] - iv[0]).total_seconds() // 60)


def subtract(free: list[Interval], busy: Interval) -> list[Interval]:
    """Remove `busy` from a list of free intervals."""
    out: list[Interval] = []
    bs, be = busy
    for fs, fe in free:
        if be <= fs or bs >= fe:          # no overlap
            out.append((fs, fe))
            continue
        if fs < bs:
            out.append((fs, bs))
        if be < fe:
            out.append((be, fe))
    return out


def free_intervals(window: Interval, occupied: list[Interval]) -> list[Interval]:
    free = [window]
    for busy in sorted(occupied):
        free = subtract(free, busy)
    return [iv for iv in free if iv[1] > iv[0]]


def resolve_overlaps(blocks: list[Interval]) -> list[Interval]:
    """Trim overlapping fixed blocks so none double-book; drop fully-covered ones.
    Meetings genuinely overlap in Outlook (a standup inside a planning call); the
    sheet can only show one thing at a time."""
    out: list[Interval] = []
    for s, e in sorted(blocks):
        if out and s < out[-1][1]:
            s = out[-1][1]
        if e > s:
            out.append((s, e))
    return out
