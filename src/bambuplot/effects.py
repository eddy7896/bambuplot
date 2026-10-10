"""Vector effects: turn a Tone field into pen paths.

Every effect returns a list of polylines, each an (N, 2) array in area
coordinates (mm, origin at the drawing area's front-left corner, +Y back).
A one-point polyline is a dot: the pen taps down and lifts again.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import cv2
import numpy as np
from scipy.spatial import cKDTree

from . import vectorize as vz
from .image import Tone
from .svgin import PX_TO_MM
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
    svg: Any = None  # SvgArt for effects with source="svg"


@dataclass
class Effect:
    name: str
    label: str
    description: str
    params: list[Param]
    fn: Callable[..., Lines] = field(repr=False)
    source: str = "raster"  # "raster" (photos, the sample) or "svg"
    resolution: float | None = None  # px/mm this effect wants from the tone field, at least

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
            "source": self.source,
            "params": [asdict(p) for p in self.params],
        }


EFFECTS: dict[str, Effect] = {}


def register(name: str, label: str, description: str, params: list[Param],
             source: str = "raster", resolution: float | None = None):
    def deco(fn):
        EFFECTS[name] = Effect(name, label, description, params, fn, source, resolution)
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


# ------------------------------------------------------------------- vector


HATCH_ANGLES = (0, 90, 45, -45)  # added on top of the base angle, darkest levels last


def _fill_and_outline(masks: list[np.ndarray], w: float, h: float, ctx: Context, outline: bool,
                      fill: bool, spacing: float, angle: float) -> Lines:
    """Outline each mask and hatch it; each darker mask adds a hatch at a new angle."""
    out: Lines = []
    for k, m in enumerate(masks):
        if outline:
            out += vz.outlines(m, w, h)
        if fill:
            out += vz.hatch(m, w, h, spacing, angle + HATCH_ANGLES[k % 4], min_len=ctx.pen_width)
    return out


@register(
    "trace",
    "Outline trace",
    "Turns the image into vector shapes: outlines around dark areas, filled with hatching. "
    "More levels give a posterized, cross-hatched look.",
    [
        Param("levels", "Tone levels", "int", 1, 1, 4, 1, help="1 = black and white"),
        Param("auto_threshold", "Automatic threshold", "bool", True, help="Only for 1 level"),
        Param("threshold", "Threshold", "float", 0.5, 0.05, 0.95, 0.05, help="Brightness cut-off, 0–1"),
        Param("smoothing", "Smoothing", "float", 0.3, 0, 3, 0.05, help="mm of blur before tracing"),
        Param("min_area", "Ignore specks under", "float", 1.0, 0, 50, 0.5, help="mm²"),
        Param("outline", "Draw outlines", "bool", True),
        Param("fill", "Hatch-fill dark areas", "bool", True),
        Param("hatch_spacing", "Hatch spacing", "float", 0.8, 0.2, 10, 0.05, help="mm between hatch lines"),
        Param("hatch_angle", "Hatch angle", "float", 45.0, -90, 90, 5, help="degrees"),
    ],
    resolution=8.0,
)
def trace(tone: Tone, ctx: Context, levels, auto_threshold, threshold, smoothing, min_area, outline, fill,
          hatch_spacing, hatch_angle) -> Lines:
    rows, cols = tone.data.shape
    ppm = cols / tone.w
    gray = vz.smooth(tone.data, smoothing * ppm)
    if levels == 1:
        cuts = [None if auto_threshold else threshold]
    else:
        cuts = [(levels - k) / (levels + 1) for k in range(levels)]  # lightest cut first
    masks = [vz.clean_mask(vz.threshold(gray, t), min_area * ppm * ppm) for t in cuts]
    return _fill_and_outline(masks, tone.w, tone.h, ctx, outline, fill, hatch_spacing, hatch_angle)


@register(
    "centerline",
    "Centerline trace",
    "For sketches, line art and handwriting: one stroke down the middle of each drawn line, "
    "instead of an outline around it.",
    [
        Param("auto_threshold", "Automatic threshold", "bool", True),
        Param("threshold", "Threshold", "float", 0.5, 0.05, 0.95, 0.05, help="Brightness cut-off, 0–1"),
        Param("smoothing", "Smoothing", "float", 0.15, 0, 2, 0.05, help="mm of blur before tracing"),
        Param("close_gaps", "Close gaps", "float", 0.0, 0, 2, 0.1, help="mm; joins broken lines"),
        Param("min_length", "Drop strokes under", "float", 1.5, 0, 20, 0.5, help="mm"),
    ],
    resolution=8.0,
)
def centerline(tone: Tone, ctx: Context, auto_threshold, threshold, smoothing, close_gaps, min_length) -> Lines:
    rows, cols = tone.data.shape
    ppm = cols / tone.w
    gray = vz.smooth(tone.data, smoothing * ppm)
    mask = vz.threshold(gray, None if auto_threshold else threshold)
    if close_gaps > 0:
        r = max(1, int(round(close_gaps * ppm)))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel).astype(bool)
    mask = vz.clean_mask(mask, (ctx.pen_width * ppm) ** 2)
    return vz.centerlines(mask, tone.w, tone.h, min_length)


@register(
    "contours",
    "Contour lines",
    "Lines of equal brightness, like a topographic map of the image.",
    [
        Param("levels", "Number of levels", "int", 8, 2, 40, 1),
        Param("smoothing", "Smoothing", "float", 0.6, 0, 5, 0.1, help="mm of blur before tracing"),
        Param("min_length", "Drop lines under", "float", 2.0, 0, 50, 0.5, help="mm"),
    ],
    resolution=4.0,
)
def contours(tone: Tone, ctx: Context, levels, smoothing, min_length) -> Lines:
    ppm = tone.data.shape[1] / tone.w
    gray = vz.smooth(tone.data, smoothing * ppm)
    cuts = [(k + 1) / (levels + 1) for k in range(levels)]
    lines = vz.iso_lines(gray, cuts, tone.w, tone.h)
    return [l for l in lines if np.hypot(*np.diff(l, axis=0).T).sum() >= min_length]


# ---------------------------------------------------------------------- svg


def _clip_to_box(line: np.ndarray, w: float, h: float) -> Lines:
    """Cut a polyline at the area edges (Liang–Barsky per segment)."""
    out: Lines = []
    cur: list[np.ndarray] = []
    for a, b in zip(line[:-1], line[1:]):
        d = b - a
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-d[0], a[0]), (d[0], w - a[0]), (-d[1], a[1]), (d[1], h - a[1])):
            if abs(p) < 1e-12:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not ok or t0 > t1:
            if len(cur) > 1:
                out.append(np.vstack(cur))
            cur = []
            continue
        p0, p1 = a + t0 * d, a + t1 * d
        if cur and np.allclose(cur[-1], p0):
            cur.append(p1)
        else:
            if len(cur) > 1:
                out.append(np.vstack(cur))
            cur = [p0, p1]
        if t1 < 1.0:
            out.append(np.vstack(cur))
            cur = []
    if len(cur) > 1:
        out.append(np.vstack(cur))
    return out


@register(
    "svg",
    "SVG paths",
    "Draws the SVG's own lines. Filled shapes are outlined and hatch-filled.",
    [
        Param("fit", "Size", "choice", "fit", choices=["fit", "actual size"],
              help="Fit scales the art to the drawing area"),
        Param("margin", "Margin", "float", 0.0, 0, 30, 0.5, help="mm inside the drawing area"),
        Param("strokes", "Draw stroked lines", "bool", True),
        Param("outline_fills", "Outline filled shapes", "bool", True),
        Param("hatch_fills", "Hatch-fill filled shapes", "bool", True),
        Param("hatch_spacing", "Hatch spacing", "float", 0.8, 0.2, 10, 0.05, help="mm between hatch lines"),
        Param("hatch_angle", "Hatch angle", "float", 45.0, -90, 90, 5, help="degrees"),
        Param("cross_hatch", "Cross-hatch", "bool", False),
    ],
    source="svg",
)
def svg_paths(tone: Tone, ctx: Context, fit, margin, strokes, outline_fills, hatch_fills, hatch_spacing,
              hatch_angle, cross_hatch) -> Lines:
    art = ctx.svg
    if art is None:
        return []
    w, h = tone.w, tone.h
    x0, y0, x1, y1 = art.bbox()
    bw, bh = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    if fit == "fit":
        s = min((w - 2 * margin) / bw, (h - 2 * margin) / bh)
    else:
        s = PX_TO_MM
    s = max(s, 1e-9)
    bx, by = (x0 + x1) / 2, (y0 + y1) / 2

    def place(pts: np.ndarray) -> np.ndarray:  # SVG px (y down) → area mm (y up), centered
        return np.column_stack([w / 2 + (pts[:, 0] - bx) * s, h / 2 - (pts[:, 1] - by) * s])

    out: Lines = []
    if strokes:
        out += [place(l) for l in art.strokes]

    if art.fills and (outline_fills or hatch_fills):
        ppm = float(min(10.0, np.sqrt(4e6 / (w * h))))
        shape = (max(2, int(round(h * ppm))), max(2, int(round(w * ppm))))
        mask = np.zeros(shape, bool)
        for f in art.fills:
            rings = [place(r) for r in f.rings]
            m = vz.rasterize_rings(rings, shape, w, h)
            if f.dark:
                mask |= m
                if outline_fills:
                    out += rings
            else:
                # A light fill erases what's under it; outline it only if it actually covers dark art,
                # so a white page background doesn't become a border.
                if outline_fills and m.any() and (m & mask).sum() > 0.05 * m.sum():
                    out += rings
                mask &= ~m
        if hatch_fills:
            out += vz.hatch(mask, w, h, hatch_spacing, hatch_angle, min_len=ctx.pen_width)
            if cross_hatch:
                out += vz.hatch(mask, w, h, hatch_spacing, hatch_angle + 90, min_len=ctx.pen_width)

    clipped: Lines = []
    for l in out:
        if len(l) < 2:
            continue
        if l[:, 0].min() < 0 or l[:, 1].min() < 0 or l[:, 0].max() > w or l[:, 1].max() > h:
            clipped += _clip_to_box(l, w, h)
        else:
            clipped.append(l)
    return clipped
