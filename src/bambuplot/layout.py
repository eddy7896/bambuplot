"""Bed layout: paper, clips, pen holder and drawing area on the A1 Mini plate.

All values are millimeters in the printer frame: origin at the plate's
front-left corner, +X to the right, +Y toward the back.

"Pen" coordinates are where the pen tip lands on the paper. "Nozzle"
coordinates are what the printer is told to move to: pen minus the holder
offset. Effects work in "area" coordinates, with the origin at the drawing
area's front-left corner.
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

    def inset(self, m: float) -> Rect:
        return Rect(x=self.x + m, y=self.y + m, w=self.w - 2 * m, h=self.h - 2 * m)

    def intersect(self, o: Rect) -> Rect | None:
        x, y = max(self.x, o.x), max(self.y, o.y)
        x2, y2 = min(self.x2, o.x2), min(self.y2, o.y2)
        if x2 <= x or y2 <= y:
            return None
        return Rect(x=x, y=y, w=x2 - x, h=y2 - y)

    def contains(self, o: Rect) -> bool:
        return (
            o.x >= self.x - EPS
            and o.y >= self.y - EPS
            and o.x2 <= self.x2 + EPS
            and o.y2 <= self.y2 + EPS
        )

    def overlaps(self, o: Rect) -> bool:
        return self.x < o.x2 and self.x2 > o.x and self.y < o.y2 and self.y2 > o.y


BED = Rect(x=0, y=0, w=BED_W, h=BED_H)


class Paper(BaseModel):
    size: Literal["A5", "A6"] = "A5"
    orientation: Literal["portrait", "landscape"] = "portrait"
    x: float = 16.0  # left edge
    y: float = -15.0  # front edge (negative = hangs off the front of the plate)
    margin: float = 5.0  # keep the drawing this far inside the paper edges

    def rect(self) -> Rect:
        w, h = PAPER_SIZES[self.size]
        if self.orientation == "landscape":
            w, h = h, w
        return Rect(x=self.x, y=self.y, w=w, h=h)


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
    area: Rect = Field(default_factory=lambda: Rect(x=22, y=24, w=136, h=132))
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
        return [r.inset(-self.clip_keep_out) for r in self.clip_rects()]

    def touch_point(self) -> tuple[float, float, int, int]:
        """Pen XY of the touch-off point and the inward direction for the test mark."""
        a = self.area
        return {
            "fl": (a.x, a.y, 1, 1),
            "fr": (a.x2, a.y, -1, 1),
            "bl": (a.x, a.y2, 1, -1),
            "br": (a.x2, a.y2, -1, -1),
            "c": (a.x + a.w / 2, a.y + a.h / 2, 1, 1),
        }[self.touch_off]

    def area_to_pen(self, pts: np.ndarray) -> np.ndarray:
        return np.asarray(pts, float) + (self.area.x, self.area.y)

    def to_nozzle(self, pts: np.ndarray) -> np.ndarray:
        return np.asarray(pts, float) - (self.pen.offset_x, self.pen.offset_y)

    def checks(self) -> list[Check]:
        a, p = self.area, self.paper.rect()
        out: list[Check] = []

        big = a.w >= MIN_AREA and a.h >= MIN_AREA
        if not big:
            out.append(Check("bad", "Drawing area too small", f"Make it at least {MIN_AREA:g} × {MIN_AREA:g} mm."))

        m = self.paper.margin
        ok = p.inset(m).contains(a)
        out.append(Check(
            "ok" if ok else "bad", "Inside the paper",
            f"At least {m:g} mm from every paper edge." if ok else f"Keep the area {m:g} mm inside the paper edges.",
        ))

        ok = BED.contains(a)
        out.append(Check(
            "ok" if ok else "bad", "On supported paper",
            "The whole area sits on the plate." if ok
            else "Part of the area is off the plate, where the paper has nothing under it.",
        ))

        ok = self.reach().contains(a)
        out.append(Check(
            "ok" if ok else "bad", "Within pen reach",
            "Every nozzle move stays inside X 0–180, Y 0–180." if ok
            else "With this pen offset the nozzle would have to leave 0–180 mm.",
        ))

        hits = [str(i + 1) for i, k in enumerate(self.keep_outs()) if k.overlaps(a)]
        out.append(Check(
            "bad" if hits else "ok", "Clear of clips",
            f"Overlaps the keep-out of clip {', '.join(hits)}." if hits
            else f"At least {self.clip_keep_out:g} mm from every clip.",
        ))

        loose = [str(i + 1) for i, r in enumerate(self.clip_rects()) if not r.overlaps(p)]
        if loose:
            out.append(Check("warn", "Clip not on paper", f"Clip {', '.join(loose)} doesn't touch the sheet."))

        over = {
            "front": max(0.0, -p.y), "back": max(0.0, p.y2 - BED_H),
            "left": max(0.0, -p.x), "right": max(0.0, p.x2 - BED_W),
        }
        hang = [f"{k} {v:g} mm" for k, v in over.items() if v > 0.05]
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
