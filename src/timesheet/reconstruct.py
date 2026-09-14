"""Turn normalized signals into a believable day of timesheet blocks.

Algorithm (all deterministic):
  1. Fixed anchors  = calendar meetings (overlaps trimmed); optional on-call rota.
  2. Correspondence = email and chat sittings, clipped against those anchors so a
                      reply typed during a call is the call, and capped per source.
  3. Day length     = anchors + git-hours of commits + capped credit for
                      reviews/PRs/issues + correspondence, floored to 8h on core
                      days so a quiet week still reads as a working week.
  4. Free time      = the workday window [start, start+length] minus the anchors.
  5. Blocks         = free time filled with, in priority order, the correspondence
                      that really happened in that slot, the day's GitHub work
                      themes, and finally generic admin.

The day opens at 08:30 (earlier if activity shows it). Work past midnight rolls
back to the day it started (day_rollover_hour). The in-progress day is capped at
'now', and days that haven't happened yet are not emitted.

A full-day busy calendar event (leave) short-circuits all of this: that day is
reported as the event, not reconstructed (see `full_day`).
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from . import activity as act
from .config import Config
from .model import ActivityEvent, ActivitySession, Block, Commit, Day, FullDayEvent, Meeting
from .summarize import classify_focus, summarize_block
from .timeutil import Interval, free_intervals, mins, parse_hhmm, resolve_overlaps, snap

# Correspondence that proves someone was at their desk. Mail *arriving* proves
# only that a sender was, so it never drags a day open early.
_STARTS_DAY = ("email_sent", "chat")


def _worked(meetings: list, commits: list, events: list | None) -> bool:
    """Did this day carry evidence that the person actually worked?

    Incoming mail alone does not count, and deliberately so. A mailing list that
    fires on a Sunday would otherwise manufacture a whole working day — and on a
    core weekday it would be floored to eight hours, inventing a full day out of
    someone else's send button. Every other signal here is an act: a commit, a
    meeting attended, a message sent.
    """
    if meetings or commits:
        return True
    return any(e.kind in _STARTS_DAY for e in (events or []))


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
    """Empirical coding time from commit timestamps (the session heuristic shared
    with correspondence — see `activity.session_minutes`): consecutive commits
    closer than `gap_min` share a session (add the real gap); a larger gap starts
    a new session (credit a fixed lead-in for the unseen work before it))."""
    return act.session_minutes([c.ts for c in commits], gap_min, lead_min) / 60.0


def _at(day: datetime, hhmm: str) -> datetime:
    return day.replace(hour=parse_hhmm(hhmm) // 60, minute=parse_hhmm(hhmm) % 60,
                       second=0, microsecond=0)


def logical_date(dt: datetime, rollover_hour: int):
    """The work-day an event belongs to: activity before `rollover_hour` counts as
    the previous day, so 01:00 work is credited to the night before, not a phantom
    early start the next morning."""
    return (dt - timedelta(hours=rollover_hour)).date()


def full_day(date: datetime, ev: FullDayEvent, cfg: Config) -> Day:
    """A day owned by one all-day busy calendar event: a single normal-length block.

    Nothing is reconstructed around it. A booked day off is a fact, not an estimate,
    so a stray commit or a standup invite that stayed in the calendar can't turn
    leave back into a working day — and it isn't clipped at 'now' either, because a
    whole day of leave is known up front, not accrued hour by hour."""
    start = _at(date, cfg.day_start)
    block = Block(start, start + timedelta(minutes=cfg.full_day_minutes),
                  ev.project, ev.taak, ev.kind)
    return Day(date=date, blocks=[block], dropped_after_hours=0)


def reconstruct_day(date: datetime, meetings: list[Meeting], commits: list[Commit],
                    cfg: Config, tz: ZoneInfo, llm=None, *,
                    events: list[ActivityEvent] | None = None,
                    now: datetime | None = None, floor: int | None = None) -> Day:
    # `commits`/`meetings`/`events` are already bucketed to this work-day by the
    # caller. `now` (set only for the in-progress day) caps the day so nothing past
    # the current moment is shown. `floor` is the day's minimum length (8h on core
    # days, 0 on weekends so a Saturday shows real effort, not a padded 8h).
    labels = cfg.labels
    if floor is None:
        floor = cfg.min_day_minutes
    events = list(events or [])
    if now is not None:                          # today: drop/clip anything after now
        meetings = [Meeting(m.start, min(m.end, now), m.project, m.taak)
                    for m in meetings if m.start < now]
        events = [e for e in events if e.ts < now]
    day_events = list(commits)
    if not _worked(meetings, day_events, events):
        return Day(date=date, blocks=[], dropped_after_hours=0)
    day_commits = [c for c in day_events if c.kind == "commit"]
    day_reviews = [c for c in day_events if c.kind == "review"]
    day_prs = [c for c in day_events if c.kind == "pr"]
    day_issues = [c for c in day_events if c.kind == "issue"]
    # Start at the configured hour (08:30), but open earlier if the day's own
    # activity — an early commit, a meeting, a mail actually sent — proves work
    # began sooner. Never before the floor, so a stray late-night commit can't
    # drag the day open. (Reviews and *incoming* mail are excluded: both can land
    # off-hours without anyone being at a desk.)
    default_start = _at(date, cfg.day_start)
    earliest = min([c.ts for c in day_commits] + [m.start for m in meetings]
                   + [e.ts for e in events if e.kind in _STARTS_DAY],
                   default=default_start)
    start = min(default_start, max(earliest, _at(date, cfg.earliest_start_floor)))

    fixed: list[tuple[Interval, str, str, str]] = []   # (interval, project, taak, kind)
    if cfg.rota_enabled:
        fixed.append(((start, start + timedelta(minutes=cfg.rota_minutes)),
                      labels.rota_project, labels.rota_task, "rota"))
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

    # Correspondence, clipped against those anchors and capped per source. Done
    # after the anchors exist precisely so that the clipping can happen: an hour
    # of replies during a two-hour workshop is the workshop, and crediting both
    # would push every meeting-heavy day past twelve hours.
    busy = [(b.start, b.end) for b in anchors]
    comms = act.day_sessions(events, cfg, busy=busy)
    comms_min = act.total_minutes(comms)

    # The day length is DRIVEN by real effort: rota + meetings + estimated coding
    # time (git-hours of the day's commits) + non-commit GitHub work (reviews, PRs
    # opened, issues) + correspondence + a little admin, floored to a normal day.
    # A heavy day shows well over 8h, so a prolific week is not flattened to 40h.
    coding_min = _git_hours(day_commits, cfg.session_gap_minutes, cfg.first_commit_minutes) * 60.0
    # Non-commit work is credited per item (reviews deduped per PR in the collector),
    # capped per day — NOT via git-hours: these are point events, not 2h of lead-in.
    noncommit_min = min(
        len(day_reviews) * cfg.review_minutes_each
        + len(day_prs) * cfg.pr_minutes_each
        + len(day_issues) * cfg.issue_minutes_each,
        cfg.noncommit_cap_minutes,
    )
    activity_min = coding_min + noncommit_min
    fixed_min = sum(b.minutes for b in anchors)
    target = max(int(fixed_min + activity_min + comms_min + cfg.admin_floor_minutes), floor)
    day_end = max(start + timedelta(minutes=target),
                  max((b.end for b in anchors), default=start))
    if now is not None:                          # never show blocks past the current moment
        day_end = min(day_end, now.replace(second=0, microsecond=0))
        if day_end <= start:                     # the day hasn't really started yet
            return Day(date=date, blocks=[], dropped_after_hours=0)
    free = free_intervals((start, day_end), [(b.start, b.end) for b in anchors])

    # The day's GitHub work themes, clustered PER KIND so PRs, issues, reviews and
    # commits each surface with their own label (Pull requests / Issues / Code review
    # / a commit category) instead of dissolving into one mixed 'Development' session.
    themes: list[tuple[str, str]] = []
    for group in (day_commits, day_prs, day_issues, day_reviews):
        for sess in _cluster(group, cfg.session_gap_minutes):
            project = classify_focus(sess, cfg)
            themes.append((project, summarize_block(sess, project, cfg, llm)))

    # Fill the free time. A chunk that sits where correspondence actually happened
    # is labelled as correspondence; then about `activity_min` of what is left goes
    # to the day's GitHub themes; the remainder is admin. So the sheet's hours
    # track the evidence, and a generic admin label is only the leftover.
    free_total = sum(mins(iv) for iv in free)
    comms_budget = max(0, min(comms_min, free_total - cfg.admin_floor_minutes))
    coding_budget = max(0, min(round(activity_min),
                               free_total - comms_budget - cfg.admin_floor_minutes))
    chunks = [p for iv in free
              for p in _split(iv, cfg.max_admin_minutes, cfg.snap_minutes)
              if mins(p) >= cfg.snap_minutes]
    admin_tasks = tuple(labels.admin_tasks) or (labels.admin_project,)
    fill: list[Block] = []
    spent_comms = spent_focus = 0
    ti = ai = 0
    for a, b in chunks:
        here = [s for s in comms if a < s.end + timedelta(minutes=1) and s.start < b]
        if here and spent_comms < comms_budget:
            project, taak = act.label(here[0], cfg)
            spent_comms += mins((a, b))
            fill.append(Block(a, b, project, taak,
                              "chat" if here[0].kind == "chat" else "email"))
        elif themes and spent_focus < coding_budget:
            project, taak = themes[ti % len(themes)]
            ti += 1
            spent_focus += mins((a, b))
            fill.append(Block(a, b, project, taak, "focus"))
        else:
            # rotate the admin label so a quiet day isn't a column of identical rows
            fill.append(Block(a, b, labels.admin_project, admin_tasks[ai % len(admin_tasks)],
                              "admin"))
            ai += 1

    blocks = sorted(anchors + fill, key=lambda b: b.start)
    return Day(date=date, blocks=blocks, dropped_after_hours=0)


def reconstruct_week(meetings: list[Meeting], commits: list[Commit],
                     week_start: datetime, cfg: Config, llm=None, *,
                     events: list[ActivityEvent] | None = None,
                     now: datetime | None = None,
                     full_days: list[FullDayEvent] | None = None) -> list[Day]:
    tz = ZoneInfo(cfg.tz)
    if now is None:
        now = datetime.now(tz)
    roll = cfg.day_rollover_hour
    today = logical_date(now, roll)
    owned: dict[_date, FullDayEvent] = {}
    for ev in full_days or []:             # leave wins if a date has several all-day events
        cur = owned.get(ev.date)
        if cur is None or (cur.kind != "leave" and ev.kind == "leave"):
            owned[ev.date] = ev
    days: list[Day] = []
    for i in range(7):
        date = week_start + timedelta(days=i)
        d = date.date()
        if d > today:
            continue                       # a day that hasn't happened yet
        is_core = date.weekday() in cfg.workdays
        if is_core and d in owned:         # leave / another all-day busy event owns the day
            days.append(full_day(date, owned[d], cfg))
            continue
        # bucket by work-day (past-midnight work rolls back to the day it started)
        m = [x for x in meetings if logical_date(x.start, roll) == d]
        c = [x for x in commits if logical_date(x.ts, roll) == d]
        e = [x for x in (events or []) if logical_date(x.ts, roll) == d]
        if not _worked(m, c, e):
            continue                       # nothing happened (quiet weekend)
        day = reconstruct_day(
            date, m, c, cfg, tz, llm, events=e,
            now=(now if d == today else None),
            floor=(cfg.min_day_minutes if is_core else 0),   # weekends: real effort, no 8h floor
        )
        if day.blocks:                     # skip an in-progress day that hasn't started yet
            days.append(day)
    return days


__all__ = ["ActivitySession", "full_day", "logical_date", "reconstruct_day", "reconstruct_week"]
