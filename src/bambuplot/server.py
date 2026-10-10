"""FastAPI backend for the web UI. Run with `bambuplot serve`."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import effects
from . import printer
from .calibrate import DEFAULT_PRESSES, press_ladder, smoke_test
from .fit import center_area, fit_area
from .gcode import UnsafeLayout
from .image import Prep, load_gray
from .layout import Layout
from .paths import stats
from .pipeline import Plan, make_plan, plan_to_gcode, touch_in_area
from .package import TemplateError
from .safety import GcodeSafetyError

WEB = Path(__file__).parent / "web"
MAX_IMAGES = 16
MAX_PLANS = 8
MAX_UPLOAD = 25 * 1024 * 1024

app = FastAPI(title="BambuPlot")
_images: OrderedDict[str, object] = OrderedDict()  # id -> grayscale array
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
    gray = None
    if req.image_id:
        if req.image_id not in _images:
            raise HTTPException(404, "That image is no longer loaded. Upload it again.")
        gray = _images[req.image_id]
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
    }, sort_keys=True).encode()).hexdigest()
    if key in _plans:
        _plans.move_to_end(key)
        plan = _plans[key]
    else:
        plan = make_plan(lay, req.effect, req.params, req.prep, gray, req.seed)
        _remember(_plans, key, plan, MAX_PLANS)
    # Stats depend on feeds and Z, which aren't part of the cache key.
    return Plan(lines=plan.lines, stats=stats(plan.lines, lay, touch_in_area(lay)))


def _refuse(err: Exception):
    raise HTTPException(422, {"message": str(err).split("\n", 1)[0], "problems": getattr(err, "problems", [])})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


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
    try:
        gray = load_gray(data)
    except Exception:
        raise HTTPException(422, "That file isn't an image BambuPlot can read. Try a JPG or PNG.")
    image_id = hashlib.sha1(data).hexdigest()[:16]
    _remember(_images, image_id, gray, MAX_IMAGES)
    h, w = gray.shape
    return {"id": image_id, "width": w, "height": h}


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


@app.post("/api/send")
def send(req: SendRequest):
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
    min_z = min(lay.z.down, lay.z.touch - max(DEFAULT_PRESSES))
    try:
        return printer.send_job(printer.load_config(), text, stem, min_z=min_z, start=req.start)
    except (printer.PrinterError, TemplateError) as err:
        _printer_error(err)
