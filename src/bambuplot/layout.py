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
from pydantic import BaseModel, Field

BED_W = 180.0
BED_H = 180.0
PAPER_SIZES = {"A5": (148.0, 210.0), "A6": (105.0, 148.0)}
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


BED = Rect(x=0, y=0, w=BED_W, h=BED_H)


class Paper(BaseModel):
    size: Literal["A5", "A6"] = "A5"
    orientation: Literal["portrait", "landscape"] = "portrait"
    x: float = 16.0  # left edge before rotation
    y: float = -15.0  # front edge before rotation (negative = hangs off the front)
    rotation: float = 0.0
    margin: float = 5.0  # keep the drawing this far inside the paper edges

    def dims(self) -> tuple[float, float]:
        w, h = PAPER_SIZES[self.size]
        return (h, w) if self.orientation == "landscape" else (w, h)

    def poly(self, inset: float = 0.0) -> np.ndarray:
        w, h = self.dims()
        return box(self.x + w / 2, self.y + h / 2, w - 2 * inset, h - 2 * inset, self.rotation)


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
    touch_off: TouchOff = "fl"

    @property
    def safe_z(self) -> float:
        return self.clip_height + 5.0

    def reach(self) -> Rect:
        """Where the pen tip can go while the nozzle stays inside 0–180 mm."""
        return Rect(x=self.pen.offset_x, y=self.pen.offset_y, w=BED_W, h=BED_H)

    def clip_rects(self) -> list[Rect]:
        return [Rect(x=c.x, y=c.y, w=self.clip_w, h=self.clip_d) for c in self.clips]

    def keep_outs(self) -> list[Rect]:
        return [r.grow(self.clip_keep_out) for r in self.clip_rects()]

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
        keep_in = [self.paper.poly(self.paper.margin), BED.poly(), self.reach().poly()]
        keep_out = [k.poly() for k in self.keep_outs()]
        return keep_in, keep_out

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

        ok = contains(BED.poly(), ap)
        out.append(Check(
            "ok" if ok else "bad", "On supported paper",
            "The whole area sits on the plate." if ok
            else "Part of the area is off the plate, where the paper has nothing under it.",
        ))

        ok = contains(self.reach().poly(), ap)
        out.append(Check(
            "ok" if ok else "bad", "Within pen reach",
            "Every nozzle move stays inside X 0–180, Y 0–180." if ok
            else "With this pen offset the nozzle would have to leave 0–180 mm.",
        ))

        hits = [str(i + 1) for i, k in enumerate(self.keep_outs()) if overlaps(k.poly(), ap)]
        out.append(Check(
            "bad" if hits else "ok", "Clear of clips",
            f"Overlaps the keep-out of clip {', '.join(hits)}." if hits
            else f"At least {self.clip_keep_out:g} mm from every clip.",
        ))

        loose = [str(i + 1) for i, r in enumerate(self.clip_rects()) if not overlaps(r.poly(), paper)]
        if loose:
            out.append(Check("warn", "Clip not on paper", f"Clip {', '.join(loose)} doesn't touch the sheet."))

        over = {
            "front": max(0.0, -paper[:, 1].min()), "back": max(0.0, paper[:, 1].max() - BED_H),
            "left": max(0.0, -paper[:, 0].min()), "right": max(0.0, paper[:, 0].max() - BED_W),
        }
        hang = [f"{k} {v:.0f} mm" for k, v in over.items() if v > 0.05]
        out.append(Check(
            "warn" if hang else "ok", "Paper overhang",
            f"The sheet hangs over the plate ({', '.join(hang)}). Check it clears the frame as the bed moves."
            if hang else "The sheet fits on the plate.",
        ))

        if self.z.press > 0.3:
            out.append(Check("warn", "Pen pressure", f"{self.z.press:g} mm is a lot for a rigid holder."))
        return out

    @property
    def is_safe(self) -> bool:
        return not any(c.state == "bad" for c in self.checks())
