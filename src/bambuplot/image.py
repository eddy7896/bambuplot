"""Raster pre-processing: load, grayscale, crop to the drawing area, adjust tone."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, field_validator
from scipy.ndimage import map_coordinates


def _clean_crop(v):
    if v is None:
        return None
    try:
        u0, v0, u1, v1 = (min(1.0, max(0.0, float(x))) for x in v)
    except (TypeError, ValueError):
        return None
    u0, u1 = sorted((u0, u1))
    v0, v1 = sorted((v0, v1))
    if u1 - u0 < 0.01 or v1 - v0 < 0.01 or (u0 <= 0 and v0 <= 0 and u1 >= 1 and v1 >= 1):
        return None  # too small to use, or the whole picture
    return [u0, v0, u1, v1]


class Prep(BaseModel):
    clahe: bool = True
    clahe_clip: float = Field(2.0, gt=0)
    contrast: float = Field(1.0, ge=0)
    brightness: float = Field(0.0, ge=-1, le=1)
    gamma: float = Field(1.0, gt=0)
    invert: bool = False
    paper_white: float = Field(0.0, ge=0, le=0.5)  # darkness below this becomes pure white
    use_mask: bool = True  # apply the subject selection / background removal
    crop: list[float] | None = None  # (u0, v0, u1, v1) of the original picture, 0–1, before turning
    fit: Literal["fill", "fit"] = "fill"  # fill the area (trim edges) or fit inside it (blank margins)

    @field_validator("crop", mode="before")
    @classmethod
    def _crop(cls, v):
        return _clean_crop(v)
    mask_feather: float = Field(0.0, ge=0, le=5)  # mm of softening at the selection edge
    rotate: Literal[0, 90, 180, 270] = 0  # clockwise, applied before cropping
    px_per_mm: float = Field(4.0, gt=0, le=20)


@dataclass
class Tone:
    """Brightness field over the drawing area: 0 = black, 1 = white.

    Row 0 of ``data`` is the back edge of the area (largest Y).
    """

    data: np.ndarray
    w: float
    h: float

    def sample(self, x, y) -> np.ndarray:
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        rows_n, cols_n = self.data.shape
        cols = np.clip(x / self.w, 0, 1) * (cols_n - 1)
        rows = np.clip(1 - y / self.h, 0, 1) * (rows_n - 1)
        vals = map_coordinates(self.data, [rows.ravel(), cols.ravel()], order=1, mode="nearest")
        return vals.reshape(x.shape)

    def darkness(self, x, y) -> np.ndarray:
        return 1.0 - self.sample(x, y)

    def grid(self, px_per_mm: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Pixel centers (mm) and darkness, resampled to ``px_per_mm``."""
        nx = max(2, int(round(self.w * px_per_mm)))
        ny = max(2, int(round(self.h * px_per_mm)))
        xs = (np.arange(nx) + 0.5) * self.w / nx
        ys = (np.arange(ny) + 0.5) * self.h / ny
        gx, gy = np.meshgrid(xs, ys)
        return gx.ravel(), gy.ravel(), self.darkness(gx, gy).ravel()


def load_gray(src: bytes | str | Path) -> np.ndarray:
    """Load an image as float32 grayscale in 0..1, honoring EXIF rotation and alpha."""
    img = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    return np.asarray(img.convert("L"), dtype=np.float32) / 255.0


def cover_crop(gray: np.ndarray, aspect: float) -> np.ndarray:
    """Center-crop so width / height == aspect."""
    h, w = gray.shape
    if w / h > aspect:
        nw = max(1, int(round(h * aspect)))
        x0 = (w - nw) // 2
        return gray[:, x0 : x0 + nw]
    nh = max(1, int(round(w / aspect)))
    y0 = (h - nh) // 2
    return gray[y0 : y0 + nh, :]


def crop_norm(a: np.ndarray, crop: list[float] | None) -> np.ndarray:
    """Cut a normalized (u0, v0, u1, v1) box out of an image array."""
    if not crop:
        return a
    rows, cols = a.shape[:2]
    c0, c1 = int(round(crop[0] * cols)), max(int(round(crop[2] * cols)), int(round(crop[0] * cols)) + 2)
    r0, r1 = int(round(crop[1] * rows)), max(int(round(crop[3] * rows)), int(round(crop[1] * rows)) + 2)
    return a[r0:min(r1, rows), c0:min(c1, cols)]


def pad_to(a: np.ndarray, aspect: float, value: float) -> np.ndarray:
    """Pad evenly so width / height == aspect (the opposite of cover_crop)."""
    rows, cols = a.shape[:2]
    if cols / rows < aspect:
        extra = int(round(rows * aspect)) - cols
        return np.pad(a, ((0, 0), (extra // 2, extra - extra // 2)), constant_values=value)
    extra = int(round(cols / aspect)) - rows
    return np.pad(a, ((extra // 2, extra - extra // 2), (0, 0)), constant_values=value)


def sample_image(cols: int, rows: int, w: float, h: float) -> np.ndarray:
    """A shaded sphere with a soft shadow, used when no image is loaded."""
    m = min(w, h)
    u = (np.arange(cols) + 0.5) / cols
    v = (np.arange(rows) + 0.5) / rows
    uu, vv = np.meshgrid(u, v)
    X = (uu - 0.5) * w / m
    Y = (0.46 - vv) * h / m
    r = 0.36
    d = np.hypot(X, Y) / r
    light = np.array([-0.5, 0.6, 0.62])
    light /= np.linalg.norm(light)
    z = np.sqrt(np.clip(1 - d**2, 0, 1))
    dot = (X / r) * light[0] + (Y / r) * light[1] + z * light[2]
    sphere = 0.1 + 0.86 * np.clip(dot, 0, 1)

    bg = 0.93 - 0.2 * vv
    sx, sy = (X + 0.07) / 0.4, (Y + 0.42) / 0.08
    q = sx**2 + sy**2
    bg = np.where(q < 1, bg - 0.5 * (1 - q), bg)
    return np.clip(np.where(d < 1, sphere, bg), 0, 1).astype(np.float32)


def prepare(gray: np.ndarray | None, w: float, h: float, prep: Prep | None = None,
            mask: np.ndarray | None = None) -> Tone:
    """Fit the image to a w × h mm area and apply the tone adjustments.

    ``mask`` (same shape as ``gray``, 1 = keep, 0 = removed) turns removed areas
    paper-white after every adjustment, so no effect puts ink there.
    """
    prep = prep or Prep()
    cols = max(8, int(round(w * prep.px_per_mm)))
    rows = max(8, int(round(h * prep.px_per_mm)))
    keep = None

    if gray is None:
        data = sample_image(cols, rows, w, h)
    else:
        use_mask = mask is not None and prep.use_mask
        gray = crop_norm(gray, prep.crop)
        mask = crop_norm(mask, prep.crop) if use_mask else None
        if prep.rotate:
            k = -(prep.rotate // 90)
            gray = np.rot90(gray, k=k)
            mask = np.rot90(mask, k=k) if use_mask else None
        if prep.fit == "fit":  # whole picture inside the area, blank (and unselected) margins
            if not use_mask:
                mask, use_mask = np.ones(gray.shape, np.float32), True
            gray = pad_to(gray, w / h, 1.0)
            mask = pad_to(np.asarray(mask, np.float32), w / h, 0.0)
        crop = cover_crop(gray, w / h)
        data = cv2.resize(np.ascontiguousarray(crop), (cols, rows), interpolation=cv2.INTER_AREA)
        if use_mask:
            keep = cv2.resize(np.ascontiguousarray(cover_crop(mask, w / h), dtype=np.float32), (cols, rows),
                              interpolation=cv2.INTER_AREA)
            if prep.mask_feather > 0:
                keep = cv2.GaussianBlur(keep, (0, 0), prep.mask_feather * prep.px_per_mm)
            keep = np.clip(keep, 0, 1)
            data = data * keep + (1 - keep)  # so local contrast doesn't react to the removed background
        if prep.clahe:
            u8 = np.clip(data * 255, 0, 255).astype(np.uint8)
            tiles = max(2, min(8, cols // 16))
            clahe = cv2.createCLAHE(clipLimit=prep.clahe_clip, tileGridSize=(tiles, tiles))
            data = clahe.apply(u8).astype(np.float32) / 255.0

    data = (data - 0.5) * prep.contrast + 0.5 + prep.brightness
    data = np.clip(data, 0, 1) ** prep.gamma
    if prep.invert:
        data = 1.0 - data
    if prep.paper_white > 0:  # leave near-white areas blank instead of faint marks
        data = np.where(1.0 - data < prep.paper_white, 1.0, data)
    if keep is not None:  # last, so invert or brightness can't put ink back on removed areas
        data = data * keep + (1 - keep)
    return Tone(data=data.astype(np.float32), w=w, h=h)
