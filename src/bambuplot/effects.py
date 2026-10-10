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
    unit: str = ""  # shown next to the slider value: "mm", "°", "s", "×" …
    when: dict | None = None  # only shown while other params have these values


@dataclass
class Context:
    pen_width: float = 0.4
    seed: int = 0
    svg: Any = None  # SvgArt for effects with source="svg"


# Menu groups, in the order the app lists them.
GROUPS = ("Trace to vector lines", "Single continuous line", "Hatching & engraving", "Dots", "Vector file")


@dataclass
class Effect:
    name: str
    label: str
    description: str
    params: list[Param]
    fn: Callable[..., Lines] = field(repr=False)
    source: str = "raster"  # "raster" (photos, the sample) or "svg"
    resolution: float | None = None  # px/mm this effect wants from the tone field, at least
    group: str = "Single continuous line"

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
                if not np.isfinite(v):
                    v = p.default
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
            "group": self.group,
            "params": [asdict(p) for p in self.params],
        }


EFFECTS: dict[str, Effect] = {}


def register(name: str, label: str, description: str, params: list[Param],
             source: str = "raster", resolution: float | None = None, group: str = "Single continuous line"):
    def deco(fn):
        EFFECTS[name] = Effect(name, label, description, params, fn, source, resolution, group)
        return fn

    return deco


def run(name: str, tone: Tone, params: dict | None = None, ctx: Context | None = None) -> Lines:
    if name not in EFFECTS:
        raise KeyError(f"Unknown effect '{name}'. Available: {', '.join(EFFECTS)}")
    eff = EFFECTS[name]
    return eff.fn(tone, ctx or Context(), **eff.coerce(params))


def _runs_inside(pts: np.ndarray, w: float, h: float, min_pts: int = 2) -> Lines:
    """Split a polyline wherever it leaves the w × h area."""
    inside = (pts[:, 0] >= 0) & (pts[:, 0] <= w) & (pts[:, 1] >= 0) & (pts[:, 1] <= h)
    edges = np.diff(np.concatenate([[0], inside.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [pts[s:e] for s, e in zip(starts, ends) if e - s >= min_pts]


# --------------------------------------------------------------------------- waves


@register(
    "waves",
    "Wave lines",
    "Parallel lines whose waves grow taller and tighter in dark areas.",
    [
        Param("spacing", "Line spacing", "float", 2.5, 0.8, 10, 0.1, unit="mm"),
        Param("angle", "Line angle", "float", 0.0, -90, 90, 1, unit="°"),
        Param("amplitude", "Wave height", "float", 0.46, 0.1, 1.0, 0.02, unit="×",
              help="× line spacing. Above 0.5, neighboring lines overlap to reach full black."),
        Param("min_wavelength", "Shortest wave", "float", 1.4, 0.5, 10, 0.1, unit="mm", help="In the darkest areas"),
        Param("max_wavelength", "Longest wave", "float", 7.0, 1, 30, 0.5, unit="mm", help="In the lightest areas"),
        Param("continuous", "One continuous line", "bool", True, help="Join the lines at the edges so the pen never lifts"),
    ],
)
def waves(tone: Tone, ctx: Context, spacing, angle, amplitude, min_wavelength, max_wavelength, continuous) -> Lines:
    w, h = tone.w, tone.h
    a = np.radians(angle)
    d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
    r = np.hypot(w, h) / 2
    t = np.arange(-r, r, 0.3)
    amp_max = spacing * amplitude
    rows: Lines = []
    for i, v in enumerate(np.arange(r - spacing / 2, -r, -spacing)):  # back of the area first
        base = np.array([w / 2, h / 2]) + t[:, None] * d + v * n
        dark = tone.darkness(base[:, 0], base[:, 1])
        wl = max_wavelength - (max_wavelength - min_wavelength) * dark
        phase = np.cumsum(2 * np.pi * 0.3 / wl)
        pts = base + (dark * amp_max * np.sin(phase))[:, None] * n
        runs = _runs_inside(pts, w, h)
        if i % 2:
            runs = [rr[::-1] for rr in runs[::-1]]
        rows += runs
    if continuous and rows:
        # Consecutive rows end and start on the area's edge; the area is convex, so the
        # joining stroke stays inside it.
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


def _square(cx, cy, r, rot, pitch, fill) -> np.ndarray:
    """Square of half-size r, optionally filled with an inward square spiral."""
    c, s = np.cos(rot), np.sin(rot)
    pts = []
    k = r
    while k > 0:
        pts += [(-k, -k), (k, -k), (k, k), (-k, k), (-k, -k + pitch if fill and k - pitch > 0 else -k)]
        if not fill:
            break
        k -= pitch
    P = np.array(pts, float)
    return np.column_stack([cx + P[:, 0] * c - P[:, 1] * s, cy + P[:, 0] * s + P[:, 1] * c])


@register(
    "halftone",
    "Halftone",
    "A rotated grid of dots, squares or dashes sized by darkness, like newspaper print.",
    [
        Param("shape", "Dot shape", "choice", "circle", choices=["circle", "square", "line", "cross"]),
        Param("cell", "Grid size", "float", 3.0, 1.0, 15, 0.1, unit="mm", help="Distance between dot centers"),
        Param("angle", "Grid angle", "float", 45.0, 0, 90, 1, unit="°"),
        Param("max_size", "Largest dot", "float", 0.95, 0.2, 1.4, 0.05, unit="×", help="× grid size"),
        Param("min_radius", "Smallest dot", "float", 0.25, 0.05, 3, 0.05, unit="mm", help="Lighter dots are skipped"),
        Param("fill", "Fill shapes with ink", "bool", True, when={"shape": ["circle", "square"]}),
    ],
    group="Dots",
)
def halftone(tone: Tone, ctx: Context, shape, cell, angle, max_size, min_radius, fill) -> Lines:
    w, h = tone.w, tone.h
    half = np.hypot(w, h) / 2 + cell
    k = np.arange(-half, half + cell, cell)
    gu, gv = np.meshgrid(k, k)
    th = np.radians(angle)
    xs = (w / 2 + gu * np.cos(th) - gv * np.sin(th)).ravel()
    ys = (h / 2 + gu * np.sin(th) + gv * np.cos(th)).ravel()
    inside = (xs >= 0) & (xs <= w) & (ys >= 0) & (ys <= h)
    xs, ys = xs[inside], ys[inside]
    dark = tone.darkness(xs, ys)
    # Area ∝ darkness for dots and squares; length ∝ darkness for lines and crosses.
    size = (np.sqrt(dark) if shape in ("circle", "square") else dark) * cell / 2 * max_size
    reach = size * (np.sqrt(2) if shape == "square" else 1)
    size = np.where(reach > np.minimum.reduce([xs, w - xs, ys, h - ys]),
                    size * np.minimum.reduce([xs, w - xs, ys, h - ys]) / np.maximum(reach, 1e-9), size)
    keep = size >= min_radius
    pitch = ctx.pen_width * 0.9
    d = np.array([np.cos(th), np.sin(th)])
    nrm = np.array([-np.sin(th), np.cos(th)])
    out: Lines = []
    for x, y, r in zip(xs[keep], ys[keep], size[keep]):
        c = np.array([x, y])
        if shape == "circle":
            out.append(_filled_dot(x, y, r, pitch) if fill else _circle(x, y, r))
        elif shape == "square":
            out.append(_square(x, y, r, th, pitch, fill))
        elif shape == "line":
            out.append(np.vstack([c - r * d, c + r * d]))
        else:  # cross
            out.append(np.vstack([c - r * d, c + r * d]))
            out.append(np.vstack([c - r * nrm, c + r * nrm]))
    return out


# -------------------------------------------------------------------------- spiral


@register(
    "spiral",
    "Spiral",
    "One continuous spiral, wobbling more in dark areas.",
    [
        Param("spacing", "Ring spacing", "float", 2.0, 0.8, 8, 0.1, unit="mm"),
        Param("amplitude", "Wobble height", "float", 0.46, 0.1, 1.0, 0.02, unit="×",
              help="× ring spacing. Above 0.5, rings overlap to reach full black."),
        Param("min_wavelength", "Shortest wobble", "float", 1.2, 0.5, 10, 0.1, unit="mm", help="In the darkest areas"),
        Param("max_wavelength", "Longest wobble", "float", 6.0, 1, 30, 0.5, unit="mm", help="In the lightest areas"),
        Param("center_x", "Center left/right", "float", 0.0, -0.5, 0.5, 0.01, unit="×", help="× area width"),
        Param("center_y", "Center front/back", "float", 0.0, -0.5, 0.5, 0.01, unit="×", help="× area height"),
        Param("fill_corners", "Reach the corners", "bool", False, help="Keep spiralling to fill the whole area"),
    ],
)
def spiral(tone: Tone, ctx: Context, spacing, amplitude, min_wavelength, max_wavelength, center_x, center_y,
           fill_corners) -> Lines:
    w, h = tone.w, tone.h
    amp_max = spacing * amplitude
    cx, cy = w / 2 + center_x * w, h / 2 + center_y * h
    if fill_corners:
        r_max = max(np.hypot(px - cx, py - cy) for px in (0, w) for py in (0, h))
    else:
        r_max = min(cx, w - cx, cy, h - cy) - amp_max
    if r_max <= spacing:
        return []
    step = 0.3
    total = np.pi * r_max**2 / spacing  # arc length of an Archimedean spiral out to r_max
    s = np.arange(0, total, step)
    theta = np.sqrt(4 * np.pi * s / spacing)
    r = spacing * theta / (2 * np.pi)
    bx, by = cx + r * np.cos(theta), cy + r * np.sin(theta)
    dark = tone.darkness(bx, by)
    wl = max_wavelength - (max_wavelength - min_wavelength) * dark
    phase = np.cumsum(2 * np.pi * step / wl)
    rr = r + dark * amp_max * np.sin(phase)
    pts = np.column_stack([cx + rr * np.cos(theta), cy + rr * np.sin(theta)])
    return _runs_inside(pts, w, h) if fill_corners else [pts]


# ------------------------------------------------------------------------- stipple


@register(
    "stipple",
    "Stipple",
    "Dots spread by darkness, then joined into one line (TSP art) or drawn as dots.",
    [
        Param("points", "Number of points", "int", 3000, 100, 30000, 100),
        Param("density_contrast", "Density contrast", "float", 1.5, 0.5, 4, 0.1, unit="×",
              help="Higher puts more dots in the shadows and fewer in the light"),
        Param("iterations", "Relaxation passes", "int", 10, 0, 40, 1, help="More passes spread dots more evenly"),
        Param("mode", "Draw as", "choice", "line", choices=["line", "dots"]),
        Param("dot_size", "Dot size", "float", 0.0, 0, 2, 0.05, unit="mm",
              help="0 = a single pen tap", when={"mode": ["dots"]}),
        Param("optimize_seconds", "Path optimizing time", "float", 3.0, 0, 30, 0.5, unit="s",
              help="Time spent shortening the route", when={"mode": ["line"]}),
    ],
    group="Dots",
)
def stipple(tone: Tone, ctx: Context, points, density_contrast, iterations, mode, dot_size, optimize_seconds) -> Lines:
    px_per_mm = min(4.0, max(1.0, np.sqrt(points * 40 / (tone.w * tone.h))))
    gx, gy, dark = tone.grid(px_per_mm)
    weight = dark**density_contrast + 1e-4
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
    if mode == "line" and optimize_seconds > 0:
        order = two_opt(pts, order, optimize_seconds)
    pts = pts[order]
    if mode == "dots":
        if dot_size <= 0:
            return [p[None, :] for p in pts]
        r = np.minimum.reduce([np.full(len(pts), dot_size / 2), pts[:, 0], tone.w - pts[:, 0],
                               pts[:, 1], tone.h - pts[:, 1]])
        return [_filled_dot(x, y, rad, ctx.pen_width * 0.9) if rad > ctx.pen_width / 2 else np.array([[x, y]])
                for (x, y), rad in zip(pts, r)]
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
        Param("auto_threshold", "Automatic threshold", "bool", True, when={"levels": [1]}),
        Param("threshold", "Threshold", "float", 0.5, 0.05, 0.95, 0.05, help="Brightness cut-off, 0–1",
              when={"auto_threshold": [False], "levels": [1]}),
        Param("smoothing", "Smoothing", "float", 0.3, 0, 3, 0.05, unit="mm", help="Blur before tracing"),
        Param("min_area", "Ignore specks under", "float", 1.0, 0, 50, 0.5, unit="mm²"),
        Param("outline", "Draw outlines", "bool", True),
        Param("fill", "Hatch-fill dark areas", "bool", True),
        Param("hatch_spacing", "Hatch spacing", "float", 0.8, 0.2, 10, 0.05, unit="mm", when={"fill": [True]}),
        Param("hatch_angle", "Hatch angle", "float", 45.0, -90, 90, 5, unit="°", when={"fill": [True]}),
    ],
    resolution=8.0,
    group="Trace to vector lines",
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
        Param("threshold", "Threshold", "float", 0.5, 0.05, 0.95, 0.05, help="Brightness cut-off, 0–1",
              when={"auto_threshold": [False]}),
        Param("smoothing", "Smoothing", "float", 0.15, 0, 2, 0.05, unit="mm", help="Blur before tracing"),
        Param("close_gaps", "Close gaps", "float", 0.0, 0, 2, 0.1, unit="mm", help="Joins broken lines"),
        Param("min_length", "Drop strokes under", "float", 1.5, 0, 20, 0.5, unit="mm"),
    ],
    resolution=8.0,
    group="Trace to vector lines",
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
        Param("smoothing", "Smoothing", "float", 0.6, 0, 5, 0.1, unit="mm", help="Blur before tracing"),
        Param("min_length", "Drop lines under", "float", 2.0, 0, 50, 0.5, unit="mm"),
    ],
    resolution=4.0,
    group="Trace to vector lines",
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
        Param("margin", "Margin", "float", 0.0, 0, 30, 0.5, unit="mm", help="Inside the drawing area", when={"fit": ["fit"]}),
        Param("strokes", "Draw stroked lines", "bool", True),
        Param("outline_fills", "Outline filled shapes", "bool", True),
        Param("hatch_fills", "Hatch-fill filled shapes", "bool", True),
        Param("hatch_spacing", "Hatch spacing", "float", 0.8, 0.2, 10, 0.05, unit="mm", when={"hatch_fills": [True]}),
        Param("hatch_angle", "Hatch angle", "float", 45.0, -90, 90, 5, unit="°", when={"hatch_fills": [True]}),
        Param("cross_hatch", "Cross-hatch", "bool", False, when={"hatch_fills": [True]}),
    ],
    source="svg",
    group="Vector file",
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


# ---------------------------------------------------------------- cross-hatch


@register(
    "crosshatch",
    "Cross-hatch",
    "Classic pen-and-ink shading: each darker tone adds another layer of hatching at a new angle.",
    [
        Param("layers", "Layers", "int", 4, 1, 4, 1, help="How many tones get their own hatch direction"),
        Param("spacing", "Line spacing", "float", 1.2, 0.3, 6, 0.05, unit="mm"),
        Param("angle", "First angle", "float", 45.0, -90, 90, 5, unit="°"),
        Param("lightest", "Lightest tone hatched", "float", 0.85, 0.3, 1.0, 0.01, unit="",
              help="Brightness below which the first layer starts, 0–1"),
        Param("smoothing", "Smoothing", "float", 0.4, 0, 3, 0.05, unit="mm", help="Blur before cutting tones"),
        Param("min_length", "Drop strokes under", "float", 0.8, 0, 10, 0.1, unit="mm"),
    ],
    resolution=6.0,
    group="Hatching & engraving",
)
def crosshatch(tone: Tone, ctx: Context, layers, spacing, angle, lightest, smoothing, min_length) -> Lines:
    ppm = tone.data.shape[1] / tone.w
    gray = vz.smooth(tone.data, smoothing * ppm)
    out: Lines = []
    for k in range(layers):
        cut = lightest * (layers - k) / layers  # e.g. 4 layers: 0.85, 0.64, 0.43, 0.21
        mask = vz.clean_mask(gray < cut, (ctx.pen_width * ppm) ** 2)
        out += vz.hatch(mask, tone.w, tone.h, spacing, angle + HATCH_ANGLES[k], max(min_length, ctx.pen_width))
    return out


# ----------------------------------------------------------------- flow lines


@register(
    "flow",
    "Flow lines",
    "Engraving-style lines that follow the shapes in the picture, packed closer in the shadows.",
    [
        Param("direction", "Lines run", "choice", "along edges", choices=["along edges", "across edges"]),
        Param("min_spacing", "Spacing in shadows", "float", 0.7, 0.3, 3, 0.05, unit="mm"),
        Param("max_spacing", "Spacing in highlights", "float", 3.5, 1, 10, 0.1, unit="mm"),
        Param("smoothing", "Flow smoothness", "float", 1.5, 0.3, 6, 0.1, unit="mm",
              help="Higher gives calmer, longer curves"),
        Param("paper_white", "Leave light areas blank", "float", 0.08, 0, 0.5, 0.01, unit="",
              help="Darkness below this gets no lines, 0–1"),
        Param("fallback_angle", "Angle in flat areas", "float", 30.0, -90, 90, 5, unit="°"),
        Param("min_length", "Drop lines under", "float", 2.0, 0, 20, 0.5, unit="mm"),
    ],
    resolution=4.0,
    group="Hatching & engraving",
)
def flow(tone: Tone, ctx: Context, direction, min_spacing, max_spacing, smoothing, paper_white, fallback_angle,
         min_length) -> Lines:
    w, h = tone.w, tone.h
    rows, cols = tone.data.shape
    ppm = cols / w
    gray = vz.smooth(tone.data, 0.5 * smoothing * ppm)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = -cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)  # rows grow toward the front, so flip to +Y back
    # Structure tensor: a stable edge direction that doesn't flip sign between pixels.
    sig = smoothing * ppm
    j11, j22, j12 = (vz.smooth(v, sig) for v in (gx * gx, gy * gy, gx * gy))
    grad_angle = 0.5 * np.arctan2(2 * j12, j11 - j22)
    coherence = np.sqrt((j11 - j22) ** 2 + 4 * j12**2) / (j11 + j22 + 1e-6)
    flow_angle = grad_angle + (np.pi / 2 if direction == "along edges" else 0.0)
    # Blend toward the fallback angle where the picture has no clear direction.
    fb = np.radians(fallback_angle)
    wgt = np.clip(coherence * 3, 0, 1)
    vx = wgt * np.cos(2 * flow_angle) + (1 - wgt) * np.cos(2 * fb)
    vy = wgt * np.sin(2 * flow_angle) + (1 - wgt) * np.sin(2 * fb)
    field_angle = 0.5 * np.arctan2(vy, vx)
    fxs, fys = np.cos(field_angle), np.sin(field_angle)

    def at(arr, x, y):
        c = min(cols - 1, max(0, int(x * ppm)))
        r = min(rows - 1, max(0, int((h - y) * ppm)))
        return arr[r, c]

    dark = 1.0 - tone.data
    def sep(x, y):
        return max_spacing - (max_spacing - min_spacing) * at(dark, x, y)

    # Occupancy grid: cells within half the local spacing of a finished line are taken.
    res = max(0.1, min_spacing / 4)
    gw, gh = int(np.ceil(w / res)) + 1, int(np.ceil(h / res)) + 1
    occ = np.zeros((gh, gw), bool)

    def free(x, y):
        return not occ[int(y / res), int(x / res)]

    def stamp(line):
        for x, y in line[::2]:
            rad = 0.5 * sep(x, y)
            i0, i1 = int((y - rad) / res), int((y + rad) / res) + 1
            j0, j1 = int((x - rad) / res), int((x + rad) / res) + 1
            i0, j0 = max(i0, 0), max(j0, 0)
            sub = occ[i0:i1, j0:j1]
            if sub.size:
                ii, jj = np.ogrid[i0:i0 + sub.shape[0], j0:j0 + sub.shape[1]]
                sub |= ((ii * res - y) ** 2 + (jj * res - x) ** 2) <= rad * rad

    step = 0.3

    def trace_dir(x, y, sign):
        pts = []
        px, py = fxs[min(rows - 1, int((h - y) * ppm)), min(cols - 1, int(x * ppm))] * sign, \
            fys[min(rows - 1, int((h - y) * ppm)), min(cols - 1, int(x * ppm))] * sign
        for _ in range(2000):
            dx, dy = at(fxs, x, y), at(fys, x, y)
            if dx * px + dy * py < 0:  # keep heading the same way
                dx, dy = -dx, -dy
            x, y = x + step * dx, y + step * dy
            if not (0 <= x <= w and 0 <= y <= h) or at(dark, x, y) < paper_white or not free(x, y):
                break
            pts.append((x, y))
            px, py = dx, dy
        return pts

    # Seeds: a jittered grid, darkest first so shadows get their lines before highlights crowd them.
    rng = np.random.default_rng(ctx.seed)
    g = min_spacing
    sx, sy = np.meshgrid(np.arange(g / 2, w, g), np.arange(g / 2, h, g))
    seeds = np.column_stack([sx.ravel(), sy.ravel()]) + rng.uniform(-g / 3, g / 3, (sx.size, 2))
    seeds = seeds[(seeds[:, 0] > 0) & (seeds[:, 0] < w) & (seeds[:, 1] > 0) & (seeds[:, 1] < h)]
    sd = np.array([at(dark, x, y) for x, y in seeds])
    seeds = seeds[np.argsort(-sd + rng.uniform(0, 0.05, len(sd)))]

    out: Lines = []
    for x, y in seeds:
        if at(dark, x, y) < paper_white or not free(x, y):
            continue
        back = trace_dir(x, y, -1)[::-1]
        fwd = trace_dir(x, y, 1)
        line = np.array(back + [(x, y)] + fwd)
        if len(line) > 1 and np.hypot(*np.diff(line, axis=0).T).sum() >= min_length:
            out.append(line)
            stamp(line)
    return out


# ---------------------------------------------------------------- dither dots


@register(
    "dither",
    "Dither dots",
    "Error-diffusion dithering on a fine grid: each grid cell is either a dot or blank, like a newspaper photo up close.",
    [
        Param("cell", "Grid size", "float", 0.8, 0.3, 4, 0.05, unit="mm", help="At least your pen width"),
        Param("method", "Method", "choice", "Floyd–Steinberg", choices=["Floyd–Steinberg", "Atkinson", "ordered"]),
        Param("dot_size", "Dot size", "float", 0.0, 0, 3, 0.05, unit="mm", help="0 = a single pen tap"),
        Param("serpentine", "Alternate row direction", "bool", True, help="Reduces diagonal artifacts"),
    ],
    resolution=4.0,
    group="Dots",
)
def dither(tone: Tone, ctx: Context, cell, method, dot_size, serpentine) -> Lines:
    w, h = tone.w, tone.h
    nx, ny = max(1, int(w / cell)), max(1, int(h / cell))
    small = cv2.resize(tone.data, (nx, ny), interpolation=cv2.INTER_AREA).astype(np.float64)
    d = 1.0 - small  # 1 = ink
    on = np.zeros_like(d, bool)
    if method == "ordered":
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16 + 1 / 32
        on = d > bayer[np.arange(ny)[:, None] % 4, np.arange(nx)[None, :] % 4]
    else:
        if method == "Atkinson":
            kern = [(0, 1, 1 / 8), (0, 2, 1 / 8), (1, -1, 1 / 8), (1, 0, 1 / 8), (1, 1, 1 / 8), (2, 0, 1 / 8)]
        else:
            kern = [(0, 1, 7 / 16), (1, -1, 3 / 16), (1, 0, 5 / 16), (1, 1, 1 / 16)]
        for r in range(ny):
            flip = serpentine and r % 2
            for c in (range(nx - 1, -1, -1) if flip else range(nx)):
                v = d[r, c]
                on[r, c] = v >= 0.5
                err = v - (1.0 if on[r, c] else 0.0)
                for dr, dc, f in kern:
                    cc = c - dc if flip else c + dc
                    if r + dr < ny and 0 <= cc < nx:
                        d[r + dr, cc] += err * f
    xs = (np.arange(nx) + 0.5) * w / nx
    ys = h - (np.arange(ny) + 0.5) * h / ny
    out: Lines = []
    rad = min(dot_size / 2, cell / 2)
    for r in range(ny):
        cols = np.flatnonzero(on[r])
        if r % 2:
            cols = cols[::-1]
        for c in cols:
            x, y = xs[c], ys[r]
            out.append(_filled_dot(x, y, rad, ctx.pen_width * 0.9) if rad > ctx.pen_width / 2 else np.array([[x, y]]))
    return out


# ----------------------------------------------------------------- squiggle


def _hilbert(order: int) -> np.ndarray:
    """Hilbert curve visiting every cell of a 2^order square grid, as (x, y) cell indices."""
    n = 1 << order
    d = np.arange(n * n)
    x = np.zeros_like(d)
    y = np.zeros_like(d)
    t = d.copy()
    s = 1
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        swap = ry == 0
        flip = swap & (rx == 1)
        x = np.where(flip, s - 1 - x, x)
        y = np.where(flip, s - 1 - y, y)
        x, y = np.where(swap, y, x), np.where(swap, x, y)
        x = x + s * rx
        y = y + s * ry
        t //= 4
        s *= 2
    return np.column_stack([x, y])


@register(
    "squiggle",
    "Hilbert squiggle",
    "One unbroken space-filling line that wiggles harder in dark areas.",
    [
        Param("cell", "Path spacing", "float", 2.0, 0.6, 8, 0.1, unit="mm"),
        Param("amplitude", "Wiggle height", "float", 0.45, 0.05, 1.0, 0.05, unit="×", help="× path spacing"),
        Param("wavelength", "Wiggle length", "float", 0.8, 0.3, 4, 0.05, unit="mm", help="In the darkest areas"),
        Param("rounded", "Round the corners", "bool", True),
    ],
)
def squiggle(tone: Tone, ctx: Context, cell, amplitude, wavelength, rounded) -> Lines:
    w, h = tone.w, tone.h
    order = int(np.ceil(np.log2(max(w, h) / cell)))
    side = (1 << order) * cell
    pts = (_hilbert(order) + 0.5) * cell - (side - np.array([w, h])) / 2  # center the square on the area
    keep = (pts[:, 0] > 0) & (pts[:, 0] < w) & (pts[:, 1] > 0) & (pts[:, 1] < h)
    out: Lines = []
    edges = np.diff(np.concatenate([[0], keep.astype(np.int8), [0]]))
    for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        p = pts[s:e]
        if len(p) < 2:
            continue
        if rounded:  # Chaikin corner cutting, twice
            for _ in range(2):
                q = np.empty((2 * len(p) - 2, 2))
                q[0::2] = 0.75 * p[:-1] + 0.25 * p[1:]
                q[1::2] = 0.25 * p[:-1] + 0.75 * p[1:]
                p = np.vstack([p[:1], q, p[-1:]])
        seg = np.hypot(*np.diff(p, axis=0).T)
        cum = np.concatenate([[0], np.cumsum(seg)])
        t = np.arange(0, cum[-1], 0.2)
        base = np.column_stack([np.interp(t, cum, p[:, 0]), np.interp(t, cum, p[:, 1])])
        tang = np.gradient(base, axis=0)
        tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]
        nrm = np.column_stack([-tang[:, 1], tang[:, 0]])
        dark = tone.darkness(base[:, 0], base[:, 1])
        wl = wavelength + (6 * wavelength) * (1 - dark)
        phase = np.cumsum(2 * np.pi * 0.2 / wl)
        line = base + (dark * amplitude * cell / 2 * np.sin(phase))[:, None] * nrm
        out += _runs_inside(line, w, h)
    return out
