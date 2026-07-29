"""Named date ranges for the filter: this/last week, this/last month.

A period is a inclusive [start, end] date range. The service turns it into the set
of Mondays whose weeks overlap the range, pulls each week's reconstructed days from
the store (building+caching any that are missing), then trims to the range."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

PERIOD_KEYS = ("this-week", "last-week", "this-month", "last-month")
DEFAULT = "this-week"
_LABELS = {
    "this-week": "Deze week",
    "last-week": "Vorige week",
    "this-month": "Deze maand",
    "last-month": "Vorige maand",
}


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date      # inclusive
    end: date        # inclusive
    is_month: bool


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
        else first.replace(month=first.month + 1)
    return first, nxt - timedelta(days=1)


def resolve_period(key: str | None, today: date) -> Period:
    key = key if key in PERIOD_KEYS else DEFAULT
    if key == "this-week":
        m = _monday(today)
        return Period(key, _LABELS[key], m, m + timedelta(days=6), False)
    if key == "last-week":
        m = _monday(today) - timedelta(days=7)
        return Period(key, _LABELS[key], m, m + timedelta(days=6), False)
    if key == "this-month":
        a, b = _month_bounds(today)
        return Period(key, _LABELS[key], a, b, True)
    # last-month
    a, b = _month_bounds(today.replace(day=1) - timedelta(days=1))
    return Period(key, _LABELS[key], a, b, True)


def mondays_covering(start: date, end: date) -> list[date]:
    """Mondays of every Mon–Sun week that overlaps [start, end]."""
    m, out = _monday(start), []
    while m <= end:
        out.append(m)
        m += timedelta(days=7)
    return out
