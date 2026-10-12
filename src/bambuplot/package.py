"""Wrap plotter G-code in a .gcode.3mf, the project format Bambu printers start from.

Writing a valid .gcode.3mf from scratch means reproducing Bambu's metadata,
so instead we take a real one as a template: slice any small model for the
A1 Mini in Bambu Studio, export the plate ("Export plate sliced file"), and
BambuPlot swaps its own G-code into ``Metadata/plate_N.gcode`` and refreshes
the matching ``.md5``. Everything else in the template is copied untouched.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from xml.etree import ElementTree

PLATE_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
A1_MINI = "N1"
MODEL_NAMES = {
    "N1": "A1 mini",
    "N2S": "A1",
    "C11": "P1P",
    "C12": "P1S",
    "BL-P001": "X1 Carbon",
    "BL-P002": "X1",
}


class TemplateError(ValueError):
    pass


def inspect_template(data: bytes, expected_model: str | tuple[str, ...] | None = A1_MINI) -> dict:
    """Check a .gcode.3mf template and describe it."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TemplateError("That file isn't a .gcode.3mf (it isn't a zip archive).")
    with zf:
        names = zf.namelist()
        plates = sorted((int(m.group(1)), n) for n in names if (m := PLATE_RE.match(n)))
        if not plates:
            raise TemplateError(
                "No sliced G-code inside. In Bambu Studio, slice the plate first, "
                "then use File → Export → Export plate sliced file."
            )
        model = None
        if "Metadata/slice_info.config" in names:
            try:
                root = ElementTree.fromstring(zf.read("Metadata/slice_info.config"))
                for md in root.iter("metadata"):
                    if md.get("key") == "printer_model_id":
                        model = md.get("value")
                        break
            except ElementTree.ParseError:
                pass
    plate = plates[0][1]
    warning = None
    expected = (expected_model,) if isinstance(expected_model, str) else tuple(expected_model or ())
    if model and expected and model not in expected:
        want = " / ".join(MODEL_NAMES.get(m, m) for m in expected)
        warning = (f"This template was sliced for the {MODEL_NAMES.get(model, model)}, not the {want}. "
                   f"Export one sliced for the {want}.")
    return {
        "plate": plate,
        "param": plate,  # what the print command names as the file to run
        "printer_model_id": model,
        "printer_model": MODEL_NAMES.get(model, model) if model else None,
        "warning": warning,
    }


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().upper()


def package(gcode: str, template: bytes) -> bytes:
    """Return a new .gcode.3mf with ``gcode`` in place of the template's plate G-code."""
    info = inspect_template(template)
    plate, md5_name = info["plate"], info["plate"] + ".md5"
    body = gcode.encode()
    out = io.BytesIO()
    wrote_md5 = False
    with zipfile.ZipFile(io.BytesIO(template)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == plate:
                zout.writestr(item, body, compress_type=zipfile.ZIP_DEFLATED)
            elif item.filename == md5_name:
                zout.writestr(item, md5_hex(gcode))
                wrote_md5 = True
            elif PLATE_RE.match(item.filename) or (
                item.filename.endswith(".gcode.md5") and item.filename.startswith("Metadata/plate_")
            ):
                continue  # drop other plates' G-code so only ours can run
            else:
                zout.writestr(item, zin.read(item.filename))
        if not wrote_md5:
            zout.writestr(md5_name, md5_hex(gcode))
    return out.getvalue()


def read_plate_gcode(data: bytes) -> str:
    info = inspect_template(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(info["plate"]).decode()


# ------------------------------------------------------------------ built-in project files
# A complete .gcode.3mf written by BambuPlot, laid out like the plate files Bambu Studio
# exports (one plate): no slicer template needed.

import json as _json
import time as _time
from dataclasses import dataclass as _dataclass, field as _field

PRINTER_LABELS = {"N1": "Bambu Lab A1 mini", "N2S": "Bambu Lab A1", "C11": "Bambu Lab P1P", "C12": "Bambu Lab P1S",
                  "BL-P001": "Bambu Lab X1 Carbon", "BL-P002": "Bambu Lab X1"}


@_dataclass
class ProjectInfo:
    title: str
    printer_model_id: str = "N1"
    nozzle: float = 0.4
    bed_w: float = 180.0
    bed_h: float = 180.0
    seconds: float = 0.0  # predicted job time
    pauses: int = 0
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)  # drawing's nozzle-space bounding box: x0, y0, x1, y1
    colors: list = _field(default_factory=lambda: ["#1D1D1F"])
    app: str = "BambuPlot"


def _x(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _project_settings(info: ProjectInfo, base: dict | None) -> str:
    """Project settings: the user's template's settings if available, adjusted; else a minimal set."""
    printer = PRINTER_LABELS.get(info.printer_model_id, "Bambu Lab A1 mini")
    s = dict(base or {})
    s.update({
        "printer_model": printer,
        "printer_settings_id": f"{printer} {info.nozzle:g} nozzle",
        "printer_variant": f"{info.nozzle:g}",
        "nozzle_diameter": [f"{info.nozzle:g}"],
        "printable_area": ["0x0", f"{info.bed_w:g}x0", f"{info.bed_w:g}x{info.bed_h:g}", f"0x{info.bed_h:g}"],
        "printable_height": s.get("printable_height", "180"),
        "curr_bed_type": s.get("curr_bed_type", "Textured PEI Plate"),
        "filament_type": ["PLA"] * len(info.colors),
        "filament_colour": [c.upper() for c in info.colors],
        "from": "project",
        "name": "project_settings",
        "printer_technology": "FFF",
        "gcode_flavor": "marlin",
        "version": s.get("version", "02.08.02.61"),
    })
    return _json.dumps(s, indent=4)


def build_project(gcode: str, info: ProjectInfo, thumbs: dict[str, bytes],
                  base_settings: dict | None = None) -> bytes:
    """Write a one-plate .gcode.3mf. ``thumbs``: 'plate', 'small', 'top', 'pick' PNG bytes."""
    today = _time.strftime("%Y-%m-%d")
    plate_json = {
        "bbox_all": list(info.bbox),
        "bbox_objects": [{"area": float((info.bbox[2] - info.bbox[0]) * (info.bbox[3] - info.bbox[1])),
                          "bbox": list(info.bbox), "id": 1, "layer_height": 0.2, "name": _x(info.title)}],
        "bed_type": "textured_plate", "filament_colors": [c.upper() for c in info.colors],
        "filament_ids": list(range(len(info.colors))), "first_extruder": 0, "first_layer_time": 0.0,
        "is_seq_print": False, "nozzle_diameter": info.nozzle, "version": 2,
    }
    filaments = "\n".join(
        f'    <filament id="{i + 1}" type="PLA" color="{c.upper()}" used_m="0.00" used_g="0.00" '
        f'used_for_object="true" used_for_support="false"/>' for i, c in enumerate(info.colors))
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
 <Relationship Target="/Metadata/plate_1.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>
 <Relationship Target="/Metadata/plate_1.png" Id="rel-4" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"/>
 <Relationship Target="/Metadata/plate_1_small.png" Id="rel-5" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-small"/>
</Relationships>
""",
        "3D/3dmodel.model": f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">{_x(info.app)}</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <metadata name="CreationDate">{today}</metadata>
 <metadata name="ModificationDate">{today}</metadata>
 <metadata name="Description">Pen plot made with BambuPlot</metadata>
 <metadata name="Thumbnail_Middle">/Metadata/plate_1.png</metadata>
 <metadata name="Thumbnail_Small">/Metadata/plate_1_small.png</metadata>
 <metadata name="Title">{_x(info.title)}</metadata>
 <resources>
 </resources>
 <build/>
</model>
""",
        "Metadata/_rels/model_settings.config.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/Metadata/plate_1.gcode" Id="rel-1" Type="http://schemas.bambulab.com/package/2021/gcode"/>
</Relationships>
""",
        "Metadata/model_settings.config": """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    <metadata key="filament_map_mode" value="Auto For Flush"/>
    <metadata key="filament_maps" value="1"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>
    <metadata key="thumbnail_no_light_file" value="Metadata/plate_no_light_1.png"/>
    <metadata key="top_file" value="Metadata/top_1.png"/>
    <metadata key="pick_file" value="Metadata/pick_1.png"/>
    <metadata key="pattern_bbox_file" value="Metadata/plate_1.json"/>
  </plate>
</config>
""",
        "Metadata/slice_info.config": f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="02.08.02.61"/>
  </header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="extruder_type" value="0"/>
    <metadata key="nozzle_volume_type" value="0"/>
    <metadata key="printer_model_id" value="{_x(info.printer_model_id)}"/>
    <metadata key="nozzle_diameters" value="{info.nozzle:g}"/>
    <metadata key="timelapse_type" value="0"/>
    <metadata key="prediction" value="{int(round(info.seconds))}"/>
    <metadata key="weight" value="0.00"/>
    <metadata key="pause_count" value="{info.pauses}"/>
    <metadata key="outside" value="false"/>
    <metadata key="support_used" value="false"/>
    <metadata key="label_object_enabled" value="false"/>
    <object identify_id="1" name="{_x(info.title)}" skipped="false" />
{filaments}
    <nozzle id="0" extruder_id="1" nozzle_diameter="{info.nozzle:g}" volume_type="Standard"/>
  </plate>
</config>
""",
        "Metadata/plate_1.json": _json.dumps(plate_json),
        "Metadata/project_settings.config": _project_settings(info, base_settings),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
        z.writestr("Metadata/plate_1.gcode", gcode.encode())
        z.writestr("Metadata/plate_1.gcode.md5", md5_hex(gcode))
        for name, key in (("plate_1.png", "plate"), ("plate_1_small.png", "small"), ("plate_no_light_1.png", "plate"),
                          ("top_1.png", "top"), ("pick_1.png", "pick")):
            if key in thumbs:
                z.writestr(f"Metadata/{name}", thumbs[key])
    return out.getvalue()


def template_settings(data: bytes) -> dict | None:
    """The project settings inside a user's template, to start the built-in file from."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return _json.loads(z.read("Metadata/project_settings.config"))
    except (KeyError, ValueError, zipfile.BadZipFile):
        return None
