"""Projects saved on this computer.

Each project is a folder under ``~/.config/bambuplot/projects/<id>/``:

- ``project.json``: name, timestamps and the editor state (layout, effect,
  effect settings, tone settings and crop).
- ``source.<ext>``: the original picture or SVG, byte for byte.
- ``mask.png``: the subject selection, if any (white = kept).
- ``thumb.png``: a small rendering of the plot for the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from . import printer

ID_RE = re.compile(r"^[a-f0-9]{12}$")
THUMB_W = 360
SOURCE_EXTS = {"png", "jpg", "jpeg", "webp", "svg"}


class ProjectError(ValueError):
    pass


def root() -> Path:
    return printer.CONFIG_DIR / "projects"


def _dir(pid: str) -> Path:
    if not ID_RE.match(pid or ""):
        raise ProjectError("That isn't a valid project id.")
    d = root() / pid
    if not (d / "project.json").exists():
        raise ProjectError("That project doesn't exist any more.")
    return d


def _now() -> float:
    return time.time()  # full precision, so projects made back to back still sort by age


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic, so a crash mid-save can't leave half a file


def _meta(d: Path) -> dict:
    return json.loads((d / "project.json").read_text())


def _summary(d: Path, meta: dict) -> dict:
    return {
        "id": meta["id"],
        "name": meta["name"],
        "created": meta["created"],
        "updated": meta["updated"],
        "effect": (meta.get("state") or {}).get("effect"),
        "source": meta.get("source"),
        "has_thumb": (d / "thumb.png").exists(),
    }


def clean_name(name: str | None, fallback: str = "Untitled") -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())[:80]
    return name or fallback


# ------------------------------------------------------------------ listing


def list_projects() -> list[dict]:
    out = []
    r = root()
    if not r.exists():
        return out
    for d in r.iterdir():
        if not (d / "project.json").exists():
            continue
        try:
            out.append(_summary(d, _meta(d)))
        except (ValueError, KeyError):
            continue  # skip a damaged project rather than breaking the dashboard
    out.sort(key=lambda p: p["updated"], reverse=True)
    return out


def _unique_name(base: str) -> str:
    taken = {p["name"] for p in list_projects()}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


# ------------------------------------------------------------------ CRUD


def create(name: str | None = None, state: dict | None = None) -> dict:
    pid = uuid.uuid4().hex[:12]
    d = root() / pid
    d.mkdir(parents=True, exist_ok=False)
    meta = {"id": pid, "name": _unique_name(clean_name(name)), "created": _now(), "updated": _now(),
            "state": state or {}, "source": None}
    _write_json(d / "project.json", meta)
    return _summary(d, meta)


def load(pid: str) -> dict:
    d = _dir(pid)
    meta = _meta(d)
    return {**_summary(d, meta), "state": meta.get("state") or {}}


def rename(pid: str, name: str) -> dict:
    d = _dir(pid)
    meta = _meta(d)
    meta["name"] = clean_name(name, meta["name"])
    meta["updated"] = _now()
    _write_json(d / "project.json", meta)
    return _summary(d, meta)


def save_state(pid: str, state: dict, name: str | None = None) -> dict:
    d = _dir(pid)
    meta = _meta(d)
    meta["state"] = state
    if name is not None:
        meta["name"] = clean_name(name, meta["name"])
    meta["updated"] = _now()
    _write_json(d / "project.json", meta)
    return _summary(d, meta)


def delete(pid: str) -> None:
    shutil.rmtree(_dir(pid))


def duplicate(pid: str) -> dict:
    src = _dir(pid)
    meta = _meta(src)
    new_id = uuid.uuid4().hex[:12]
    dst = root() / new_id
    shutil.copytree(src, dst)
    meta.update(id=new_id, name=_unique_name(f"{meta['name']} copy"), created=_now(), updated=_now())
    _write_json(dst / "project.json", meta)
    return _summary(dst, meta)


# ------------------------------------------------------------------ source, mask, thumbnail


def _source_file(d: Path) -> Path | None:
    for p in d.glob("source.*"):
        return p
    return None


def source(pid: str) -> tuple[bytes, dict] | None:
    d = _dir(pid)
    meta = _meta(d)
    f = _source_file(d)
    if f is None or not meta.get("source"):
        return None
    return f.read_bytes(), meta["source"]


def set_source(pid: str, data: bytes | None, name: str = "", kind: str = "raster") -> bool:
    """Store the original file. Returns True if it changed."""
    d = _dir(pid)
    meta = _meta(d)
    if data is None:
        if meta.get("source") is None:
            return False
        for p in d.glob("source.*"):
            p.unlink()
        (d / "mask.png").unlink(missing_ok=True)
        meta["source"] = None
    else:
        digest = hashlib.sha1(data).hexdigest()
        if (meta.get("source") or {}).get("sha1") == digest:
            return False
        for p in d.glob("source.*"):
            p.unlink()
        (d / "mask.png").unlink(missing_ok=True)
        ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "") or ("svg" if kind == "svg" else "png")
        ext = ext if ext in SOURCE_EXTS else ("svg" if kind == "svg" else "png")
        (d / f"source.{ext}").write_bytes(data)
        meta["source"] = {"name": clean_name(name, f"picture.{ext}"), "kind": kind, "sha1": digest, "bytes": len(data)}
    meta["updated"] = _now()
    _write_json(d / "project.json", meta)
    return True


def set_mask(pid: str, mask: np.ndarray | None) -> None:
    d = _dir(pid)
    path = d / "mask.png"
    if mask is None:
        path.unlink(missing_ok=True)
        return
    ok, buf = cv2.imencode(".png", (mask > 0).astype(np.uint8) * 255)
    if ok and (not path.exists() or path.read_bytes() != buf.tobytes()):
        path.write_bytes(buf.tobytes())


def mask(pid: str) -> np.ndarray | None:
    path = _dir(pid) / "mask.png"
    if not path.exists():
        return None
    m = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
    return None if m is None else (m > 127).astype(np.uint8)


def render_thumb(pid: str, lines: list[np.ndarray], w: float, h: float, pen_mm: float = 0.4) -> None:
    """Draw the plot (area mm, Y up) as a small dark-on-paper PNG."""
    d = _dir(pid)
    tw = THUMB_W
    th = max(40, int(round(tw * h / max(w, 1e-6))))
    th = min(th, tw * 2)
    s = min(tw / w, th / h)
    img = np.full((th, tw, 3), (249, 249, 246), np.uint8)
    width = max(1, int(round(pen_mm * s)))
    ox, oy = (tw - w * s) / 2, (th - h * s) / 2
    for l in lines:
        pts = np.column_stack([ox + l[:, 0] * s, oy + (h - l[:, 1]) * s])
        pts = np.round(pts * 4).astype(np.int32)  # 2 fractional bits for smooth lines
        if len(pts) == 1:
            cv2.circle(img, tuple(int(v) for v in pts[0]), max(1, width) * 4 // 2 + 2, (138, 44, 28), -1, cv2.LINE_AA, 2)
        else:
            cv2.polylines(img, [pts], False, (138, 44, 28), width, cv2.LINE_AA, 2)
    ok, buf = cv2.imencode(".png", img)
    if ok:
        (d / "thumb.png").write_bytes(buf.tobytes())


def thumb_path(pid: str) -> Path | None:
    p = _dir(pid) / "thumb.png"
    return p if p.exists() else None
