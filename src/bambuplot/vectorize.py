"""Raster-to-vector building blocks shared by the trace and SVG effects.

Masks are boolean arrays covering the drawing area, row 0 at the back
(largest Y), like ``Tone.data``. Output polylines are in area millimeters.
"""

from __future__ import annotations

import cv2
import numpy as np
from skimage.measure import find_contours
from skimage.morphology import skeletonize

Lines = list[np.ndarray]


def px_to_mm(rows_cols: np.ndarray, shape: tuple[int, int], w: float, h: float) -> np.ndarray:
    """(row, col) pixel coordinates (centers at +0.5) → area mm, Y up."""
    rows, cols = shape
    r, c = rows_cols[:, 0], rows_cols[:, 1]
    return np.column_stack([(c + 0.5) * w / cols, h - (r + 0.5) * h / rows])


def mm_to_px(pts: np.ndarray, shape: tuple[int, int], w: float, h: float) -> np.ndarray:
    """Area mm → (x, y) pixel coordinates for OpenCV drawing."""
    rows, cols = shape
    return np.column_stack([pts[:, 0] * cols / w - 0.5, (h - pts[:, 1]) * rows / h - 0.5])


def threshold(gray: np.ndarray, level: float | None) -> np.ndarray:
    """Dark-pixel mask. ``level`` is a brightness 0–1; None picks one automatically (Otsu)."""
    if level is None:
        u8 = np.clip(gray * 255, 0, 255).astype(np.uint8)
        t, _ = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        level = t / 255
    return gray < level


def smooth(gray: np.ndarray, sigma_px: float) -> np.ndarray:
    if sigma_px <= 0:
        return gray
    return cv2.GaussianBlur(gray, (0, 0), sigma_px)


def clean_mask(mask: np.ndarray, min_area_px: float) -> np.ndarray:
    """Drop specks and fill pinholes smaller than ``min_area_px``."""
    if min_area_px <= 0:
        return mask
    out = mask.copy()
    for target in (True, False):
        m = (out == target).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        small = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] < min_area_px) + 1
        if small.size:
            out[np.isin(labels, small)] = not target
    return out


def outlines(mask: np.ndarray, w: float, h: float) -> Lines:
    """Closed outlines around every dark region (and its holes), in mm."""
    u8 = np.pad(mask.astype(np.uint8), 1)  # pad so regions touching the edge close properly
    contours, _ = cv2.findContours(u8, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        if len(c) < 3:
            continue
        xy = c[:, 0, :].astype(float) - 1  # undo the pad
        rc = np.column_stack([xy[:, 1], xy[:, 0]])
        pts = px_to_mm(rc, mask.shape, w, h)
        out.append(np.vstack([pts, pts[:1]]))
    return out


def iso_lines(gray: np.ndarray, levels: list[float], w: float, h: float) -> Lines:
    """Lines of equal brightness (like a topographic map)."""
    padded = np.pad(gray, 1, mode="edge")
    out = []
    for lv in levels:
        for c in find_contours(padded, lv):
            if len(c) > 2:
                out.append(px_to_mm(c - 1, gray.shape, w, h))
    return out


def hatch(mask: np.ndarray, w: float, h: float, spacing: float, angle: float, min_len: float = 0.0) -> Lines:
    """Parallel lines filling the True part of ``mask``."""
    rows, cols = mask.shape
    step = 0.5 * min(w / cols, h / rows)
    r = np.hypot(w, h) / 2
    t = np.arange(-r, r + step, step)
    offs = np.arange(-r + spacing / 2, r, spacing)
    a = np.radians(angle)
    d = np.array([np.cos(a), np.sin(a)])
    n = np.array([-np.sin(a), np.cos(a)])
    X = w / 2 + t[None, :] * d[0] + offs[:, None] * n[0]
    Y = h / 2 + t[None, :] * d[1] + offs[:, None] * n[1]
    inside = (X >= 0) & (X <= w) & (Y >= 0) & (Y <= h)
    ci = np.clip((X * cols / w).astype(int), 0, cols - 1)
    ri = np.clip(((h - Y) * rows / h).astype(int), 0, rows - 1)
    on = inside & mask[ri, ci]
    out = []
    pad = np.zeros((len(offs), 1), bool)
    edges = np.diff(np.hstack([pad, on, pad]).astype(np.int8), axis=1)
    for k in range(len(offs)):
        starts = np.flatnonzero(edges[k] == 1)
        ends = np.flatnonzero(edges[k] == -1) - 1
        for s, e in zip(starts, ends):
            p0 = np.array([X[k, s], Y[k, s]])
            p1 = np.array([X[k, e], Y[k, e]])
            if np.hypot(*(p1 - p0)) >= max(min_len, 1e-9):
                out.append(np.vstack([p0, p1]))
    return out


def rasterize_rings(rings_mm: list[np.ndarray], shape: tuple[int, int], w: float, h: float) -> np.ndarray:
    """Fill closed rings with the even-odd rule."""
    out = np.zeros(shape, bool)
    for ring in rings_mm:
        m = np.zeros(shape, np.uint8)
        px = np.round(mm_to_px(ring, shape, w, h) * 16).astype(np.int32)  # 4 fractional bits
        cv2.fillPoly(m, [px], 1, lineType=cv2.LINE_8, shift=4)
        out ^= m.astype(bool)
    return out


# --------------------------------------------------------------- centerline


_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def centerlines(mask: np.ndarray, w: float, h: float, min_len: float) -> Lines:
    """Trace the one-pixel skeleton of ``mask`` into polylines."""
    skel = skeletonize(mask)
    rows, cols = skel.shape
    sk = np.pad(skel, 1)
    cache: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def nbrs(p):
        # A diagonal neighbor only counts when no straight step already reaches it,
        # otherwise every staircase corner would look like a junction.
        if p not in cache:
            r, c = p
            cache[p] = [
                (r + dr, c + dc) for dr, dc in _NB
                if sk[r + dr, c + dc] and not (dr and dc and (sk[r + dr, c] or sk[r, c + dc]))
            ]
        return cache[p]

    used: set[tuple] = set()

    def key(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start, nxt):
        path = [start, nxt]
        used.add(key(start, nxt))
        prev, cur = start, nxt
        while len(nbrs(cur)) == 2 and cur != start:
            step = [q for q in nbrs(cur) if q != prev and key(cur, q) not in used]
            if not step:
                break
            prev, cur = cur, step[0]
            used.add(key(prev, cur))
            path.append(cur)
        return path

    pixels = [(int(r), int(c)) for r, c in zip(*np.nonzero(sk))]
    # Line ends and junctions first, so strokes run end to end; leftovers are closed loops.
    pixels.sort(key=lambda p: len(nbrs(p)) == 2)
    paths = [walk(p, q) for p in pixels for q in nbrs(p) if key(p, q) not in used]

    out = []
    for path in paths:
        line = px_to_mm(np.array(path, float) - 1, (rows, cols), w, h)  # -1 undoes the pad
        if np.hypot(*np.diff(line, axis=0).T).sum() >= min_len:
            out.append(line)
    return out
