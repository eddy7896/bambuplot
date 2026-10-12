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


# ------------------------------------------------------------------ several pictures (layers)
# Each picture is stored as sources/<image id>.<ext>, its selection as sources/<image id>.mask.png.
# Image ids come from the file's content, so the same picture always gets the same id.


def image_id_for(data: bytes, kind: str) -> str:
    digest = hashlib.sha1(data).hexdigest()[:16]
    return f"svg-{digest}" if kind == "svg" else digest


def _ext(name: str, kind: str) -> str:
    ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "") or ("svg" if kind == "svg" else "png")
    return ext if ext in SOURCE_EXTS else ("svg" if kind == "svg" else "png")


def list_sources(pid: str) -> dict:
    """image id → info (name, kind, ext), including a single picture saved by an older version."""
    d = _dir(pid)
    meta = _meta(d)
    out = dict(meta.get("sources") or {})
    legacy = meta.get("source")
    f = _source_file(d)
    if legacy and f is not None:
        iid = image_id_for(f.read_bytes(), legacy.get("kind", "raster"))
        out.setdefault(iid, {"name": legacy.get("name", f.name), "kind": legacy.get("kind", "raster"),
                             "ext": f.suffix.lstrip("."), "legacy": True})
    return out


def source_bytes(pid: str, iid: str) -> tuple[bytes, dict] | None:
    d = _dir(pid)
    info = list_sources(pid).get(iid)
    if info is None:
        return None
    if info.get("legacy"):
        f = _source_file(d)
        return (f.read_bytes(), info) if f else None
    f = d / "sources" / f"{iid}.{info['ext']}"
    return (f.read_bytes(), info) if f.exists() else None


def source_mask(pid: str, iid: str) -> np.ndarray | None:
    d = _dir(pid)
    info = list_sources(pid).get(iid) or {}
    path = d / "mask.png" if info.get("legacy") else d / "sources" / f"{iid}.mask.png"
    if not path.exists():
        return None
    m = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
    return None if m is None else (m > 127).astype(np.uint8)


def set_sources(pid: str, items: dict, referenced: set[str]) -> None:
    """Store the pictures the layers use.

    ``items``: image id → (bytes, name, kind, mask or None) for pictures loaded right now.
    ``referenced``: every image id the layers point at; stored pictures still referenced are
    kept even if they aren't loaded (e.g. after a restart), the rest are removed.
    """
    d = _dir(pid)
    meta = _meta(d)
    known = dict(meta.get("sources") or {})
    sd = d / "sources"
    sd.mkdir(exist_ok=True)
    # Move an older single picture into the new layout first.
    legacy = meta.get("source")
    f = _source_file(d)
    if legacy and f is not None:
        data = f.read_bytes()
        iid = image_id_for(data, legacy.get("kind", "raster"))
        ext = _ext(legacy.get("name", ""), legacy.get("kind", "raster"))
        (sd / f"{iid}.{ext}").write_bytes(data)
        if (d / "mask.png").exists():
            (d / "mask.png").replace(sd / f"{iid}.mask.png")
        known[iid] = {"name": legacy.get("name", f.name), "kind": legacy.get("kind", "raster"), "ext": ext}
        f.unlink()
        meta["source"] = None
    new = {}
    for iid in referenced:
        if iid in items:
            data, name, kind, mask = items[iid]
            ext = _ext(name, kind)
            path = sd / f"{iid}.{ext}"
            if not path.exists():
                path.write_bytes(data)
            new[iid] = {"name": clean_name(name, f"picture.{ext}"), "kind": kind, "ext": ext, "bytes": len(data)}
            mpath = sd / f"{iid}.mask.png"
            if mask is None:
                mpath.unlink(missing_ok=True)
            else:
                ok, buf = cv2.imencode(".png", (mask > 0).astype(np.uint8) * 255)
                if ok and (not mpath.exists() or mpath.read_bytes() != buf.tobytes()):
                    mpath.write_bytes(buf.tobytes())
        elif iid in known:
            new[iid] = known[iid]
    for iid, info in known.items():
        if iid not in new:
            (sd / f"{iid}.{info.get('ext', 'png')}").unlink(missing_ok=True)
            (sd / f"{iid}.mask.png").unlink(missing_ok=True)
    meta["sources"] = new
    _write_json(d / "project.json", meta)
