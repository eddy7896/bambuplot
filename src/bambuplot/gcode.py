"""Compile pen paths into A1 Mini G-code with the manual touch-off start sequence.

Start sequence:
  1. Home with no pen in the holder.
  2. Rise above the clips and move to the touch-off point.
  3. PAUSE 1: clip the paper, insert the pen, lower it to touch the paper, tighten.
  4. Hover 5 mm and trace the drawing area outline.
  5. PAUSE 2: check the outline sits where you want.
  6. Draw a small L test mark.
  7. PAUSE 3: check the mark.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .layout import TOUCH_NAMES, Layout
from .safety import GcodeSafetyError, validate


class UnsafeLayout(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Layout fails safety checks:\n" + "\n".join(problems))


@dataclass
class Screen:
    """What the printer's screen shows during a job.

    Bambu firmware can't display custom text. ``M73`` drives the progress bar and time left,
    and ``M1002 gcode_claim_action : N`` picks one of the printer's own status messages.
    ``None`` leaves the screen as it is for that stage.
    """

    progress: bool = True
    status: bool = True
    claim_home: int | None = 13  # "Homing toolhead"
    claim_pause: int | None = None  # the three setup pauses
    claim_draw: int | None = 0  # "Printing"
    claim_pen: int | None = 4  # "Changing filament": the nearest to a pen change
    setup_seconds: float = 75.0  # homing, moves and the outline trace (not your time at the pauses)


# The printer's status messages (Bambu stage numbers) offered in the app.
SCREEN_MESSAGES = {
    0: "Printing",
    1: "Auto bed leveling",
    2: "Heatbed preheating",
    4: "Changing filament",
    7: "Heating hotend",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    16: "Paused by the user",
}


def _claim(w: "_Writer", screen: Screen | None, code: int | None, why: str) -> None:
    if screen and screen.status and code is not None:
        w(f"M1002 gcode_claim_action : {int(code)}", f"screen: {SCREEN_MESSAGES.get(int(code), code)} ({why})")


@dataclass
class Stroke:
    points: np.ndarray  # pen coordinates, mm
    press: float | None = None  # override layout.z.press (calibration ladders)


class _Writer:
    def __init__(self):
        self.lines: list[str] = []
        self._last = ""

    def __call__(self, cmd: str, comment: str | None = None):
        self.lines.append(f"{cmd:<26}; {comment}" if comment else cmd)
        self._last = cmd

    def move(self, cmd: str):
        if cmd != self._last:  # skip moves that round to the same spot
            self(cmd)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _f(v: float) -> str:
    return f"{v:.2f}"


def _z(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _xy(layout: Layout, x: float, y: float) -> str:
    nx, ny = layout.to_nozzle(np.array([x, y]))
    return f"X{_f(nx)} Y{_f(ny)}"


def start_sequence(w: _Writer, layout: Layout, title: str = "", screen: Screen | None = None) -> None:
    a, z, f, paper = layout.area, layout.z, layout.feed, layout.paper
    lx, ly, ix, iy = layout.local_touch()

    def xy(u: float, v: float) -> str:  # area-local point → nozzle words
        x, y = layout.area_to_pen([u, v])
        return _xy(layout, x, y)

    w("; ===== BambuPlot start =====")
    if title:
        w(f"; job         {title}")
    w(f"; paper       {paper.size} {paper.orientation}, front-left at X{_f(paper.x)} Y{_f(paper.y)}, "
      f"rotated {paper.rotation:g} deg")
    w(f"; area (pen)  {a.w:g} x {a.h:g} mm centered at X{_f(a.center[0])} Y{_f(a.center[1])}, "
      f"rotated {a.rotation:g} deg")
    w(f"; printer     {layout.bed.label}, plate {layout.bed.w:g} x {layout.bed.h:g} mm")
    w(f"; pen offset  dX {_f(layout.pen.offset_x)} dY {_f(layout.pen.offset_y)} (nozzle = pen - offset)")
    w(f"; Z           touch {_z(z.touch)} down {_z(z.down)} up {_z(z.up)} safe {_z(layout.safe_z)}")
    w("G90", "absolute positioning")
    w("G21", "millimeters")
    w(f"M204 S{f.accel}", "gentle acceleration")
    _claim(w, screen, screen.claim_home if screen else None, "homing")
    w("G28", "home - pen NOT in the holder")
    w(f"G1 Z{_z(layout.safe_z)} F{f.z}", "rise above the clips")
    w(f"G1 {xy(lx, ly)} F{f.travel}", f"touch-off point: {TOUCH_NAMES[layout.touch_off]}")
    w(f"G1 Z{_z(z.touch)} F{f.z_slow}", "touch-off height")
    w("M400")
    _claim(w, screen, screen.claim_pause if screen else None, "pause 1")
    w("M400 U1", "PAUSE 1: clip paper, insert pen, lower tip to paper, tighten")
    w(f"G1 Z{_z(z.trace)} F{f.z}", "hover 5 mm for the outline")
    w(f"G1 {xy(0, 0)} F{f.travel}")
    w(f"G1 {xy(a.w, 0)} F{f.draw}", "trace drawing area")
    w(f"G1 {xy(a.w, a.h)}")
    w(f"G1 {xy(0, a.h)}")
    w(f"G1 {xy(0, 0)}")
    w("M400")
    _claim(w, screen, screen.claim_pause if screen else None, "pause 2")
    w("M400 U1", "PAUSE 2: check the outline sits where you want")
    w(f"G1 {xy(lx, ly)} F{f.travel}")
    w(f"G1 Z{_z(z.down)} F{f.z_slow}", "test mark")
    w(f"G1 {xy(lx + 5 * ix, ly)} F{f.draw}")
    w(f"G1 {xy(lx + 5 * ix, ly + 5 * iy)}")
    w(f"G1 Z{_z(z.up)} F{f.z}")
    w("M400")
    _claim(w, screen, screen.claim_pause if screen else None, "pause 3")
    w("M400 U1", "PAUSE 3: check the mark; re-seat pen if faint")
    w("; ===== artwork =====")
    _claim(w, screen, screen.claim_draw if screen else None, "drawing")


def end_sequence(w: _Writer, layout: Layout) -> None:
    w("; ===== end =====")
    w(f"G1 Z{_z(layout.safe_z)} F{layout.feed.z}", "lift clear of the clips")
    w("M400")


def compile_strokes(layout: Layout, strokes: list[Stroke], title: str = "", check_layout: bool = True) -> str:
    if check_layout:
        bad = [f"{c.title}: {c.detail}" for c in layout.checks() if c.state == "bad"]
        if bad:
            raise UnsafeLayout(bad)

    z, f = layout.z, layout.feed
    w = _Writer()
    start_sequence(w, layout, title)
    lowest = z.down
    for s in strokes:
        pts = np.asarray(s.points, float)
        if len(pts) == 0:
            continue
        down = z.touch - (z.press if s.press is None else s.press)
        lowest = min(lowest, down)
        w.move(f"G1 {_xy(layout, *pts[0])} F{f.travel}")
        w(f"G1 Z{_z(down)} F{f.z}")
        if len(pts) > 1:
            w(f"G1 {_xy(layout, *pts[1])} F{f.draw}")
            for x, y in pts[2:]:
                w.move(f"G1 {_xy(layout, x, y)}")
        w(f"G1 Z{_z(z.up)} F{f.z}")
    end_sequence(w, layout)

    text = w.text()
    problems = validate(text, min_z=lowest, bed_w=layout.bed.w, bed_h=layout.bed.h)
    if problems:
        raise GcodeSafetyError(problems)
    return text


def compile_lines(layout: Layout, lines_pen: list[np.ndarray], title: str = "") -> str:
    return compile_strokes(layout, [Stroke(l) for l in lines_pen], title)


def pen_change(w: _Writer, layout: Layout, label: str, screen: Screen | None = None) -> None:
    """Go back to the touch-off point and pause so the next pen can be fitted and touched off."""
    z, f = layout.z, layout.feed
    lx, ly, _, _ = layout.local_touch()
    x, y = layout.area_to_pen([lx, ly])
    w(f"; ===== pen change: {label} =====")
    w(f"G1 Z{_z(z.trace)} F{f.z}", "lift the pen clear")
    w(f"G1 {_xy(layout, x, y)} F{f.travel}", "back to the touch-off point")
    w(f"G1 Z{_z(z.touch)} F{f.z_slow}", "touch-off height")
    w("M400")
    _claim(w, screen, screen.claim_pen if screen else None, "pen change")
    w("M400 U1", f"PAUSE: swap to {label}, lower its tip to the paper, tighten")
    w(f"G1 Z{_z(z.up)} F{f.z}")
    _claim(w, screen, screen.claim_draw if screen else None, "drawing")


def stroke_seconds(layout: Layout, groups: list[tuple[str, list[np.ndarray]]]) -> list[list[float]]:
    """Estimated seconds for each stroke (travel to it, pen down, draw, pen up), per group."""
    z, f = layout.z, layout.feed
    lift = 2 * (z.hop + z.press) / (f.z / 60)
    out = []
    pos = np.asarray(layout.area_to_pen(layout.local_touch()[:2]), float)
    for _, lines in groups:
        times = []
        for pts in lines:
            pts = np.asarray(pts, float)
            if len(pts) == 0:
                times.append(0.0)
                continue
            travel = float(np.hypot(*(pts[0] - pos))) / (f.travel / 60)
            draw = float(np.hypot(*np.diff(pts, axis=0).T).sum()) / (f.draw / 60) if len(pts) > 1 else 0.0
            times.append(travel + draw + lift)
            pos = pts[-1]
        out.append(times)
    return out


def job_seconds(layout: Layout, groups: list[tuple[str, list[np.ndarray]]], screen: Screen | None = None) -> float:
    """Machine time for the whole job (not counting the time you spend at the pauses)."""
    setup = (screen or Screen()).setup_seconds
    return setup * (1 + 0.4 * max(0, len(groups) - 1)) + sum(sum(t) for t in stroke_seconds(layout, groups))


def _hms(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def compile_groups(layout: Layout, groups: list[tuple[str, list[np.ndarray]]], title: str = "",
                   check_layout: bool = True, screen: Screen | None = None) -> str:
    """Several pens in one job: (pen label, pen-coordinate lines) groups, with a pause between them."""
    if check_layout:
        bad = [f"{c.title}: {c.detail}" for c in layout.checks() if c.state == "bad"]
        if bad:
            raise UnsafeLayout(bad)
    z, f = layout.z, layout.feed
    w = _Writer()
    per_stroke = stroke_seconds(layout, groups)
    total = job_seconds(layout, groups, screen)
    pens = ", ".join(label for label, _ in groups)
    # The header block Bambu Studio writes: Bambu Studio and Bambu Connect read the time from it.
    w("; HEADER_BLOCK_START")
    w("; BambuPlot pen plot")
    w(f"; model printing time: {_hms(total)}; total estimated time: {_hms(total)}")
    w("; total layer number: 1")
    w("; total filament weight [g] : 0.00")
    w("; HEADER_BLOCK_END")
    w("")
    w("; EXECUTABLE_BLOCK_START")
    progress = bool(screen and screen.progress)
    if progress:
        w(f"M73 P0 R{int(np.ceil(total / 60))}", "screen: progress and time left")
    start_sequence(w, layout, title + (f" · pens: {pens}" if len(groups) > 1 else ""), screen)
    done = (screen or Screen()).setup_seconds
    last_pct = -1
    for i, (label, lines) in enumerate(groups):
        if i > 0:
            pen_change(w, layout, label, screen)
            done += 0.4 * (screen or Screen()).setup_seconds
        else:
            w(f"; pen: {label}")
        for k, pts in enumerate(lines):
            pts = np.asarray(pts, float)
            if len(pts) == 0:
                continue
            w.move(f"G1 {_xy(layout, *pts[0])} F{f.travel}")
            w(f"G1 Z{_z(z.down)} F{f.z}")
            if len(pts) > 1:
                w(f"G1 {_xy(layout, *pts[1])} F{f.draw}")
                for x, y in pts[2:]:
                    w.move(f"G1 {_xy(layout, x, y)}")
            w(f"G1 Z{_z(z.up)} F{f.z}")
            if progress:
                done += per_stroke[i][k]
                pct = min(99, int(100 * done / max(total, 1e-9)))
                if pct > last_pct:
                    w(f"M73 P{pct} R{int(np.ceil(max(0.0, total - done) / 60))}")
                    last_pct = pct
    if progress:
        w("M73 P100 R0")
    end_sequence(w, layout)
    w("; EXECUTABLE_BLOCK_END")
    text = w.text()
    problems = validate(text, min_z=z.down, bed_w=layout.bed.w, bed_h=layout.bed.h)
    if problems:
        raise GcodeSafetyError(problems)
    return text
