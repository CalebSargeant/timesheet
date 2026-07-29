"""Render the week as a self-contained, theme-aware HTML page — the live view
Marc opens behind Cloudflare Access."""
from __future__ import annotations

import html as _html

from ..model import Day
from ..timeutil import NL_DAYS, NL_MONTHS, hm


def build_week(week: list[Day], *, title: str = "Uren · Team Cloud",
               download_url: str | None = None, generated: str | None = None,
               subtitle: str | None = None, nav_html: str = "") -> str:
    rows, grand = [], 0
    for day in week:
        grand += day.minutes
        d = day.date
        rows.append(
            f'<tr class="day"><td colspan="6">{NL_DAYS[d.weekday()]} {d.day} '
            f'{NL_MONTHS[d.month]} &nbsp;·&nbsp; <b>{hm(day.minutes)}u</b></td></tr>')
        for b in day.blocks:
            rows.append(
                f'<tr><td>{d.day:02d}-{d.month:02d}</td><td>{b.start:%H:%M}</td>'
                f'<td>{b.end:%H:%M}</td><td>{hm(b.minutes)}</td>'
                f'<td><span class="tag {b.kind}">{_html.escape(b.project)}</span></td>'
                f'<td>{_html.escape(b.taak)}</td></tr>')
    dl = (f'<a class="btn" href="{_html.escape(download_url)}">⬇ Download .xlsx</a>'
          if download_url else "")
    gen = f'<span class="gen">bijgewerkt {_html.escape(generated)}</span>' if generated else ""
    sub = f'<div class="sub">{_html.escape(subtitle)}</div>' if subtitle else ""
    nav = f'<nav class="periods">{nav_html}</nav>' if nav_html else ""
    empty = ("" if rows else
             '<tr><td colspan="6" style="padding:28px;text-align:center;opacity:.6">'
             'Geen gegevens voor deze periode.</td></tr>')
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)}</title>
<style>
 :root{{color-scheme:light dark}}
 *{{box-sizing:border-box}}
 body{{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px;
   background:#f6f7f9;color:#111}}
 @media(prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}}}}
 .card{{max-width:940px;margin:0 auto;background:Canvas;border:1px solid #8883;border-radius:14px;
   overflow:hidden}}
 header{{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid #8883}}
 header .titles{{flex:1}}
 header h1{{font-size:17px;margin:0}}
 .sub{{font-size:12px;opacity:.65;margin-top:2px}}
 .periods{{display:flex;gap:6px;flex-wrap:wrap;padding:10px 20px;border-bottom:1px solid #8883}}
 .pill{{font-size:12.5px;text-decoration:none;color:inherit;padding:5px 11px;border:1px solid #8884;
   border-radius:999px}}
 .pill.active{{background:#374151;color:#fff;border-color:#374151}}
 @media(prefers-color-scheme:dark){{.pill.active{{background:#c7d2fe;color:#1e3a8a;border-color:#c7d2fe}}}}
 .range{{display:inline-flex;align-items:center;gap:6px;margin-left:auto}}
 .range input[type=date]{{font:inherit;padding:4px 8px;border:1px solid #8884;border-radius:8px;
   background:Canvas;color:inherit}}
 .range .sep{{opacity:.6;font-size:12px}}
 .pill.go{{cursor:pointer;background:#374151;color:#fff;border-color:#374151}}
 @media(prefers-color-scheme:dark){{.pill.go{{background:#c7d2fe;color:#1e3a8a;border-color:#c7d2fe}}}}
 .gen{{font-size:12px;opacity:.6}}
 .btn{{font-size:13px;text-decoration:none;padding:6px 12px;border:1px solid #8884;border-radius:8px;
   color:inherit}}
 table{{width:100%;border-collapse:collapse}}
 td{{padding:7px 12px;border-bottom:1px solid #8882}}
 thead td{{font-weight:600;opacity:.7;font-size:12px;text-transform:uppercase;letter-spacing:.03em}}
 tr.day td{{background:#e8eef7;font-weight:600}}
 @media(prefers-color-scheme:dark){{tr.day td{{background:#1b2536}}}}
 .tag{{font-size:12px;padding:2px 9px;border-radius:999px;background:#8883;white-space:nowrap}}
 .tag.meeting{{background:#c7d2fe;color:#1e3a8a}}
 .tag.rota{{background:#fde68a;color:#78350f}}
 .tag.focus{{background:#bbf7d0;color:#14532d}}
 .tag.admin{{background:#e5e7eb;color:#374151}}
 tfoot td{{font-weight:700}}
</style></head><body>
<div class="card">
 <header><div class="titles"><h1>🕑 {_html.escape(title)}</h1>{sub}</div>{gen}{dl}</header>
 {nav}
 <table>
  <thead><tr><td>Datum</td><td>Van</td><td>Tot</td><td>Duur</td><td>Project / klant</td><td>Taak</td></tr></thead>
  <tbody>{''.join(rows)}{empty}</tbody>
  <tfoot><tr><td colspan="3">TOTAAL</td><td colspan="3">{hm(grand)}</td></tr></tfoot>
 </table>
</div></body></html>"""
