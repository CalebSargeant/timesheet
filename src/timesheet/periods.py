"""Named date ranges for the filter: this/last week, this/last month.

A period is a inclusive [start, end] date range. The service turns it into the set
of Mondays whose weeks overlap the range, pulls each week's reconstructed days from
the store (building+caching any that are missing), then trims to the range."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import i18n

PERIOD_KEYS = ("this-week", "last-week", "this-month", "last-month")
DEFAULT = "this-week"


def _labels(loc: i18n.Locale) -> dict[str, str]:
    return {
        "this-week": loc.period_this_week,
        "last-week": loc.period_last_week,
        "this-month": loc.period_this_month,
        "last-month": loc.period_last_month,
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


def resolve_period(key: str | None, today: date,
                   locale: str | i18n.Locale | None = None) -> Period:
    loc = locale if isinstance(locale, i18n.Locale) else i18n.get(locale)
    labels = _labels(loc)
    key = key if key in PERIOD_KEYS else DEFAULT
    if key == "this-week":
        m = _monday(today)
        return Period(key, labels[key], m, m + timedelta(days=6), False)
    if key == "last-week":
        m = _monday(today) - timedelta(days=7)
        return Period(key, labels[key], m, m + timedelta(days=6), False)
    if key == "this-month":
        a, b = _month_bounds(today)
        return Period(key, labels[key], a, b, True)
    # last-month
    a, b = _month_bounds(today.replace(day=1) - timedelta(days=1))
    return Period(key, labels[key], a, b, True)


def mondays_covering(start: date, end: date) -> list[date]:
    """Mondays of every Mon..Sun week that overlaps [start, end]."""
    m, out = _monday(start), []
    while m <= end:
        out.append(m)
        m += timedelta(days=7)
    return out


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return None


def custom_period(start: date, end: date,
                  locale: str | i18n.Locale | None = None) -> Period:
    """An explicit start/end range chosen with the date pickers."""
    loc = locale if isinstance(locale, i18n.Locale) else i18n.get(locale)
    if end < start:
        start, end = end, start
    label = f"{start.isoformat()} {loc.period_to} {end.isoformat()}"
    return Period("custom", label, start, end, is_month=(end - start).days > 6)
