"""Plain-text week preview — for CLI/debugging and log output."""
from __future__ import annotations

from ..model import Day
from ..timeutil import NL_DAYS, NL_MONTHS, hm


def render_text(week: list[Day]) -> str:
    lines, grand = [], 0
    for day in week:
        grand += day.minutes
        d = day.date
        lines.append(f"\n{NL_DAYS[d.weekday()]} {d.day} {NL_MONTHS[d.month]}  ·  {hm(day.minutes)}u")
        lines.append(f"  {'Van':>5} {'Tot':>5} {'Duur':>5}  {'Project / klant':16} Taak")
        for b in day.blocks:
            lines.append(f"  {b.start:%H:%M} {b.end:%H:%M} {hm(b.minutes):>5}  "
                         f"{b.project:16} {b.taak}")
        if day.dropped_after_hours:
            lines.append(f"  (+{day.dropped_after_hours} after-hours commit session(s) not logged)")
    lines.append(f"\nTOTAAL  ·  {hm(grand)}")
    return "\n".join(lines)
