"""Vector effects: turn a Tone field into pen paths.

Every effect returns a list of polylines, each an (N, 2) array in area
coordinates (mm, origin at the drawing area's front-left corner, +Y back).
A one-point polyline is a dot: the pen taps down and lifts again.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import numpy as np
from scipy.spatial import cKDTree

from .image import Tone
from .tsp import nn_tour, two_opt

Lines = list[np.ndarray]


@dataclass
class Param:
    name: str
    label: str
    kind: str  # "float" | "int" | "bool" | "choice"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[str] | None = None
    help: str = ""


@dataclass
class Context:
    pen_width: float = 0.4
    seed: int = 0


@dataclass
class Effect:
    name: str
    label: str
    description: str
    params: list[Param]
    fn: Callable[..., Lines] = field(repr=False)

    def coerce(self, values: dict | None) -> dict:
        """Fill defaults, cast types, clamp to limits and drop unknown keys."""
        values = values or {}
        out = {}
        for p in self.params:
            v = values.get(p.name, p.default)
            try:
                if p.kind == "float":
                    v = float(v)
                elif p.kind == "int":
                    v = int(round(float(v)))
                elif p.kind == "bool":
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif p.kind == "choice":
                    v = v if v in (p.choices or []) else p.default
            except (TypeError, ValueError):
                v = p.default
            if p.kind in ("float", "int"):
                if p.min is not None:
                    v = max(p.min, v)
                if p.max is not None:
                    v = min(p.max, v)
            out[p.name] = v
        return out

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "params": [asdict(p) for p in self.params],
        }


EFFECTS: dict[str, Effect] = {}


def register(name: str, label: str, description: str, params: list[Param]):
    def deco(fn):
        EFFECTS[name] = Effect(name, label, description, params, fn)
        return fn

    return deco


def run(name: str, tone: Tone, params: dict | None = None, ctx: Context | None = None) -> Lines:
    if name not in EFFECTS:
        raise KeyError(f"Unknown effect '{name}'. Available: {', '.join(EFFECTS)}")
    eff = EFFECTS[name]
    return eff.fn(tone, ctx or Context(), **eff.coerce(params))


# --------------------------------------------------------------------------- waves


@register(
    "waves",
    "Wave lines",
    "Horizontal lines whose waves grow taller and tighter in dark areas.",
    [
        Param("spacing", "Line spacing", "float", 2.5, 0.8, 10, 0.1, help="mm between lines"),
        Param("min_wavelength", "Shortest wave", "float", 1.4, 0.5, 10, 0.1, help="mm, used in the darkest areas"),
        Param("max_wavelength", "Longest wave", "float", 7.0, 1, 30, 0.5, help="mm, used in the lightest areas"),
        Param("continuous", "One continuous line", "bool", True, help="Join the rows at the edges so the pen never lifts"),
    ],
)
def waves(tone: Tone, ctx: Context, spacing, min_wavelength, max_wavelength, continuous) -> Lines:
    w, h = tone.w, tone.h
    n = int(h // spacing)
    amp_max = spacing * 0.46
    xs = np.linspace(0, w, max(2, int(np.ceil(w / 0.3)) + 1))
    dx = np.diff(xs, prepend=xs[0])
    rows = []
    for i in range(n):
        yc = h - (i + 0.5) * spacing
        dark = tone.darkness(xs, np.full_like(xs, yc))
        wl = max_wavelength - (max_wavelength - min_wavelength) * dark
        phase = np.cumsum(2 * np.pi * dx / wl)
        y = yc + dark * amp_max * np.sin(phase)
        pts = np.column_stack([xs, y])
        rows.append(pts[::-1] if i % 2 else pts)
    if continuous and rows:
        return [np.concatenate(rows)]
    return rows


# ------------------------------------------------------------------------ halftone


def _circle(cx: float, cy: float, r: float) -> np.ndarray:
    n = max(8, int(np.ceil(2 * np.pi * r / 0.3)))
    t = np.linspace(0, 2 * np.pi, n + 1)
    return np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)])


def _filled_dot(cx: float, cy: float, r: float, pitch: float) -> np.ndarray:
    """Outer circle followed by an inward spiral, drawn as one stroke."""
    ring = _circle(cx, cy, r)
    turns = max(0.0, (r - pitch / 2) / pitch)
    if turns <= 0:
        return ring
    n = max(8, int(np.ceil(2 * np.pi * r * turns / 0.3)))
    t = np.linspace(0, 2 * np.pi * turns, n)
    rr = r - pitch * t / (2 * np.pi)
    spiral = np.column_stack([cx + rr * np.cos(t), cy + rr * np.sin(t)])
    return np.concatenate([ring, spiral[1:]])


@register(
    "halftone",
    "Halftone dots",
    "A rotated grid of circles sized by darkness, like newspaper print.",
    [
        Param("cell", "Grid size", "float", 3.0, 1.0, 15, 0.1, help="mm between dot centers"),
        Param("angle", "Grid angle", "float", 45.0, 0, 90, 1, help="degrees"),
        Param("max_size", "Largest dot", "float", 0.95, 0.2, 1.4, 0.05, help="fraction of the grid size"),
        Param("min_radius", "Smallest dot", "float", 0.25, 0.05, 3, 0.05, help="mm; lighter dots are skipped"),
        Param("fill", "Fill dots", "bool", True, help="Spiral in to fill each dot with ink"),
    ],
)
def halftone(tone: Tone, ctx: Context, cell, angle, max_size, min_radius, fill) -> Lines:
    w, h = tone.w, tone.h
    cx0, cy0 = w / 2, h / 2
    half = np.hypot(w, h) / 2 + cell
    k = np.arange(-half, half + cell, cell)
    gu, gv = np.meshgrid(k, k)
    th = np.radians(angle)
    xs = cx0 + gu * np.cos(th) - gv * np.sin(th)
    ys = cy0 + gu * np.sin(th) + gv * np.cos(th)
    xs, ys = xs.ravel(), ys.ravel()
    inside = (xs >= 0) & (xs <= w) & (ys >= 0) & (ys <= h)
    xs, ys = xs[inside], ys[inside]
    r = np.sqrt(tone.darkness(xs, ys)) * cell / 2 * max_size
    # Keep each dot fully inside the area.
    r = np.minimum.reduce([r, xs, w - xs, ys, h - ys])
    keep = r >= min_radius
    pitch = ctx.pen_width * 0.9
    out = []
    for x, y, rad in zip(xs[keep], ys[keep], r[keep]):
        out.append(_filled_dot(x, y, rad, pitch) if fill else _circle(x, y, rad))
    return out


# -------------------------------------------------------------------------- spiral


@register(
    "spiral",
    "Spiral",
    "One continuous spiral from the center out, wobbling in dark areas.",
    [
        Param("spacing", "Ring spacing", "float", 2.0, 0.8, 8, 0.1, help="mm between turns"),
        Param("min_wavelength", "Shortest wobble", "float", 1.2, 0.5, 10, 0.1, help="mm, used in the darkest areas"),
        Param("max_wavelength", "Longest wobble", "float", 6.0, 1, 30, 0.5, help="mm, used in the lightest areas"),
    ],
)
def spiral(tone: Tone, ctx: Context, spacing, min_wavelength, max_wavelength) -> Lines:
    w, h = tone.w, tone.h
    amp_max = spacing * 0.46
    r_max = min(w, h) / 2 - amp_max
    if r_max <= spacing:
        return []
    step = 0.3
    total = np.pi * r_max**2 / spacing  # arc length of an Archimedean spiral out to r_max
    s = np.arange(0, total, step)
    theta = np.sqrt(4 * np.pi * s / spacing)
    r = spacing * theta / (2 * np.pi)
    cx, cy = w / 2, h / 2
    bx, by = cx + r * np.cos(theta), cy + r * np.sin(theta)
    dark = tone.darkness(bx, by)
    wl = max_wavelength - (max_wavelength - min_wavelength) * dark
    phase = np.cumsum(2 * np.pi * step / wl)
    rr = r + dark * amp_max * np.sin(phase)
    return [np.column_stack([cx + rr * np.cos(theta), cy + rr * np.sin(theta)])]


# ------------------------------------------------------------------------- stipple


@register(
    "stipple",
    "TSP stipple",
    "Dots spread by darkness, then joined into one line (TSP art) or tapped as dots.",
    [
        Param("points", "Number of points", "int", 3000, 100, 20000, 100),
        Param("iterations", "Relaxation passes", "int", 10, 0, 40, 1, help="More passes spread dots more evenly"),
        Param("mode", "Draw as", "choice", "line", choices=["line", "dots"]),
        Param("optimize_seconds", "Path optimizing time", "float", 3.0, 0, 30, 0.5, help="seconds of 2-opt"),
    ],
)
def stipple(tone: Tone, ctx: Context, points, iterations, mode, optimize_seconds) -> Lines:
    px_per_mm = min(4.0, max(1.0, np.sqrt(points * 40 / (tone.w * tone.h))))
    gx, gy, dark = tone.grid(px_per_mm)
    weight = dark**1.5 + 1e-4
    rng = np.random.default_rng(ctx.seed)
    idx = rng.choice(len(weight), size=points, p=weight / weight.sum())
    cell = 1 / px_per_mm
    pts = np.column_stack([gx[idx], gy[idx]]) + rng.uniform(-cell / 2, cell / 2, (points, 2))

    # Weighted Lloyd relaxation: move each point to the dark-weighted center of its cell.
    pix = np.column_stack([gx, gy])
    for _ in range(iterations):
        _, owner = cKDTree(pts).query(pix)
        wsum = np.bincount(owner, weights=weight, minlength=points)
        mx = np.bincount(owner, weights=weight * gx, minlength=points)
        my = np.bincount(owner, weights=weight * gy, minlength=points)
        moved = wsum > 1e-9
        pts[moved, 0] = mx[moved] / wsum[moved]
        pts[moved, 1] = my[moved] / wsum[moved]

    pts[:, 0] = np.clip(pts[:, 0], 0, tone.w)
    pts[:, 1] = np.clip(pts[:, 1], 0, tone.h)
    start = int(np.argmin(pts[:, 0] + pts[:, 1]))  # nearest the front-left corner
    order = nn_tour(pts, start)
    if optimize_seconds > 0:
        order = two_opt(pts, order, optimize_seconds)
    pts = pts[order]
    if mode == "dots":
        return [p[None, :] for p in pts]
    return [pts]
