"""Finishing touches added on top of any effect: a border and the user's signature.

The signature is drawn once (signature pad or traced from a photo) and stored
for all projects in ``~/.config/bambuplot/signature.json`` as strokes
normalized to a height of 1 (origin at the bottom-left, +Y up).
"""

from __future__ import annotations

import json
import time
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from . import printer
from .paths import rdp

Lines = list[np.ndarray]
MAX_STROKES = 400
MAX_POINTS = 20000


class Extras(BaseModel):
    border: bool = False
    border_inset: float = Field(0.0, ge=0, le=30)  # mm inside the drawing area's edge
    signature: bool = False
    sig_corner: Literal["br", "bl", "bc", "tr", "tl"] = "br"
    sig_height: float = Field(8.0, ge=3, le=60)  # mm
    sig_margin: float = Field(3.0, ge=0, le=40)  # mm from the area's edge (and from the border)
    sig_clear: bool = True  # remove artwork behind the signature so it stays readable


# ------------------------------------------------------------------ storage


def path():
    return printer.CONFIG_DIR / "signature.json"


def normalize(strokes) -> list[list[list[float]]]:
    """Clean and scale strokes to height 1, starting at (0, 0). Raises ValueError if empty."""
    clean = []
    total = 0
    for s in strokes or []:
        a = np.asarray(s, dtype=float)
        if a.ndim != 2 or a.shape[1] != 2 or len(a) == 0 or not np.isfinite(a).all():
            continue
        clean.append(a)
        total += len(a)
        if len(clean) > MAX_STROKES or total > MAX_POINTS:
            raise ValueError("That signature is too detailed. Draw it again a little simpler.")
    if not clean:
        raise ValueError("The signature is empty.")
    pts = np.vstack(clean)
    (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
    span = max(y1 - y0, (x1 - x0) / 20, 1e-9)  # guard against a flat line
    out = []
    for a in clean:
        n = (a - [x0, y0]) / span
        n = rdp(n, 0.004) if len(n) > 2 else n
        out.append([[round(float(x), 4), round(float(y), 4)] for x, y in n])
    return out


def load() -> list[np.ndarray] | None:
    p = path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return [np.asarray(s, float) for s in data["strokes"]] or None
    except (ValueError, KeyError, TypeError):
        return None


def info() -> dict:
    p = path()
    if not p.exists():
        return {"has": False, "strokes": [], "version": None}
    data = json.loads(p.read_text())
    return {"has": True, "strokes": data["strokes"], "version": data.get("updated")}


def save(strokes) -> dict:
    norm = normalize(strokes)
    printer.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path().with_suffix(".tmp")
    tmp.write_text(json.dumps({"strokes": norm, "updated": time.time()}))
    tmp.replace(path())
    return info()


def delete() -> None:
    path().unlink(missing_ok=True)


def trace_image(data: bytes) -> list[list[list[float]]]:
    """Turn a photo or scan of a signature into strokes (centerline trace)."""
    import cv2

    from .image import load_gray
    from .vectorize import centerlines, smooth, threshold

    gray = load_gray(data)
    rows, cols = gray.shape
    scale = 360 / max(rows, cols)
    if scale < 1:
        gray = cv2.resize(gray, (max(8, int(cols * scale)), max(8, int(rows * scale))), interpolation=cv2.INTER_AREA)
    rows, cols = gray.shape
    mask = threshold(smooth(gray, 0.8), None)
    if mask.mean() > 0.5:  # light ink on a dark background
        mask = ~mask
    # Crop to the ink so the signature fills the result.
    ys, xs = np.nonzero(mask)
    if len(xs) < 10:
        raise ValueError("No signature found in that picture. Use dark ink on white paper.")
    pad = 4
    mask = mask[max(0, ys.min() - pad):ys.max() + pad, max(0, xs.min() - pad):xs.max() + pad]
    h, w = mask.shape
    lines = centerlines(mask, float(w), float(h), min_len=4.0)
    return normalize(lines)


# ------------------------------------------------------------------ geometry


def _subtract_box(line: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> Lines:
    """The parts of a polyline outside a box."""
    if len(line) == 1:
        p = line[0]
        return [] if (x0 <= p[0] <= x1 and y0 <= p[1] <= y1) else [line]
    out: Lines = []
    cur: list[np.ndarray] = []

    def flush():
        nonlocal cur
        if len(cur) > 1:
            out.append(np.vstack(cur))
        cur = []

    for a, b in zip(line[:-1], line[1:]):
        d = b - a
        t0, t1 = 0.0, 1.0
        hit = True
        for p, q in ((-d[0], a[0] - x0), (d[0], x1 - a[0]), (-d[1], a[1] - y0), (d[1], y1 - a[1])):
            if abs(p) < 1e-12:
                if q < 0:
                    hit = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not hit or t0 >= t1:  # segment entirely outside the box
            if not cur:
                cur.append(a)
            cur.append(b)
            continue
        if t0 > 0:  # the part before entering
            if not cur:
                cur.append(a)
            cur.append(a + t0 * d)
        flush()
        if t1 < 1:  # the part after leaving
            cur = [a + t1 * d, b]
    flush()
    return out


def place_signature(strokes: list[np.ndarray], w: float, h: float, ex: Extras, inset: float = 0.0):
    """Scale and position the signature in the area. Returns (strokes, its bounding box)."""
    pts = np.vstack(strokes)
    nw = float(pts[:, 0].max())  # normalized width (height is 1)
    m = ex.sig_margin + inset
    height = ex.sig_height
    if nw * height > w - 2 * m:  # too wide for the area: shrink to fit
        height = max(1.0, (w - 2 * m) / max(nw, 1e-9))
    height = min(height, max(1.0, h - 2 * m))
    sw = nw * height
    x = {"bl": m, "tl": m, "br": w - m - sw, "tr": w - m - sw, "bc": (w - sw) / 2}[ex.sig_corner]
    y = h - m - height if ex.sig_corner in ("tr", "tl") else m
    placed = [np.column_stack([x + s[:, 0] * height, y + s[:, 1] * height]) for s in strokes]
    return placed, (x, y, x + sw, y + height)


def border_lines(w: float, h: float, inset: float) -> Lines:
    i = min(inset, w / 2 - 1, h / 2 - 1)
    return [np.array([[i, i], [w - i, i], [w - i, h - i], [i, h - i], [i, i]])]


def apply(lines: Lines, w: float, h: float, ex: Extras | None, signature: list[np.ndarray] | None,
          pen_width: float = 0.4) -> Lines:
    """Add the border and signature to an effect's lines (area coordinates)."""
    if ex is None or not (ex.border or (ex.signature and signature)):
        return lines
    out = list(lines)
    if ex.signature and signature:
        sig, (sx0, sy0, sx1, sy1) = place_signature(signature, w, h, ex, ex.border_inset if ex.border else 0.0)
        if ex.sig_clear:
            pad = 1.0 + pen_width
            cut: Lines = []
            for l in out:
                cut += _subtract_box(l, sx0 - pad, sy0 - pad, sx1 + pad, sy1 + pad)
            out = cut
        out += sig
    if ex.border:
        out += border_lines(w, h, ex.border_inset)
    return out
