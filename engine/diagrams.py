#!/usr/bin/env python3
"""
CoreBench -- vector technical diagrams for reports AND hardware manuals.

Built on reportlab.graphics (shapes), so each diagram is a Drawing that:
  * embeds natively (as vectors) into the branded PDF, and
  * exports to a standalone, print-crisp .svg for manuals / the web,
with NO extra dependencies and no headless browser.

Diagrams (parametric, data-driven):
  * rack_elevation(...)  -- 42U rack with the server's U-position highlighted
  * railkit_steps(...)   -- step-by-step rail-kit / rack-mount install panels
  * motherboard_map(...) -- board layout: CPU sockets, DIMM banks, PCIe slots, I/O

CLI:  python engine/diagrams.py {rack|railkit|motherboard} out.svg
These are clean schematics meant to be data-driven; swap in designer art anytime.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Circle, Drawing, Group, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.units import mm

NAVY = colors.HexColor("#1F3863")
RED = colors.HexColor("#BF0303")
GOLD = colors.HexColor("#D4A53A")
INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#6B7280")
GRID = colors.HexColor("#C7CED6")
LIGHT = colors.HexColor("#EEF2F7")
BOARD = colors.HexColor("#E7EEF4")
WHITE = colors.white
GREEN = colors.HexColor("#2E8B57")


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _arrow(d: Drawing, x1: float, y: float, x2: float, color=RED, w: float = 1.4) -> None:
    d.add(Line(x1, y, x2, y, strokeColor=color, strokeWidth=w))
    s = 2.0 * mm
    tip = x2
    pts = ([tip, y, tip - s, y + s * 0.7, tip - s, y - s * 0.7] if x2 > x1
           else [tip, y, tip + s, y + s * 0.7, tip + s, y - s * 0.7])
    d.add(Polygon(pts, fillColor=color, strokeColor=color))


# --------------------------------------------------------------------------- #
#  Rack elevation                                                             #
# --------------------------------------------------------------------------- #
def rack_elevation(total_u: int = 42, occupants: Optional[List[Tuple[int, int, str]]] = None,
                   width: float = 72 * mm, title: str = "Rack elevation",
                   u_h: float = 3.5 * mm) -> Drawing:
    """occupants: list of (start_u_from_bottom, height_u, label)."""
    occupants = occupants or []
    label_w = 11 * mm
    pad_top, pad_bot = 8 * mm, 4 * mm
    rack_h = total_u * u_h
    height = rack_h + pad_top + pad_bot
    d = Drawing(width, height)
    d.add(String(2, height - 5 * mm, title, fontName="Helvetica-Bold", fontSize=8, fillColor=NAVY))
    x0, y0 = label_w, pad_bot
    rack_w = width - label_w - 3 * mm
    d.add(Rect(x0, y0, rack_w, rack_h, strokeColor=NAVY, strokeWidth=1.2, fillColor=WHITE))
    for u in range(total_u + 1):
        y = y0 + u * u_h
        d.add(Line(x0, y, x0 + rack_w, y, strokeColor=GRID, strokeWidth=0.3))
    for u in range(1, total_u + 1, 2):
        y = y0 + (u - 0.5) * u_h
        d.add(String(x0 - 1.5 * mm, y - 1.6, f"U{u}", fontName="Helvetica", fontSize=4.3,
                     fillColor=MUTED, textAnchor="end"))
    for start_u, h_u, lbl in occupants:
        oy = y0 + (start_u - 1) * u_h
        oh = h_u * u_h
        d.add(Rect(x0 + 1, oy + 0.5, rack_w - 2, oh - 1.0, fillColor=NAVY, strokeColor=RED, strokeWidth=1.2))
        d.add(String(x0 + rack_w / 2, oy + oh / 2 - 2.4, lbl, fontName="Helvetica-Bold",
                     fontSize=6.5, fillColor=WHITE, textAnchor="middle"))
    return d


# --------------------------------------------------------------------------- #
#  Rail-kit / rack-mount steps                                                #
# --------------------------------------------------------------------------- #
DEFAULT_RAIL_STEPS = [
    ("Attach inner rail", "Clip the inner rail to each side of the chassis until it clicks."),
    ("Mount outer rails", "Latch the outer rails into the rack posts at the chosen U height."),
    ("Slide chassis in", "Align the inner rails to the outer rails and slide the chassis in."),
    ("Secure chassis", "Engage the safety latch and tighten the captive thumbscrews."),
]


def _rail_panel(d: Drawing, x: float, y: float, w: float, h: float, step: int) -> None:
    post_w = 2.2 * mm
    lx, rx = x + 2 * mm, x + w - 2 * mm - post_w
    d.add(Rect(lx, y, post_w, h, fillColor=MUTED, strokeColor=None))
    d.add(Rect(rx, y, post_w, h, fillColor=MUTED, strokeColor=None))
    rail_y = y + h * 0.5
    rail_col = RED if step in (1,) else GRID
    d.add(Line(lx + post_w, rail_y, rx, rail_y, strokeColor=rail_col, strokeWidth=2))
    cw, ch = (rx - lx - post_w) * 0.78, h * 0.5
    cy = y + h * 0.25
    if step == 0:           # chassis off to the right, inner rail highlighted
        cx = rx - cw + 6 * mm
        d.add(Rect(cx, cy, cw, ch, fillColor=NAVY, strokeColor=NAVY))
        d.add(Line(cx, cy, cx + cw, cy, strokeColor=RED, strokeWidth=2))
    elif step == 1:         # rails in rack, no chassis yet
        pass
    elif step == 2:         # chassis half-in, arrow pushing left
        cx = lx + post_w + (rx - lx - post_w) * 0.32
        d.add(Rect(cx, cy, cw, ch, fillColor=NAVY, strokeColor=NAVY))
        _arrow(d, cx + cw + 5 * mm, rail_y, cx + cw + 0.5 * mm)
    else:                   # chassis seated + thumbscrews + check
        cx = lx + post_w + 1 * mm
        d.add(Rect(cx, cy, cw, ch, fillColor=NAVY, strokeColor=NAVY))
        for sy in (cy + 2 * mm, cy + ch - 2 * mm):
            d.add(Circle(cx + 2 * mm, sy, 0.9 * mm, fillColor=GOLD, strokeColor=None))
        d.add(String(rx - 3 * mm, cy + ch / 2 - 2, "OK", fontName="Helvetica-Bold",
                     fontSize=7, fillColor=GREEN, textAnchor="middle"))


def railkit_steps(width: float = 176 * mm,
                  steps: Optional[List[Tuple[str, str]]] = None,
                  title: str = "Rail-kit installation") -> Drawing:
    steps = steps or DEFAULT_RAIL_STEPS
    n = len(steps)
    gap = 4 * mm
    pw = (width - (n - 1) * gap) / n
    panel_h, cap_h, head_h = 34 * mm, 18 * mm, 7 * mm
    height = panel_h + cap_h + head_h
    d = Drawing(width, height)
    d.add(String(0, height - 5 * mm, title, fontName="Helvetica-Bold", fontSize=9, fillColor=NAVY))
    for i, (st_title, desc) in enumerate(steps):
        x = i * (pw + gap)
        py = cap_h
        d.add(Rect(x, py, pw, panel_h, strokeColor=GRID, strokeWidth=0.6, fillColor=LIGHT))
        _rail_panel(d, x, py, pw, panel_h, i)
        d.add(Circle(x + 5 * mm, py + panel_h - 5 * mm, 3.1 * mm, fillColor=RED, strokeColor=None))
        d.add(String(x + 5 * mm, py + panel_h - 6.3 * mm, str(i + 1), fontName="Helvetica-Bold",
                     fontSize=8, fillColor=WHITE, textAnchor="middle"))
        d.add(String(x + pw / 2, cap_h - 4 * mm, st_title, fontName="Helvetica-Bold",
                     fontSize=6.8, fillColor=NAVY, textAnchor="middle"))
        for li, line in enumerate(_wrap(desc, 36)[:3]):
            d.add(String(x + pw / 2, cap_h - 7.5 * mm - li * 3.8 * mm, line,
                         fontName="Helvetica", fontSize=5.4, fillColor=INK, textAnchor="middle"))
    return d


# --------------------------------------------------------------------------- #
#  Motherboard map                                                            #
# --------------------------------------------------------------------------- #
DEFAULT_MB = {
    "name": "Server mainboard",
    "sockets": 2, "dimms_per_socket": 16,
    "pcie": ["PCIe5 x16 (GPU)", "PCIe5 x16 (GPU)", "PCIe5 x16 (NIC)", "OCP 3.0"],
    "rear_io": ["BMC", "2x USB", "VGA", "2x RJ45"],
}


def motherboard_map(width: float = 176 * mm, spec: Optional[Dict[str, Any]] = None,
                    title: str = "Mainboard layout") -> Drawing:
    spec = spec or DEFAULT_MB
    bh = 112 * mm
    d = Drawing(width, bh)
    d.add(Rect(0, 0, width, bh, strokeColor=NAVY, strokeWidth=1.2, fillColor=BOARD))
    d.add(String(4 * mm, bh - 6 * mm, spec.get("name", "Mainboard"),
                 fontName="Helvetica-Bold", fontSize=8.5, fillColor=NAVY))
    # rear I/O strip (left edge)
    io = spec.get("rear_io", [])
    d.add(Rect(3 * mm, 8 * mm, 9 * mm, bh - 22 * mm, strokeColor=MUTED, strokeWidth=0.6, fillColor=WHITE))
    for k, lbl in enumerate(io):
        yy = bh - 20 * mm - k * 7 * mm
        d.add(Rect(4 * mm, yy, 7 * mm, 4 * mm, fillColor=MUTED, strokeColor=None))
        d.add(String(13 * mm, yy + 0.6, lbl, fontName="Helvetica", fontSize=5, fillColor=INK))
    # CPU sockets with flanking DIMM banks
    sockets = int(spec.get("sockets", 2))
    dps = int(spec.get("dimms_per_socket", 16))
    sock_w = 24 * mm
    half = max(1, dps // 2)
    for s in range(sockets):
        sx = 34 * mm + s * (sock_w + 42 * mm)
        sy = bh - 54 * mm
        d.add(Rect(sx, sy, sock_w, sock_w, strokeColor=NAVY, strokeWidth=1, fillColor=NAVY))
        d.add(String(sx + sock_w / 2, sy + sock_w / 2 - 2, f"CPU{s}", fontName="Helvetica-Bold",
                     fontSize=8, fillColor=WHITE, textAnchor="middle"))
        for bx in (sx - 9.5 * mm, sx + sock_w + 1.5 * mm):    # left + right DIMM bank
            for k in range(half):
                dy = sy + sock_w - 3 * mm - k * 2.9 * mm
                d.add(Rect(bx, dy, 8 * mm, 2.0 * mm, fillColor=GOLD, strokeColor=None))
        d.add(String(sx + sock_w / 2, sy - 5 * mm, f"{dps} DIMM slots", fontName="Helvetica",
                     fontSize=5.5, fillColor=MUTED, textAnchor="middle"))
    # PCIe / OCP slots along the bottom
    for k, lbl in enumerate(spec.get("pcie", [])):
        px = 16 * mm + k * 40 * mm
        d.add(Rect(px, 10 * mm, 34 * mm, 4.5 * mm, fillColor=RED, strokeColor=None))
        d.add(String(px + 17 * mm, 5 * mm, lbl, fontName="Helvetica", fontSize=5,
                     fillColor=INK, textAnchor="middle"))
    return d


# --------------------------------------------------------------------------- #
#  Export                                                                     #
# --------------------------------------------------------------------------- #
def to_svg(drawing: Drawing) -> str:
    return renderSVG.drawToString(drawing)


def save_svg(drawing: Drawing, path: str) -> str:
    renderSVG.drawToFile(drawing, path)
    return path


_KINDS = {"rack": lambda: rack_elevation(occupants=[(20, 8, "Tyrone Camarero AI (8U)")]),
          "railkit": railkit_steps,
          "motherboard": motherboard_map}


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2 or argv[0] not in _KINDS:
        print(f"usage: diagrams.py {{{'|'.join(_KINDS)}}} <out.svg>", file=sys.stderr)
        return 2
    save_svg(_KINDS[argv[0]](), argv[1])
    print(f"wrote {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
