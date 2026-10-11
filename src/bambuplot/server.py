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
from pydantic import BaseModel, Field

from . import effects
from . import __version__, printer
from .calibrate import DEFAULT_PRESSES, press_ladder, smoke_test
from .fit import center_area, fit_area
from .gcode import UnsafeLayout
from . import mask as masking
from .image import Prep, load_gray
from .layout import Layout
from .paths import stats
from .pipeline import Plan, make_plan, plan_to_gcode, touch_in_area
from .package import TemplateError
from .safety import GcodeSafetyError
from .svgin import SvgArt, SvgError, load_svg

WEB = Path(__file__).parent / "web"
MAX_IMAGES = 16
MAX_PLANS = 8
MAX_UPLOAD = 25 * 1024 * 1024

app = FastAPI(title="BambuPlot")
_images: OrderedDict[str, object] = OrderedDict()  # id -> RasterSource or SvgArt


@dataclass
class RasterSource:
    gray: np.ndarray  # full-resolution grayscale, 0–1
    state: masking.MaskState  # working-size color copy plus the subject mask
_plans: OrderedDict[str, Plan] = OrderedDict()


class PlanRequest(BaseModel):
    layout: Layout = Field(default_factory=Layout)
    effect: str = "waves"
    params: dict = Field(default_factory=dict)
    prep: Prep = Field(default_factory=Prep)
    image_id: str | None = None
    seed: int = 0


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
        if req.image_id not in _images:
            raise HTTPException(404, "That image is no longer loaded. Upload it again.")
        src = _images[req.image_id]
        if isinstance(src, SvgArt):
            svg = src
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
    }, sort_keys=True).encode()).hexdigest()
    if key in _plans:
        _plans.move_to_end(key)
        plan = _plans[key]
    else:
        if mask_version is not None:
            full = masking.full_mask(src.state, gray.shape)
        try:
            plan = make_plan(lay, req.effect, req.params, req.prep, gray, req.seed, svg=svg, mask=full)
        except (ValueError, SvgError) as err:
            raise HTTPException(422, str(err))
        _remember(_plans, key, plan, MAX_PLANS)
    # Stats depend on feeds and Z, which aren't part of the cache key.
    return Plan(lines=plan.lines, stats=stats(plan.lines, lay, touch_in_area(lay)))


def _refuse(err: Exception):
    raise HTTPException(422, {"message": str(err).split("\n", 1)[0], "problems": getattr(err, "problems", [])})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/version")
def version():
    return {"version": __version__, "features": ["rotation", "printer", "vector", "selection", "ai"]}


@app.get("/api/effects")
def list_effects():
    return [e.describe() for e in effects.EFFECTS.values()]


@app.get("/api/defaults")
def defaults():
    return {"layout": Layout().model_dump(), "prep": Prep().model_dump()}


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
    head = data[:4096].lstrip().lower()
    if (file.filename or "").lower().endswith(".svg") or head.startswith(b"<svg") or (
        head.startswith(b"<?xml") and b"<svg" in head
    ):
        try:
            art = load_svg(data)
            w, h = art.size_mm()
        except SvgError as err:
            raise HTTPException(422, str(err))
        image_id = "svg-" + hashlib.sha1(data).hexdigest()[:16]
        _remember(_images, image_id, art, MAX_IMAGES)
        return {"id": image_id, "kind": "svg", "width": round(w, 1), "height": round(h, 1),
                "warnings": art.warnings, "strokes": len(art.strokes), "fills": len(art.fills)}
    try:
        gray = load_gray(data)
        state = masking.MaskState(rgb=masking.load_rgb(data))
        alpha = masking.alpha_mask(data)
    except Exception:
        raise HTTPException(422, "That file isn't an image BambuPlot can read. Try a JPG, PNG or SVG.")
    notes = []
    if alpha is not None:  # an already cut-out PNG: start with its transparency as the selection
        masking.set_mask(state, alpha)
        state.history.clear()
        notes.append("Transparent areas are treated as removed background. Edit or reset the selection to change that.")
    image_id = hashlib.sha1(data).hexdigest()[:16]
    _remember(_images, image_id, RasterSource(gray=gray, state=state), MAX_IMAGES)
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
    p = _plan(req)
    return {
        "lines": [[[round(float(x), 2), round(float(y), 2)] for x, y in l] for l in p.lines],
        "stats": p.stats,
        "checks": [asdict(c) for c in req.layout.checks()],
    }


@app.post("/api/gcode", response_class=PlainTextResponse)
def gcode(req: PlanRequest):
    p = _plan(req)
    try:
        text = plan_to_gcode(req.layout, p, title=req.effect)
    except (UnsafeLayout, GcodeSafetyError) as err:
        _refuse(err)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="bambuplot-{req.effect}.gcode"'})


@app.post("/api/calibrate/{kind}", response_class=PlainTextResponse)
def calibrate(kind: str, layout: Layout):
    if kind not in ("smoke", "press"):
        raise HTTPException(404, "Use 'smoke' or 'press'.")
    try:
        text = smoke_test(layout) if kind == "smoke" else press_ladder(layout)
    except (UnsafeLayout, GcodeSafetyError, ValueError) as err:
        _refuse(err)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="bambuplot-{kind}.gcode"'})


# ------------------------------------------------------------------ printer


class PrinterSettings(BaseModel):
    ip: str = ""
    serial: str = ""
    access_code: str | None = None  # None keeps the saved code; "" clears it
    url_prefix: str = "file:///sdcard/"


class SendRequest(PlanRequest):
    kind: str = "art"  # "art" | "smoke" | "press"
    start: bool = False


def _printer_error(err: Exception):
    raise HTTPException(502 if isinstance(err, printer.PrinterError) else 422, str(err))


@app.get("/api/printer")
def printer_settings():
    return printer.load_config().public()


@app.post("/api/printer")
def save_printer_settings(body: PrinterSettings):
    cfg = printer.load_config()
    cfg.ip, cfg.serial, cfg.url_prefix = body.ip.strip(), body.serial.strip(), body.url_prefix.strip()
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


def _job_text(req: SendRequest) -> tuple[str, str]:
    lay = req.layout
    try:
        if req.kind == "smoke":
            text, stem = smoke_test(lay), "bambuplot-smoke"
        elif req.kind == "press":
            text, stem = press_ladder(lay), "bambuplot-press"
        else:
            text, stem = plan_to_gcode(lay, _plan(req), title=req.effect), f"bambuplot-{req.effect}"
    except (UnsafeLayout, GcodeSafetyError, ValueError) as err:
        _refuse(err)
    return text, stem


def _min_z(lay: Layout) -> float:
    return min(lay.z.down, lay.z.touch - max(DEFAULT_PRESSES))


@app.post("/api/send")
def send(req: SendRequest):
    text, stem = _job_text(req)
    try:
        return printer.send_job(printer.load_config(), text, stem, min_z=_min_z(req.layout), start=req.start)
    except (printer.PrinterError, TemplateError) as err:
        _printer_error(err)


@app.post("/api/package")
def package_job(req: SendRequest):
    """The job as a .gcode.3mf built from the saved template, for Bambu Connect or the SD card."""
    text, stem = _job_text(req)
    try:
        name, data, _ = printer.build_job(text, stem, min_z=_min_z(req.layout), require_template=True)
    except (printer.PrinterError, TemplateError) as err:
        raise HTTPException(422, str(err))
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/connect")
def bambu_connect(req: SendRequest):
    text, stem = _job_text(req)
    try:
        return printer.open_in_bambu_connect(text, stem, min_z=_min_z(req.layout))
    except printer.PrinterError as err:
        raise HTTPException(422, str(err))
    except TemplateError as err:
        raise HTTPException(422, str(err))
