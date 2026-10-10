"""Image → tone → effect → cleaned, ordered paths → G-code."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import effects
from .gcode import compile_lines
from .image import Prep, prepare
from .layout import Layout
from .paths import clamp, merge, order_paths, rdp, stats
from .svgin import SvgArt

SIMPLIFY_TOL = 0.05  # mm; well below any pen width


@dataclass
class Plan:
    lines: list[np.ndarray]  # area coordinates, in drawing order
    stats: dict

    def pen_lines(self, layout: Layout) -> list[np.ndarray]:
        return [layout.area_to_pen(l) for l in self.lines]


def touch_in_area(layout: Layout) -> tuple[float, float]:
    lx, ly, _, _ = layout.local_touch()
    return lx, ly


def make_plan(
    layout: Layout,
    effect: str,
    params: dict | None = None,
    prep: Prep | None = None,
    gray: np.ndarray | None = None,
    seed: int = 0,
    svg: SvgArt | None = None,
) -> Plan:
    a = layout.area
    eff = effects.EFFECTS.get(effect)
    if eff is None:
        raise KeyError(f"Unknown effect '{effect}'. Available: {', '.join(effects.EFFECTS)}")
    if eff.source == "svg" and svg is None:
        raise ValueError(f"'{eff.label}' needs an SVG file.")
    if eff.source != "svg" and svg is not None:
        raise ValueError(f"'{eff.label}' works on photos and pictures. Use 'SVG paths' for an SVG.")
    prep = prep or Prep()
    if eff.resolution and eff.resolution > prep.px_per_mm:
        prep = prep.model_copy(update={"px_per_mm": eff.resolution})
    tone = prepare(None if svg is not None else gray, a.w, a.h, prep)
    ctx = effects.Context(pen_width=layout.pen.width_mm, seed=seed, svg=svg)
    raw = effects.run(effect, tone, params, ctx)
    lines = [rdp(l, SIMPLIFY_TOL) for l in clamp(raw, a.w, a.h)]
    start = touch_in_area(layout)
    lines = order_paths(lines, start)
    lines = merge(lines, layout.pen.width_mm * 0.5)
    return Plan(lines=lines, stats=stats(lines, layout, start))


def plan_to_gcode(layout: Layout, plan: Plan, title: str = "") -> str:
    return compile_lines(layout, plan.pen_lines(layout), title)
