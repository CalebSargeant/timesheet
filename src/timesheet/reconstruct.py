"""Turn normalized signals into a believable day of timesheet blocks.

Algorithm (all deterministic):
  1. Fixed anchors  = morning on-call rota + calendar meetings (overlaps trimmed).
  2. Free time      = the workday window [07:30, 07:30+target] minus the anchors.
  3. Focus blocks   = commit sessions (git-hours clustering) dropped into the free
                      slot *nearest when they actually happened*, labelled from
                      their commit content. Long blocks are split.
  4. Admin fill     = any leftover free time -> 'Administratie / Mail·GitHub·Teams',
                      split into <=90m chunks so nothing looks like a 6h monolith.
Sessions that fall outside the workday (late-evening commits) are dropped by
default and only counted, matching how the sheet is kept by hand.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from .config import Config
from .model import Block, Commit, Day, Meeting
from .summarize import classify_focus, summarize_block
from .timeutil import Interval, free_intervals, mins, parse_hhmm, resolve_overlaps, snap, subtract


def _cluster(commits: list[Commit], gap_min: int) -> list[list[Commit]]:
    if not commits:
        return []
    sessions, cur = [], [commits[0]]
    for prev, c in pairwise(commits):
        if (c.ts - prev.ts).total_seconds() / 60 <= gap_min:
            cur.append(c)
        else:
            sessions.append(cur)
            cur = [c]
    sessions.append(cur)
    return sessions


def _split(iv: Interval, cap: int, step: int) -> list[Interval]:
    """Chop an interval into <=cap-minute pieces on `step` boundaries."""
    out, s = [], iv[0]
    while mins((s, iv[1])) > cap:
        e = snap(s + timedelta(minutes=cap), step)
        if e <= s:
            e = s + timedelta(minutes=cap)
        out.append((s, min(e, iv[1])))
        s = out[-1][1]
    if iv[1] > s:
        out.append((s, iv[1]))
    return out


def _best_slot(free: list[Interval], want: Interval) -> Interval | None:
    """Free interval overlapping `want`, else the nearest one starting after it."""
    overlapping = [iv for iv in free if iv[0] < want[1] and iv[1] > want[0]]
    if overlapping:
        return max(overlapping, key=lambda iv: min(iv[1], want[1]) - max(iv[0], want[0]))
    later = [iv for iv in free if iv[0] >= want[0]]
    return min(later, key=lambda iv: iv[0]) if later else None


def reconstruct_day(date: datetime, meetings: list[Meeting], commits: list[Commit],
                    cfg: Config, tz: ZoneInfo, llm=None) -> Day:
    start = date.replace(hour=parse_hhmm(cfg.day_start) // 60,
                         minute=parse_hhmm(cfg.day_start) % 60, second=0, microsecond=0)

    fixed: list[tuple[Interval, str, str, str]] = []   # (interval, project, taak, kind)
    if cfg.rota_enabled:
        fixed.append(((start, start + timedelta(minutes=cfg.rota_minutes)),
                      cfg.rota_project, cfg.rota_taak, "rota"))
    for m in meetings:
        fixed.append(((m.start, m.end), m.project, m.taak, "meeting"))

    # trim overlaps among the fixed anchors (keep labels by re-matching trimmed spans)
    trimmed = resolve_overlaps([f[0] for f in fixed])
    anchors: list[Block] = []
    for span in trimmed:
        # the label comes from the original block whose start is closest at/under span start
        src = min((f for f in fixed if f[0][0] <= span[0] + timedelta(minutes=1)),
                  key=lambda f: abs((f[0][0] - span[0]).total_seconds()), default=fixed[0])
        anchors.append(Block(span[0], span[1], src[1], src[2], src[3]))

    day_end = max(start + timedelta(minutes=cfg.target_minutes),
                  max((b.end for b in anchors), default=start))
    free = free_intervals((start, day_end), [(b.start, b.end) for b in anchors])

    # place commit sessions where they actually happened
    day_commits = [c for c in commits if c.ts.date() == date.date()]
    sessions = _cluster(day_commits, cfg.session_gap_minutes)
    focus: list[Block] = []
    dropped = 0
    for sess in sessions:
        want = (snap(sess[0].ts, cfg.snap_minutes),
                snap(sess[-1].ts, cfg.snap_minutes) + timedelta(minutes=cfg.min_block_minutes))
        slot = _best_slot(free, want)
        if slot is None or (cfg.include_after_hours is False and want[0] >= day_end):
            dropped += 1
            continue
        b_start = max(slot[0], min(want[0], slot[1] - timedelta(minutes=cfg.min_block_minutes)))
        b_start = max(b_start, slot[0])
        length = max(cfg.min_block_minutes, mins(want))
        b_end = min(slot[1], b_start + timedelta(minutes=length))
        if mins((b_start, b_end)) < cfg.snap_minutes:
            dropped += 1
            continue
        project = classify_focus(sess, cfg)
        taak = summarize_block(sess, project, cfg, llm)
        for piece in _split((b_start, b_end), cfg.max_focus_minutes, cfg.snap_minutes):
            focus.append(Block(piece[0], piece[1], project, taak, "focus"))
        free = subtract(free, (b_start, b_end))

    # fill whatever's left with admin, split into digestible chunks
    admin: list[Block] = []
    for iv in free:
        if mins(iv) < cfg.snap_minutes:
            continue
        for piece in _split(iv, cfg.max_admin_minutes, cfg.snap_minutes):
            if mins(piece) >= cfg.snap_minutes:
                admin.append(Block(piece[0], piece[1], cfg.admin_project, cfg.admin_taak, "admin"))

    blocks = sorted(anchors + focus + admin, key=lambda b: b.start)
    return Day(date=date, blocks=blocks, dropped_after_hours=dropped)


def reconstruct_week(meetings: list[Meeting], commits: list[Commit],
                     week_start: datetime, cfg: Config, llm=None) -> list[Day]:
    tz = ZoneInfo(cfg.tz)
    days: list[Day] = []
    for i in range(7):
        date = week_start + timedelta(days=i)
        if date.weekday() not in cfg.workdays:
            continue
        m = [x for x in meetings if x.start.date() == date.date()]
        c = [x for x in commits if x.ts.date() == date.date()]
        if not m and not c:
            continue                       # nothing happened (leave / weekend)
        days.append(reconstruct_day(date, m, commits, cfg, tz, llm))
    return days
