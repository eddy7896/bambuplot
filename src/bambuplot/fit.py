"""Find the largest safe drawing area at the area's current rotation.

The free space (inside the paper margin, on the plate, within pen reach and
clear of every clip) is sampled on a grid aligned with the area's own axes.
The largest all-free rectangle of cells is found with the classic
histogram-stack method, shrunk by one cell for safety, then verified exactly.
"""

from __future__ import annotations

import numpy as np

from .layout import Area, Layout, contains, inside, overlaps, rotation

STEP = 1.0  # mm grid


def _free_grid(layout: Layout):
    keep_in, keep_out = layout.allowed_polys()
    paper = layout.paper.poly()
    origin = paper.mean(axis=0)
    span = np.hypot(*(paper.max(0) - paper.min(0))) / 2 + 20
    k = np.arange(-span, span + STEP, STEP)
    uu, vv = np.meshgrid(k, k)  # rows = v (along area height), cols = u (along area width)
    local = np.column_stack([uu.ravel(), vv.ravel()])
    world = local @ rotation(layout.area.rotation).T + origin
    free = np.ones(len(world), bool)
    for poly in keep_in:
        free &= inside(poly, world)
    for poly in keep_out:
        free &= ~inside(poly, world)
    return free.reshape(uu.shape), k, origin


def _largest_rect(free: np.ndarray):
    """(area, row_top, row_bottom, col_left, col_right) of the largest all-True rectangle."""
    rows, cols = free.shape
    heights = np.zeros(cols, int)
    best = (0, 0, 0, 0, 0)
    for r in range(rows):
        heights = np.where(free[r], heights + 1, 0)
        stack: list[int] = []
        for c in range(cols + 1):
            h = heights[c] if c < cols else 0
            while stack and heights[stack[-1]] >= h:
                top = stack.pop()
                height = heights[top]
                left = stack[-1] + 1 if stack else 0
                area = height * (c - left)
                if area > best[0]:
                    best = (area, r - height + 1, r, left, c - 1)
            stack.append(c)
    return best


def _area_from_local(layout: Layout, u0: float, u1: float, v0: float, v1: float, origin) -> Area:
    w, h = u1 - u0, v1 - v0
    center = np.array([(u0 + u1) / 2, (v0 + v1) / 2]) @ rotation(layout.area.rotation).T + origin
    return Area(x=float(center[0] - w / 2), y=float(center[1] - h / 2), w=float(w), h=float(h),
                rotation=layout.area.rotation)


def _is_safe(layout: Layout, a: Area) -> bool:
    keep_in, keep_out = layout.allowed_polys()
    p = a.poly()
    return all(contains(k, p) for k in keep_in) and not any(overlaps(k, p) for k in keep_out)


def fit_area(layout: Layout) -> Area | None:
    """Largest safe area at the current rotation, or None if there's no room."""
    free, k, origin = _free_grid(layout)
    size, r0, r1, c0, c1 = _largest_rect(free)
    if size == 0:
        return None
    # Cell centers are free; pull in by one cell so the true edges are too.
    u0, u1 = k[c0] + STEP, k[c1] - STEP
    v0, v1 = k[r0] + STEP, k[r1] - STEP
    u0, u1 = np.ceil(u0), np.floor(u1)
    v0, v1 = np.ceil(v0), np.floor(v1)
    for _ in range(6):
        if u1 - u0 < 10 or v1 - v0 < 10:
            return None
        a = _area_from_local(layout, u0, u1, v0, v1, origin)
        if _is_safe(layout, a):
            return a
        u0, u1, v0, v1 = u0 + 0.5, u1 - 0.5, v0 + 0.5, v1 - 0.5
    return None


def center_area(layout: Layout) -> Area | None:
    """Keep the current size and rotation; move it to the middle of the free space."""
    best = fit_area(layout)
    if best is None:
        return None
    a = layout.area
    cx, cy = best.center
    moved = Area(x=cx - a.w / 2, y=cy - a.h / 2, w=a.w, h=a.h, rotation=a.rotation)
    return moved if _is_safe(layout, moved) else None
