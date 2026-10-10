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


def inspect_template(data: bytes) -> dict:
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
    if model and model != A1_MINI:
        warning = (f"This template was sliced for the {MODEL_NAMES.get(model, model)}, not the A1 mini. "
                   "Export one sliced for the A1 mini.")
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
