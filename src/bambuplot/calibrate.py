"""Calibration jobs for setting up a new pen or holder."""

from __future__ import annotations

import numpy as np

from .gcode import Stroke, compile_strokes
from .layout import Layout

DEFAULT_PRESSES = (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)


def _origin(layout: Layout, inset: float = 6.0) -> tuple[float, float, int, int]:
    """Start near the touch-off corner, stepping inward."""
    tx, ty, ix, iy = layout.touch_point()
    if layout.touch_off == "c":
        return tx, ty, ix, iy
    return tx + inset * ix, ty + inset * iy, ix, iy


def smoke_test(layout: Layout, size: float = 20.0) -> str:
    """A single square: proves homing, the pauses and pen contact work."""
    size = min(size, layout.area.w - 12, layout.area.h - 12)
    x, y, ix, iy = _origin(layout)
    sq = np.array([[x, y], [x + size * ix, y], [x + size * ix, y + size * iy], [x, y + size * iy], [x, y]])
    return compile_strokes(layout, [Stroke(sq)], title=f"smoke test, {size:g} mm square")


def press_ladder(layout: Layout, presses=DEFAULT_PRESSES, length: float = 25.0, gap: float = 4.0) -> str:
    """One short line per press depth; pick the faintest line that is still solid."""
    x, y, ix, iy = _origin(layout)
    length = min(length, layout.area.w - 12)
    if (len(presses) - 1) * gap > layout.area.h - 12:
        raise ValueError("Drawing area is too short for the press ladder.")
    strokes = [
        Stroke(np.array([[x, y + i * gap * iy], [x + length * ix, y + i * gap * iy]]), press=p)
        for i, p in enumerate(presses)
    ]
    order = ", ".join(f"{p:g}" for p in presses)
    return compile_strokes(layout, strokes, title=f"press ladder, from the touch-off corner inward: {order} mm")
