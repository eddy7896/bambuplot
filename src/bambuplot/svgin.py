"""Read an SVG file into strokes and filled shapes the plotter can draw.

Everything is flattened to polylines in the document's own pixel space
(96 per inch, y pointing down), with all transforms applied. The ``svg``
effect then scales the art into the drawing area.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from svgelements import SVG, Close, Image, Line, Move, Path, Shape, Text

PX_TO_MM = 25.4 / 96


class SvgError(ValueError):
    pass


@dataclass
class FillShape:
    rings: list[np.ndarray]  # closed rings, filled with the even-odd rule
    dark: bool  # False for white-ish fills, which erase what's under them


@dataclass
class SvgArt:
    strokes: list[np.ndarray] = field(default_factory=list)
    fills: list[FillShape] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def bbox(self) -> tuple[float, float, float, float]:
        pts = [s for s in self.strokes] + [r for f in self.fills for r in f.rings]
        if not pts:
            raise SvgError("The SVG has nothing to draw: no visible strokes or filled shapes.")
        allp = np.vstack(pts)
        (x0, y0), (x1, y1) = allp.min(axis=0), allp.max(axis=0)
        return float(x0), float(y0), float(x1), float(y1)

    def size_mm(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox()
        return (x1 - x0) * PX_TO_MM, (y1 - y0) * PX_TO_MM


def _paint(color) -> tuple[bool, bool]:
    """(is painted, is dark) for an svgelements Color or None."""
    if color is None or color.value is None:
        return False, False
    if color.alpha == 0:
        return False, False
    lum = 0.299 * color.red + 0.587 * color.green + 0.114 * color.blue
    return True, lum < 0.85 * 255


def _flatten(path: Path, step: float) -> list[np.ndarray]:
    """Split a path into subpaths and sample curves every ``step`` px."""
    out: list[np.ndarray] = []
    cur: list[np.ndarray] = []

    def flush():
        if len(cur) > 1:
            pts = np.vstack(cur)
            keep = np.ones(len(pts), bool)
            keep[1:] = np.any(np.abs(np.diff(pts, axis=0)) > 1e-9, axis=1)
            pts = pts[keep]
            if len(pts) > 1:
                out.append(pts)
        cur.clear()

    for seg in path:
        if isinstance(seg, Move):
            flush()
            cur.append(np.array([[seg.end.x, seg.end.y]]))
        elif isinstance(seg, (Line, Close)):
            if seg.start is None or seg.end is None:
                continue
            if not cur:
                cur.append(np.array([[seg.start.x, seg.start.y]]))
            cur.append(np.array([[seg.end.x, seg.end.y]]))
            if isinstance(seg, Close):
                flush()
        else:  # curves and arcs
            if seg.start is None:
                continue
            coarse = np.asarray(seg.npoint(np.linspace(0, 1, 9)), float)
            approx = float(np.hypot(*np.diff(coarse, axis=0).T).sum())
            n = int(np.clip(np.ceil(approx / step), 4, 2000))
            pts = np.asarray(seg.npoint(np.linspace(0, 1, n + 1)), float)
            if not cur:
                cur.append(pts[:1])
            cur.append(pts[1:])
    flush()
    return out


def load_svg(data: bytes) -> SvgArt:
    try:
        doc = SVG.parse(io.BytesIO(data), reify=True, ppi=96)
    except Exception as err:  # svgelements raises a variety of parser errors
        raise SvgError(f"That SVG couldn't be read ({type(err).__name__}).") from err

    shapes, text, images = [], 0, 0
    for el in doc.elements():
        if isinstance(el, Text):
            text += 1
        elif isinstance(el, Image):
            images += 1
        elif isinstance(el, Shape):
            if el.values.get("visibility") in ("hidden", "collapse"):
                continue
            shapes.append(el)

    # Sample curves finely relative to the whole drawing, so tiny icons scaled up stay smooth.
    probe = []
    for el in shapes:
        try:
            bb = abs(Path(el)).bbox()
        except Exception:
            bb = None
        if bb:
            probe.append(bb)
    if not probe:
        raise SvgError(_nothing_message(text, images))
    x0 = min(b[0] for b in probe); y0 = min(b[1] for b in probe)
    x1 = max(b[2] for b in probe); y1 = max(b[3] for b in probe)
    step = max(np.hypot(x1 - x0, y1 - y0) / 3000, 1e-6)

    art = SvgArt()
    for el in shapes:
        stroked, _ = _paint(el.stroke)
        filled, dark = _paint(el.fill)
        if not (stroked or filled):
            continue
        subpaths = _flatten(abs(Path(el)), step)
        if stroked:
            art.strokes.extend(subpaths)
        if filled:
            rings = [p if np.allclose(p[0], p[-1]) else np.vstack([p, p[:1]]) for p in subpaths if len(p) > 2]
            if rings:
                art.fills.append(FillShape(rings=rings, dark=dark))

    if not art.strokes and not art.fills:
        raise SvgError(_nothing_message(text, images))
    if text:
        art.warnings.append(f"{text} text element(s) skipped. In your editor, convert text to paths/outlines first.")
    if images:
        art.warnings.append(f"{images} embedded image(s) skipped. Upload the image itself to trace it.")
    return art


def _nothing_message(text: int, images: int) -> str:
    if text and not images:
        return "This SVG only contains text. Convert the text to paths/outlines in your editor first."
    if images:
        return "This SVG only contains embedded images. Upload the PNG or JPG itself to trace it."
    return "The SVG has nothing to draw: no visible strokes or filled shapes."
