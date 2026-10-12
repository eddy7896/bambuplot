"""Layers: several effects and pictures stacked in one drawing area.

Each layer is drawn in its own frame inside the drawing area (``Place``, as
fractions of the area, optionally rotated) and with its own pen. Strokes are
plotted in layer order: each layer is finished before the next one starts.
Neighbouring layers that use the same pen share one pen group, so the job
pauses for a pen change only when the pen actually changes.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from .effects import _clip_to_box
from .image import Prep

Lines = list[np.ndarray]

# Pen slots: label, colour name (used in pause messages), preview colour.
PENS = {
    1: ("Pen 1", "black", "#1d1d1f"),
    2: ("Pen 2", "blue", "#1f5fd6"),
    3: ("Pen 3", "red", "#d6332a"),
    4: ("Pen 4", "green", "#2a9a4b"),
    5: ("Pen 5", "orange", "#e8820c"),
    6: ("Pen 6", "purple", "#8a3fd1"),
}


def pen_label(n: int) -> str:
    label, colour, _ = PENS.get(n, (f"Pen {n}", "", ""))
    return f"{label} ({colour})" if colour else label


class Place(BaseModel):
    """A layer's frame inside the drawing area, as fractions of the area."""

    x: float = Field(0.0, ge=-1, le=2)
    y: float = Field(0.0, ge=-1, le=2)
    w: float = Field(1.0, gt=0.02, le=3)
    h: float = Field(1.0, gt=0.02, le=3)
    rotation: float = 0.0

    @property
    def is_full(self) -> bool:
        return (abs(self.x) < 1e-9 and abs(self.y) < 1e-9 and abs(self.w - 1) < 1e-9
                and abs(self.h - 1) < 1e-9 and abs(self.rotation) < 1e-9)


class Layer(BaseModel):
    id: str = "l1"
    name: str = ""
    visible: bool = True
    effect: str = "waves"
    params: dict = Field(default_factory=dict)  # settings for this layer's effect
    prep: Prep = Field(default_factory=Prep)
    image_id: str | None = None
    place: Place = Field(default_factory=Place)
    pen: int = Field(1, ge=1, le=6)


def frame_size(place: Place, area_w: float, area_h: float) -> tuple[float, float]:
    return place.w * area_w, place.h * area_h


def place_lines(lines: Lines, place: Place, area_w: float, area_h: float) -> Lines:
    """Move a layer's lines (its frame's own coordinates) into the drawing area, cut at its edges."""
    if place.is_full:
        return lines
    fw, fh = frame_size(place, area_w, area_h)
    cx, cy = place.x * area_w + fw / 2, place.y * area_h + fh / 2
    t = np.radians(place.rotation)
    rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    out: Lines = []
    for line in lines:
        p = (np.asarray(line, float) - [fw / 2, fh / 2]) @ rot.T + [cx, cy]
        if len(p) == 1:
            if 0 <= p[0, 0] <= area_w and 0 <= p[0, 1] <= area_h:
                out.append(p)
            continue
        inside = (p[:, 0].min() >= 0 and p[:, 1].min() >= 0 and p[:, 0].max() <= area_w and p[:, 1].max() <= area_h)
        out += [p] if inside else _clip_to_box(p, area_w, area_h)
    return out


def group_in_order(per_layer: list[tuple[int, Lines]]) -> list[tuple[int, Lines]]:
    """Keep layer order; merge only neighbouring layers that use the same pen.

    Pens 1, 2, 1 give three groups (two pen changes), so layer 3 is never drawn before layer 2.
    """
    groups: list[tuple[int, Lines]] = []
    for pen, lines in per_layer:
        if not lines:
            continue
        if groups and groups[-1][0] == pen:
            groups[-1][1].extend(lines)
        else:
            groups.append((pen, list(lines)))
    return groups

