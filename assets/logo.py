"""Draw the timesheet mark.

A clock face whose ring is the day itself: unequal coloured arcs in the app's own
block colours, in the order a real day runs. It says "time" and "categorised
blocks" at once, which is exactly what the tool does — and unlike a plain clock
it survives being shrunk to a favicon, because the colour rhythm carries it once
the detail is gone.

Supersampled 4x and downscaled, because PIL's arc has no antialiasing of its own
and the raw edges look chewed at any size worth using.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "assets")
SS = 4  # supersample factor

# Saturated counterparts of the tag colours in render/theme.py. The pastels used
# for chips on a white card turn to mush below about 32px.
FOCUS = "#22c55e"     # development
MEETING = "#6366f1"   # meetings, standups
EMAIL = "#0ea5e9"     # correspondence
CHAT = "#ec4899"      # chat
ADMIN = "#94a3b8"     # admin
ROTA = "#f59e0b"      # on-call
HAND = "#64748b"      # slate: legible on white and on the dark ground alike
HAND_ON_DARK = "#94a3b8"   # a step lighter, for the dark app tile

# One day, clockwise from the top: a long morning of focus, the standup, more
# focus, a burst of mail, admin, a meeting, chat, a long afternoon, admin.
DAY: list[tuple[str, float]] = [
    (ROTA, 18), (MEETING, 38), (FOCUS, 58), (EMAIL, 34),
    (ADMIN, 26), (FOCUS, 44), (CHAT, 30), (MEETING, 32),
    (FOCUS, 38), (ADMIN, 24),
]

TILE_BG = "#0d1117"   # the app's own dark ground


def ring(size: int, *, thickness: float = 0.115, gap: float = 2.6,
         inset: float = 0.04, hands: bool = True, hand: str = HAND) -> Image.Image:
    """The mark on transparency. `thickness` and `inset` are fractions of size.

    The ring stays thin on purpose: fat arcs read as a pie chart, and a pie chart
    is the wrong idea entirely — this is a clock whose hours happen to be
    coloured.
    """
    px = size * SS
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = px * inset
    width = max(2, int(px * thickness))
    box = (pad, pad, px - pad - 1, px - pad - 1)

    total = sum(deg for _, deg in DAY)
    angle = -90.0  # start at twelve o'clock
    for colour, deg in DAY:
        span = deg / total * 360.0
        # Half the gap comes off each end, so the arcs stay centred on their slots.
        draw.arc(box, angle + gap / 2, angle + span - gap / 2,
                 fill=colour, width=width)
        angle += span

    if hands:
        _hands(draw, px, hand)

    return img.resize((size, size), Image.LANCZOS)


def _hands(draw: ImageDraw.ImageDraw, px: int, hand: str = HAND) -> None:
    """Hour and minute hands, plus the pivot. Without them the ring is a donut
    chart; with them it is unmistakably a clock."""
    import math

    c = px / 2
    stroke = max(2, int(px * 0.045))
    for degrees, length in ((-60.0, 0.23), (25.0, 0.33)):   # ~10:10, the classic
        rad = math.radians(degrees - 90)
        draw.line((c, c, c + math.cos(rad) * px * length, c + math.sin(rad) * px * length),
                  fill=hand, width=stroke)
    r = stroke * 0.9
    draw.ellipse((c - r, c - r, c + r, c + r), fill=hand)


def rounded_tile(size: int, bg: str, radius_frac: float = 0.225) -> Image.Image:
    """An app-icon tile with the mark on it."""
    px = size * SS
    tile = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, px - 1, px - 1), radius=int(px * radius_frac), fill=bg)
    tile = tile.resize((size, size), Image.LANCZOS)

    mark = ring(int(size * 0.72), thickness=0.13, inset=0.02, hand=HAND_ON_DARK)
    off = (size - mark.width) // 2
    tile.alpha_composite(mark, (off, off))
    return tile


def svg(thickness: float = 0.115, gap: float = 2.6, inset: float = 0.04,
        hands: bool = True) -> str:
    """The same mark as SVG. Sharp at every size, and small enough to inline as a
    data URI, which is what the served pages do — no static route, no extra
    request, nothing to 404 when the deployment has no egress."""
    import math

    r = 50 - inset * 100 - thickness * 100 / 2
    w = thickness * 100
    total = sum(deg for _, deg in DAY)
    parts, angle = [], -90.0
    for colour, deg in DAY:
        span = deg / total * 360.0
        a0, a1 = angle + gap / 2, angle + span - gap / 2
        x0, y0 = 50 + r * math.cos(math.radians(a0)), 50 + r * math.sin(math.radians(a0))
        x1, y1 = 50 + r * math.cos(math.radians(a1)), 50 + r * math.sin(math.radians(a1))
        large = 1 if (a1 - a0) > 180 else 0
        parts.append(
            f'<path d="M{x0:.2f} {y0:.2f}A{r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f}" '
            f'stroke="{colour}" stroke-width="{w:.2f}" fill="none" stroke-linecap="butt"/>')
        angle += span

    if hands:
        for degrees, length in ((-60.0, 0.23), (25.0, 0.33)):
            rad = math.radians(degrees - 90)
            parts.append(
                f'<line x1="50" y1="50" x2="{50 + math.cos(rad) * length * 100:.2f}" '
                f'y2="{50 + math.sin(rad) * length * 100:.2f}" stroke="{HAND}" '
                f'stroke-width="4.5" stroke-linecap="round"/>')
        parts.append(f'<circle cx="50" cy="50" r="4" fill="{HAND}"/>')

    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            + "".join(parts) + "</svg>")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # The bare mark, transparent — works on the light and the dark page alike.
    ring(1024).save(OUT / "logo.png")
    ring(512).save(OUT / "logo@512.png")
    ring(256).save(OUT / "logo@256.png")

    # App icon / social tile.
    rounded_tile(512, TILE_BG).save(OUT / "icon.png")
    rounded_tile(180, TILE_BG).save(OUT / "apple-touch-icon.png")

    # Favicons: the mark alone, thicker and tighter so it survives the size.
    # Favicons: thicker ring, wider gaps, no hands. Below ~32px the hands turn
    # into a smudge in the middle and cost more than they say.
    small = {"thickness": 0.2, "gap": 5.0, "inset": 0.03, "hands": False}
    for n in (16, 32, 48, 64):
        ring(n, **small).save(OUT / f"favicon-{n}.png")
    ring(64, **small).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    (OUT / "logo.svg").write_text(svg(), encoding="utf-8")
    # A favicon-tuned SVG: thicker, no hands, same reasoning as the small PNGs.
    (OUT / "favicon.svg").write_text(
        svg(thickness=0.2, gap=5.0, inset=0.03, hands=False), encoding="utf-8")

    for p in sorted(OUT.iterdir()):
        print(f"{p.name:24} {p.stat().st_size / 1024:6.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
