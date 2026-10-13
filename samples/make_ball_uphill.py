"""Make samples/ball-uphill.mp4: a red-and-white ball rolls in, climbs a hill and stops on top.

    uv run python samples/make_ball_uphill.py

A clear, high-contrast clip for trying BambuPlot's flipbook mode.
"""

from pathlib import Path

import cv2
import numpy as np

W, H, FPS, SECONDS = 640, 480, 24, 4
S = 2  # render at 2x, then shrink, for smooth edges
OUT = Path(__file__).with_name("ball-uphill.mp4")

X0, Y0 = 120, 400  # foot of the slope (screen y points down)
X1, Y1 = 470, 190  # top of the slope
R = 34  # ball radius

ANG = np.arctan2(Y0 - Y1, X1 - X0)
LENGTH = np.hypot(X1 - X0, Y1 - Y0)
UX, UY = np.cos(ANG), -np.sin(ANG)  # uphill along the slope
NX, NY = -np.sin(ANG), -np.cos(ANG)  # out of the slope's surface


def centre(s: float) -> tuple[float, float]:
    """Ball centre after rolling s px: flat ground (s < 0), the slope, then the flat top."""
    if s < 0:
        return X0 + s, Y0 - R
    if s <= LENGTH:
        return X0 + UX * s + NX * R, Y0 + UY * s + NY * R
    return X1 + (s - LENGTH), Y1 - R


def contact(s: float) -> tuple[float, float, float]:
    """Where the ball touches the ground, and the ground's angle (degrees)."""
    cx, cy = centre(s)
    if 0 <= s <= LENGTH:
        return cx - NX * R, cy - NY * R, -np.degrees(ANG)
    return cx, cy + R, 0.0


def main():
    start, end = -110.0, LENGTH + 70
    v0 = 2 * (end - start) / SECONDS  # slows evenly, coming to rest at the end
    dist = lambda t: start + v0 * t - 0.5 * (v0 / SECONDS) * t * t  # noqa: E731
    P = lambda x, y: (int(round(x * S)), int(round(y * S)))  # noqa: E731
    w, h = W * S, H * S
    sky = np.empty((h, w, 3), np.uint8)
    for k, (top, bottom) in enumerate(zip((238, 228, 214), (250, 246, 240))):  # BGR
        sky[..., k] = np.linspace(top, bottom, h)[:, None]
    hill = np.array([P(0, Y0), P(X0, Y0), P(X1, Y1), P(W, Y1), P(W, H), P(0, H)], np.int32)

    out = cv2.VideoWriter(str(OUT), cv2.VideoWriter_fourcc(*"avc1"), FPS, (W, H))
    if not out.isOpened():
        out = cv2.VideoWriter(str(OUT), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(FPS * SECONDS):
        s = dist(i / FPS)
        img = sky.copy()
        cv2.circle(img, P(540, 80), 30 * S, (150, 200, 245), -1, cv2.LINE_AA)  # sun
        cv2.fillPoly(img, [hill], (95, 125, 110), cv2.LINE_AA)
        cv2.polylines(img, [hill[:4]], False, (55, 75, 65), 3 * S, cv2.LINE_AA)
        for gx in range(10, W, 26):  # grass ticks
            gy = Y0 if gx < X0 else Y1 if gx > X1 else Y0 + (Y1 - Y0) * (gx - X0) / (X1 - X0)
            cv2.line(img, P(gx, gy + 14), P(gx + 6, gy + 26), (70, 100, 85), 2 * S, cv2.LINE_AA)
        sx, sy, sa = contact(s)
        cv2.ellipse(img, P(sx, sy), (int(R * 0.9 * S), 5 * S), sa, 0, 360, (60, 80, 70), -1, cv2.LINE_AA)
        cx, cy = centre(s)
        c, turn = P(cx, cy), np.degrees((s - start) / R)  # rolling without slipping
        cv2.circle(img, c, R * S, (40, 45, 190), -1, cv2.LINE_AA)
        for q in (0, 180):  # two white quarters show it turning
            cv2.ellipse(img, c, (R * S, R * S), turn + q, 0, 90, (245, 245, 245), -1, cv2.LINE_AA)
        cv2.circle(img, c, R * S, (25, 30, 110), 2 * S, cv2.LINE_AA)
        out.write(cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA))
    out.release()
    print(f"Wrote {OUT} ({FPS * SECONDS} frames, {SECONDS} s)")


if __name__ == "__main__":
    main()
