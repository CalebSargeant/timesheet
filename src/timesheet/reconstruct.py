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
from .summarize import classify_focus, summarize_lines
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
    whole day of leave is known up front, not accrued hour by hour.

    Its length is the person's own normal day (`min_day_minutes`). A four-day-week
    contract whose days are six hours got eight hours of leave out of a separate
    constant, which is both wrong and, on a timesheet somebody signs, awkward."""
    start = _at(date, cfg.day_start)
    block = Block(start, start + timedelta(minutes=cfg.min_day_minutes),
                  ev.project, ev.taak, ev.kind)
    return Day(date=date, blocks=[block], dropped_after_hours=0)


def _budgeted(chunks: list[Interval], budget: int) -> list[Interval]:
    """`chunks` trimmed to `budget` minutes of fillable time.

    This is what keeps the day the length the evidence says it is. Free time runs
    to the last thing in the calendar, so without a budget a single 20:00 call
    stretched the window to 21:00 and every quarter of an hour between the end of
    the working day and that call was filled in as admin — one hour of evening
    incident work reported as four and a half hours of invented afternoon.

    Anchors keep their real times either way; what is bounded is how much
    reconstructed work may be placed around them.
    """
    out: list[Interval] = []
    placed = 0
    for start, end in chunks:
        if placed >= budget:
            break
        span = mins((start, end))
        if placed + span > budget:
            end = start + timedelta(minutes=budget - placed)
            if end <= start:
                break
        out.append((start, end))
        placed += mins((start, end))
    return out


def _chunks(free: list[Interval], cfg: Config) -> list[Interval]:
    """Free time cut into fillable pieces, avoiding slivers where possible.

    A short tail is folded back into the piece before it rather than emitted on
    its own. Nothing is discarded: the quarter of an hour between arriving and
    the stand-up is real time at a desk, and dropping it would make the day open
    later than the person actually started.
    """
    out: list[Interval] = []
    for iv in free:
        pieces = _split(iv, cfg.max_admin_minutes, cfg.snap_minutes)
        if len(pieces) > 1 and mins(pieces[-1]) < cfg.min_block_minutes:
            tail = pieces.pop()
            pieces[-1] = (pieces[-1][0], tail[1])
        out.extend(pieces)
    return out


def _shares(themes: list[tuple[str, list[str], int]], budget: int) -> list[int]:
    """How many minutes of the day each theme gets, proportional to its weight.

    Rounded up, so rounding never strands a theme on zero and leaves the last one
    silently swallowing the whole day.
    """
    if not themes:
        return []
    total = sum(w for _, _, w in themes) or len(themes)
    return [max(1, -(-budget * w // total)) for _, _, w in themes]


def _merge_repeats(blocks: list[Block], cfg: Config) -> list[Block]:
    """Fuse adjacent blocks that say exactly the same thing.

    Two consecutive rows with an identical project and task are not two pieces of
    work, they are one piece the chunker happened to cut in half. Capped at
    `max_focus_minutes` so merging cannot recreate the mega-block problem.
    """
    out: list[Block] = []
    for b in blocks:
        prev = out[-1] if out else None
        if (prev and prev.kind == b.kind and prev.project == b.project
                and prev.taak == b.taak and prev.end == b.start
                and (b.end - prev.start).total_seconds() / 60 <= cfg.max_focus_minutes):
            out[-1] = Block(prev.start, b.end, prev.project, prev.taak, prev.kind)
        else:
            out.append(b)
    return out


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
    # What may still be placed around the anchors. `target` is the whole day; the
    # anchors are the part of it that is already accounted for and already sits at
    # a real time, so the rest is what gets reconstructed.
    fill_budget = max(0, target - fixed_min)

    # The day's GitHub work themes, clustered PER KIND so PRs, issues, reviews and
    # commits each surface with their own label (Pull requests / Issues / Code review
    # / a commit category) instead of dissolving into one mixed 'Development' session.
    # Each theme carries the weight of the session behind it, so a cluster of
    # fifteen commits gets more of the day than a cluster of one.
    themes: list[tuple[str, list[str], int]] = []
    for group in (day_commits, day_prs, day_issues, day_reviews):
        for sess in _cluster(group, cfg.session_gap_minutes):
            project = classify_focus(sess, cfg)
            # Enough lines to cover the rows this theme's run could occupy, so a
            # long afternoon on one theme does not render as the same sentence
            # three times over.
            rows = max(1, -(-cfg.max_day_minutes // cfg.max_focus_minutes))
            themes.append((project, summarize_lines(sess, project, cfg, llm, rows), len(sess)))

    # Fill the free time. A chunk that sits where correspondence actually happened
    # is labelled as correspondence; then about `activity_min` of what is left goes
    # to the day's GitHub themes; the remainder is admin. So the sheet's hours
    # track the evidence, and a generic admin label is only the leftover.
    free_total = min(sum(mins(iv) for iv in free), fill_budget)
    comms_budget = max(0, min(comms_min, free_total - cfg.admin_floor_minutes))
    coding_budget = max(0, min(round(activity_min),
                               free_total - comms_budget - cfg.admin_floor_minutes))
    chunks = _budgeted(_chunks(free, cfg), fill_budget)
    admin_tasks = tuple(labels.admin_tasks) or (labels.admin_project,)
    # Each theme gets one contiguous run of the day, sized by its weight. Cycling
    # per chunk instead produced Development/Security/Development/Security down
    # the whole afternoon — which is not what anyone's day looks like, and made
    # the sheet read as generated rather than recorded.
    shares = _shares(themes, coding_budget)
    fill: list[Block] = []
    spent_comms = spent_focus = 0
    ti = ai = 0
    in_theme = used_lines = 0
    for a, b in chunks:
        # A piece too short to be a real sitting of anything gets the generic
        # label. `min_block_minutes` was declared and never read, which is how a
        # fifteen-minute gap between two meetings came out as a lone
        # "Development" row — an obvious piece of padding on an otherwise
        # defensible sheet.
        substantial = mins((a, b)) >= cfg.min_block_minutes
        here = [s for s in comms if a < s.end + timedelta(minutes=1) and s.start < b]
        if here and substantial and spent_comms < comms_budget:
            project, taak = act.label(here[0], cfg)
            spent_comms += mins((a, b))
            fill.append(Block(a, b, project, taak,
                              "chat" if here[0].kind == "chat" else "email"))
        elif themes and substantial and spent_focus < coding_budget:
            # Move on once this theme has had its share, so themes come out as
            # runs rather than interleaved.
            while ti < len(themes) - 1 and in_theme >= shares[ti]:
                ti += 1
                in_theme = used_lines = 0
            project, lines, _ = themes[ti]
            taak = lines[used_lines % len(lines)]
            used_lines += 1
            in_theme += mins((a, b))
            spent_focus += mins((a, b))
            fill.append(Block(a, b, project, taak, "focus"))
        else:
            # rotate the admin label so a quiet day isn't a column of identical rows
            fill.append(Block(a, b, labels.admin_project, admin_tasks[ai % len(admin_tasks)],
                              "admin"))
            ai += 1

    fill = _merge_repeats(fill, cfg)

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
