"""More effects: scribbles, relief lines, mazes, tiles, engraving styles and text.

Each one registers itself in ``effects.EFFECTS`` like the effects in
``effects.py``, and returns polylines in area millimeters (origin at the
drawing area's front-left corner, +Y toward the back).
"""

from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np
from scipy.spatial import Delaunay, Voronoi, cKDTree
from skimage.measure import find_contours

from . import vectorize as vz
from .effects import Context, Lines, Param, _circle, _pack, _runs_inside, _wiggle_path, register
from .image import Tone
from .tsp import nn_tour, two_opt

LINE, LINES, HATCH, DOTS, TILES = (
    "Single continuous line", "Lines & relief", "Hatching & engraving", "Dots", "Patterns & tiles")


# ---------------------------------------------------------------------------- helpers


def _grid(tone: Tone, ppm: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cell centers and darkness on a regular grid. Row 0 is the FRONT of the area (smallest Y)."""
    nx, ny = max(2, int(round(tone.w * ppm))), max(2, int(round(tone.h * ppm)))
    xs, ys = (np.arange(nx) + 0.5) * tone.w / nx, (np.arange(ny) + 0.5) * tone.h / ny
    gx, gy = np.meshgrid(xs, ys)
    return xs, ys, tone.darkness(gx, gy)


def _rc_to_mm(rc: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """(row, col) on a ``_grid`` → mm."""
    sx, sy = xs[1] - xs[0], ys[1] - ys[0]
    return np.column_stack([xs[0] + rc[:, 1] * sx, ys[0] + rc[:, 0] * sy])


def _density_points(tone: Tone, n: int, contrast: float, seed: int, relax: int = 6) -> np.ndarray:
    """``n`` points spread by darkness, evened out with a few weighted Lloyd passes."""
    ppm = min(4.0, max(1.0, np.sqrt(n * 30 / (tone.w * tone.h))))
    xs, ys, dark = _grid(tone, ppm)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy, wgt = gx.ravel(), gy.ravel(), dark.ravel() ** contrast + 1e-4
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(wgt), size=n, p=wgt / wgt.sum())
    cell = 1 / ppm
    pts = np.column_stack([gx[idx], gy[idx]]) + rng.uniform(-cell / 2, cell / 2, (n, 2))
    pix = np.column_stack([gx, gy])
    for _ in range(relax):
        _, owner = cKDTree(pts).query(pix)
        ws = np.bincount(owner, wgt, n)
        ok = ws > 1e-9
        pts[ok, 0] = np.bincount(owner, wgt * gx, n)[ok] / ws[ok]
        pts[ok, 1] = np.bincount(owner, wgt * gy, n)[ok] / ws[ok]
    pts[:, 0] = np.clip(pts[:, 0], 0, tone.w)
    pts[:, 1] = np.clip(pts[:, 1], 0, tone.h)
    return pts


def _catmull_rom(p: np.ndarray, step: float = 0.25) -> np.ndarray:
    """A smooth curve through every point."""
    if len(p) < 3:
        return p
    q = np.vstack([p[:1], p, p[-1:]])
    out = [p[:1]]
    for i in range(1, len(q) - 2):
        p0, p1, p2, p3 = q[i - 1], q[i], q[i + 1], q[i + 2]
        n = max(2, int(np.hypot(*(p2 - p1)) / step))
        t = np.linspace(0, 1, n + 1)[1:, None]
        out.append(0.5 * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t ** 2
                          + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    return np.vstack(out)


def _clip_convex(p0: np.ndarray, d: np.ndarray, poly: np.ndarray) -> np.ndarray | None:
    """The part of the infinite line p0 + t·d inside a convex polygon, or None."""
    t0, t1 = -np.inf, np.inf
    n = len(poly)
    area = sum(poly[i, 0] * poly[(i + 1) % n, 1] - poly[(i + 1) % n, 0] * poly[i, 1] for i in range(n))
    sgn = 1 if area > 0 else -1
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        e = b - a
        nrm = sgn * np.array([-e[1], e[0]])  # inward normal
        den = nrm @ d
        num = nrm @ (a - p0)
        if abs(den) < 1e-12:
            if num > 0:
                return None
            continue
        t = num / den
        if den > 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
    if t1 - t0 < 1e-6:
        return None
    return np.vstack([p0 + t0 * d, p0 + t1 * d])


def _hatch_poly(poly: np.ndarray, spacing: float, angle: float, flip: bool = False) -> Lines:
    """Parallel lines filling a convex polygon, alternating direction."""
    a = np.radians(angle)
    d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
    proj = poly @ n
    out: Lines = []
    for k, off in enumerate(np.arange(proj.min() + spacing / 2, proj.max(), spacing)):
        seg = _clip_convex(off * n, d, poly)
        if seg is not None:
            out.append(seg[::-1] if (k % 2) ^ flip else seg)
    return out


def _cut_dashes(line: np.ndarray, share, period: float, rng, min_on: float = 0.4) -> Lines:
    """Break a line into dashes; ``share(x, y)`` gives each dash's share of the period (0–1)."""
    seg = np.hypot(*np.diff(line, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])
    out: Lines = []
    s = rng.uniform(0, period)
    while s < cum[-1]:
        p = np.array([np.interp(s, cum, line[:, 0]), np.interp(s, cum, line[:, 1])])
        on = period * float(share(p[0], p[1]))
        if on >= min_on:
            t = np.linspace(s, min(s + on, cum[-1]), max(2, int(on / 0.3) + 1))
            out.append(np.column_stack([np.interp(t, cum, line[:, 0]), np.interp(t, cum, line[:, 1])]))
        s += period * rng.uniform(0.85, 1.15)
    return out


def _rotate(pts: np.ndarray, deg: float, about=(0.0, 0.0)) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    q = pts - about
    return np.column_stack([q[:, 0] * c - q[:, 1] * s, q[:, 0] * s + q[:, 1] * c]) + about


# =========================================================================== single line


@register(
    "scribble",
    "Scribble",
    "One loose, looping pencil line that wanders the whole picture and piles up in the shadows.",
    [
        Param("points", "Detail", "int", 2500, 300, 15000, 100, help="Points the line loops around"),
        Param("contrast", "Density contrast", "float", 1.8, 0.5, 4, 0.1, unit="×"),
        Param("curl", "Loopiness", "float", 0.7, 0, 1.5, 0.05, unit="×", help="0 = smooth curves, higher = loops"),
        Param("optimize_seconds", "Path optimizing time", "float", 2.0, 0, 20, 0.5, unit="s"),
    ],
    group=LINE,
)
def scribble(tone: Tone, ctx: Context, points, contrast, curl, optimize_seconds) -> Lines:
    pts = _density_points(tone, points, contrast, ctx.seed, relax=4)
    order = nn_tour(pts, int(np.argmin(pts[:, 0] + pts[:, 1])))
    if optimize_seconds > 0:
        order = two_opt(pts, order, optimize_seconds)
    pts = pts[order]
    rng = np.random.default_rng(ctx.seed + 1)
    if curl > 0:  # swing around each point on a small arc, so the spline loops
        gap = np.hypot(*np.diff(pts, axis=0).T)
        r = curl * 0.45 * np.minimum(np.concatenate([gap, gap[-1:]]), np.concatenate([gap[:1], gap]))
        a0 = rng.uniform(0, 2 * np.pi, len(pts))
        turn = rng.choice([-1, 1], len(pts))
        ring = []
        for k in range(3):
            ang = a0 + turn * k * 2.1
            ring.append(pts + np.column_stack([r * np.cos(ang), r * np.sin(ang)]))
        pts = np.stack(ring, axis=1).reshape(-1, 2)
    line = _catmull_rom(pts)
    line[:, 0] = np.clip(line[:, 0], 0, tone.w)
    line[:, 1] = np.clip(line[:, 1], 0, tone.h)
    return [line]


# --- space-filling curves

_LSYS = {
    "peano": ("X", {"X": "XFYFX+F+YFXFY-F-XFYFX", "Y": "YFXFY-F-XFYFX+F+YFXFY"}, 90, "F"),
    "moore": ("LFL+F+LFL", {"L": "-RF+LFL+FR-", "R": "+LF-RFR-FL+"}, 90, "F"),
    "gosper": ("FX", {"X": "X+YF++YF-FX--FXFX-YF+", "Y": "-FX+YFYF++YF+FX--FX-Y"}, 60, "F"),
}


@lru_cache(maxsize=16)
def _lsystem(name: str, order: int) -> np.ndarray:
    axiom, rules, angle, draw = _LSYS[name]
    s = axiom
    for _ in range(order):
        s = "".join(rules.get(c, c) for c in s)
    turns = np.cumsum([angle if c == "+" else -angle if c == "-" else 0 for c in s])
    steps = [i for i, c in enumerate(s) if c in draw]
    a = np.radians(turns[steps])
    return np.vstack([[0, 0], np.cumsum(np.column_stack([np.cos(a), np.sin(a)]), axis=0)])


@register(
    "curve",
    "Space-filling curve",
    "One unbroken line that visits every part of the picture along a Peano, Moore or Gosper curve, "
    "wiggling harder in dark areas.",
    [
        Param("curve", "Curve", "choice", "gosper", choices=["gosper", "peano", "moore"]),
        Param("cell", "Path spacing", "float", 2.0, 0.6, 8, 0.1, unit="mm"),
        Param("amplitude", "Wiggle height", "float", 0.45, 0.05, 1.0, 0.05, unit="×", help="× path spacing"),
        Param("wavelength", "Wiggle length", "float", 0.8, 0.3, 4, 0.05, unit="mm", help="In the darkest areas"),
        Param("rounded", "Round the corners", "bool", True),
        Param("rotate", "Turn the curve", "float", 0.0, -90, 90, 1, unit="°"),
    ],
    group=LINE,
)
def curve(tone: Tone, ctx: Context, curve, cell, amplitude, wavelength, rounded, rotate) -> Lines:
    w, h = tone.w, tone.h
    need = np.hypot(w, h) / cell  # the curve's square must cover the area at any turn
    pts = None
    for order in range(1, 9):
        p = _lsystem(curve, order)
        span = p.max(axis=0) - p.min(axis=0)
        if min(span) >= need * (1.3 if curve == "gosper" else 1.0) or len(p) > 3_000_000:
            pts = p
            break
        pts = p
    pts = (pts - (pts.min(axis=0) + pts.max(axis=0)) / 2) * cell
    pts = _rotate(pts, rotate) + [w / 2, h / 2]
    keep = (pts[:, 0] > 0) & (pts[:, 0] < w) & (pts[:, 1] > 0) & (pts[:, 1] < h)
    out: Lines = []
    edges = np.diff(np.concatenate([[0], keep.astype(np.int8), [0]]))
    for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        if e - s >= 2:
            out += _wiggle_path(pts[s:e], tone, cell, amplitude, wavelength, rounded)
    return out


# =========================================================================== lines & relief


@register(
    "ridges",
    "Ridge lines",
    "Stacked lines lifted by the picture, with the lines behind hidden, like a mountain range or the "
    "famous pulsar album cover.",
    [
        Param("spacing", "Line spacing", "float", 2.0, 0.6, 8, 0.1, unit="mm"),
        Param("height", "Ridge height", "float", 8.0, 1, 40, 0.5, unit="mm", help="Lift at full black"),
        Param("lift", "Lifted by", "choice", "dark", choices=["dark", "light"]),
        Param("smoothing", "Smoothing", "float", 0.8, 0, 5, 0.1, unit="mm"),
        Param("hidden", "Hide lines behind", "bool", True),
        Param("noise", "Roughness", "float", 0.0, 0, 1, 0.05, unit="×", help="Adds texture to the ridges"),
    ],
    group=LINES,
)
def ridges(tone: Tone, ctx: Context, spacing, height, lift, smoothing, hidden, noise) -> Lines:
    w, h = tone.w, tone.h
    step = 0.25
    xs = np.arange(0, w + step / 2, step)
    top = h - height  # baselines stay low enough for full lifts to fit
    if top <= spacing:
        height, top = h * 0.5, h * 0.5
    bases = np.arange(spacing / 2, top, spacing)
    horizon = np.full(len(xs), -np.inf)
    rng = np.random.default_rng(ctx.seed)
    k = max(1, int(smoothing / step))
    kernel = np.exp(-0.5 * (np.arange(-3 * k, 3 * k + 1) / k) ** 2) if smoothing > 0 else np.ones(1)
    kernel /= kernel.sum()
    out: Lines = []
    for i, yb in enumerate(bases):  # front to back: nearer lines hide farther ones
        sample_y = yb / max(top, 1e-9) * h  # each line shows its own band of the picture
        v = tone.darkness(xs, np.full(len(xs), sample_y))
        if lift == "light":
            v = 1 - v
        if noise > 0:
            v = v * (1 + noise * rng.normal(0, 0.35, len(xs)))
        v = np.convolve(np.pad(v, len(kernel) // 2, mode="edge"), kernel, mode="valid")
        y = yb + height * np.clip(v, 0, 1.2)
        y = np.minimum(y, h)
        vis = y > horizon + 0.05 if hidden else np.ones(len(xs), bool)
        horizon = np.maximum(horizon, y)
        pts = np.column_stack([xs, y])
        edges = np.diff(np.concatenate([[0], vis.astype(np.int8), [0]]))
        runs = [pts[s:e] for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)) if e - s >= 2]
        out += [r[::-1] for r in runs[::-1]] if i % 2 else runs
    return out


@register(
    "rings",
    "Concentric rings",
    "Rings around a center point that ripple in dark areas, like a fingerprint or rings in water.",
    [
        Param("spacing", "Ring spacing", "float", 2.0, 0.8, 8, 0.1, unit="mm"),
        Param("amplitude", "Ripple height", "float", 0.46, 0.1, 1.0, 0.02, unit="×", help="× ring spacing"),
        Param("min_wavelength", "Shortest ripple", "float", 1.2, 0.5, 10, 0.1, unit="mm"),
        Param("max_wavelength", "Longest ripple", "float", 6.0, 1, 30, 0.5, unit="mm"),
        Param("center_x", "Center left/right", "float", 0.0, -0.5, 0.5, 0.01, unit="×"),
        Param("center_y", "Center front/back", "float", 0.0, -0.5, 0.5, 0.01, unit="×"),
    ],
    group=LINES,
)
def rings(tone: Tone, ctx: Context, spacing, amplitude, min_wavelength, max_wavelength, center_x, center_y) -> Lines:
    w, h = tone.w, tone.h
    cx, cy = w / 2 + center_x * w, h / 2 + center_y * h
    r_max = max(np.hypot(px - cx, py - cy) for px in (0, w) for py in (0, h))
    out: Lines = []
    for k, r in enumerate(np.arange(spacing, r_max + spacing, spacing)):
        th = np.linspace(0, 2 * np.pi, max(24, int(2 * np.pi * r / 0.3)) + 1)
        bx, by = cx + r * np.cos(th), cy + r * np.sin(th)
        dark = tone.darkness(bx, by)
        wl = max_wavelength - (max_wavelength - min_wavelength) * dark
        phase = np.cumsum(np.concatenate([[0], 2 * np.pi * np.hypot(np.diff(bx), np.diff(by)) / wl[1:]]))
        rr = r + dark * spacing * amplitude * np.sin(phase)
        pts = np.column_stack([cx + rr * np.cos(th), cy + rr * np.sin(th)])
        runs = _runs_inside(pts, w, h)
        out += [p[::-1] for p in runs] if k % 2 else runs
    return out


@register(
    "mesh",
    "Mesh warp",
    "A grid of lines pushed sideways by the picture, so it seems to bulge out of a net.",
    [
        Param("spacing", "Grid spacing", "float", 2.5, 0.8, 10, 0.1, unit="mm"),
        Param("amplitude", "Push", "float", 4.5, 0.2, 15, 0.1, unit="mm", help="At full black"),
        Param("direction", "Push direction", "float", 45.0, -180, 180, 5, unit="°"),
        Param("lines", "Lines", "choice", "both", choices=["both", "across", "up and down"]),
        Param("smoothing", "Smoothing", "float", 1.0, 0, 6, 0.1, unit="mm"),
    ],
    resolution=2.0,
    group=LINES,
)
def mesh(tone: Tone, ctx: Context, spacing, amplitude, direction, lines, smoothing) -> Lines:
    w, h = tone.w, tone.h
    ppm = tone.data.shape[1] / w
    smooth = Tone(vz.smooth(tone.data, smoothing * ppm), w, h)
    a = np.radians(direction)
    push = amplitude * np.array([np.cos(a), np.sin(a)])
    step = 0.3
    out: Lines = []
    fams = []
    along_x, along_y = np.arange(-amplitude, w + amplitude, step), np.arange(-amplitude, h + amplitude, step)
    if lines in ("both", "across"):
        fams += [np.column_stack([along_x, np.full_like(along_x, y)]) for y in np.arange(-amplitude, h + amplitude, spacing)]
    if lines in ("both", "up and down"):
        fams += [np.column_stack([np.full_like(along_y, x), along_y]) for x in np.arange(-amplitude, w + amplitude, spacing)]
    for k, base in enumerate(fams):
        d = smooth.darkness(np.clip(base[:, 0], 0, w), np.clip(base[:, 1], 0, h))
        pts = base + d[:, None] * push
        runs = _runs_inside(pts, w, h)
        out += [r[::-1] for r in runs] if k % 2 else runs
    return out


@register(
    "moire",
    "Moiré",
    "Two sets of parallel lines laid over each other. Where the picture is dark the second set slides "
    "between the first, so the overlap draws the picture. A small twist adds rippling bands.",
    [
        Param("spacing", "Line spacing", "float", 1.6, 0.6, 6, 0.1, unit="mm"),
        Param("angle", "Line angle", "float", 0.0, -90, 90, 1, unit="°"),
        Param("twist", "Twist", "float", 0.0, 0, 15, 0.25, unit="°", help="Angle between the two sets"),
        Param("shift", "Slide at full black", "float", 0.5, 0.1, 1.0, 0.05, unit="×", help="× line spacing"),
        Param("smoothing", "Smoothing", "float", 0.6, 0, 4, 0.1, unit="mm"),
    ],
    resolution=2.0,
    group=LINES,
)
def moire(tone: Tone, ctx: Context, spacing, angle, twist, shift, smoothing) -> Lines:
    w, h = tone.w, tone.h
    ppm = tone.data.shape[1] / w
    smooth = Tone(vz.smooth(tone.data, smoothing * ppm), w, h)
    r = np.hypot(w, h) / 2
    t = np.arange(-r, r, 0.3)
    out: Lines = []
    for fam, (ang, slide) in enumerate(((angle, False), (angle + twist, True))):
        a = np.radians(ang)
        d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
        for i, v in enumerate(np.arange(-r, r, spacing)):
            base = np.array([w / 2, h / 2]) + t[:, None] * d + v * n
            if slide:
                dk = smooth.darkness(np.clip(base[:, 0], 0, w), np.clip(base[:, 1], 0, h))
                base = base + (dk * shift * spacing)[:, None] * n
            runs = _runs_inside(base, w, h)
            out += [q[::-1] for q in runs] if (i + fam) % 2 else runs
    return out


@register(
    "morse",
    "Morse lines",
    "Parallel lines broken into dots and dashes: long dashes and short gaps in the dark, "
    "scattered dots in the light.",
    [
        Param("spacing", "Line spacing", "float", 1.6, 0.6, 8, 0.1, unit="mm"),
        Param("angle", "Line angle", "float", 0.0, -90, 90, 1, unit="°"),
        Param("dash", "Longest dash", "float", 4.0, 0.5, 20, 0.25, unit="mm"),
        Param("gap", "Widest gap", "float", 3.0, 0.4, 15, 0.1, unit="mm", help="In the lightest areas"),
        Param("min_dark", "Skip lighter than", "float", 0.05, 0, 0.5, 0.01),
    ],
    group=LINES,
)
def morse(tone: Tone, ctx: Context, spacing, angle, dash, gap, min_dark) -> Lines:
    w, h = tone.w, tone.h
    a = np.radians(angle)
    d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
    r = np.hypot(w, h) / 2
    min_gap = 0.5
    out: Lines = []
    for i, v in enumerate(np.arange(r - spacing / 2, -r, -spacing)):
        line = np.array([w / 2, h / 2]) + np.arange(-r, r, 0.1)[:, None] * d + v * n
        for run in _runs_inside(line, w, h):
            if i % 2:
                run = run[::-1]
            dk = tone.darkness(run[:, 0], run[:, 1])
            j = 0
            while j < len(run):
                if dk[j] < min_dark:
                    j += 1
                    continue
                on = max(1, int(dk[j] * dash / 0.1))  # samples are 0.1 mm apart
                seg = run[j:j + on]
                out.append(seg[[0, -1]] if len(seg) > 1 else seg)
                j += on + max(1, int((min_gap + (gap - min_gap) * (1 - dk[j])) / 0.1))
    return out


@register(
    "harmonograph",
    "Harmonograph",
    "Looping pendulum curves, the kind a drawing machine makes, slowly turning as they fade inward. "
    "Each extra pass is drawn only where the picture is darker, so the loops pile up into the image.",
    [
        Param("passes", "Passes", "int", 10, 1, 30, 1, help="More passes give more tones"),
        Param("ratio", "Swing ratio", "choice", "2:3", choices=["1:1", "1:2", "2:3", "3:4", "3:5", "4:5"]),
        Param("density", "Density", "float", 1.5, 0.2, 4, 0.05, unit="×",
              help="Loops per pass; 1 is about right for this pen width"),
        Param("inner", "Smallest swing", "float", 0.05, 0, 0.8, 0.01, unit="×",
              help="× the largest, where each pass ends"),
        Param("drift", "Turn while drawing", "float", 1.0, 0, 6, 0.1, unit="×",
              help="How far each pass rotates as it fades"),
    ],
    group=TILES,
)
def harmonograph(tone: Tone, ctx: Context, passes, ratio, density, inner, drift) -> Lines:
    w, h = tone.w, tone.h
    p, q = (int(v) for v in ratio.split(":"))
    R = min(w, h) / 2 * 0.97
    # Enough loops that all the passes together could just cover the disc with this pen.
    loops = max(3.0, density * 0.5 * R / (ctx.pen_width * passes * (p + q) / 2))
    T = 2 * np.pi * loops
    n = max(4000, int(loops * 2 * np.pi * R / 0.35))
    t = np.linspace(0, T, n)
    decay = np.sqrt(1 - (1 - inner ** 2) * t / T)  # shrinks so the loops cover the disc evenly
    # Two slightly detuned pendulums per axis, as on a real harmonograph.
    rng = np.random.default_rng(ctx.seed)
    e = rng.uniform(-0.006, 0.006, 4)
    ph = rng.uniform(0, 2 * np.pi, 4)
    ph[2:] = ph[:2] + np.pi / 2  # a quarter turn apart: loops and rosettes, never a flat back-and-forth line
    x = 0.6 * np.sin((p + e[0]) * t + ph[0]) + 0.4 * np.sin((p + e[1]) * t + ph[1])
    y = 0.6 * np.sin((q + e[2]) * t + ph[2]) + 0.4 * np.sin((q + e[3]) * t + ph[3])
    base = np.column_stack([x, y])
    base /= np.hypot(base[:, 0], base[:, 1]).max()  # the swing never leaves the disc
    base *= decay[:, None]
    out: Lines = []
    for k in range(passes):
        turn = np.pi * k / passes + drift * np.pi * t / T  # each pass starts at its own angle and precesses
        c, s_ = np.cos(turn), np.sin(turn)
        pts = np.column_stack([base[:, 0] * c - base[:, 1] * s_, base[:, 0] * s_ + base[:, 1] * c]) * R
        pts += [w / 2, h / 2]
        show = tone.darkness(pts[:, 0], pts[:, 1]) > (k + 0.5) / passes
        edges = np.diff(np.concatenate([[0], show.astype(np.int8), [0]]))
        out += [pts[a:b] for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)) if b - a >= 3]
    return out


# =========================================================================== hatching & engraving


@register(
    "engraving",
    "Banknote engraving",
    "Fine parallel lines that swell in the shadows: extra pen passes beside each line make it bolder, "
    "like the portraits on banknotes. The lines can bend gently.",
    [
        Param("spacing", "Line spacing", "float", 1.4, 0.6, 6, 0.05, unit="mm"),
        Param("angle", "Line angle", "float", 20.0, -90, 90, 1, unit="°"),
        Param("passes", "Boldest line", "int", 4, 1, 8, 1, help="Pen passes side by side at full black"),
        Param("bend", "Bend", "float", 1.5, 0, 15, 0.25, unit="mm", help="How far the lines curve"),
        Param("bend_length", "Bend length", "float", 60.0, 10, 300, 5, unit="mm"),
        Param("min_dark", "Lightest line drawn", "float", 0.05, 0, 0.5, 0.01),
    ],
    group=HATCH,
)
def engraving(tone: Tone, ctx: Context, spacing, angle, passes, bend, bend_length, min_dark) -> Lines:
    w, h = tone.w, tone.h
    a = np.radians(angle)
    d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
    r = np.hypot(w, h) / 2 + bend
    t = np.arange(-r, r, 0.25)
    pitch = ctx.pen_width * 0.85
    passes = max(1, min(passes, int(spacing * 0.8 / pitch) + 1))
    offsets = [0.0] + [s * k * pitch for k in range(1, passes) for s in (1, -1)]
    out: Lines = []
    for i, v in enumerate(np.arange(-r, r, spacing)):
        curve = bend * np.sin(2 * np.pi * t / bend_length + v * 0.05)
        base = np.array([w / 2, h / 2]) + t[:, None] * d + (v + curve)[:, None] * n
        dark = tone.darkness(np.clip(base[:, 0], 0, w), np.clip(base[:, 1], 0, h))
        for j, off in enumerate(offsets[: 2 * passes - 1]):
            level = min_dark if j == 0 else min_dark + (1 - min_dark) * ((j + 1) // 2) / passes
            show = dark >= level
            pts = base + off * n
            edges = np.diff(np.concatenate([[0], show.astype(np.int8), [0]]))
            for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
                for run in _runs_inside(pts[s:e], w, h):
                    out.append(run[::-1] if (i + j) % 2 else run)
    return out


def _offset_rings(mask: np.ndarray, xs, ys, spacing: float, phase: float = 0.0) -> Lines:
    """Rings inside a mask at equal distances from its edge (like contour lines of a pond's depth)."""
    if not mask.any():
        return []
    px = xs[1] - xs[0]
    dist = cv2.distanceTransform(np.pad(mask, 1).astype(np.uint8), cv2.DIST_L2, 5)[1:-1, 1:-1] * px
    out: Lines = []
    lv = spacing / 2 + phase
    while lv < dist.max():
        for c in find_contours(np.pad(dist, 1), lv):
            if len(c) > 3:
                out.append(_rc_to_mm(c - 1, xs, ys))
        lv += spacing
    return out


@register(
    "offset_fill",
    "Offset fill",
    "Each tone becomes a shape filled with rings that follow its own outline inward, like a "
    "topographic map. Darker tones overlap more rings.",
    [
        Param("levels", "Tone levels", "int", 4, 1, 8, 1),
        Param("spacing", "Ring spacing", "float", 1.6, 0.4, 8, 0.05, unit="mm"),
        Param("smoothing", "Smoothing", "float", 0.8, 0, 5, 0.1, unit="mm"),
        Param("min_length", "Drop rings under", "float", 1.5, 0, 20, 0.5, unit="mm"),
    ],
    group=HATCH,
)
def offset_fill(tone: Tone, ctx: Context, levels, spacing, smoothing, min_length) -> Lines:
    xs, ys, dark = _grid(tone, 4.0)
    dark = cv2.GaussianBlur(dark.astype(np.float32), (0, 0), max(0.01, smoothing * 4.0))
    out: Lines = []
    for k in range(levels):
        mask = dark > (k + 0.5) / (levels + 0.5)
        out += _offset_rings(mask, xs, ys, spacing, phase=spacing * k / max(levels, 1) * 0.5)
    return [l for l in out if np.hypot(*np.diff(l, axis=0).T).sum() >= min_length]


@register(
    "woodcut",
    "Woodcut",
    "Bold black shapes for the shadows and short gouge marks that follow the forms in the mid-tones, "
    "like a block print.",
    [
        Param("black", "Solid black from", "float", 0.72, 0.3, 1, 0.01, help="Darkness above which areas are filled"),
        Param("mid", "Gouges from", "float", 0.3, 0.05, 0.9, 0.01, help="Darkness above which marks start"),
        Param("spacing", "Gouge spacing", "float", 1.1, 0.4, 5, 0.05, unit="mm"),
        Param("dash", "Gouge length", "float", 5.0, 1, 25, 0.5, unit="mm"),
        Param("smoothing", "Smoothing", "float", 1.0, 0, 5, 0.1, unit="mm"),
        Param("outline", "Outline the black shapes", "bool", True),
    ],
    group=HATCH,
)
def woodcut(tone: Tone, ctx: Context, black, mid, spacing, dash, smoothing, outline) -> Lines:
    w, h = tone.w, tone.h
    ppm = 5.0
    xs, ys, dark = _grid(tone, ppm)
    dark = cv2.GaussianBlur(dark.astype(np.float32), (0, 0), max(0.01, smoothing * ppm))
    solid = vz.clean_mask(dark >= black, (1.0 * ppm) ** 2)
    band = vz.clean_mask((dark >= mid) & ~solid, (1.0 * ppm) ** 2)
    flipped = solid[::-1]  # vectorize helpers want row 0 at the back
    out: Lines = vz.hatch(flipped, w, h, ctx.pen_width * 0.85, 45, min_len=ctx.pen_width)
    if outline:
        out += vz.outlines(flipped, w, h)
    rng = np.random.default_rng(ctx.seed)

    def share(x, y):  # mid-tones get short nicks, near-black gets long gouges
        d = float(tone.darkness(x, y))
        return float(np.clip((d - mid) / max(black - mid, 1e-3), 0, 1)) * 0.85 + 0.1

    # Gouges run along the forms, as rings following the shapes' outlines.
    for ring in _offset_rings(band, xs, ys, spacing):
        out += _cut_dashes(ring, share, dash, rng, min_on=ctx.pen_width * 2)
    return out


@register(
    "sketch",
    "Sketch",
    "Loose pencil-style drawing: short, slightly wobbly strokes along the edges, with quick hatching "
    "in the shadows.",
    [
        Param("edges", "Edge detail", "int", 4, 1, 10, 1, help="How many tone edges get outlined"),
        Param("stroke", "Stroke length", "float", 8.0, 2, 30, 0.5, unit="mm"),
        Param("wobble", "Wobble", "float", 0.35, 0, 2, 0.05, unit="mm"),
        Param("passes", "Edge passes", "int", 2, 1, 4, 1, help="Times each edge is gone over"),
        Param("shading", "Shading", "float", 1.0, 0, 3, 0.05, unit="×"),
        Param("hatch_angle", "Hatch angle", "float", 50.0, -90, 90, 5, unit="°"),
        Param("hatch_spacing", "Hatch spacing", "float", 1.2, 0.4, 5, 0.05, unit="mm"),
        Param("smoothing", "Smoothing", "float", 1.0, 0, 5, 0.1, unit="mm"),
    ],
    resolution=4.0,
    group=HATCH,
)
def sketch(tone: Tone, ctx: Context, edges, stroke, wobble, passes, shading, hatch_angle, hatch_spacing,
           smoothing) -> Lines:
    w, h = tone.w, tone.h
    rng = np.random.default_rng(ctx.seed)
    gray = vz.smooth(tone.data, smoothing * tone.data.shape[1] / w)
    out: Lines = []

    def wobbly(p: np.ndarray) -> np.ndarray:
        if len(p) < 2 or wobble <= 0:
            return p
        seg = np.hypot(*np.diff(p, axis=0).T)
        cum = np.concatenate([[0], np.cumsum(seg)])
        tt = np.arange(0, cum[-1] + 1e-9, 0.4)
        q = np.column_stack([np.interp(tt, cum, p[:, 0]), np.interp(tt, cum, p[:, 1])])
        tang = np.gradient(q, axis=0)
        tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]
        nrm = np.column_stack([-tang[:, 1], tang[:, 0]])
        k = rng.uniform(0.15, 0.5)
        off = wobble * (np.sin(tt * k + rng.uniform(0, 6.3)) * 0.6 + rng.normal(0, 0.25) + 0.15 * np.sin(tt * 3.1 * k))
        return q + off[:, None] * nrm

    # Edges: tone outlines cut into overlapping strokes, each gone over a couple of times.
    cuts = [(k + 1) / (edges + 1) for k in range(edges)]
    for line in vz.iso_lines(gray, cuts, w, h):
        seg = np.hypot(*np.diff(line, axis=0).T)
        cum = np.concatenate([[0], np.cumsum(seg)])
        if cum[-1] < 1.0:
            continue
        s = 0.0
        while s < cum[-1]:
            L = stroke * rng.uniform(0.6, 1.3)
            e = min(cum[-1], s + L)
            tt = np.linspace(s, e, max(2, int((e - s) / 0.4)))
            piece = np.column_stack([np.interp(tt, cum, line[:, 0]), np.interp(tt, cum, line[:, 1])])
            for _ in range(passes):
                out.append(wobbly(piece))
            s = e - min(1.0, (e - s) * 0.15)  # overlap the next stroke a little
            if e >= cum[-1]:
                break
    # Shading: quick hatch strokes, more where it's darker; a cross layer in the deepest shadows.
    if shading > 0:
        for layer, (ang, cut) in enumerate(((hatch_angle, 0.22), (hatch_angle + 75, 0.6))):
            a = np.radians(ang)
            d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
            r = np.hypot(w, h) / 2
            for v in np.arange(-r, r, hatch_spacing):
                tt = -r + rng.uniform(0, stroke)
                while tt < r:
                    L = stroke * rng.uniform(0.5, 1.0)
                    c = np.array([w / 2, h / 2]) + (tt + L / 2) * d + v * n
                    if 0 <= c[0] <= w and 0 <= c[1] <= h:
                        dk = float(tone.darkness(c[0], c[1]))
                        if dk > cut and rng.random() < min(1.0, ((dk - cut) / (1 - cut)) ** 1.3 * shading):
                            jitter = np.radians(rng.normal(0, 4))
                            dj = np.array([np.cos(a + jitter), np.sin(a + jitter)])
                            p = np.vstack([c - dj * L / 2, c + dj * L / 2])
                            out += _runs_inside(wobbly(p), w, h)
                    tt += L * rng.uniform(1.05, 1.4)
    out = [np.column_stack([np.clip(l[:, 0], 0, w), np.clip(l[:, 1], 0, h)]) for l in out if len(l) >= 2]
    return out


@register(
    "pixel_hatch",
    "Pixel hatch",
    "A coarse grid where each square gets 0 to 4 layers of hatching by how dark it is, for a "
    "retro, low-resolution look.",
    [
        Param("cell", "Pixel size", "float", 3.0, 1, 15, 0.1, unit="mm"),
        Param("lines", "Lines per pixel", "int", 3, 1, 8, 1),
        Param("levels", "Most layers", "int", 4, 1, 4, 1),
        Param("gap", "Gap between pixels", "float", 0.3, 0, 2, 0.05, unit="mm"),
    ],
    group=HATCH,
)
def pixel_hatch(tone: Tone, ctx: Context, cell, lines, levels, gap) -> Lines:
    w, h = tone.w, tone.h
    nx, ny = int(w // cell), int(h // cell)
    x0, y0 = (w - nx * cell) / 2, (h - ny * cell) / 2
    angles = (0, 90, 45, -45)
    out: Lines = []
    for j in range(ny):
        for i in range(nx):
            xa, ya = x0 + i * cell + gap / 2, y0 + j * cell + gap / 2
            s = cell - gap
            sub = np.linspace(0.2, 0.8, 3) * s
            dk = float(tone.darkness(xa + sub[None, :], ya + sub[:, None]).mean())
            k = min(levels, int(dk * levels + 0.25))
            sq = np.array([[xa, ya], [xa + s, ya], [xa + s, ya + s], [xa, ya + s]])
            for layer in range(k):
                out += _hatch_poly(sq, s / lines, angles[layer], flip=bool((i + j) % 2))
    return out


# =========================================================================== dots


@register(
    "dashes",
    "Dashes",
    "Short dashes laid along the edges and forms, packed tighter in the shadows, like the dot-and-dash "
    "portraits in newspapers.",
    [
        Param("points", "Dashes", "int", 3000, 200, 20000, 100),
        Param("length", "Dash length", "float", 2.2, 0.4, 10, 0.1, unit="mm"),
        Param("contrast", "Density contrast", "float", 1.6, 0.5, 4, 0.1, unit="×"),
        Param("align", "Direction", "choice", "follow edges", choices=["follow edges", "across edges", "fixed angle"]),
        Param("angle", "Angle", "float", 45.0, -90, 90, 1, unit="°", when={"align": ["fixed angle"]}),
        Param("smoothing", "Smoothing", "float", 1.5, 0, 6, 0.1, unit="mm"),
    ],
    group=DOTS,
)
def dashes(tone: Tone, ctx: Context, points, length, contrast, align, angle, smoothing) -> Lines:
    w, h = tone.w, tone.h
    pts = _density_points(tone, points, contrast, ctx.seed, relax=6)
    ppm = 3.0
    xs, ys, dark = _grid(tone, ppm)
    dark = cv2.GaussianBlur(dark.astype(np.float32), (0, 0), max(0.01, smoothing * ppm))
    gy, gx = np.gradient(dark)
    ci = np.clip((pts[:, 0] * ppm).astype(int), 0, len(xs) - 1)
    ri = np.clip((pts[:, 1] * ppm).astype(int), 0, len(ys) - 1)
    vx, vy = gx[ri, ci], gy[ri, ci]
    mag = np.hypot(vx, vy)
    fixed = np.radians(angle)
    if align == "fixed angle":
        th = np.full(len(pts), fixed)
    else:
        th = np.arctan2(vy, vx) + (np.pi / 2 if align == "follow edges" else 0)
        flat = mag < np.percentile(mag, 20)
        th[flat] = fixed  # no clear edge: fall back to the angle
    dk = tone.darkness(pts[:, 0], pts[:, 1])
    half = (length * (0.6 + 0.4 * dk) / 2)[:, None] * np.column_stack([np.cos(th), np.sin(th)])
    out: Lines = []
    for p, hv in zip(pts, half):
        seg = np.vstack([p - hv, p + hv])
        seg[:, 0] = np.clip(seg[:, 0], 0, w)
        seg[:, 1] = np.clip(seg[:, 1], 0, h)
        out.append(seg)
    return out


def _polygon(x, y, r, sides, rot) -> np.ndarray:
    a = rot + 2 * np.pi * np.arange(sides + 1) / sides
    return np.column_stack([x + r * np.cos(a), y + r * np.sin(a)])


@register(
    "shapes",
    "Shape packing",
    "Like circle packing, with squares, hexagons or triangles: small and tight in the shadows, "
    "large and open in the light.",
    [
        Param("shape", "Shape", "choice", "hexagon", choices=["square", "hexagon", "triangle"]),
        Param("turn", "Turning", "choice", "random", choices=["random", "aligned"]),
        Param("min_radius", "Smallest shape", "float", 0.7, 0.2, 5, 0.05, unit="mm"),
        Param("max_radius", "Largest shape", "float", 5.0, 1, 25, 0.5, unit="mm"),
        Param("gap", "Gap", "float", 0.3, 0, 3, 0.05, unit="mm"),
        Param("attempts", "Packing effort", "int", 40000, 2000, 200000, 1000),
        Param("curve", "Tone curve", "float", 0.7, 0.2, 3, 0.05, unit="×"),
        Param("invert", "Large shapes in the dark", "bool", False),
        Param("empty_white", "Leave white areas empty", "bool", True),
    ],
    group=DOTS,
)
def shapes(tone: Tone, ctx: Context, shape, turn, min_radius, max_radius, gap, attempts, curve, invert,
           empty_white) -> Lines:
    sides = {"square": 4, "hexagon": 6, "triangle": 3}[shape]
    placed = _pack(tone, ctx, min_radius, max_radius, gap, attempts, curve, invert, empty_white)
    rng = np.random.default_rng(ctx.seed + 7)
    base = {4: np.pi / 4, 6: 0.0, 3: np.pi / 2}[sides]
    return [_polygon(x, y, r, sides, base + (rng.uniform(0, 2 * np.pi) if turn == "random" else 0.0))
            for x, y, r in placed]


# =========================================================================== patterns & tiles


@register(
    "maze",
    "Maze",
    "A real maze with one path between any two points. Its corridors get narrower and its turns "
    "denser where the picture is dark.",
    [
        Param("min_cell", "Narrowest corridor", "float", 1.6, 0.8, 6, 0.1, unit="mm", help="In the darkest areas"),
        Param("levels", "Corridor sizes", "int", 3, 0, 5, 1, help="Each size doubles the last"),
        Param("contrast", "Contrast", "float", 1.0, 0.3, 3, 0.1, unit="×"),
        Param("openings", "Entrance and exit", "bool", True),
    ],
    group=TILES,
)
def maze(tone: Tone, ctx: Context, min_cell, levels, contrast, openings) -> Lines:
    w, h = tone.w, tone.h
    big = 2 ** levels
    bx, by = max(1, round(w / (min_cell * big))), max(1, round(h / (min_cell * big)))
    nx, ny = bx * big, by * big  # fine cells
    cw, ch = w / nx, h / ny
    gx, gy = np.meshgrid((np.arange(nx) + 0.5) * cw, (np.arange(ny) + 0.5) * ch)
    dark = tone.darkness(gx, gy) ** contrast
    integ = np.pad(dark.cumsum(0).cumsum(1), ((1, 0), (1, 0)))

    def mean(r, c, s):
        return (integ[r + s, c + s] - integ[r, c + s] - integ[r + s, c] + integ[r, c]) / (s * s)

    label = np.zeros((ny, nx), np.int32)
    leaves = 0

    def split(r, c, s):
        nonlocal leaves
        # Darker blocks split into smaller corridors: level 0 (darkest) is min_cell wide.
        want = 1 + (1 - mean(r, c, s)) * (big - 1)
        if s > 1 and s > want:
            hs = s // 2
            for dr in (0, hs):
                for dc in (0, hs):
                    split(r + dr, c + dc, hs)
        else:
            label[r:r + s, c:c + s] = leaves
            leaves += 1

    for j in range(by):
        for i in range(bx):
            split(j * big, i * big, big)
    # Neighbouring cells, then a random spanning tree (Kruskal): the open passages.
    a, b = label[:, :-1], label[:, 1:]
    hp = np.stack([np.minimum(a, b), np.maximum(a, b)], -1)[a != b]
    a2, b2 = label[:-1, :], label[1:, :]
    vp = np.stack([np.minimum(a2, b2), np.maximum(a2, b2)], -1)[a2 != b2]
    pairs = np.unique(np.vstack([hp, vp]), axis=0)
    rng = np.random.default_rng(ctx.seed)
    rng.shuffle(pairs)
    parent = np.arange(leaves)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    passages = []
    for p, q in pairs.tolist():
        rp, rq = find(p), find(q)
        if rp != rq:
            parent[rp] = rq
            passages.append(p * leaves + q)
    passages = np.array(passages, np.int64)
    out: Lines = []
    # Walls: fine-cell edges between different cells that aren't a passage, merged into long runs.
    key = lambda u, v: np.minimum(u, v).astype(np.int64) * leaves + np.maximum(u, v)  # noqa: E731
    wall_v = (a != b) & ~np.isin(key(a, b), passages)
    wall_h = (a2 != b2) & ~np.isin(key(a2, b2), passages)
    for c in range(nx - 1):  # vertical walls at x = (c + 1) · cw
        col = wall_v[:, c].astype(np.int8)
        e = np.diff(np.concatenate([[0], col, [0]]))
        for s, t in zip(np.flatnonzero(e == 1), np.flatnonzero(e == -1)):
            out.append(np.array([[(c + 1) * cw, s * ch], [(c + 1) * cw, t * ch]]))
    for r in range(ny - 1):  # horizontal walls at y = (r + 1) · ch
        row = wall_h[r, :].astype(np.int8)
        e = np.diff(np.concatenate([[0], row, [0]]))
        for s, t in zip(np.flatnonzero(e == 1), np.flatnonzero(e == -1)):
            out.append(np.array([[s * cw, (r + 1) * ch], [t * cw, (r + 1) * ch]]))
    # The outside wall, with a way in at the front-left and out at the back-right.
    if openings:
        out.append(np.array([[0, ch], [0, h], [w - cw, h]]))
        out.append(np.array([[cw, 0], [w, 0], [w, h - ch]]))
    else:
        out.append(np.array([[0, 0], [w, 0], [w, h], [0, h], [0, 0]]))
    return out


@register(
    "truchet",
    "Truchet tiles",
    "A grid of square tiles, each with arcs or diagonals that join its neighbours into flowing "
    "patterns. Darker tiles get more lines.",
    [
        Param("cell", "Tile size", "float", 6.0, 2, 30, 0.5, unit="mm"),
        Param("style", "Style", "choice", "arcs", choices=["arcs", "diagonals"]),
        Param("max_lines", "Most lines per tile", "int", 4, 1, 10, 1),
        Param("min_dark", "Empty tiles lighter than", "float", 0.04, 0, 0.5, 0.01),
    ],
    group=TILES,
)
def truchet(tone: Tone, ctx: Context, cell, style, max_lines, min_dark) -> Lines:
    w, h = tone.w, tone.h
    nx, ny = int(w // cell), int(h // cell)
    x0, y0 = (w - nx * cell) / 2, (h - ny * cell) / 2
    rng = np.random.default_rng(ctx.seed)
    step = cell / (2 * max_lines + 1)
    # Radii (or offsets) symmetric about the middle, so tiles always meet their neighbours.
    order = [0.0] + [s * k * step for k in range(1, max_lines) for s in (1, -1)]
    out: Lines = []
    for j in range(ny):
        for i in range(nx):
            xa, ya = x0 + i * cell, y0 + j * cell
            dk = float(tone.darkness(xa + cell / 2, ya + cell / 2))
            if dk < min_dark:
                continue
            m = max(1, int(round(dk * (2 * max_lines - 1))))
            flip = rng.random() < 0.5
            for off in order[:m]:
                r = cell / 2 + off
                if style == "arcs":
                    corners = ((xa, ya, 0), (xa + cell, ya + cell, 180)) if flip else (
                        (xa + cell, ya, 90), (xa, ya + cell, 270))
                    for cx, cy, start in corners:
                        t = np.radians(np.linspace(start, start + 90, max(6, int(r * np.pi / 2 / 0.3))))
                        out.append(np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)]))
                else:  # two diagonal strokes per offset, parallel to one of the tile's diagonals
                    poly = np.array([[xa, ya], [xa + cell, ya], [xa + cell, ya + cell], [xa, ya + cell]])
                    a = np.radians(45 if flip else -45)
                    nrm = np.array([-np.sin(a), np.cos(a)])
                    seg = _clip_convex(np.array([xa + cell / 2, ya + cell / 2]) + nrm * off * np.sqrt(2),
                                       np.array([np.cos(a), np.sin(a)]), poly)
                    if seg is not None:
                        out.append(seg)
    return out


def _bounded_voronoi(pts: np.ndarray, w: float, h: float) -> list[np.ndarray]:
    """Voronoi cells clipped to the w × h box (by mirroring the points across each edge)."""
    mirrored = np.vstack([pts, pts * [-1, 1], pts * [1, -1], [2 * w, 0] + pts * [-1, 1], [0, 2 * h] + pts * [1, -1]])
    vor = Voronoi(mirrored)
    cells = []
    for i in range(len(pts)):
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            cells.append(None)
            continue
        poly = vor.vertices[reg]
        poly[:, 0] = np.clip(poly[:, 0], 0, w)
        poly[:, 1] = np.clip(poly[:, 1], 0, h)
        cells.append(poly)
    return cells


@register(
    "voronoi",
    "Voronoi mosaic",
    "A mosaic of cells, small and packed in the shadows, large in the light, like cracked glaze or "
    "a dragonfly wing.",
    [
        Param("cells", "Cells", "int", 900, 50, 8000, 50),
        Param("contrast", "Density contrast", "float", 1.4, 0.3, 4, 0.1, unit="×"),
        Param("gap", "Gap between cells", "float", 0.5, 0, 3, 0.05, unit="mm", help="0 draws shared edges"),
        Param("fill", "Fill dark cells", "bool", False, help="Hatch cells by how dark they are"),
        Param("relax", "Evenness", "int", 8, 0, 30, 1),
    ],
    group=TILES,
)
def voronoi(tone: Tone, ctx: Context, cells, contrast, gap, fill, relax) -> Lines:
    w, h = tone.w, tone.h
    pts = _density_points(tone, cells, contrast, ctx.seed, relax=relax)
    pts += np.random.default_rng(ctx.seed).normal(0, 1e-6, pts.shape)  # break exact ties
    polys = _bounded_voronoi(pts, w, h)
    out: Lines = []
    if gap <= 0:
        seen = set()
        for poly in polys:
            if poly is None:
                continue
            for a, b in zip(poly, np.roll(poly, -1, axis=0)):
                key = tuple(np.round(np.concatenate([a, b]) if tuple(a) < tuple(b) else np.concatenate([b, a]), 3))
                if key not in seen:
                    seen.add(key)
                    out.append(np.vstack([a, b]))
    for p, poly in zip(pts, polys):
        if poly is None:
            continue
        c = poly.mean(axis=0)
        v = poly - c
        dist = np.maximum(np.hypot(v[:, 0], v[:, 1]), 1e-9)
        shrunk = c + v * np.clip((dist - gap) / dist, 0, 1)[:, None] if gap > 0 else poly
        if gap > 0:
            out.append(np.vstack([shrunk, shrunk[:1]]))
        if fill:
            dk = float(tone.darkness(c[0], c[1]))
            if dk > 0.15:
                out += _hatch_poly(shrunk, max(ctx.pen_width * 1.1, ctx.pen_width * 1.1 / dk ** 1.5), 45)
    return out


@register(
    "lowpoly",
    "Low-poly",
    "A mesh of triangles that is finer around edges and in the shadows; triangles can be hatched by "
    "how dark they are.",
    [
        Param("points", "Points", "int", 700, 50, 6000, 50),
        Param("edge_share", "Points on edges", "float", 0.25, 0, 0.9, 0.05, help="Share of points snapped to edges"),
        Param("contrast", "Density contrast", "float", 1.0, 0.2, 3, 0.1, unit="×"),
        Param("fill", "Hatch the triangles", "bool", True),
        Param("spacing", "Darkest hatch spacing", "float", 0.7, 0.3, 4, 0.05, unit="mm", when={"fill": [True]}),
        Param("outline", "Draw the triangles", "bool", True),
    ],
    group=TILES,
)
def lowpoly(tone: Tone, ctx: Context, points, edge_share, contrast, fill, spacing, outline) -> Lines:
    w, h = tone.w, tone.h
    rng = np.random.default_rng(ctx.seed)
    n_edge = int(points * edge_share)
    pts = [_density_points(tone, max(4, points - n_edge), contrast, ctx.seed, relax=4)]
    if n_edge:
        ppm = 4.0
        xs, ys, dark = _grid(tone, ppm)
        u8 = np.clip(cv2.GaussianBlur(dark.astype(np.float32), (0, 0), 1.2) * 255, 0, 255).astype(np.uint8)
        e = cv2.Canny(u8, 30, 80) > 0
        r, c = np.nonzero(e)
        if len(r):
            pick = rng.choice(len(r), size=min(n_edge, len(r)), replace=False)
            pts.append(np.column_stack([xs[c[pick]], ys[r[pick]]]))
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
    lx, ly = np.linspace(0, w, 8), np.linspace(0, h, 8)
    border = np.vstack([np.column_stack([lx, np.zeros(8)]), np.column_stack([lx, np.full(8, h)]),
                        np.column_stack([np.zeros(8), ly]), np.column_stack([np.full(8, w), ly])])
    P = np.unique(np.round(np.vstack(pts + [corners, border]), 3), axis=0)
    tri = Delaunay(P)
    out: Lines = []
    if outline:
        edges = set()
        for s in tri.simplices:
            for a, b in ((s[0], s[1]), (s[1], s[2]), (s[2], s[0])):
                edges.add((min(a, b), max(a, b)))
        out += [P[[a, b]] for a, b in edges]
    if fill:
        for s in tri.simplices:
            poly = P[s]
            c = poly.mean(axis=0)
            e = np.hypot(*(np.roll(poly, -1, axis=0) - poly).T)
            u, v = poly[1] - poly[0], poly[2] - poly[0]
            if 0.5 * abs(u[0] * v[1] - u[1] * v[0]) < 0.08 * e.max() ** 2:
                continue  # a sliver: hatching it only makes a spike
            samples = (poly + c * 2) / 3
            dk = float(np.mean(tone.darkness(np.append(samples[:, 0], c[0]), np.append(samples[:, 1], c[1]))))
            if dk < 0.12:
                continue
            gap = spacing / dk ** 1.2
            ang = 45 + 90 * (int(c[0] * 7 + c[1] * 3) % 2)
            out += _hatch_poly(poly, gap, ang)
    return out


# --- single-stroke letters, traced from OpenCV's built-in Hershey font


_FONT, _SCALE, _THICK, _PAD = cv2.FONT_HERSHEY_SIMPLEX, 4.0, 4, 24


def _render(ch: str) -> np.ndarray:
    (tw, th), base = cv2.getTextSize(ch, _FONT, _SCALE, _THICK)
    img = np.zeros((160 + 2 * _PAD, tw + 2 * _PAD), np.uint8)
    cv2.putText(img, ch, (_PAD, _PAD + 120), _FONT, _SCALE, 255, _THICK, cv2.LINE_8)  # baseline at row PAD+120
    return img


@lru_cache(maxsize=1)
def _cap() -> tuple[float, float]:
    """Rows of the capital H's top and bottom edges (its height and the baseline)."""
    rows = np.flatnonzero(_render("H").any(axis=1))
    return float(rows[0]), float(rows[-1])


@lru_cache(maxsize=256)
def _glyph(ch: str) -> tuple[tuple[np.ndarray, ...], float]:
    """A character as strokes with its baseline at 0 and capital height 1. Returns (strokes, advance)."""
    if ch == " ":
        return (), 0.6
    top, bottom = _cap()
    cap = bottom - top
    img = _render(ch)
    rows, cols = img.shape
    lines = vz.centerlines(img > 0, cols, rows, min_len=8.0)
    strokes = []
    for l in lines:
        q = l.copy()
        q[:, 0] = (q[:, 0] - _PAD) / cap  # from the glyph's left edge
        q[:, 1] = (q[:, 1] - (rows - bottom - 0.5)) / cap  # from the baseline
        strokes.append(q)
    (tw, _), _ = cv2.getTextSize(ch, _FONT, _SCALE, _THICK)
    return tuple(strokes), (tw + 8) / cap


def _text_lines(text: str, x: float, y: float, height: float) -> tuple[Lines, float]:
    out: Lines = []
    for ch in text:
        strokes, adv = _glyph(ch)
        out += [s * height + [x, y] for s in strokes]
        x += adv * height
    return out, x


RAMP = " .:-=+*#%@"


@register(
    "text",
    "Text art",
    "The picture made of letters: either characters picked by how much ink they use (classic "
    "ASCII art), or your own words repeated, bolder and bigger where it's dark.",
    [
        Param("mode", "Letters", "choice", "your words", choices=["your words", "ink ramp"]),
        Param("words", "Your words", "text", "BAMBUPLOT ", help="Repeated through the picture",
              when={"mode": ["your words"]}),
        Param("size", "Letter height", "float", 3.5, 1.5, 20, 0.25, unit="mm"),
        Param("line_gap", "Line spacing", "float", 1.35, 1, 3, 0.05, unit="×"),
        Param("bold", "Boldest", "int", 4, 1, 6, 1, help="Pen passes per letter at full black",
              when={"mode": ["your words"]}),
        Param("min_dark", "Skip lighter than", "float", 0.08, 0, 0.6, 0.01, when={"mode": ["your words"]}),
    ],
    group=TILES,
)
def text(tone: Tone, ctx: Context, mode, words, size, line_gap, bold, min_dark) -> Lines:
    w, h = tone.w, tone.h
    words = (words or "BAMBUPLOT").replace("\n", " ") or "BAMBUPLOT"
    out: Lines = []
    row_h = size * line_gap
    k = 0
    y = h - size - 0.3
    while y >= 0.3:
        x = 0.3
        while True:
            ch = words[k % len(words)] if mode == "your words" else None
            cx, cy = x + size * 0.3, y + size * 0.5
            if cx > w:
                break
            dk = float(tone.darkness(min(cx, w), cy))
            if mode == "ink ramp":
                ch = RAMP[min(len(RAMP) - 1, int(dk * len(RAMP)))]
                strokes, adv = _glyph(ch)
                if x + adv * size > w:
                    break
                out += [s * size + [x, y] for s in strokes]
                x += max(_glyph(c)[1] for c in RAMP) * 0.85 * size  # monospaced: the grid stays even
                continue
            strokes, adv = _glyph(ch)
            if x + adv * size > w:
                break
            k += 1
            if dk >= min_dark and strokes:
                passes = 1 + int(round(dk * (bold - 1)))
                for p in range(passes):
                    off = ((p % 2) * ctx.pen_width * 0.7, (p // 2) * ctx.pen_width * 0.7)
                    out += [s * size + [x + off[0], y + off[1]] for s in strokes]
            x += adv * size
        y -= row_h
    return [l for l in out if len(l) >= 2 and l[:, 0].max() <= w and l[:, 1].max() <= h and l.min() >= 0]
