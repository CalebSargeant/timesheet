"""Point-in-time events -> credited minutes.

Commits, emails and chat messages are all the same shape of evidence: a stamp
saying "this person did something at this moment", with the actual work either
side of it invisible. The session heuristic is the standard answer — consecutive
events closer together than `gap_minutes` belong to one sitting and the real gap
between them is counted; a larger gap starts a new sitting and credits a fixed
lead-in for the work that must have come before its first event.

That single rule is what keeps the numbers honest in both directions. Forty chat
messages fired off in ten minutes are worth ten minutes plus a lead-in, not forty
messages' worth. One email at 14:00 and one at 16:30 are two sittings, not a
two-and-a-half-hour block of email.

Correspondence is then **clipped against the day's meetings** before any of it is
credited: replying to mail during a call is the call, and counting both would
inflate every meeting-heavy day. Finally each source is capped per day, because
a mailbox that takes 400 messages says more about the sender's distribution list
than about the reader's hours.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import Config, SignalSpec
from .model import ActivityEvent, ActivitySession
from .timeutil import Interval, mins


def session_minutes(stamps: list[datetime], gap_minutes: int, lead_minutes: int) -> float:
    """Credited minutes for a set of timestamps under the session heuristic.

    Equal to, for each sitting, `lead_minutes` plus the sitting's own real span.
    Empty input is zero — never a lead-in for nothing.
    """
    ts = sorted(t.timestamp() for t in stamps)
    if not ts:
        return 0.0
    gap, lead = gap_minutes * 60.0, lead_minutes * 60.0
    seconds = lead
    for prev, cur in zip(ts, ts[1:]):
        delta = cur - prev
        seconds += delta if delta < gap else lead
    return seconds / 60.0


def cluster(events: list[ActivityEvent], gap_minutes: int) -> list[list[ActivityEvent]]:
    """Split events into sittings: a new one starts after a gap of `gap_minutes`."""
    ordered = sorted(events, key=lambda e: e.ts)
    if not ordered:
        return []
    out, cur = [], [ordered[0]]
    for prev, ev in zip(ordered, ordered[1:]):
        if (ev.ts - prev.ts).total_seconds() / 60.0 <= gap_minutes:
            cur.append(ev)
        else:
            out.append(cur)
            cur = [ev]
    out.append(cur)
    return out


def _clip(span: Interval, busy: list[Interval]) -> list[Interval]:
    """`span` minus every busy interval — what is left of a sitting outside meetings.

    A sitting of one message has no span at all, and a zero-length interval
    cannot be subtracted from: it either falls inside a meeting or it does not.
    Handling that separately is the difference between crediting a lone email and
    silently dropping every one of them.
    """
    start, end = span
    if end <= start:
        inside = any(lo <= start < hi for lo, hi in busy)
        return [] if inside else [span]
    free = [span]
    for b in sorted(busy):
        nxt: list[Interval] = []
        for s, e in free:
            if b[1] <= s or b[0] >= e:
                nxt.append((s, e))
                continue
            if s < b[0]:
                nxt.append((s, b[0]))
            if b[1] < e:
                nxt.append((b[1], e))
        free = nxt
    return [iv for iv in free if iv[1] > iv[0]]


def sessions(events: list[ActivityEvent], spec: SignalSpec, *,
             busy: list[Interval] | None = None) -> list[ActivitySession]:
    """Sittings for one source, clipped against `busy` and capped at `spec.cap_minutes`.

    A sitting swallowed whole by a meeting disappears: its lead-in is the work
    that led up to the first message, and during a call that work is the call.
    A sitting only partly covered keeps the uncovered part and is credited pro
    rata, so a lunchtime inbox sweep that runs into a 14:00 stand-up still counts
    for the half of it that happened first.

    The cap is applied by dropping whole sittings from the end of the day, not by
    shaving every one of them, so what survives still lines up with real moments.
    """
    out: list[ActivitySession] = []
    for group in cluster(events, spec.gap_minutes):
        start, end = group[0].ts, group[-1].ts
        credited = session_minutes([e.ts for e in group], spec.gap_minutes, spec.lead_minutes)
        span = (start, end)
        if busy:
            remaining = _clip(span, busy)
            if not remaining:
                continue
            # Pro rata on the visible span. A single-message sitting has no span at
            # all, so it survives on the strength of being outside a meeting.
            whole = mins(span)
            if whole > 0:
                credited *= sum(mins(iv) for iv in remaining) / whole
            start, end = remaining[0][0], remaining[-1][1]
        minutes = int(round(credited))
        if minutes <= 0:
            continue
        subjects = tuple(dict.fromkeys(e.subject for e in group if e.subject))[:4]
        out.append(ActivitySession(start=start, end=max(end, start), kind=spec.kind,
                                   count=len(group), minutes=minutes, subjects=subjects))

    out.sort(key=lambda s: s.start)
    kept, running = [], 0
    for s in out:
        if running >= spec.cap_minutes:
            break
        if running + s.minutes > spec.cap_minutes:
            s = ActivitySession(s.start, s.end, s.kind, s.count,
                                spec.cap_minutes - running, s.subjects)
        kept.append(s)
        running += s.minutes
    return kept


def specs(cfg: Config) -> dict[str, SignalSpec]:
    """The enabled correspondence sources, keyed by event kind."""
    out: dict[str, SignalSpec] = {}
    if cfg.include_email:
        out["email_sent"] = cfg.email_sent
        out["email_received"] = cfg.email_received
    if cfg.include_chat:
        out["chat"] = cfg.chat
    return out


def day_sessions(events: list[ActivityEvent], cfg: Config, *,
                 busy: list[Interval] | None = None) -> list[ActivitySession]:
    """Every enabled source's sittings for one day, already clipped and capped."""
    by_kind: dict[str, list[ActivityEvent]] = {}
    for ev in events:
        by_kind.setdefault(ev.kind, []).append(ev)
    out: list[ActivitySession] = []
    for kind, spec in specs(cfg).items():
        if by_kind.get(kind):
            out.extend(sessions(by_kind[kind], spec, busy=busy if cfg.clip_comms_to_free_time
                                else None))
    out.sort(key=lambda s: s.start)
    return out


def total_minutes(found: list[ActivitySession]) -> int:
    return sum(s.minutes for s in found)


def label(session: ActivitySession, cfg: Config) -> tuple[str, str]:
    """(project, task) for a correspondence block.

    The task names the real subjects when the connector gave any, because "Re:
    incident 4412, deployment window" is a far better line on a timesheet than
    "Correspondence" — and falls back to a plain count, which is still true.
    """
    labels = cfg.labels
    project = labels.chat_project if session.kind == "chat" else labels.email_project
    if session.subjects:
        joined = ", ".join(s.strip() for s in session.subjects if s.strip())
        if joined:
            return project, joined[:90]
    noun = {
        "chat": "chat messages",
        "email_sent": "emails sent",
        "email_received": "emails received",
    }.get(session.kind, "messages")
    return project, f"{session.count} {noun}"


def spread(found: list[ActivitySession], gap: timedelta = timedelta(minutes=1)) -> list[Interval]:
    """The spans correspondence actually occupied, merged where they touch.

    Used to decide which free-time chunk gets a correspondence label rather than a
    generic admin one.
    """
    spans = sorted((s.start, max(s.end, s.start)) for s in found)
    out: list[Interval] = []
    for s, e in spans:
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
