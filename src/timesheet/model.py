"""Normalized signals + the reconstructed Block.

Collectors emit Meeting / FullDayEvent / Commit / ActivityEvent; `activity.py`
folds the point events into sessions; the reconstructor emits Block; the
renderers consume Block."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime


@dataclass
class Meeting:
    start: datetime          # tz-aware, local
    end: datetime
    project: str
    taak: str


@dataclass
class FullDayEvent:
    """An all-day calendar event that owns a whole work-day (leave, a training day).

    A date, not a moment: an all-day event has no local start time, and the day it
    covers is what the sheet reports."""
    date: _date              # local calendar date
    project: str             # 'Project / client' column
    taak: str                # the calendar subject, cleaned
    kind: str = "leave"      # leave | meeting


@dataclass
class Commit:
    ts: datetime             # tz-aware, local
    repo: str
    message: str             # first line, merges already dropped
    kind: str = "commit"     # commit | review | pr | issue


@dataclass
class ActivityEvent:
    """One timestamped act of correspondence: a message sent, a mail received.

    Carries no content — only when it happened and, where the source gives one, a
    subject used to label the block it lands in. Nothing downstream stores or
    renders message bodies.
    """
    ts: datetime             # tz-aware, local
    kind: str                # email_sent | email_received | chat
    subject: str = ""


@dataclass
class ActivitySession:
    """A run of correspondence with no long gap in it, and the effort it is worth.

    `minutes` is already the credited figure (lead-in + span, see SignalSpec), not
    the wall-clock span, so a single message is worth its lead-in rather than zero.
    """
    start: datetime
    end: datetime
    kind: str
    count: int
    minutes: int
    subjects: tuple[str, ...] = ()


@dataclass
class Block:
    start: datetime
    end: datetime
    project: str             # 'Project / client' column
    taak: str                # 'Task' column
    kind: str = "focus"      # rota | meeting | focus | admin | leave | email | chat

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "project": self.project, "taak": self.taak, "kind": self.kind}

    @classmethod
    def from_dict(cls, d: dict) -> Block:
        return cls(datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]),
                   d["project"], d["taak"], d.get("kind", "focus"))


@dataclass
class Day:
    date: datetime
    blocks: list[Block]
    dropped_after_hours: int = 0     # commit sessions outside the workday we didn't log

    @property
    def minutes(self) -> int:
        return sum(b.minutes for b in self.blocks)

    def to_dict(self) -> dict:
        return {"date": self.date.isoformat(), "blocks": [b.to_dict() for b in self.blocks],
                "dropped_after_hours": self.dropped_after_hours}

    @classmethod
    def from_dict(cls, d: dict) -> Day:
        return cls(datetime.fromisoformat(d["date"]),
                   [Block.from_dict(b) for b in d["blocks"]],
                   d.get("dropped_after_hours", 0))
