"""Calibration jobs for setting up a new pen or holder."""

from __future__ import annotations

import numpy as np

from .gcode import Stroke, compile_strokes
from .layout import Layout

DEFAULT_PRESSES = (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)


def _origin(layout: Layout, inset: float = 6.0) -> tuple[float, float, int, int]:
    """Area-local start near the touch-off corner, plus the inward direction."""
    lx, ly, ix, iy = layout.local_touch()
    if layout.touch_off == "c":
        return lx, ly, ix, iy
    return lx + inset * ix, ly + inset * iy, ix, iy


def smoke_test(layout: Layout, size: float = 20.0) -> str:
    """A single square: proves homing, the pauses and pen contact work."""
    size = min(size, layout.area.w - 12, layout.area.h - 12)
    x, y, ix, iy = _origin(layout)
    sq = np.array([[x, y], [x + size * ix, y], [x + size * ix, y + size * iy], [x, y + size * iy], [x, y]])
    return compile_strokes(layout, [Stroke(layout.area_to_pen(sq))], title=f"smoke test, {size:g} mm square")


def press_ladder(layout: Layout, presses=DEFAULT_PRESSES, length: float = 25.0, gap: float = 4.0) -> str:
    """One short line per press depth; pick the faintest line that is still solid."""
    x, y, ix, iy = _origin(layout)
    length = min(length, layout.area.w - 12)
    if (len(presses) - 1) * gap > layout.area.h - 12:
        raise ValueError("Drawing area is too short for the press ladder.")
    strokes = [
        Stroke(layout.area_to_pen([[x, y + i * gap * iy], [x + length * ix, y + i * gap * iy]]), press=p)
        for i, p in enumerate(presses)
    ]
    order = ", ".join(f"{p:g}" for p in presses)
    return compile_strokes(layout, strokes, title=f"press ladder, from the touch-off corner inward: {order} mm")


def alignment_test(layout: Layout, radius: float = 25.0) -> str:
    """The alignment target as a complete job, drawn at the center of the drawing area."""
    from .pipeline import make_plan, plan_to_gcode

    plan = make_plan(layout, "alignment", {"radius": radius})
    return plan_to_gcode(layout, plan, title=f"alignment test, {radius:g} mm circle at the area center")


def alignment_correction(layout: Layout, measured_left: float, measured_front: float) -> dict:
    """Compare where the target's center landed on the sheet with where it was meant to land.

    Measurements are from the sheet's own left and front edges to the cross. Returns the error
    in the sheet's frame and in the plate's frame, plus the fixes.
    """
    import numpy as np

    from .layout import rotation

    p, a = layout.paper, layout.area
    w, h = p.dims()
    pc = np.array([p.x + w / 2, p.y + h / 2])
    target = np.array(a.center)
    # Where the center should be, measured from the sheet's own front-left corner.
    local = rotation(-p.rotation) @ (target - pc) + [w / 2, h / 2]
    err_local = np.array([measured_left, measured_front]) - local  # + = landed right / toward the back
    err_bed = rotation(p.rotation) @ err_local
    return {
        "expected_left": float(local[0]),
        "expected_front": float(local[1]),
        "error_x": float(err_local[0]),
        "error_y": float(err_local[1]),
        "error_bed_x": float(err_bed[0]),
        "error_bed_y": float(err_bed[1]),
        # Fixes: move the sheet by +error; or tell the layout the sheet is at -error; or the pen is at +error.
        "move_sheet": [float(err_bed[0]), float(err_bed[1])],
        "paper_xy": [float(p.x - err_bed[0]), float(p.y - err_bed[1])],
        "pen_offset": [float(layout.pen.offset_x + err_bed[0]), float(layout.pen.offset_y + err_bed[1])],
    }
