"""Plain-text week preview — for CLI/debugging and log output."""
from __future__ import annotations

from .. import i18n
from ..model import Day
from ..timeutil import hm


def render_text(week: list[Day], locale: str | i18n.Locale | None = None) -> str:
    loc = locale if isinstance(locale, i18n.Locale) else i18n.get(locale)
    lines, grand = [], 0
    for day in week:
        grand += day.minutes
        d = day.date
        lines.append(f"\n{loc.days[d.weekday()]} {d.day} {loc.months[d.month]}"
                     f"  ·  {hm(day.minutes)}{loc.subtitle_hours}")
        lines.append(f"  {loc.col_from:>5} {loc.col_to:>5} {loc.col_duration:>5}  "
                     f"{loc.col_project:16} {loc.col_task}")
        for b in day.blocks:
            lines.append(f"  {b.start:%H:%M} {b.end:%H:%M} {hm(b.minutes):>5}  "
                         f"{b.project:16} {b.taak}")
        if day.dropped_after_hours:
            lines.append(f"  (+{day.dropped_after_hours} after-hours commit session(s) not logged)")
    lines.append(f"\n{loc.total}  ·  {hm(grand)}")
    return "\n".join(lines)
