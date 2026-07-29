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
from .timeutil import Interval, free_intervals, mins, parse_hhmm, resolve_overlaps, snap


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


def _git_hours(commits: list[Commit], gap_min: int, lead_min: int) -> float:
    """Empirical coding time from commit timestamps (the kimmobrunfeldt session
    heuristic, matching github-contributions/effort.session_hours): consecutive
    commits closer than `gap_min` share a session (add the real gap); a larger gap
    starts a new session (credit a fixed lead-in for the unseen work before it)."""
    ts = sorted(c.ts.timestamp() for c in commits)
    if not ts:
        return 0.0
    gap, lead = gap_min * 60, lead_min * 60
    seconds = float(lead)
    for prev, cur in pairwise(ts):
        delta = cur - prev
        seconds += delta if delta < gap else lead
    return seconds / 3600.0


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

    # The day length is DRIVEN by real effort: rota + meetings + estimated coding
    # time (git-hours of the day's commits) + a little admin, floored to a normal day
    # and capped so a marathon day stays believable. A heavy coding day shows well
    # over 8h, so a prolific week is not flattened to 40h.
    day_commits = [c for c in commits if c.ts.date() == date.date()]
    coding_min = _git_hours(day_commits, cfg.session_gap_minutes, cfg.first_commit_minutes) * 60.0
    fixed_min = sum(b.minutes for b in anchors)
    target = min(max(int(fixed_min + coding_min + cfg.admin_floor_minutes),
                     cfg.min_day_minutes), cfg.max_day_minutes)
    day_end = max(start + timedelta(minutes=target),
                  max((b.end for b in anchors), default=start))
    free = free_intervals((start, day_end), [(b.start, b.end) for b in anchors])

    # The day's GHE work themes: one per commit session (classified + summarised).
    themes: list[tuple[str, str]] = []
    for sess in _cluster(day_commits, cfg.session_gap_minutes):
        project = classify_focus(sess, cfg)
        themes.append((project, summarize_block(sess, project, cfg, llm)))

    # Fill the free time: about `coding_min` of it is the GHE work (labelled from the
    # day's commit themes, cycled across the chunks); the remainder is admin. So the
    # sheet's coding hours track the git-hours estimate, and generic "Mail / GitHub /
    # Teams" is only the leftover, not the bulk of a busy day.
    free_total = sum(mins(iv) for iv in free)
    coding_budget = max(0, min(round(coding_min), free_total - cfg.admin_floor_minutes))
    chunks = [p for iv in free
              for p in _split(iv, cfg.max_admin_minutes, cfg.snap_minutes)
              if mins(p) >= cfg.snap_minutes]
    fill: list[Block] = []
    assigned, ti = 0, 0
    for a, b in chunks:
        if themes and assigned < coding_budget:
            project, taak = themes[ti % len(themes)]
            ti += 1
            assigned += mins((a, b))
            fill.append(Block(a, b, project, taak, "focus"))
        else:
            fill.append(Block(a, b, cfg.admin_project, cfg.admin_taak, "admin"))

    blocks = sorted(anchors + fill, key=lambda b: b.start)
    return Day(date=date, blocks=blocks, dropped_after_hours=0)


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
