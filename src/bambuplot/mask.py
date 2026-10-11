"""Subject selection, eraser and background removal for raster sources.

The mask lives on a working-size color copy of the picture (longest side
``WORK_MAX`` px) and is 1 where the picture is kept, 0 where it's removed.
Removed areas are turned paper-white before any effect sees the picture, so
no ink goes there.

Tools:
- ``auto``: remove a plain background by color, or fall back to GrabCut.
- ``select``: GrabCut from a box drawn around the subject.
- ``paint``: erase or restore along brush strokes, either exactly or as
  "smart" hints that GrabCut uses to find the real edge.
- ``invert``, ``grow``, ``reset``, ``undo``.

Coordinates from the UI are normalized: (0, 0) is the picture's top-left
corner and (1, 1) its bottom-right, before any rotation or cropping.
"""

from __future__ import annotations

import base64
import io
import threading
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps

WORK_MAX = 800
HISTORY = 25
GC_BGD, GC_FGD, GC_PR_BGD, GC_PR_FGD = cv2.GC_BGD, cv2.GC_FGD, cv2.GC_PR_BGD, cv2.GC_PR_FGD


class MaskError(ValueError):
    pass


@dataclass
class MaskState:
    rgb: np.ndarray  # working-size color picture, uint8 (h, w, 3)
    mask: np.ndarray | None = None  # uint8 0/1 at working size; None = nothing removed
    labels: np.ndarray | None = None  # GrabCut labels, kept so smart brushes can refine
    bgd: np.ndarray = field(default_factory=lambda: np.zeros((1, 65), np.float64))
    fgd: np.ndarray = field(default_factory=lambda: np.zeros((1, 65), np.float64))
    history: list = field(default_factory=list)
    version: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.rgb.shape[:2]
        return w, h

    def _push(self):
        self.history.append((None if self.mask is None else self.mask.copy(),
                             None if self.labels is None else self.labels.copy()))
        del self.history[:-HISTORY]

    def _done(self, mask: np.ndarray | None):
        self.mask = None if mask is None else _tidy(mask.astype(np.uint8))
        self.version += 1

    def kept_fraction(self) -> float:
        return 1.0 if self.mask is None else float(self.mask.mean())


# ------------------------------------------------------------------ loading


def load_rgb(data: bytes) -> np.ndarray:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    img = img.convert("RGB")
    img.thumbnail((WORK_MAX, WORK_MAX), Image.LANCZOS)
    return np.asarray(img, np.uint8).copy()


def alpha_mask(data: bytes) -> np.ndarray | None:
    """If the file already has transparency (a cut-out PNG), use it as the mask."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode not in ("RGBA", "LA") and not (img.mode == "P" and "transparency" in img.info):
        return None
    a = img.convert("RGBA")
    a.thumbnail((WORK_MAX, WORK_MAX), Image.LANCZOS)
    alpha = np.asarray(a, np.uint8)[:, :, 3]
    if alpha.min() > 250:
        return None
    return (alpha > 127).astype(np.uint8)


# ------------------------------------------------------------------ helpers


def _tidy(mask: np.ndarray) -> np.ndarray:
    """Drop specks smaller than 0.05% of the picture, both kept and removed."""
    min_area = max(4, int(mask.size * 0.0005))
    out = mask.copy()
    for target in (1, 0):
        m = (out == target).astype(np.uint8)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        small = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] < min_area) + 1
        if small.size:
            out[np.isin(lab, small)] = 1 - target
    return out


def _border(a: np.ndarray, frac: float = 0.02) -> np.ndarray:
    h, w = a.shape[:2]
    b = max(2, int(min(h, w) * frac))
    m = np.zeros((h, w), bool)
    m[:b], m[-b:], m[:, :b], m[:, -b:] = True, True, True, True
    return m


def _grabcut(state: MaskState, labels: np.ndarray, rect=None, iters: int = 5) -> np.ndarray:
    bgr = cv2.cvtColor(state.rgb, cv2.COLOR_RGB2BGR)
    mode = cv2.GC_INIT_WITH_RECT if rect is not None else cv2.GC_INIT_WITH_MASK
    try:
        cv2.grabCut(bgr, labels, rect, state.bgd, state.fgd, iters, mode)
    except cv2.error as err:
        raise MaskError("Couldn't separate the subject here. Try a box that fits the subject more closely.") from err
    state.labels = labels
    return ((labels == GC_FGD) | (labels == GC_PR_FGD)).astype(np.uint8)


def _labels_from_mask(mask: np.ndarray) -> np.ndarray:
    return np.where(mask > 0, GC_PR_FGD, GC_PR_BGD).astype(np.uint8)


# -------------------------------------------------------------------- tools


def auto(state: MaskState) -> str:
    """Remove the background automatically. Returns the method used."""
    with state.lock:
        state._push()
        lab = cv2.cvtColor(state.rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        border = _border(lab)
        edge_px = lab[border]
        bg = np.median(edge_px, axis=0)
        spread = float(np.percentile(np.linalg.norm(edge_px - bg, axis=1), 90))
        if spread < 18:  # a plain, even background: remove what matches it and touches the edge
            dist = np.linalg.norm(lab - bg, axis=2)
            cand = (dist < max(12.0, spread * 1.5 + 6)).astype(np.uint8)
            n, cc = cv2.connectedComponents(cand, connectivity=4)
            touching = np.unique(cc[border & (cand > 0)])
            background = np.isin(cc, touching[touching > 0])
            keep = (~background).astype(np.uint8)
            state.labels = _labels_from_mask(keep)
            state.bgd[:] = 0
            state.fgd[:] = 0
            method = "plain background"
        else:  # a busy photo: assume the subject is inside a small margin and let GrabCut decide
            h, w = lab.shape[:2]
            mx, my = max(1, int(w * 0.04)), max(1, int(h * 0.04))
            state.bgd[:] = 0
            state.fgd[:] = 0
            keep = _grabcut(state, np.zeros((h, w), np.uint8), (mx, my, w - 2 * mx, h - 2 * my))
            method = "photo"
        if keep.mean() < 0.01:
            state.history.pop()
            raise MaskError("No subject found. Draw a box around it with Select subject instead.")
        state._done(keep)
        return method


def select(state: MaskState, rect: list[float]) -> None:
    """GrabCut from a box: (u0, v0, u1, v1), normalized."""
    w, h = state.size
    u0, v0, u1, v1 = rect
    x0, x1 = sorted((int(round(u0 * w)), int(round(u1 * w))))
    y0, y1 = sorted((int(round(v0 * h)), int(round(v1 * h))))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise MaskError("Draw a bigger box around the subject.")
    with state.lock:
        state._push()
        state.bgd[:] = 0
        state.fgd[:] = 0
        keep = _grabcut(state, np.zeros((h, w), np.uint8), (x0, y0, x1 - x0, y1 - y0))
        state._done(keep)


def paint(state: MaskState, strokes: list[dict], smart: bool = False) -> None:
    """Erase or restore along strokes: [{"mode": "erase"|"keep", "points": [[u, v]...], "size": u}]."""
    w, h = state.size
    with state.lock:
        state._push()
        mask = (np.ones((h, w), np.uint8) if state.mask is None else state.mask.copy())
        labels = state.labels.copy() if state.labels is not None else _labels_from_mask(mask)
        for s in strokes:
            keep = s.get("mode") == "keep"
            pts = np.array(s.get("points") or [], float)
            if pts.ndim != 2 or len(pts) == 0:
                continue
            px = np.round(pts * [w, h]).astype(np.int32)
            th = max(1, int(round(float(s.get("size", 0.02)) * w)))
            for target, value in ((mask, 1 if keep else 0), (labels, GC_FGD if keep else GC_BGD)):
                if len(px) == 1:
                    cv2.circle(target, tuple(int(v) for v in px[0]), max(1, th // 2), int(value), -1)
                else:
                    cv2.polylines(target, [px], False, int(value), th, lineType=cv2.LINE_8)
                    for p in (px[0], px[-1]):
                        cv2.circle(target, tuple(int(v) for v in p), max(1, th // 2), int(value), -1)
        if smart:
            if not ((labels == GC_FGD) | (labels == GC_PR_FGD)).any() or not (
                (labels == GC_BGD) | (labels == GC_PR_BGD)
            ).any():
                state._done(mask)  # nothing to separate yet: behave like an exact brush
                state.labels = labels
                return
            mask = _grabcut(state, labels, None, iters=3)
        else:
            state.labels = labels
        state._done(mask)


def invert(state: MaskState) -> None:
    with state.lock:
        state._push()
        mask = np.ones(state.rgb.shape[:2], np.uint8) if state.mask is None else state.mask
        new = 1 - mask
        state.labels = None if state.labels is None else _labels_from_mask(new)
        state._done(new)


def grow(state: MaskState, px: int) -> None:
    """Grow (+) or shrink (−) the kept area by ``px`` working pixels."""
    if state.mask is None or px == 0:
        return
    with state.lock:
        state._push()
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * abs(px) + 1, 2 * abs(px) + 1))
        op = cv2.dilate if px > 0 else cv2.erode
        new = op(state.mask, k)
        state.labels = _labels_from_mask(new)
        state._done(new)


def set_mask(state: MaskState, mask: np.ndarray) -> None:
    with state.lock:
        state._push()
        h, w = state.rgb.shape[:2]
        m = cv2.resize(mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
        state.labels = _labels_from_mask(m)
        state._done(m)


def reset(state: MaskState) -> None:
    with state.lock:
        state._push()
        state.labels = None
        state._done(None)


def undo(state: MaskState) -> bool:
    with state.lock:
        if not state.history:
            return False
        state.mask, state.labels = state.history.pop()
        state.version += 1
        return True


# ------------------------------------------------------------------- output


def full_mask(state: MaskState, shape: tuple[int, int]) -> np.ndarray | None:
    """The mask scaled to a full-resolution picture of ``shape`` (rows, cols), soft-edged 0–1."""
    if state.mask is None:
        return None
    rows, cols = shape
    return cv2.resize(state.mask.astype(np.float32), (cols, rows), interpolation=cv2.INTER_LINEAR)


def _data_url(img: np.ndarray, fmt: str) -> str:
    ok, buf = cv2.imencode("." + fmt, img, [cv2.IMWRITE_JPEG_QUALITY, 85] if fmt == "jpg" else [])
    mime = "image/jpeg" if fmt == "jpg" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.tobytes()).decode()


def mask_png(state: MaskState) -> str | None:
    return None if state.mask is None else _data_url(state.mask * 255, "png")


def preview_jpeg(state: MaskState) -> str:
    """The picture with removed areas turned white, for the plate preview."""
    rgb = state.rgb if state.mask is None else np.where(state.mask[..., None] > 0, state.rgb, 255).astype(np.uint8)
    return _data_url(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), "jpg")


def describe(state: MaskState, method: str | None = None) -> dict:
    w, h = state.size
    return {
        "version": state.version,
        "has_mask": state.mask is not None,
        "kept": round(state.kept_fraction(), 3),
        "mask": mask_png(state),
        "preview": preview_jpeg(state),
        "can_undo": bool(state.history),
        "method": method,
        "work_size": [w, h],
    }
