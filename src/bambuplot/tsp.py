"""Open-path travelling-salesperson helpers: nearest-neighbor tour plus 2-opt."""

from __future__ import annotations

import time

import numpy as np
from scipy.spatial import cKDTree


def nn_tour(pts: np.ndarray, start: int = 0) -> np.ndarray:
    """Greedy nearest-neighbor visiting order."""
    n = len(pts)
    if n < 3:
        return np.arange(n)
    visited = np.zeros(n, bool)
    order = np.empty(n, int)
    ids = np.arange(n)
    tree = cKDTree(pts)
    cur = start
    visited[cur] = True
    order[0] = cur
    for step in range(1, n):
        found = -1
        for k in (8, 32):
            kk = min(k, len(ids))
            _, j = tree.query(pts[cur], k=kk)
            cand = ids[np.atleast_1d(j)]
            cand = cand[~visited[cand]]
            if cand.size:
                found = int(cand[0])
                break
        if found < 0:
            # Most near points are used up: rebuild the tree on what's left.
            ids = np.flatnonzero(~visited)
            tree = cKDTree(pts[ids])
            _, j = tree.query(pts[cur])
            found = int(ids[int(j)])
        visited[found] = True
        order[step] = found
        cur = found
    return order


def two_opt(pts: np.ndarray, order: np.ndarray, time_limit: float = 4.0) -> np.ndarray:
    """Improve an open path by reversing segments until no gain or time runs out."""
    n = len(order)
    if n < 4:
        return order
    order = order.copy()
    P = pts[order].copy()
    t0 = time.perf_counter()
    improved = True
    while improved:
        improved = False
        for i in range(n - 2):
            a, b = P[i], P[i + 1]
            c = P[i + 2 :]
            d = P[i + 3 :]
            ab = np.hypot(*(a - b))
            ac = np.hypot(c[:, 0] - a[0], c[:, 1] - a[1])
            gains = np.empty(len(c))
            gains[-1] = ab - ac[-1]  # reverse the whole tail
            if len(d):
                cd = np.hypot(*(c[:-1] - d).T)
                bd = np.hypot(d[:, 0] - b[0], d[:, 1] - b[1])
                gains[:-1] = ab + cd - ac[:-1] - bd
            k = int(np.argmax(gains))
            if gains[k] > 1e-9:
                j = i + 2 + k
                P[i + 1 : j + 1] = P[i + 1 : j + 1][::-1]
                order[i + 1 : j + 1] = order[i + 1 : j + 1][::-1]
                improved = True
            if time.perf_counter() - t0 > time_limit:
                return order
    return order


def path_length(pts: np.ndarray) -> float:
    if len(pts) < 2:
        return 0.0
    return float(np.hypot(*np.diff(pts, axis=0).T).sum())
