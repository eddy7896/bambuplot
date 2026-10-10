"""Path clean-up and ordering: clamp, simplify, sort to cut pen-up travel, merge."""

from __future__ import annotations

import numpy as np

from .layout import Layout

Lines = list[np.ndarray]


def clamp(lines: Lines, w: float, h: float) -> Lines:
    """Hard safety net: no point may leave the drawing area."""
    out = []
    for line in lines:
        line = np.asarray(line, float)
        if line.ndim != 2 or len(line) == 0:
            continue
        out.append(np.column_stack([np.clip(line[:, 0], 0, w), np.clip(line[:, 1], 0, h)]))
    return out


def rdp(points: np.ndarray, tol: float) -> np.ndarray:
    """Ramer–Douglas–Peucker simplification (iterative)."""
    n = len(points)
    if n < 3 or tol <= 0:
        return points
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = points[i], points[j]
        seg = b - a
        mid = points[i + 1 : j]
        L = np.hypot(*seg)
        if L < 1e-12:
            d = np.hypot(*(mid - a).T)
        else:
            d = np.abs(seg[0] * (mid[:, 1] - a[1]) - seg[1] * (mid[:, 0] - a[0])) / L
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return points[keep]


def order_paths(lines: Lines, start=(0.0, 0.0)) -> Lines:
    """Greedy nearest-next ordering; open paths may be drawn in either direction."""
    n = len(lines)
    if n <= 1:
        return list(lines)
    S = np.array([l[0] for l in lines])
    E = np.array([l[-1] for l in lines])
    left = np.ones(n, bool)
    pos = np.asarray(start, float)
    out = []
    for _ in range(n):
        ds = np.hypot(S[:, 0] - pos[0], S[:, 1] - pos[1])
        de = np.hypot(E[:, 0] - pos[0], E[:, 1] - pos[1])
        ds[~left] = np.inf
        de[~left] = np.inf
        i_s, i_e = int(np.argmin(ds)), int(np.argmin(de))
        if ds[i_s] <= de[i_e]:
            i, line = i_s, lines[i_s]
        else:
            i, line = i_e, lines[i_e][::-1]
        left[i] = False
        out.append(line)
        pos = line[-1]
    return out


def merge(lines: Lines, tol: float) -> Lines:
    """Join consecutive paths whose gap is within ``tol`` so the pen stays down."""
    if not lines:
        return []
    out = [lines[0]]
    for line in lines[1:]:
        prev = out[-1]
        if len(prev) > 1 and len(line) > 1 and np.hypot(*(prev[-1] - line[0])) <= tol:
            out[-1] = np.concatenate([prev, line[1:]])
        else:
            out.append(line)
    return out


def stats(lines: Lines, layout: Layout, start=(0.0, 0.0)) -> dict:
    """Ink length, travel, pen lifts and a rough time estimate (ignores acceleration)."""
    draw = sum(float(np.hypot(*np.diff(l, axis=0).T).sum()) for l in lines if len(l) > 1)
    travel = 0.0
    pos = np.asarray(start, float)
    for l in lines:
        travel += float(np.hypot(*(l[0] - pos)))
        pos = l[-1]
    lifts = len(lines)
    f = layout.feed
    seconds = (
        draw / (f.draw / 60)
        + travel / (f.travel / 60)
        + lifts * 2 * (layout.z.hop + layout.z.press) / (f.z / 60)
    )
    return {
        "paths": lifts,
        "points": int(sum(len(l) for l in lines)),
        "draw_m": round(draw / 1000, 2),
        "travel_m": round(travel / 1000, 2),
        "pen_lifts": lifts,
        "minutes": round(seconds / 60, 1),
    }
