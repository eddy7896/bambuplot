"""Thumbnails for project files: what the printer screen, Bambu Connect and Bambu Studio show.

Bambu uses 512 × 512 plate images and a 128 × 128 small one. The plate image
can show the plot, the source picture, or the picture with the plot over it.
"""

from __future__ import annotations

import cv2
import numpy as np

PAPER = (246, 249, 249)  # BGR, warm white
PLATE = (41, 45, 44)


def _hex_bgr(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return b, g, r


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else b""


def _draw(img, lines, to_px, color, width):
    for l in lines:
        pts = np.round(to_px(np.asarray(l, float)) * 4).astype(np.int32)  # 2 fractional bits
        if len(pts) == 1:
            cv2.circle(img, tuple(int(v) for v in pts[0]), max(4, width * 2), color, -1, cv2.LINE_AA, 2)
        else:
            cv2.polylines(img, [pts], False, color, width, cv2.LINE_AA, 2)


def artwork(groups: list[tuple[str, list]], w: float, h: float, picture: np.ndarray | None = None,
            mode: str = "plot", size: int = 512, pen_mm: float = 0.4) -> np.ndarray:
    """The drawing area on paper, centered in a transparent square.

    ``groups``: (hex colour, lines in area mm). ``picture``: the source as a 0–1 brightness
    array covering the area (row 0 at the back), used by the "picture" and "both" modes.
    """
    pad = int(size * 0.06)
    s = min((size - 2 * pad) / w, (size - 2 * pad) / h)
    pw, ph = int(round(w * s)), int(round(h * s))
    ox, oy = (size - pw) // 2, (size - ph) // 2
    img = np.zeros((size, size, 4), np.uint8)
    paper = np.empty((ph, pw, 3), np.uint8)
    paper[:] = PAPER
    if picture is not None and mode in ("picture", "both"):
        pic = cv2.resize(np.clip(picture, 0, 1).astype(np.float32), (pw, ph), interpolation=cv2.INTER_AREA)
        strength = 1.0 if mode == "picture" else 0.35
        shade = (1 - strength * (1 - pic))[..., None]
        paper = (paper.astype(np.float32) * shade).astype(np.uint8)
    canvas = np.ascontiguousarray(paper)
    if mode != "picture":
        width = max(1, int(round(pen_mm * s * 1.2)))
        to_px = lambda p: np.column_stack([p[:, 0] * s, ph - p[:, 1] * s])  # noqa: E731
        for color, lines in groups:
            _draw(canvas, lines, to_px, _hex_bgr(color), width)
    img[oy:oy + ph, ox:ox + pw, :3] = canvas
    img[oy:oy + ph, ox:ox + pw, 3] = 255
    cv2.rectangle(img, (ox, oy), (ox + pw - 1, oy + ph - 1), (200, 204, 204, 255), 1)
    return img


def plate_top(layout, groups_pen: list[tuple[str, list]], size: int = 512) -> np.ndarray:
    """Top view of the whole plate: plate, sheet, drawing (pen coordinates)."""
    bw, bh = layout.bed.w, layout.bed.h
    s = size / max(bw, bh)
    img = np.zeros((size, size, 4), np.uint8)
    to_px = lambda p: np.column_stack([p[:, 0] * s, (bh - p[:, 1]) * s])  # noqa: E731
    cv2.rectangle(img, (0, 0), (int(bw * s) - 1, int(bh * s) - 1), (*PLATE, 255), -1)
    paper = np.round(to_px(layout.paper.poly()) * 4).astype(np.int32)
    cv2.fillPoly(img, [paper], (*PAPER, 255), cv2.LINE_AA, 2)
    width = max(1, int(round(layout.pen.width_mm * s)))
    for color, lines in groups_pen:
        _draw(img, lines, to_px, (*_hex_bgr(color), 255), width)
    return img


def thumbnails(groups: list[tuple[str, list]], groups_pen: list[tuple[str, list]], layout,
               picture: np.ndarray | None, mode: str) -> dict[str, bytes]:
    a = layout.area
    plate = artwork(groups, a.w, a.h, picture, mode, 512, layout.pen.width_mm)
    small = cv2.resize(plate, (128, 128), interpolation=cv2.INTER_AREA)
    top = plate_top(layout, groups_pen)
    pick = np.zeros((512, 512, 4), np.uint8)
    return {"plate": _png(plate), "small": _png(small), "top": _png(top), "pick": _png(pick)}
