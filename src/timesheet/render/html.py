"""Render a set of days as a self-contained, theme-aware HTML page.

Every visible string comes from the caller's `Locale`, so one deployment serves
an English sheet and a Dutch one from the same code path."""
from __future__ import annotations

from .. import i18n
from ..model import Day
from ..timeutil import hm
from .theme import esc, page


def table(week: list[Day], loc: i18n.Locale) -> tuple[str, int]:
    """The timesheet table, and the grand total it adds up to."""
    rows, grand = [], 0
    for day in week:
        grand += day.minutes
        d = day.date
        rows.append(
            f'<tr class="day"><td colspan="6">{esc(loc.days[d.weekday()])} {d.day} '
            f'{esc(loc.months[d.month])} &nbsp;·&nbsp; '
            f'<b>{hm(day.minutes)}{esc(loc.subtitle_hours)}</b></td></tr>')
        for b in day.blocks:
            rows.append(
                f'<tr><td>{d.day:02d}-{d.month:02d}</td><td>{b.start:%H:%M}</td>'
                f'<td>{b.end:%H:%M}</td><td>{hm(b.minutes)}</td>'
                f'<td><span class="tag {esc(b.kind)}">{esc(b.project)}</span></td>'
                f'<td>{esc(b.taak)}</td></tr>')
    empty = ("" if rows else
             '<tr><td colspan="6" style="padding:28px;text-align:center;opacity:.6">'
             f'{esc(loc.empty)}</td></tr>')
    head = "".join(f"<td>{esc(h)}</td>" for h in (
        loc.col_date, loc.col_from, loc.col_to, loc.col_duration, loc.col_project, loc.col_task))
    return (f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(rows)}{empty}</tbody>"
            f'<tfoot><tr><td colspan="3">{esc(loc.total)}</td>'
            f'<td colspan="3">{hm(grand)}</td></tr></tfoot></table></div>'), grand


def build_week(week: list[Day], *, locale: str | i18n.Locale | None = None,
               title: str | None = None, download_url: str | None = None,
               generated: str | None = None, subtitle: str | None = None,
               nav_html: str = "", account_html: str = "") -> str:
    loc = locale if isinstance(locale, i18n.Locale) else i18n.get(locale)
    body, _ = table(week, loc)
    dl = (f'<a class="btn" href="{esc(download_url)}">⬇ {esc(loc.download)}</a>'
          if download_url else "")
    gen = f'<span class="gen">{esc(loc.updated)} {esc(generated)}</span>' if generated else ""
    sub = f'<div class="sub">{esc(subtitle)}</div>' if subtitle else ""
    nav = f'<nav class="periods">{nav_html}</nav>' if nav_html else ""
    head = (f'<header><div class="titles"><h1>🕑 {esc(title or loc.title)}</h1>{sub}</div>'
            f"{account_html}{gen}{dl}</header>")
    return page(title or loc.title, f'<div class="card">{head}{nav}{body}</div>', lang=loc.code)
