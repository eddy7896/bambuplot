"""Flipbooks: one video frame per page, plotted page after page.

The drawing area holds ``cols × rows`` pages (1 × 1 = one frame per sheet).
Pages are numbered in reading order from the back-left corner. Each page can
keep a blank binding strip on its left for staples or glue, carry its frame
number, and get corner marks for cutting it out. After the last page on a
sheet the job pauses for the next sheet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pydantic import BaseModel, Field

from .extras import _subtract_box

Lines = list[np.ndarray]


class Flipbook(BaseModel):
    enabled: bool = False
    frame: int = Field(0, ge=0)  # the frame shown in the editor (0-based)
    first: int = Field(0, ge=0)  # frames to plot, 0-based and inclusive
    last: int | None = Field(None, ge=0)
    cols: int = Field(1, ge=1, le=8)
    rows: int = Field(1, ge=1, le=8)
    gap: float = Field(4.0, ge=0, le=30)  # mm between pages, where you cut
    binding: float = Field(0.0, ge=0, le=40)  # mm left blank on each page's left edge
    numbers: bool = True
    number_size: float = Field(2.5, ge=1.5, le=8)  # mm, digit height
    cut_marks: bool = True
    check_each_sheet: bool = False  # touch the pen off again on every new sheet

    @property
    def per_sheet(self) -> int:
        return self.cols * self.rows

    def frames(self, count: int) -> list[int]:
        if count <= 0:
            return []
        last = count - 1 if self.last is None else min(self.last, count - 1)
        first = min(self.first, last)
        return list(range(first, last + 1))


@dataclass
class Cell:
    slot: int  # position on the sheet, reading order
    page: tuple[float, float, float, float]  # x, y, w, h in area mm (the whole page)
    art: tuple[float, float, float, float]  # where the picture goes (page minus the binding strip)


def cells(fb: Flipbook, area_w: float, area_h: float) -> list[Cell]:
    n = fb.per_sheet
    pitch_w, pitch_h = area_w / fb.cols, area_h / fb.rows
    inset = fb.gap / 2 if n > 1 else 0.0
    out = []
    for k in range(n):
        r, c = divmod(k, fb.cols)
        x = c * pitch_w + inset
        y = area_h - (r + 1) * pitch_h + inset  # row 0 at the back
        w, h = pitch_w - 2 * inset, pitch_h - 2 * inset
        b = min(fb.binding, w * 0.6)
        out.append(Cell(k, (x, y, w, h), (x + b, y, w - b, h)))
    return out


def check(fb: Flipbook, area_w: float, area_h: float) -> str | None:
    """A reason the pages don't fit, or None."""
    c = cells(fb, area_w, area_h)[0]
    if c.page[2] < 8 or c.page[3] < 8:
        return "The pages are too small. Use fewer pages per sheet or a smaller gap."
    if c.art[2] < 6:
        return "The binding strip leaves no room for the picture. Make it narrower."
    return None


# ------------------------------------------------------------------ frame numbers

# Single-stroke digits on a 1 × 2 box (seven-segment shapes, drawn with as few lifts as possible).
_DIGITS: dict[str, list[list[tuple[float, float]]]] = {
    "0": [[(0, 0), (1, 0), (1, 2), (0, 2), (0, 0)]],
    "1": [[(0.2, 1.6), (0.55, 2), (0.55, 0)]],
    "2": [[(0, 2), (1, 2), (1, 1), (0, 1), (0, 0), (1, 0)]],
    "3": [[(0, 2), (1, 2), (1, 0), (0, 0)], [(0.15, 1), (1, 1)]],
    "4": [[(0, 2), (0, 1), (1, 1)], [(1, 2), (1, 0)]],
    "5": [[(1, 2), (0, 2), (0, 1), (1, 1), (1, 0), (0, 0)]],
    "6": [[(1, 2), (0, 2), (0, 0), (1, 0), (1, 1), (0, 1)]],
    "7": [[(0, 2), (1, 2), (0.35, 0)]],
    "8": [[(0, 0), (1, 0), (1, 2), (0, 2), (0, 0)], [(0, 1), (1, 1)]],
    "9": [[(1, 1), (0, 1), (0, 2), (1, 2), (1, 0), (0, 0)]],
}
_ADVANCE = 1.6  # character spacing, in digit widths


def number_lines(text: str, height: float) -> tuple[Lines, float]:
    """Strokes for a number with its bottom-left at (0, 0). Returns (lines, width)."""
    s = height / 2.0
    out: Lines = []
    for i, ch in enumerate(text):
        for stroke in _DIGITS.get(ch, []):
            out.append(np.array([[(x + i * _ADVANCE) * s, y * s] for x, y in stroke], float))
    width = ((len(text) - 1) * _ADVANCE + 1) * s if text else 0.0
    return out, width


def number_for(fb: Flipbook, cell: Cell, label: str) -> tuple[Lines, tuple[float, float, float, float] | None]:
    """The page number's strokes and, when it sits on the picture, the box to clear behind it."""
    if not fb.numbers:
        return [], None
    h = fb.number_size
    lines, w = number_lines(label, h)
    px, py, pw, ph = cell.page
    strip = cell.art[0] - px
    pad = 1.0
    if strip >= w + 2 * pad:  # upright in the binding strip, near the bottom
        dx, dy = px + (strip - w) / 2, py + pad + 1
        return [l + [dx, dy] for l in lines], None
    if strip >= h + 2 * pad:  # turned to run up the binding strip
        rot = np.array([[0, -1], [1, 0]])  # 90° counter-clockwise
        dx, dy = px + (strip + h) / 2, py + pad + 1
        return [l @ rot.T + [dx, dy] for l in lines], None
    # On the picture: bottom-left corner, with the art cleared behind it.
    ax, ay = cell.art[0], cell.art[1]
    dx, dy = ax + pad + 0.5, ay + pad + 0.5
    box = (dx - 0.8, dy - 0.8, dx + w + 0.8, dy + h + 0.8)
    return [l + [dx, dy] for l in lines], box


def cut_marks(fb: Flipbook, cell: Cell, area_w: float, area_h: float) -> Lines:
    """L-shaped marks just outside each page corner, inside the gap."""
    if not fb.cut_marks or fb.per_sheet <= 1 or fb.gap < 1.5:
        return []
    x, y, w, h = cell.page
    off = 0.4
    length = min(3.0, fb.gap / 2 - off)
    if length < 0.6:
        return []
    out: Lines = []
    for cx, cy, sx, sy in ((x, y, -1, -1), (x + w, y, 1, -1), (x, y + h, -1, 1), (x + w, y + h, 1, 1)):
        # one tick along each edge line, pointing away from the page
        out.append(np.array([[cx + sx * off, cy], [cx + sx * (off + length), cy]]))
        out.append(np.array([[cx, cy + sy * off], [cx, cy + sy * (off + length)]]))
    return [np.clip(l, [0, 0], [area_w, area_h]) for l in out]


def clear_box(lines: Lines, box) -> Lines:
    if box is None:
        return lines
    out: Lines = []
    for l in lines:
        out += _subtract_box(l, *box)
    return out


def sheets(frames: list[int], per_sheet: int) -> list[list[int]]:
    return [frames[i:i + per_sheet] for i in range(0, len(frames), per_sheet)]
