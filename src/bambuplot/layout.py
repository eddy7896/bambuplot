"""Bed layout: paper, clips, pen holder and drawing area on the A1 Mini plate.

All values are millimeters in the printer frame: origin at the plate's
front-left corner, +X to the right, +Y toward the back. Angles are degrees,
counter-clockwise seen from above.

The paper and the drawing area are rectangles that may be rotated about their
own centers; ``x, y, w, h`` describe them before rotation.

"Pen" coordinates are where the pen tip lands on the paper. "Nozzle"
coordinates are what the printer is told to move to: pen minus the holder
offset. Effects work in "area" (local) coordinates: origin at the drawing
area's front-left corner, axes along its edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, field_validator

BED_W = 180.0
BED_H = 180.0
# Portrait width × height in mm. "custom" uses Paper.width / Paper.height.
PAPER_SIZES = {
    "A5": (148.0, 210.0),
    "A6": (105.0, 148.0),
    "B6": (125.0, 176.0),
    "Half Letter": (139.7, 215.9),
    "Postcard 4×6 in": (101.6, 152.4),
    "Square 140": (140.0, 140.0),
}
PAPER_LABELS = {
    "A5": "A5 · 148 × 210",
    "A6": "A6 · 105 × 148",
    "B6": "B6 · 125 × 176",
    "Half Letter": "Half Letter · 5½ × 8½ in",
    "Postcard 4×6 in": "Postcard · 4 × 6 in",
    "Square 140": "Square · 140 × 140",
}
MIN_AREA = 10.0
EPS = 1e-6


# ---------------------------------------------------------------- polygon math


def rotation(deg: float) -> np.ndarray:
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s], [s, c]])


def box(cx: float, cy: float, w: float, h: float, deg: float = 0.0) -> np.ndarray:
    """Corners of a rectangle rotated about its center, counter-clockwise from front-left."""
    local = np.array([[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]])
    return local @ rotation(deg).T + (cx, cy)


def inside(poly: np.ndarray, pts, eps: float = EPS) -> np.ndarray:
    """Which points lie inside (or on) a convex counter-clockwise polygon."""
    pts = np.atleast_2d(np.asarray(pts, float))
    a = poly
    e = np.roll(poly, -1, axis=0) - a
    rel = pts[:, None, :] - a[None]
    cross = e[None, :, 0] * rel[..., 1] - e[None, :, 1] * rel[..., 0]
    lengths = np.hypot(e[:, 0], e[:, 1])
    return (cross >= -eps * lengths[None]).all(axis=1)


def contains(outer: np.ndarray, inner: np.ndarray) -> bool:
    return bool(inside(outer, inner).all())


def overlaps(a: np.ndarray, b: np.ndarray, eps: float = EPS) -> bool:
    """Separating-axis test for convex polygons; touching edges don't count."""
    for poly in (a, b):
        e = np.roll(poly, -1, axis=0) - poly
        for n in np.column_stack([-e[:, 1], e[:, 0]]):
            n = n / np.hypot(*n)
            pa, pb = a @ n, b @ n
            if pa.max() <= pb.min() + eps or pb.max() <= pa.min() + eps:
                return False
    return True


# ---------------------------------------------------------------------- models


class Rect(BaseModel):
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def poly(self, inset: float = 0.0) -> np.ndarray:
        return box(*self.center, self.w - 2 * inset, self.h - 2 * inset, getattr(self, "rotation", 0.0))

    def grow(self, m: float) -> Rect:
        return Rect(x=self.x - m, y=self.y - m, w=self.w + 2 * m, h=self.h + 2 * m)


class Area(Rect):
    rotation: float = 0.0

    def to_world(self, pts) -> np.ndarray:
        """Area-local points (origin at the area's front-left corner) → printer frame."""
        pts = np.asarray(pts, float)
        local = pts - (self.w / 2, self.h / 2)
        return local @ rotation(self.rotation).T + self.center


BED = Rect(x=0, y=0, w=BED_W, h=BED_H)  # the A1 mini plate; layouts carry their own Bed


# Build plates: key → (label, width, depth, Bambu printer model ids used in .gcode.3mf files)
PRINTERS = {
    "a1mini": ("Bambu Lab A1 mini", 180.0, 180.0, ("N1",)),
    "a1": ("Bambu Lab A1", 256.0, 256.0, ("N2S",)),
    "p1": ("Bambu Lab P1P / P1S", 256.0, 256.0, ("C11", "C12")),
    "x1": ("Bambu Lab X1 Carbon / X1E", 256.0, 256.0, ("BL-P001", "BL-P002")),
}


class Bed(BaseModel):
    """The printer's build plate: where the nozzle can go, X 0..w and Y 0..h."""

    printer: str = "a1mini"  # a PRINTERS key, or "custom"
    w: float = Field(BED_W, ge=50, le=600)
    h: float = Field(BED_H, ge=50, le=600)

    @property
    def label(self) -> str:
        return PRINTERS[self.printer][0] if self.printer in PRINTERS else f"Custom {self.w:g} × {self.h:g}"

    @property
    def model_id(self) -> tuple[str, ...] | None:
        return PRINTERS[self.printer][3] if self.printer in PRINTERS else None

    def rect(self) -> Rect:
        return Rect(x=0, y=0, w=self.w, h=self.h)


class Paper(BaseModel):
    size: str = "A5"  # a PAPER_SIZES key, or "custom"
    orientation: Literal["portrait", "landscape"] = "portrait"
    x: float = 16.0  # left edge before rotation
    y: float = -15.0  # front edge before rotation (negative = hangs off the front)
    rotation: float = 0.0
    margin: float = 5.0  # keep the drawing this far inside the paper edges
    width: float = Field(148.0, ge=10, le=400)  # used when size is "custom"
    height: float = Field(210.0, ge=10, le=400)

    @field_validator("size")
    @classmethod
    def _known_size(cls, v: str) -> str:
        return v if v in PAPER_SIZES or v == "custom" else "A5"

    def dims(self) -> tuple[float, float]:
        if self.size == "custom":
            return self.width, self.height  # custom sizes are as drawn; orientation doesn't swap them
        w, h = PAPER_SIZES[self.size]
        return (h, w) if self.orientation == "landscape" else (w, h)

    def poly(self, inset: float = 0.0) -> np.ndarray:
        w, h = self.dims()
        return box(self.x + w / 2, self.y + h / 2, w - 2 * inset, h - 2 * inset, self.rotation)


EDGES = ("left", "front", "right", "back")


class Tape(BaseModel):
    """Masking tape along the paper's own edges (they turn with the paper)."""

    left: bool = False
    front: bool = False
    right: bool = False
    back: bool = False
    width: float = Field(18.0, ge=3, le=60)  # roll width
    on_paper: float = Field(8.0, ge=0.5, le=40)  # how far the tape reaches onto the sheet
    keep_out: float = Field(2.0, ge=0, le=20)  # extra gap between the tape and any ink
    overhang: float = 6.0  # how far the strip runs past each corner

    def edges(self) -> list[str]:
        return [e for e in EDGES if getattr(self, e)]


class Pen(BaseModel):
    offset_x: float = 0.0  # pen tip minus nozzle, +X right
    offset_y: float = 0.0  # pen tip minus nozzle, +Y toward the back
    width_mm: float = Field(0.4, gt=0)


class ZSettings(BaseModel):
    touch: float = Field(3.0, ge=1.0)  # nozzle Z where the pen tip was set to touch the paper
    press: float = Field(0.15, ge=0.0, le=1.0)  # how far below touch-off to draw
    hop: float = Field(1.5, ge=0.3)  # pen lift above the paper between strokes

    @property
    def down(self) -> float:
        return self.touch - self.press

    @property
    def up(self) -> float:
        return self.touch + self.hop

    @property
    def trace(self) -> float:
        return self.touch + 5.0


class Feed(BaseModel):
    travel: int = Field(5000, gt=0)  # mm/min, pen up
    draw: int = Field(3000, gt=0)  # mm/min, pen down
    z: int = Field(600, gt=0)
    z_slow: int = Field(300, gt=0)
    accel: int = Field(1000, gt=0)  # mm/s², kept low for the cantilever arm


class Clip(BaseModel):
    x: float
    y: float


TouchOff = Literal["fl", "fr", "bl", "br", "c"]
TOUCH_NAMES = {
    "fl": "front-left corner",
    "fr": "front-right corner",
    "bl": "back-left corner",
    "br": "back-right corner",
    "c": "center",
}


@dataclass
class Check:
    state: Literal["ok", "warn", "bad"]
    title: str
    detail: str


def _default_clips() -> list[Clip]:
    return [Clip(x=30, y=-6), Clip(x=134, y=-6), Clip(x=30, y=170), Clip(x=134, y=170)]


class Layout(BaseModel):
    paper: Paper = Field(default_factory=Paper)
    pen: Pen = Field(default_factory=Pen)
    z: ZSettings = Field(default_factory=ZSettings)
    feed: Feed = Field(default_factory=Feed)
    clip_w: float = 16.0
    clip_d: float = 16.0
    clip_height: float = 12.0
    clip_keep_out: float = 6.0
    clips: list[Clip] = Field(default_factory=_default_clips)
    area: Area = Field(default_factory=lambda: Area(x=22, y=24, w=136, h=132))
    tape: Tape = Field(default_factory=Tape)
    bed: Bed = Field(default_factory=Bed)
    touch_off: TouchOff = "fl"

    @property
    def safe_z(self) -> float:
        return self.clip_height + 5.0

    def reach(self) -> Rect:
        """Where the pen tip can go while the nozzle stays inside 0–180 mm."""
        return Rect(x=self.pen.offset_x, y=self.pen.offset_y, w=self.bed.w, h=self.bed.h)

    def clip_rects(self) -> list[Rect]:
        return [Rect(x=c.x, y=c.y, w=self.clip_w, h=self.clip_d) for c in self.clips]

    def keep_outs(self) -> list[Rect]:
        return [r.grow(self.clip_keep_out) for r in self.clip_rects()]

    def tape_polys(self, grow: float = 0.0) -> list[tuple[str, np.ndarray]]:
        """Each taped edge's strip as a polygon in the printer frame, optionally grown."""
        t, (w, h) = self.tape, self.paper.dims()
        out_w = t.width - t.on_paper  # the part of the tape off the sheet
        ext = t.overhang
        local = {  # x0, y0, x1, y1 in the paper's own frame, origin at its front-left corner
            "left": (-out_w, -ext, t.on_paper, h + ext),
            "right": (w - t.on_paper, -ext, w + out_w, h + ext),
            "front": (-ext, -out_w, w + ext, t.on_paper),
            "back": (-ext, h - t.on_paper, w + ext, h + out_w),
        }
        pc = np.array([self.paper.x + w / 2, self.paper.y + h / 2])
        out = []
        for edge in t.edges():
            x0, y0, x1, y1 = local[edge]
            cx, cy = (x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2  # center relative to the paper's center
            c = pc + rotation(self.paper.rotation) @ np.array([cx, cy])
            out.append((edge, box(c[0], c[1], x1 - x0 + 2 * grow, y1 - y0 + 2 * grow, self.paper.rotation)))
        return out

    def local_touch(self) -> tuple[float, float, int, int]:
        """Touch-off point in area coordinates, plus the inward direction along each edge."""
        w, h = self.area.w, self.area.h
        return {
            "fl": (0.0, 0.0, 1, 1),
            "fr": (w, 0.0, -1, 1),
            "bl": (0.0, h, 1, -1),
            "br": (w, h, -1, -1),
            "c": (w / 2, h / 2, 1, 1),
        }[self.touch_off]

    def touch_point(self) -> tuple[float, float]:
        lx, ly, _, _ = self.local_touch()
        x, y = self.area.to_world([lx, ly])
        return float(x), float(y)

    def area_to_pen(self, pts) -> np.ndarray:
        return self.area.to_world(pts)

    def to_nozzle(self, pts) -> np.ndarray:
        return np.asarray(pts, float) - (self.pen.offset_x, self.pen.offset_y)

    def allowed_polys(self) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """Regions the area must stay inside, and regions it must stay out of."""
        keep_in = [self.paper.poly(self.paper.margin), self.bed.rect().poly(), self.reach().poly()]
        keep_out = [k.poly() for k in self.keep_outs()] + [p for _, p in self.tape_polys(self.tape.keep_out)]
        return keep_in, keep_out

    def edge_distances(self, poly: np.ndarray) -> dict:
        """How far a shape's outer edges are from each plate edge (negative = hangs over)."""
        (x0, y0), (x1, y1) = poly.min(axis=0), poly.max(axis=0)
        return {"left": float(x0), "front": float(y0), "right": float(self.bed.w - x1), "back": float(self.bed.h - y1)}

    def checks(self) -> list[Check]:
        a = self.area
        ap = a.poly()
        paper = self.paper.poly()
        out: list[Check] = []

        if a.w < MIN_AREA or a.h < MIN_AREA:
            out.append(Check("bad", "Drawing area too small", f"Make it at least {MIN_AREA:g} × {MIN_AREA:g} mm."))

        m = self.paper.margin
        ok = contains(self.paper.poly(m), ap)
        out.append(Check(
            "ok" if ok else "bad", "Inside the paper",
            f"At least {m:g} mm from every paper edge." if ok else f"Keep the area {m:g} mm inside the paper edges.",
        ))

        ok = contains(self.bed.rect().poly(), ap)
        out.append(Check(
            "ok" if ok else "bad", "On supported paper",
            "The whole area sits on the plate." if ok
            else "Part of the area is off the plate, where the paper has nothing under it.",
        ))

        ok = contains(self.reach().poly(), ap)
        out.append(Check(
            "ok" if ok else "bad", "Within pen reach",
            f"Every nozzle move stays inside X 0–{self.bed.w:g}, Y 0–{self.bed.h:g}." if ok
            else f"With this pen offset the nozzle would have to leave the {self.bed.w:g} × {self.bed.h:g} mm plate.",
        ))

        hits = [str(i + 1) for i, k in enumerate(self.keep_outs()) if overlaps(k.poly(), ap)]
        out.append(Check(
            "bad" if hits else "ok", "Clear of clips",
            f"Overlaps the keep-out of clip {', '.join(hits)}." if hits
            else f"At least {self.clip_keep_out:g} mm from every clip.",
        ))

        taped = [e for e, poly in self.tape_polys(self.tape.keep_out) if overlaps(poly, ap)]
        if self.tape.edges():
            out.append(Check(
                "bad" if taped else "ok", "Clear of tape",
                f"Overlaps the tape on the {', '.join(taped)} edge{'s' if len(taped) > 1 else ''}." if taped
                else f"At least {self.tape.keep_out:g} mm from the tape on the {', '.join(self.tape.edges())} "
                     f"edge{'s' if len(self.tape.edges()) > 1 else ''}.",
            ))

        loose = [str(i + 1) for i, r in enumerate(self.clip_rects()) if not overlaps(r.poly(), paper)]
        if loose:
            out.append(Check("warn", "Clip not on paper", f"Clip {', '.join(loose)} doesn't touch the sheet."))
        holding = len(self.clips) - len(loose)
        if holding == 0 and not self.tape.edges():
            out.append(Check("warn", "Nothing holds the paper",
                             "No clip touches the sheet and no edge is taped. Loose paper shifts as the bed moves."))

        over = {
            "front": max(0.0, -paper[:, 1].min()), "back": max(0.0, paper[:, 1].max() - self.bed.h),
            "left": max(0.0, -paper[:, 0].min()), "right": max(0.0, paper[:, 0].max() - self.bed.w),
        }
        hang = [f"{k} {v:.0f} mm" for k, v in over.items() if v > 0.05]
        out.append(Check(
            "warn" if hang else "ok", "Paper overhang",
            f"The sheet hangs over the plate ({', '.join(hang)}). Check it clears the frame as the bed moves."
            if hang else "The sheet fits on the plate.",
        ))

        if self.pen.offset_x == 0 and self.pen.offset_y == 0:
            out.append(Check("warn", "Pen offset is 0, 0",
                             "That means the pen tip is exactly under the nozzle. If your holder puts it beside the "
                             "nozzle, set Bed → Pen Holder → Offset, or run the alignment test."))

        if self.z.press > 0.3:
            out.append(Check("warn", "Pen pressure", f"{self.z.press:g} mm is a lot for a rigid holder."))
        return out

    @property
    def is_safe(self) -> bool:
        return not any(c.state == "bad" for c in self.checks())
