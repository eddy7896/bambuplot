"""Preview SVG of a plan on the plate, in millimeters."""

from __future__ import annotations

import numpy as np

from .layout import BED, Layout


def to_svg(layout: Layout, lines_pen: list[np.ndarray]) -> str:
    paper, area = layout.paper.poly(), layout.area.poly()
    pts = np.vstack([BED.poly(), paper])
    x0, y0 = pts.min(axis=0) - 5
    x1, y1 = pts.max(axis=0) + 5
    W, H = x1 - x0, y1 - y0

    def poly(p, style):
        coords = " ".join(f"{x - x0:.2f},{y1 - y:.2f}" for x, y in p)
        return f'<polygon points="{coords}" style="{style}"/>'

    parts = []
    for l in lines_pen:
        if len(l) == 1:  # a dot: draw a tiny segment so the round cap shows
            l = np.vstack([l, l + [0.01, 0]])
        parts.append("M" + " L".join(f"{x - x0:.2f},{y1 - y:.2f}" for x, y in l))
    path = " ".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.2f} {H:.2f}" '
        f'width="{W:.2f}mm" height="{H:.2f}mm">'
        + poly(BED.poly(), "fill:#2c2d29")
        + poly(paper, "fill:#fdfdf9;stroke:#c3c6be;stroke-width:.4")
        + poly(area, "fill:none;stroke:#2246c8;stroke-width:.3;stroke-dasharray:2 1")
        + f'<path d="{path}" style="fill:none;stroke:#1c2c8a;stroke-width:{layout.pen.width_mm};'
        'stroke-linecap:round;stroke-linejoin:round"/></svg>'
    )
