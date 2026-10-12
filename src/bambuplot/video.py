"""Videos for flipbooks: keep the clip in the project and pull frames out of it.

A project's video lives in ``<project>/video/``:

- ``source.<ext>``: the clip as uploaded, so frames can be taken again at another rate or range.
- ``meta.json``: what the clip is (size, length, frame rate) and how frames were taken.
- ``frames/00001.jpg`` …: the extracted frames, numbered from 1.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field

from . import projects as proj

VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "avi", "mkv", "gif"}
MAX_FRAMES = 300
FRAME_SIDE = 1280  # longest side of a stored frame, px
SENTINEL = "video"  # a layer whose image_id is this uses the current video frame


class VideoError(ValueError):
    pass


class Extract(BaseModel):
    """Which frames to take from the clip."""

    start: float = Field(0.0, ge=0)  # seconds
    end: float | None = Field(None, ge=0)  # seconds; None = the end of the clip
    fps: float = Field(12.0, gt=0, le=60)  # frames taken per second of video
    max_frames: int = Field(48, ge=1, le=MAX_FRAMES)


def video_dir(pid: str) -> Path:
    return proj._dir(pid) / "video"


def _source(d: Path) -> Path | None:
    for p in sorted(d.glob("source.*")):
        return p
    return None


def is_video_name(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[-1].lower() in VIDEO_EXTS


def probe(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise VideoError("That video can't be read. Try an MP4 or MOV file.")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        ok, frame = cap.read()
        if not ok or frame is None:
            raise VideoError("That video has no frames BambuPlot can read. Try an MP4 or MOV file.")
        h, w = frame.shape[:2]  # after the clip's own rotation, which OpenCV applies
    finally:
        cap.release()
    if not (0 < fps < 1000):
        fps = 30.0
    if count <= 0:  # some containers don't say; count by reading
        count = _count_frames(path)
    return {"width": w, "height": h, "fps": round(fps, 3), "frames": count, "duration": round(count / fps, 3)}


def _count_frames(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


def frame_times(info: dict, ex: Extract) -> list[float]:
    """The moments (seconds) to take frames at."""
    dur = info["duration"]
    start = min(ex.start, max(0.0, dur - 1e-3))
    end = dur if ex.end is None else min(ex.end, dur)
    if end <= start:
        raise VideoError("The end of the range must be after its start.")
    step = 1.0 / ex.fps
    times = list(np.arange(start, end - 1e-9, step))[: ex.max_frames]
    return [float(t) for t in times] or [start]


def _resize(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    s = FRAME_SIDE / max(h, w)
    if s < 1:
        frame = cv2.resize(frame, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    return frame


def extract(pid: str, ex: Extract) -> dict:
    """Take frames from the project's clip into ``frames/``. Returns the new description."""
    d = video_dir(pid)
    meta = load_meta(pid)
    src = _source(d)
    if meta is None or src is None:
        raise VideoError("This project has no video. Choose one first.")
    times = frame_times(meta["clip"], ex)
    fps = meta["clip"]["fps"]
    wanted = [min(meta["clip"]["frames"] - 1, int(round(t * fps))) for t in times]
    tmp = d / "frames.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    cap = cv2.VideoCapture(str(src))
    got = 0
    try:
        # Read straight through (seeking is unreliable in many files) and keep the wanted frames.
        targets = iter(sorted(set(wanted)))
        nxt = next(targets, None)
        i = 0
        last = None
        while nxt is not None:
            ok = cap.grab()
            if not ok:
                break
            if i == nxt:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    last = _resize(frame)
                    got += 1
                    cv2.imwrite(str(tmp / f"{got:05d}.jpg"), last, [cv2.IMWRITE_JPEG_QUALITY, 92])
                nxt = next(targets, None)
            i += 1
    finally:
        cap.release()
    if not got:
        shutil.rmtree(tmp, ignore_errors=True)
        raise VideoError("No frames could be read from that part of the video.")
    shutil.rmtree(d / "frames", ignore_errors=True)
    tmp.replace(d / "frames")
    h, w = last.shape[:2]
    version = hashlib.sha1(json.dumps([meta["clip"], ex.model_dump(), got]).encode()).hexdigest()[:10]
    meta.update(extract=ex.model_dump(), count=got, frame_w=w, frame_h=h, version=version,
                times=[round(t, 3) for t in times[:got]])
    _write_meta(pid, meta)
    return describe(pid)


def save_upload(pid: str, tmp_path: Path, name: str, ex: Extract | None = None) -> dict:
    """Move an uploaded clip into the project, read it, and take the first frames."""
    d = video_dir(pid)
    try:
        info = probe(tmp_path)
    except VideoError:
        tmp_path.unlink(missing_ok=True)
        raise
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    ext = name.rsplit(".", 1)[-1].lower() if is_video_name(name) else "mp4"
    shutil.move(str(tmp_path), d / f"source.{ext}")
    _write_meta(pid, {"name": proj.clean_name(name, f"video.{ext}"), "clip": info})
    if ex is None:  # a sensible start: about 12 frames a second, up to 48 frames
        ex = Extract(fps=min(12.0, info["fps"]), max_frames=48)
    return extract(pid, ex)


def remove(pid: str) -> None:
    shutil.rmtree(video_dir(pid), ignore_errors=True)


def load_meta(pid: str) -> dict | None:
    p = video_dir(pid) / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def _write_meta(pid: str, meta: dict) -> None:
    p = video_dir(pid) / "meta.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(p)


def describe(pid: str) -> dict | None:
    meta = load_meta(pid)
    if meta is None or not meta.get("count"):
        return None
    return {"name": meta["name"], "clip": meta["clip"], "extract": meta.get("extract"), "count": meta["count"],
            "width": meta["frame_w"], "height": meta["frame_h"], "version": meta["version"],
            "times": meta.get("times", [])}


def frame_path(pid: str, index: int) -> Path:
    """Frame ``index`` (0-based)."""
    meta = load_meta(pid)
    if meta is None or not (0 <= index < meta.get("count", 0)):
        raise VideoError("That frame doesn't exist.")
    return video_dir(pid) / "frames" / f"{index + 1:05d}.jpg"


def frame_bytes(pid: str, index: int) -> bytes:
    return frame_path(pid, index).read_bytes()


def frame_id(pid: str, index: int) -> str:
    """An image id for a frame that changes when the frames are taken again."""
    meta = load_meta(pid) or {}
    return f"vf-{pid}-{meta.get('version', '0')}-{index}"


def parse_frame_id(image_id: str) -> tuple[str, int] | None:
    parts = image_id.split("-")
    if len(parts) != 4 or parts[0] != "vf":
        return None
    try:
        return parts[1], int(parts[3])
    except ValueError:
        return None
