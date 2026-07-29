"""Render the week to an .xlsx in the manager's exact 6-column 'Uren' layout.

Produces the same columns and day-header/TOTAAL structure as
'Uren Voorbeeld Cloud Team.xlsx'. All values are strings (the original sheet has
no formulas); durations and totals are pre-computed."""
from __future__ import annotations

import io

from ..model import Day
from ..timeutil import NL_DAYS, NL_MONTHS, hm

HEADERS = ["Datum", "Van", "Tot", "Duur", "Project / klant", "Taak"]
WIDTHS = [22, 8, 8, 8, 26, 48]


def build_week(week: list[Day], title: str = "Uren") -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title[:31]
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[chr(64 + i)].width = w

    thin = Side(style="thin", color="D0D0D0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center")
    hdr_font, hdr_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="374151")
    day_font, day_fill = Font(bold=True, color="1F2A44"), PatternFill("solid", fgColor="E8EEF7")

    for c, h in enumerate(HEADERS, 1):
        cell = ws.cell(1, c, h)
        cell.font, cell.fill, cell.border = hdr_font, hdr_fill, border

    r, grand = 2, 0
    for day in week:
        grand += day.minutes
        d = day.date
        ws.cell(r, 1, f"{NL_DAYS[d.weekday()]} {d.day} {NL_MONTHS[d.month]}  ·  {hm(day.minutes)}u")
        for c in range(1, 7):
            ws.cell(r, c).fill, ws.cell(r, c).border = day_fill, border
        ws.cell(r, 1).font = day_font
        r += 1
        for b in day.blocks:
            row = [f"{d.day:02d}-{d.month:02d}-{d.year}", f"{b.start:%H:%M}", f"{b.end:%H:%M}",
                   hm(b.minutes), b.project, b.taak]
            for c, v in enumerate(row, 1):
                cell = ws.cell(r, c, v)
                cell.border = border
                if c in (2, 3, 4):
                    cell.alignment = center
            r += 1

    ws.cell(r + 1, 1, "TOTAAL").font = Font(bold=True)
    tc = ws.cell(r + 1, 4, hm(grand))
    tc.font, tc.alignment = Font(bold=True), center
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
