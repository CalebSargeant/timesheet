"""Normalized signals + the reconstructed Block. Collectors emit Meeting/Commit;
the reconstructor emits Block; the renderers consume Block."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Meeting:
    start: datetime          # tz-aware, local
    end: datetime
    project: str
    taak: str


@dataclass
class Commit:
    ts: datetime             # tz-aware, local
    repo: str
    message: str             # first line, merges already dropped


@dataclass
class Block:
    start: datetime
    end: datetime
    project: str             # 'Project / klant' column
    taak: str                # 'Taak' column
    kind: str = "focus"      # rota | meeting | focus | admin

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Day:
    date: datetime
    blocks: list[Block]
    dropped_after_hours: int = 0     # commit sessions outside the workday we didn't log

    @property
    def minutes(self) -> int:
        return sum(b.minutes for b in self.blocks)
