"""One stylesheet and one page shell, shared by the timesheet view and the
account pages, so the whole service looks like a single product.

Self-contained on purpose: no CDN, no webfont, no build step. The page is
served from a pod that may have no egress at all, and a timesheet that renders
without its stylesheet is unreadable."""
from __future__ import annotations

import html as _html

CSS = """
 :root{color-scheme:light dark;
   --bg:#f6f7f9; --fg:#111; --card:#fff; --line:#8883; --soft:#8882;
   --accent:#374151; --accent-fg:#fff; --dayrow:#e8eef7; --muted:#5b6470}
 @media(prefers-color-scheme:dark){:root{
   --bg:#0d1117; --fg:#e6edf3; --card:#161b22; --dayrow:#1b2536;
   --accent:#c7d2fe; --accent-fg:#1e3a8a; --muted:#8b949e}}
 *{box-sizing:border-box}
 body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:24px 16px;
   background:var(--bg);color:var(--fg)}
 a{color:inherit}
 .card{max-width:940px;margin:0 auto 16px;background:var(--card);border:1px solid var(--line);
   border-radius:14px;overflow:hidden}
 .card.narrow{max-width:560px}
 header{display:flex;align-items:center;gap:12px;padding:16px 20px;flex-wrap:wrap;
   border-bottom:1px solid var(--line)}
 header .titles{flex:1;min-width:180px}
 header h1{font-size:17px;margin:0}
 .sub{font-size:12px;color:var(--muted);margin-top:2px}
 .who{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted)}
 .who img{width:22px;height:22px;border-radius:50%}
 .periods{display:flex;gap:6px;flex-wrap:wrap;padding:10px 20px;align-items:center;
   border-bottom:1px solid var(--line)}
 .pill{font-size:12.5px;text-decoration:none;color:inherit;padding:5px 11px;
   border:1px solid var(--line);border-radius:999px;background:none;cursor:pointer;
   font-family:inherit}
 .pill.active,.pill.go,.pill.primary{background:var(--accent);color:var(--accent-fg);
   border-color:var(--accent)}
 .range{display:inline-flex;align-items:center;gap:6px;margin-left:auto;flex-wrap:wrap}
 .range input[type=date]{font:inherit;padding:4px 8px;border-radius:8px;
   border:1px solid var(--line);background:var(--card);color:inherit}
 .range .sep{color:var(--muted);font-size:12px}
 .gen{font-size:12px;color:var(--muted)}
 .btn{font-size:13px;text-decoration:none;padding:6px 12px;border:1px solid var(--line);
   border-radius:8px;color:inherit;display:inline-block;background:none;cursor:pointer;
   font-family:inherit}
 .btn.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
 .body{padding:18px 20px}
 .body p{margin:0 0 12px}
 table{width:100%;border-collapse:collapse}
 td{padding:7px 12px;border-bottom:1px solid var(--soft)}
 thead td{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;
   letter-spacing:.03em}
 tr.day td{background:var(--dayrow);font-weight:600}
 .tag{font-size:12px;padding:2px 9px;border-radius:999px;background:#8883;white-space:nowrap}
 .tag.meeting{background:#c7d2fe;color:#1e3a8a}
 .tag.rota{background:#fde68a;color:#78350f}
 .tag.focus{background:#bbf7d0;color:#14532d}
 .tag.admin{background:#e5e7eb;color:#374151}
 .tag.leave{background:#ddd6fe;color:#4c1d95}
 .tag.email{background:#bae6fd;color:#075985}
 .tag.chat{background:#fbcfe8;color:#831843}
 tfoot td{font-weight:700}
 .wrap{overflow-x:auto}
 label{display:block;font-size:12.5px;color:var(--muted);margin:14px 0 4px}
 .f{margin:0}
 .f label{margin-top:14px}
 .f.check{display:flex;gap:8px;align-items:center;margin-top:14px}
 .f.check label{margin:0;color:inherit;font-size:14px}
 .f.check input{width:auto;flex:none}
 input[type=text],input[type=email],input[type=number],select{font:inherit;width:100%;
   padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);
   color:inherit}
 /* One field per flex item. Without the .f wrapper each label and input would
    be items of their own, and a two-field row would lay out as four columns. */
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .row>.f{flex:1 1 200px;min-width:0}
 .hint{font-size:12px;color:var(--muted);margin-top:4px}
 .note{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 14px;
   font-size:13px}
 .note.bad{border-color:#f8717188;background:#f871711a}
 .note.good{border-color:#34d39988;background:#34d3991a}
 .code{font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.14em;
   background:var(--dayrow);padding:10px 14px;border-radius:10px;display:inline-block;
   user-select:all}
 .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;align-items:center}
 .status{display:inline-flex;align-items:center;gap:6px;font-size:12.5px}
 .dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;flex:none}
 .dot.on{background:#22c55e}
 .dot.off{background:#f87171}
"""


def esc(s: object) -> str:
    return _html.escape(str(s if s is not None else ""))


def page(title: str, body: str, *, lang: str = "en") -> str:
    """A complete document. `body` is trusted markup the caller has escaped."""
    return (f'<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>")
