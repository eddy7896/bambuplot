"""FastAPI backend for the web UI. Run with `bambuplot serve`."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import effects
from . import __version__, printer
from . import extras as extras_mod
from . import machine as mach
from . import projects as proj
from .calibrate import DEFAULT_PRESSES, alignment_correction, alignment_test, press_ladder, smoke_test
from .fit import center_area, fit_area
from .gcode import UnsafeLayout
from . import mask as masking
from .image import Prep, load_gray
from .layout import PAPER_LABELS, PAPER_SIZES, PRINTERS, Layout
from .paths import stats
from . import thumbs as thumbs_mod
from .gcode import SCREEN_MESSAGES, Screen, compile_groups, job_seconds
from .image import prepare
from .layers import PENS, Layer, frame_size, group_in_order, pen_label, place_lines
from .layout import Area
from .paths import order_paths
from .pipeline import Plan, make_plan, plan_to_gcode, touch_in_area
from . import package
from .package import TemplateError
from .safety import GcodeSafetyError
from .svgin import SvgArt, SvgError, load_svg
from . import flipbook as fbk
from . import video
from .flipbook import Flipbook
from .layers import Place

WEB = Path(__file__).parent / "web"
MAX_IMAGES = 16
MAX_PLANS = 8
MAX_UPLOAD = 25 * 1024 * 1024
MAX_VIDEO = 1024 * 1024 * 1024
MAX_FRAMES_LOADED = 12
MAX_FRAME_PLANS = 600

app = FastAPI(title="BambuPlot")
app.mount("/static", StaticFiles(directory=WEB), name="static")
_images: OrderedDict[str, object] = OrderedDict()  # id -> RasterSource or SvgSource


@dataclass
class RasterSource:
    gray: np.ndarray  # full-resolution grayscale, 0–1
    state: masking.MaskState  # working-size color copy plus the subject mask
    data: bytes = b""  # the original file, so it can be saved into a project
    name: str = ""


@dataclass
class SvgSource:
    art: SvgArt
    data: bytes = b""
    name: str = ""
_plans: OrderedDict[str, Plan] = OrderedDict()
_frames: OrderedDict[str, "RasterSource"] = OrderedDict()  # video frames, loaded from the project on demand
_frame_plans: OrderedDict[str, list] = OrderedDict()  # flipbook pages: per-layer lines for one frame


class PlanRequest(BaseModel):
    layout: Layout = Field(default_factory=Layout)
    effect: str = "waves"
    params: dict = Field(default_factory=dict)
    prep: Prep = Field(default_factory=Prep)
    image_id: str | None = None
    seed: int = 0
    extras: extras_mod.Extras = Field(default_factory=extras_mod.Extras)
    layers: list[Layer] = Field(default_factory=list)  # empty = one layer from the fields above
    project: str | None = None  # needed for layers that use the project's video
    flipbook: Flipbook = Field(default_factory=Flipbook)


@dataclass
class Composite:
    layers: list  # (layer id, pen, lines) in area coordinates, in drawing order
    groups: list  # (pen, lines): what gets plotted, one group per pen
    stats: dict
    sheets: list | None = None  # flipbooks: each group's sheet number
    flip: dict | None = None  # flipbooks: what the job covers


def _source(image_id: str):
    """A loaded picture, or a video frame read from its project."""
    src = _images.get(image_id)
    if src is not None:
        return src
    if image_id in _frames:
        _frames.move_to_end(image_id)
        return _frames[image_id]
    ref = video.parse_frame_id(image_id)
    if ref is None:
        return None
    try:
        data = video.frame_bytes(*ref)
    except (video.VideoError, proj.ProjectError, OSError):
        return None
    state = masking.MaskState(rgb=masking.load_rgb(data))
    keep = video.frame_mask(*ref)
    if keep is not None:  # the video's background removal
        masking.set_mask(state, keep)
        state.history.clear()
    src = RasterSource(gray=load_gray(data), state=state, name=f"frame {ref[1] + 1}")
    _remember(_frames, image_id, src, MAX_FRAMES_LOADED)
    return src


def _uses_video(req: PlanRequest) -> bool:
    if req.layers:
        return any(l.image_id == video.SENTINEL and l.visible for l in req.layers)
    return req.image_id == video.SENTINEL


def _video_count(req: PlanRequest) -> int:
    if not req.project:
        raise HTTPException(422, "Open the project again: its video isn't available.")
    try:
        meta = video.describe(req.project)
    except proj.ProjectError as err:
        raise HTTPException(404, str(err))
    if not meta:
        raise HTTPException(404, "This project's video is missing. Choose it again in the Picture tab.")
    return meta["count"]


def _at_frame(req: PlanRequest, index: int) -> PlanRequest:
    """The request with "the video" swapped for one of its frames."""
    if not _uses_video(req):
        return req
    count = _video_count(req)
    fid = video.frame_id(req.project, max(0, min(index, count - 1)))
    if req.layers:
        layers = [l.model_copy(update={"image_id": fid}) if l.image_id == video.SENTINEL else l for l in req.layers]
        return req.model_copy(update={"layers": layers})
    return req.model_copy(update={"image_id": fid})


def _remember(store: OrderedDict, key: str, value, limit: int):
    store[key] = value
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)


def _plan(req: PlanRequest) -> Plan:
    if req.effect not in effects.EFFECTS:
        raise HTTPException(422, f"Unknown effect '{req.effect}'")
    gray = svg = full = None
    mask_version = None
    if req.image_id:
        src = _source(req.image_id)
        if src is None:
            raise HTTPException(404, "That image is no longer loaded. Upload it again.")
        if isinstance(src, SvgSource):
            svg = src.art
        else:
            gray = src.gray
            if src.state.mask is not None and req.prep.use_mask:
                mask_version = src.state.version
    # Paths are area-local, so moving the area doesn't need a new plan; only
    # its size, the pen width, the touch-off corner and the inputs matter.
    lay = req.layout
    key = hashlib.sha1(json.dumps({
        "effect": req.effect,
        "params": effects.EFFECTS[req.effect].coerce(req.params),
        "prep": req.prep.model_dump(),
        "image": req.image_id,
        "size": [lay.area.w, lay.area.h],
        "pen": lay.pen.width_mm,
        "touch": lay.touch_off,
        "seed": req.seed,
        "mask": mask_version,
        "extras": req.extras.model_dump(),
        "signature": extras_mod.info()["version"] if req.extras.signature else None,
    }, sort_keys=True).encode()).hexdigest()
    if key in _plans:
        _plans.move_to_end(key)
        plan = _plans[key]
    else:
        if mask_version is not None:
            full = masking.full_mask(src.state, gray.shape)
        try:
            sig = extras_mod.load() if req.extras.signature else None
            plan = make_plan(lay, req.effect, req.params, req.prep, gray, req.seed, svg=svg, mask=full,
                             extras=req.extras, signature=sig)
        except (ValueError, SvgError) as err:
            raise HTTPException(422, str(err))
        _remember(_plans, key, plan, MAX_PLANS)
    # Stats depend on feeds and Z, which aren't part of the cache key.
    return Plan(lines=plan.lines, stats=stats(plan.lines, lay, touch_in_area(lay)))


def _layer_lines(req: PlanRequest, layer: Layer) -> list:
    """One layer's lines in drawing-area coordinates."""
    a = req.layout.area
    fw, fh = frame_size(layer.place, a.w, a.h)
    lay = req.layout.model_copy(deep=True)
    lay.area = Area(x=a.x, y=a.y, w=fw, h=fh)  # the effect fills the layer's own frame
    sub = PlanRequest(layout=lay, effect=layer.effect, params=layer.params, prep=layer.prep,
                      image_id=layer.image_id, seed=req.seed)
    return place_lines(_plan(sub).lines, layer.place, a.w, a.h)


def _compose(req: PlanRequest) -> Composite:
    """Every visible layer, plus the border and signature, grouped by pen."""
    lay, a = req.layout, req.layout.area
    layers = req.layers or [Layer(id="l1", effect=req.effect, params=req.params, prep=req.prep, image_id=req.image_id)]
    sig = extras_mod.load() if req.extras.signature else None
    per = []
    for layer in layers:
        if not layer.visible:
            continue
        lines = extras_mod.clear_behind(_layer_lines(req, layer), a.w, a.h, req.extras, sig, lay.pen.width_mm)
        per.append((layer.id, layer.pen, lines))
    finish = extras_mod.finishing_lines(a.w, a.h, req.extras, sig)
    if finish:  # border and signature: after the first pen's layers, with that pen (no extra pen change)
        drawn = [i for i, (_, _, l) in enumerate(per) if l]
        at, pen = len(per), 1
        if drawn:
            pen = per[drawn[0]][1]
            at = next((i for i in drawn if per[i][1] != pen), len(per))
        per.insert(at, ("finishing", pen, finish))
    # Layers plot in order, each one finished before the next; travel is optimized within a layer only.
    start = touch_in_area(lay)
    ordered = []
    pos = start
    for _, pen, lines in per:
        lines = order_paths(lines, pos) if lines else []
        if lines:
            pos = tuple(lines[-1][-1])
        ordered.append((pen, lines))
    groups = group_in_order(ordered)
    flat = [l for _, ls in groups for l in ls]
    st = stats(flat, lay, start)
    st["pens"] = len({pen for pen, _ in groups})
    st["pen_changes"] = max(0, len(groups) - 1)
    return Composite(layers=per, groups=groups, stats=st)


# ------------------------------------------------------------------ flipbooks


def _visible_layers(req: PlanRequest) -> list[Layer]:
    layers = req.layers or [Layer(id="l1", effect=req.effect, params=req.params, prep=req.prep, image_id=req.image_id)]
    return [l for l in layers if l.visible]


def _runs(layers: list[Layer]) -> tuple[list[int], list[int]]:
    """Neighbouring layers with the same pen form a run. Returns (each layer's run, each run's pen)."""
    run_of, pens = [], []
    for l in layers:
        if not pens or pens[-1] != l.pen:
            pens.append(l.pen)
        run_of.append(len(pens) - 1)
    return run_of, pens or [1]


def _page(req: PlanRequest, art_w: float, art_h: float, index: int, signed: bool) -> tuple[list, list]:
    """One frame drawn on one page: each visible layer's lines, and the border/signature lines (page-art mm)."""
    lay = req.layout.model_copy(deep=True)
    lay.area = Area(x=0, y=0, w=art_w, h=art_h)
    ex = req.extras if signed else req.extras.model_copy(update={"signature": False})
    r = _at_frame(req, index).model_copy(update={"layout": lay, "extras": ex})
    layers = _visible_layers(r)
    masks = {}
    for l in layers:
        src = _images.get(l.image_id) if l.image_id else None
        if isinstance(src, RasterSource) and src.state.mask is not None:
            masks[l.image_id] = src.state.version
    key = hashlib.sha1(json.dumps({
        "layers": [l.model_dump() for l in layers], "extras": ex.model_dump(), "size": [art_w, art_h],
        "pen": lay.pen.width_mm, "touch": lay.touch_off, "seed": r.seed, "masks": masks,
        "sig": extras_mod.info()["version"] if ex.signature else None,
    }, sort_keys=True, default=str).encode()).hexdigest()
    if key in _frame_plans:
        _frame_plans.move_to_end(key)
        return _frame_plans[key]
    sig = extras_mod.load() if ex.signature else None
    per = [extras_mod.clear_behind(_layer_lines(r, l), art_w, art_h, ex, sig, lay.pen.width_mm) for l in layers]
    finish = extras_mod.finishing_lines(art_w, art_h, ex, sig)
    _remember(_frame_plans, key, (per, finish), MAX_FRAME_PLANS)
    return per, finish


def _flip_sheet(req: PlanRequest, slots: list[tuple[int, int]], last_frame: int, marks_for_all: bool = True) -> list:
    """One sheet: (slot, frame) pairs → (pen, lines) groups in area mm, page by page within each pen run."""
    fb, lay, a = req.flipbook, req.layout, req.layout.area
    cl = fbk.cells(fb, a.w, a.h)
    layers = _visible_layers(req)
    run_of, run_pens = _runs(layers)
    runs: list[list] = [[] for _ in run_pens]
    pos = touch_in_area(lay)

    def add(k, lines):
        nonlocal pos
        lines = order_paths(lines, pos) if lines else []
        if lines:
            pos = tuple(lines[-1][-1])
            runs[k] += lines

    for slot, index in slots:
        cell = cl[slot]
        ax, ay, aw, ah = cell.art
        per, finish = _page(req, aw, ah, index, signed=index == last_frame)
        number, box = fbk.number_for(fb, cell, str(index + 1))
        for li, lines in enumerate(per):
            add(run_of[li], fbk.clear_box([l + [ax, ay] for l in lines], box))
        add(0, [l + [ax, ay] for l in finish] + number)
    marks = fbk.cells(fb, a.w, a.h) if marks_for_all else [cl[s] for s, _ in slots]
    add(0, [m for c in marks for m in fbk.cut_marks(fb, c, a.w, a.h)])
    return [(run_pens[k], lines) for k, lines in enumerate(runs) if lines]


def _flip_job(req: PlanRequest, frames: list[int] | None = None) -> Composite:
    """Every page of the flipbook, sheet after sheet."""
    fb, lay = req.flipbook, req.layout
    problem = fbk.check(fb, lay.area.w, lay.area.h)
    if problem:
        raise HTTPException(422, problem)
    count = _video_count(req)
    frames = fb.frames(count) if frames is None else frames
    if not frames:
        raise HTTPException(422, "There are no frames to plot. Check the frame range.")
    groups, sheet_of = [], []
    sheets = fbk.sheets(frames, fb.per_sheet)
    for n, sheet in enumerate(sheets, 1):
        for pen, lines in _flip_sheet(req, list(enumerate(sheet)), frames[-1]):
            groups.append((pen, lines))
            sheet_of.append(n)
    flat = [l for _, ls in groups for l in ls]
    start = touch_in_area(lay)
    st = stats(flat, lay, start)
    st["pens"] = len({pen for pen, _ in groups})
    st["pen_changes"] = sum(1 for i in range(1, len(groups)) if groups[i][0] != groups[i - 1][0])
    flip = {"frames": len(frames), "first": frames[0], "last": frames[-1], "sheets": len(sheets),
            "per_sheet": fb.per_sheet, "count": count}
    return Composite(layers=[], groups=groups, stats=st, sheets=sheet_of, flip=flip)


def _flip_preview(req: PlanRequest) -> Composite:
    """The editor's view: the frame being looked at, on its page of its sheet."""
    fb, lay = req.flipbook, req.layout
    problem = fbk.check(fb, lay.area.w, lay.area.h)
    if problem:
        raise HTTPException(422, problem)
    count = _video_count(req)
    frames = fb.frames(count)
    index = max(0, min(fb.frame, count - 1))
    slot = frames.index(index) % fb.per_sheet if index in frames else index % fb.per_sheet
    groups = _flip_sheet(req, [(slot, index)], frames[-1] if frames else index)
    flat = [l for _, ls in groups for l in ls]
    st = stats(flat, lay, touch_in_area(lay))
    st["pens"] = len({pen for pen, _ in groups})
    st["pen_changes"] = max(0, len(groups) - 1)
    n = len(frames)
    sheets = -(-n // fb.per_sheet) if n else 0
    flip = {"count": count, "frame": index, "slot": slot, "frames": n, "sheets": sheets, "per_sheet": fb.per_sheet,
            "first": frames[0] if frames else 0, "last": frames[-1] if frames else 0,
            "sheet": (frames.index(index) // fb.per_sheet + 1) if index in frames else None,
            "minutes_per_page": st["minutes"],
            "cells": [{"page": [round(v, 2) for v in c.page], "art": [round(v, 2) for v in c.art]}
                      for c in fbk.cells(fb, lay.area.w, lay.area.h)]}
    return Composite(layers=[], groups=groups, stats=st, flip=flip)


def _is_flip(req: PlanRequest) -> bool:
    return req.flipbook.enabled and _uses_video(req)


def _screen() -> Screen:
    c = printer.load_config()
    return Screen(progress=c.screen_progress, status=c.screen_status, claim_pause=c.claim_pause, claim_pen=c.claim_pen,
                  claim_sheet=c.claim_sheet)


def _job_comp(req: PlanRequest) -> Composite:
    """Everything the job plots: the layers, or for a flipbook every page."""
    if _is_flip(req):
        return _flip_job(req)
    return _compose(_at_frame(req, req.flipbook.frame))


def _job_gcode(req: PlanRequest, title: str, comp: Composite | None = None) -> str:
    comp = comp or _job_comp(req)
    if not comp.groups:
        raise HTTPException(422, "Nothing to draw: every layer is hidden or empty.")
    groups = [(pen_label(pen), [req.layout.area_to_pen(l) for l in lines]) for pen, lines in comp.groups]
    return compile_groups(req.layout, groups, title, screen=_screen(), sheets=comp.sheets,
                          check_each_sheet=req.flipbook.check_each_sheet)


def _preview_picture(req: PlanRequest):
    """The first visible full-area picture layer's tone (what gets plotted), for thumbnails."""
    if _is_flip(req):
        return None  # many pages per sheet: the plot alone reads better
    req = _at_frame(req, req.flipbook.frame)
    layers = req.layers or [Layer(effect=req.effect, prep=req.prep, image_id=req.image_id)]
    a = req.layout.area
    for layer in layers:
        src = _source(layer.image_id) if layer.image_id else None
        if layer.visible and isinstance(src, RasterSource) and layer.place.is_full:
            mask = masking.full_mask(src.state, src.gray.shape) if src.state.mask is not None else None
            return prepare(src.gray, a.w, a.h, layer.prep.model_copy(update={"px_per_mm": 3.0}), mask=mask).data
    return None


def _project_file(req: PlanRequest, text: str, title: str, comp: Composite | None) -> tuple[bytes, str] | None:
    """The .gcode.3mf for this job: built in (with thumbnails and timing), or from the user's template."""
    cfg = printer.load_config()
    tpl = printer.template_path()
    if cfg.project_file == "template":
        if not tpl.exists():
            raise HTTPException(422, "Add a project template in the Printer tab, or switch to the built-in project file.")
        data = tpl.read_bytes()
        return package.package(text, data), package.inspect_template(data)["param"]
    lay = req.layout
    all_groups = comp.groups if comp else []
    # The thumbnail shows the first sheet; the time covers every sheet.
    first_sheet = [g for i, g in enumerate(all_groups) if not comp.sheets or comp.sheets[i] == 1]
    groups_area = [(PENS.get(p, PENS[1])[2], lines) for p, lines in first_sheet]
    groups_pen = [(c, [lay.area_to_pen(l) for l in lines]) for c, lines in groups_area]
    every_pen = [(pen_label(p), [lay.area_to_pen(l) for l in lines]) for p, lines in all_groups]
    pts = [lay.to_nozzle(np.vstack(ls)) for _, ls in groups_pen if ls] or [lay.to_nozzle(lay.area.poly())]
    allp = np.vstack(pts)
    (x0, y0), (x1, y1) = allp.min(axis=0), allp.max(axis=0)
    info = package.ProjectInfo(
        title=title, printer_model_id=(lay.bed.model_id or ("N1",))[0], nozzle=0.4, bed_w=lay.bed.w, bed_h=lay.bed.h,
        seconds=job_seconds(lay, every_pen, _screen(), comp.sheets) if comp else 120.0,
        pauses=text.count("M400 U1"), bbox=(float(x0), float(y0), float(x1), float(y1)),
        colors=list(dict.fromkeys(PENS.get(p, PENS[1])[2] for p, _ in all_groups)) or [PENS[1][2]])
    picture = _preview_picture(req) if comp else None
    thumbs = thumbs_mod.thumbnails(groups_area, groups_pen, lay, picture, cfg.thumbnail)
    base = package.template_settings(tpl.read_bytes()) if tpl.exists() else None
    return package.build_project(text, info, thumbs, base), "Metadata/plate_1.gcode"


def _rounded(lines):
    return [[[round(float(x), 2), round(float(y), 2)] for x, y in l] for l in lines]


def _refuse(err: Exception):
    raise HTTPException(422, {"message": str(err).split("\n", 1)[0], "problems": getattr(err, "problems", [])})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/version")
def version():
    return {"version": __version__, "features": ["rotation", "printer", "vector", "selection", "ai", "projects", "layers", "flipbook"]}


@app.get("/api/effects")
def list_effects():
    return [e.describe() for e in effects.EFFECTS.values()]


@app.get("/api/defaults")
def defaults():
    m = mach.load()
    layout = Layout(pen=m.pen, z=m.z, feed=m.feed, bed=m.bed)  # new projects start with this printer's settings
    return {"layout": layout.model_dump(), "prep": Prep().model_dump(), "machine": m.model_dump(),
            "extras": extras_mod.Extras().model_dump(),
            "flipbook": Flipbook().model_dump(), "extract": video.Extract().model_dump(),
            "printers": [{"key": k, "label": v[0], "w": v[1], "h": v[2]} for k, v in PRINTERS.items()],
            "pens": [{"pen": n, "label": lab, "colour": col, "color": hx} for n, (lab, col, hx) in PENS.items()],
            "screen_messages": [{"code": k, "label": v} for k, v in SCREEN_MESSAGES.items()],
            "paper_sizes": [{"key": k, "label": PAPER_LABELS[k], "w": w, "h": h} for k, (w, h) in PAPER_SIZES.items()],
            "bed": [180, 180]}


@app.post("/api/checks")
def checks(layout: Layout):
    return [asdict(c) for c in layout.checks()]


@app.post("/api/fit/{mode}")
def fit(mode: str, layout: Layout):
    if mode not in ("fit", "center"):
        raise HTTPException(404, "Use 'fit' or 'center'.")
    area = fit_area(layout) if mode == "fit" else center_area(layout)
    if area is None:
        msg = ("There's no safe room at this rotation. Move the paper or clips, or change the rotation."
               if mode == "fit" else "The area doesn't fit in the free space at this size. Try Fit instead.")
        raise HTTPException(422, msg)
    return area.model_dump()


@app.post("/api/image")
async def upload_image(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Images must be under 25 MB.")
    return _ingest(data, file.filename or "")


def _ingest(data: bytes, filename: str, saved_mask: np.ndarray | None = None) -> dict:
    """Load a picture or SVG into memory and describe it for the page."""
    head = data[:4096].lstrip().lower()
    if filename.lower().endswith(".svg") or head.startswith(b"<svg") or (
        head.startswith(b"<?xml") and b"<svg" in head
    ):
        try:
            art = load_svg(data)
            w, h = art.size_mm()
        except SvgError as err:
            raise HTTPException(422, str(err))
        image_id = "svg-" + hashlib.sha1(data).hexdigest()[:16]
        _remember(_images, image_id, SvgSource(art=art, data=data, name=filename), MAX_IMAGES)
        return {"id": image_id, "kind": "svg", "width": round(w, 1), "height": round(h, 1),
                "warnings": art.warnings, "strokes": len(art.strokes), "fills": len(art.fills)}
    try:
        gray = load_gray(data)
        state = masking.MaskState(rgb=masking.load_rgb(data))
        alpha = masking.alpha_mask(data)
    except Exception:
        raise HTTPException(422, "That file isn't an image BambuPlot can read. Try a JPG, PNG or SVG.")
    notes = []
    if saved_mask is not None:  # reopening a project: restore its selection
        masking.set_mask(state, saved_mask)
        state.history.clear()
    elif alpha is not None:  # an already cut-out PNG: start with its transparency as the selection
        masking.set_mask(state, alpha)
        state.history.clear()
        notes.append("Transparent areas are treated as removed background. Edit or reset the selection to change that.")
    image_id = hashlib.sha1(data).hexdigest()[:16]
    _remember(_images, image_id, RasterSource(gray=gray, state=state, data=data, name=filename), MAX_IMAGES)
    h, w = gray.shape
    return {"id": image_id, "kind": "raster", "width": w, "height": h, "warnings": notes,
            "selection": masking.describe(state)}


class MaskOp(BaseModel):
    op: str  # auto | select | paint | invert | grow | reset | undo
    rect: list[float] | None = None
    strokes: list[dict] = Field(default_factory=list)
    smart: bool = False
    amount: int = 0
    model: str = "isnet-general-use"


def _raster(image_id: str) -> RasterSource:
    src = _images.get(image_id)
    if src is None:
        raise HTTPException(404, "That image is no longer loaded. Upload it again.")
    if not isinstance(src, RasterSource):
        raise HTTPException(422, "Selection tools work on PNG and JPG pictures, not SVG files.")
    return src


@app.get("/api/ai")
def ai_info():
    available = masking.ai_available()
    return {"available": available, "models": masking.ai_models() if available else [],
            "install": "uv sync --extra ai"}


@app.get("/api/image/{image_id}/mask")
def get_mask(image_id: str):
    return masking.describe(_raster(image_id).state)


@app.post("/api/image/{image_id}/mask")
def edit_mask(image_id: str, body: MaskOp):
    st = _raster(image_id).state
    method = None
    try:
        if body.op == "auto":
            method = masking.auto(st)
        elif body.op == "ai":
            method = "ai:" + masking.ai_remove(st, body.model)
        elif body.op == "select":
            if not body.rect or len(body.rect) != 4:
                raise HTTPException(422, "Draw a box around the subject first.")
            masking.select(st, body.rect)
        elif body.op == "paint":
            masking.paint(st, body.strokes, smart=body.smart)
        elif body.op == "invert":
            masking.invert(st)
        elif body.op == "grow":
            masking.grow(st, max(-40, min(40, body.amount)))
        elif body.op == "reset":
            masking.reset(st)
        elif body.op == "undo":
            masking.undo(st)
        else:
            raise HTTPException(404, f"Unknown selection tool '{body.op}'.")
    except masking.MaskError as err:
        raise HTTPException(422, str(err))
    return masking.describe(st, method)


@app.post("/api/plan")
def plan(req: PlanRequest):
    if _is_flip(req):
        comp = _flip_preview(req)
        return {
            "lines": _rounded([l for _, ls in comp.groups for l in ls]),
            "layers": [],
            "groups": [{"pen": pen, "label": pen_label(pen), "color": PENS.get(pen, PENS[1])[2], "lines": _rounded(lines)}
                       for pen, lines in comp.groups],
            "stats": comp.stats,
            "flipbook": comp.flip,
            "checks": [asdict(c) for c in req.layout.checks()],
        }
    comp = _compose(_at_frame(req, req.flipbook.frame))
    return {
        "lines": _rounded([l for _, ls in comp.groups for l in ls]),
        "layers": [{"id": lid, "pen": pen, "color": PENS.get(pen, PENS[1])[2], "lines": _rounded(lines)}
                   for lid, pen, lines in comp.layers],
        "groups": [{"pen": pen, "label": pen_label(pen), "color": PENS.get(pen, PENS[1])[2], "lines": _rounded(lines)}
                   for pen, lines in comp.groups],
        "stats": comp.stats,
        "checks": [asdict(c) for c in req.layout.checks()],
    }


class FrameRequest(PlanRequest):
    index: int = Field(0, ge=0)


@app.post("/api/flipbook/frame")
def flipbook_frame(req: FrameRequest):
    """One flipbook page on its own (page-art mm), for the flip preview. Also readies it for the job."""
    fb, a = req.flipbook, req.layout.area
    problem = fbk.check(fb, a.w, a.h)
    if problem:
        raise HTTPException(422, problem)
    count = _video_count(req)
    frames = fb.frames(count)
    cell = fbk.cells(fb, a.w, a.h)[0]
    _, _, aw, ah = cell.art
    per, finish = _page(req, aw, ah, min(req.index, count - 1), signed=bool(frames) and req.index == frames[-1])
    run_of, run_pens = _runs(_visible_layers(req))
    groups: dict[int, list] = {}
    for li, lines in enumerate(per):
        groups.setdefault(run_of[li], []).extend(lines)
    groups.setdefault(0, []).extend(finish)
    return {"index": req.index, "w": round(aw, 2), "h": round(ah, 2),
            "groups": [{"pen": run_pens[k], "color": PENS.get(run_pens[k], PENS[1])[2], "lines": _rounded(ls)}
                       for k, ls in sorted(groups.items()) if ls]}


@app.get("/api/pens")
def pens():
    return [{"pen": n, "label": label, "colour": colour, "color": hexcol} for n, (label, colour, hexcol) in PENS.items()]


@app.post("/api/gcode", response_class=PlainTextResponse)
def gcode(req: PlanRequest):
    try:
        text = _job_gcode(req, title="flipbook" if _is_flip(req) else req.effect if not req.layers else f"{len(req.layers)} layers")
    except (UnsafeLayout, GcodeSafetyError) as err:
        _refuse(err)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="bambuplot-{req.effect}.gcode"'})


@app.post("/api/calibrate/{kind}", response_class=PlainTextResponse)
def calibrate(kind: str, layout: Layout):
    if kind not in ("smoke", "press", "align"):
        raise HTTPException(404, "Use 'smoke', 'press' or 'align'.")
    try:
        text = {"smoke": smoke_test, "press": press_ladder, "align": alignment_test}[kind](layout)
    except (UnsafeLayout, GcodeSafetyError, ValueError) as err:
        _refuse(err)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="bambuplot-{kind}.gcode"'})


# ------------------------------------------------------------------ printer


class PrinterSettings(BaseModel):
    ip: str = ""
    serial: str = ""
    access_code: str | None = None  # None keeps the saved code; "" clears it
    url_prefix: str = "file:///sdcard/"
    project_file: str = "builtin"
    thumbnail: str = "both"
    screen_progress: bool = True
    screen_status: bool = True
    claim_pause: int | None = None
    claim_pen: int | None = 4
    claim_sheet: int | None = 16


class SendRequest(PlanRequest):
    kind: str = "art"  # "art" | "smoke" | "press" | "align"
    start: bool = False
    title: str = ""  # the project's name, shown in Bambu Connect and Bambu Studio


def _printer_error(err: Exception):
    raise HTTPException(502 if isinstance(err, printer.PrinterError) else 422, str(err))


@app.get("/api/printer")
def printer_settings():
    return printer.load_config().public()


@app.post("/api/printer")
def save_printer_settings(body: PrinterSettings):
    cfg = printer.load_config()
    cfg.ip, cfg.serial, cfg.url_prefix = body.ip.strip(), body.serial.strip(), body.url_prefix.strip()
    cfg.project_file = body.project_file if body.project_file in ("builtin", "template") else "builtin"
    cfg.thumbnail = body.thumbnail if body.thumbnail in ("plot", "picture", "both") else "both"
    cfg.screen_progress, cfg.screen_status = body.screen_progress, body.screen_status
    cfg.claim_pause = body.claim_pause if body.claim_pause in SCREEN_MESSAGES else None
    cfg.claim_pen = body.claim_pen if body.claim_pen in SCREEN_MESSAGES else None
    cfg.claim_sheet = body.claim_sheet if body.claim_sheet in SCREEN_MESSAGES else None
    if body.access_code is not None:
        cfg.access_code = body.access_code.strip()
    printer.save_config(cfg)
    return cfg.public()


@app.post("/api/printer/template")
async def upload_template(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Templates must be under 25 MB.")
    try:
        return printer.save_template(data)
    except TemplateError as err:
        raise HTTPException(422, str(err))


@app.delete("/api/printer/template")
def remove_template():
    printer.template_path().unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/printer/test")
def test_printer():
    cfg = printer.load_config()
    missing = cfg.ready()
    if missing:
        raise HTTPException(422, "Add " + " and ".join(missing) + " first.")
    try:
        return printer.status(cfg)
    except printer.PrinterError as err:
        _printer_error(err)


def _job_text(req: SendRequest) -> tuple[str, str, Composite | None]:
    lay, comp = req.layout, None
    try:
        if req.kind == "smoke":
            text, stem = smoke_test(lay), "bambuplot-smoke"
        elif req.kind == "press":
            text, stem = press_ladder(lay), "bambuplot-press"
        elif req.kind == "align":
            text, stem = alignment_test(lay), "bambuplot-alignment"
        else:
            comp = _job_comp(req)
            flip = _is_flip(req)
            text = _job_gcode(req, title="flipbook" if flip else req.effect, comp=comp)
            stem = "bambuplot-flipbook" if flip else f"bambuplot-{req.effect}"
    except (UnsafeLayout, GcodeSafetyError, ValueError) as err:
        _refuse(err)
    return text, stem, comp


def _min_z(lay: Layout) -> float:
    return min(lay.z.down, lay.z.touch - max(DEFAULT_PRESSES))


@app.post("/api/send")
def send(req: SendRequest):
    text, stem, comp = _job_text(req)
    try:
        cfg = printer.load_config()
        project = _project_file(req, text, req.title or stem, comp)
        return printer.send_job(cfg, text, stem, min_z=_min_z(req.layout), start=req.start,
                                bed=(req.layout.bed.w, req.layout.bed.h), project=project)
    except (printer.PrinterError, TemplateError) as err:
        _printer_error(err)


@app.post("/api/package")
def package_job(req: SendRequest):
    """The job as a .gcode.3mf (built in, or from the saved template), for Bambu Connect or the SD card."""
    text, stem, comp = _job_text(req)
    try:
        project = _project_file(req, text, req.title or stem, comp)
        name, data, _ = printer.build_job(text, stem, min_z=_min_z(req.layout), bed=(req.layout.bed.w, req.layout.bed.h),
                                          project=project)
    except (printer.PrinterError, TemplateError) as err:
        raise HTTPException(422, str(err))
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/connect")
def bambu_connect(req: SendRequest):
    text, stem, comp = _job_text(req)
    try:
        project = _project_file(req, text, req.title or stem, comp)
        return printer.open_in_bambu_connect(text, stem, min_z=_min_z(req.layout), bed=(req.layout.bed.w, req.layout.bed.h),
                                             project=project)
    except printer.PrinterError as err:
        raise HTTPException(422, str(err))
    except TemplateError as err:
        raise HTTPException(422, str(err))


# ------------------------------------------------------------------ projects


class ProjectState(BaseModel):
    layout: Layout = Field(default_factory=Layout)
    effect: str = "waves"
    params: dict = Field(default_factory=dict)  # per effect: {effect: {param: value}}
    prep: Prep = Field(default_factory=Prep)
    aiModel: str | None = None
    extras: extras_mod.Extras = Field(default_factory=extras_mod.Extras)
    layers: list[Layer] = Field(default_factory=list)
    active: int = 0  # the layer the inspector edits
    flipbook: Flipbook = Field(default_factory=Flipbook)


class ProjectSave(BaseModel):
    name: str | None = None
    state: ProjectState = Field(default_factory=ProjectState)
    image_id: str | None = None  # the picture currently loaded on the page, or None for the sample


class ProjectNew(BaseModel):
    name: str | None = None
    state: ProjectState | None = None


def _project_error(err: proj.ProjectError):
    raise HTTPException(404, str(err))


@app.get("/api/projects")
def list_projects():
    return proj.list_projects()


@app.post("/api/projects")
def new_project(body: ProjectNew):
    return proj.create(body.name, body.state.model_dump() if body.state else None)


@app.get("/api/projects/{pid}")
def open_project(pid: str):
    try:
        p = proj.load(pid)
        images, warnings = {}, []
        for iid, info in proj.list_sources(pid).items():
            got = proj.source_bytes(pid, iid)
            if got is None:
                continue
            try:  # a picture that no longer loads shouldn't lock the project
                image = _ingest(got[0], info.get("name", ""), saved_mask=proj.source_mask(pid, iid))
            except HTTPException as err:
                warnings.append(f"“{info.get('name', 'A picture')}” couldn't be opened ({err.detail}). Choose it again.")
                continue
            image["name"] = info.get("name", "")
            images[iid] = image
        if warnings:
            p["warning"] = " ".join(warnings)
        return {**p, "images": images, "image": next(iter(images.values()), None), "video": video.describe(pid)}
    except proj.ProjectError as err:
        _project_error(err)


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: ProjectSave):
    try:
        st = body.state
        refs = [l.image_id for l in st.layers if l.image_id and l.image_id != video.SENTINEL] if st.layers else (
            [body.image_id] if body.image_id else [])
        items = {}
        for iid in refs:
            src = _images.get(iid)
            if src is None:
                continue
            kind = "svg" if isinstance(src, SvgSource) else "raster"
            mask = src.state.mask if isinstance(src, RasterSource) else None
            items[iid] = (src.data, src.name, kind, mask)
        proj.set_sources(pid, items, set(refs))
        summary = proj.save_state(pid, st.model_dump(), body.name)
        try:  # the dashboard thumbnail is a small drawing of the current plot
            # Saved layers keep settings for every effect they've used; the plan needs this effect's.
            has_video = video.describe(pid) is not None
            keep = lambda iid: iid if iid in _images or (iid == video.SENTINEL and has_video) else None  # noqa: E731
            layers = [l.model_copy(update={
                "image_id": keep(l.image_id),
                "params": l.params.get(l.effect, {}) if isinstance(l.params.get(l.effect), dict) else l.params,
            }) for l in st.layers]
            req = PlanRequest(layout=st.layout, effect=st.effect, params=st.params.get(st.effect, {}), prep=st.prep,
                              image_id=keep(body.image_id), extras=st.extras, layers=layers, project=pid,
                              flipbook=st.flipbook)
            comp = _flip_preview(req) if _is_flip(req) else _compose(_at_frame(req, st.flipbook.frame))
            flat = [l for _, ls in comp.groups for l in ls]
            proj.render_thumb(pid, flat, st.layout.area.w, st.layout.area.h, st.layout.pen.width_mm)
            summary["has_thumb"] = True
        except HTTPException:
            pass
        return summary
    except proj.ProjectError as err:
        _project_error(err)


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: ProjectNew):
    try:
        return proj.rename(pid, body.name or "")
    except proj.ProjectError as err:
        _project_error(err)


@app.post("/api/projects/{pid}/duplicate")
def duplicate_project(pid: str):
    try:
        return proj.duplicate(pid)
    except proj.ProjectError as err:
        _project_error(err)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    try:
        proj.delete(pid)
        return {"ok": True}
    except proj.ProjectError as err:
        _project_error(err)


@app.get("/api/projects/{pid}/thumb.png")
def project_thumb(pid: str):
    try:
        path = proj.thumb_path(pid)
    except proj.ProjectError as err:
        _project_error(err)
    if path is None:
        raise HTTPException(404, "No thumbnail yet.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/api/projects/{pid}/sources/{iid}")
def project_source_file(pid: str, iid: str):
    try:
        got = proj.source_bytes(pid, iid)
    except proj.ProjectError as err:
        _project_error(err)
    if got is None:
        raise HTTPException(404, "That picture isn't in this project.")
    data, info = got
    ext = info.get("ext", "png")
    mime = "image/svg+xml" if info.get("kind") == "svg" else {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                                                             "webp": "image/webp"}.get(ext, "image/png")
    return Response(data, media_type=mime)


@app.get("/api/projects/{pid}/source")
def project_source(pid: str):
    """The project's first picture (kept for older pages; layers use /sources/{image id})."""
    try:
        first = next(iter(proj.list_sources(pid)), None)
    except proj.ProjectError as err:
        _project_error(err)
    if first is None:
        raise HTTPException(404, "This project has no picture.")
    return project_source_file(pid, first)


# ------------------------------------------------------------------ project video (flipbooks)


def _video_error(err: Exception):
    raise HTTPException(404 if isinstance(err, proj.ProjectError) else 422, str(err))


@app.post("/api/projects/{pid}/video")
async def upload_video(pid: str, file: UploadFile = File(...)):
    """Keep a video in the project and take its first frames."""
    try:
        d = proj._dir(pid)
    except proj.ProjectError as err:
        _project_error(err)
    name = file.filename or "video.mp4"
    if not video.is_video_name(name):
        raise HTTPException(422, "Choose a video file: MP4, MOV, M4V, WebM, AVI, MKV or GIF.")
    tmp = d / "video-upload.tmp"
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_VIDEO:
                    raise HTTPException(413, "Videos must be under 1 GB. Trim it first.")
                out.write(chunk)
        return video.save_upload(pid, tmp, name)
    except video.VideoError as err:
        _video_error(err)
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/projects/{pid}/video")
def get_video(pid: str):
    try:
        return video.describe(pid)
    except proj.ProjectError as err:
        _project_error(err)


@app.post("/api/projects/{pid}/video/extract")
def extract_video(pid: str, body: video.Extract):
    """Take frames again: another range or rate."""
    try:
        return video.extract(pid, body)
    except (video.VideoError, proj.ProjectError) as err:
        _video_error(err)


@app.delete("/api/projects/{pid}/video")
def delete_video(pid: str):
    try:
        proj._dir(pid)
    except proj.ProjectError as err:
        _project_error(err)
    video.remove(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/video/frames/{index}.jpg")
def video_frame(pid: str, index: int, cut: bool = False):
    """A frame; with ``cut``, its removed background shows white."""
    try:
        if cut:
            return Response(video.frame_cutout(pid, index), media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})
        path = video.frame_path(pid, index)
    except (video.VideoError, proj.ProjectError) as err:
        _video_error(err)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


class BackgroundBody(BaseModel):
    method: str = "auto"  # "auto" | "ai"
    model: str | None = None


@app.post("/api/projects/{pid}/video/background")
def video_background(pid: str, body: BackgroundBody):
    """Start removing the background from every frame. Poll GET for progress."""
    try:
        return video.start_background(pid, body.method, body.model)
    except (video.VideoError, proj.ProjectError) as err:
        _video_error(err)


@app.get("/api/projects/{pid}/video/background")
def video_background_status(pid: str):
    try:
        proj._dir(pid)
    except proj.ProjectError as err:
        _project_error(err)
    st = video.background_status(pid)
    if not st["running"]:
        st["video"] = video.describe(pid)
    return st


@app.post("/api/projects/{pid}/video/background/cancel")
def video_background_cancel(pid: str):
    video.cancel_background(pid)
    return {"ok": True}


@app.delete("/api/projects/{pid}/video/background")
def video_background_clear(pid: str):
    try:
        return video.clear_background(pid)
    except (video.VideoError, proj.ProjectError) as err:
        _video_error(err)


class AlignmentBody(BaseModel):
    layout: Layout
    left: float | None = None  # measured: sheet's left edge to the cross, mm
    front: float | None = None  # measured: sheet's front edge to the cross, mm


@app.post("/api/alignment")
def alignment(body: AlignmentBody):
    """Expected position of the alignment cross on the sheet and, given measurements, the correction."""
    c = alignment_correction(body.layout, body.left if body.left is not None else 0.0,
                             body.front if body.front is not None else 0.0)
    if body.left is None or body.front is None:
        return {"expected_left": c["expected_left"], "expected_front": c["expected_front"]}
    return c


# ------------------------------------------------------------------ machine (shared by all projects)


@app.get("/api/machine")
def get_machine():
    return mach.load().model_dump()


@app.put("/api/machine")
def put_machine(body: mach.Machine):
    return mach.save(body).model_dump()


# ------------------------------------------------------------------ signature (shared by all projects)


class SignatureBody(BaseModel):
    strokes: list[list[list[float]]]


@app.get("/api/signature")
def get_signature():
    return extras_mod.info()


@app.put("/api/signature")
def put_signature(body: SignatureBody):
    try:
        return extras_mod.save(body.strokes)
    except ValueError as err:
        raise HTTPException(422, str(err))


@app.delete("/api/signature")
def delete_signature():
    extras_mod.delete()
    return {"has": False, "strokes": [], "version": None}


@app.post("/api/signature/trace")
async def trace_signature(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Pictures must be under 25 MB.")
    try:
        return {"strokes": extras_mod.trace_image(data)}
    except ValueError as err:
        raise HTTPException(422, str(err))
    except Exception:
        raise HTTPException(422, "That file isn't a picture BambuPlot can read. Try a JPG or PNG.")
