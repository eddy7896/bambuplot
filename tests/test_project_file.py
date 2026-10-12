import io
import re
import zipfile
from xml.etree import ElementTree

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bambuplot import printer
from bambuplot.gcode import Screen, compile_groups
from bambuplot.layout import Layout
from bambuplot.safety import validate
from bambuplot.server import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    return tmp_path


def png():
    img = np.full((120, 160, 3), 240, np.uint8)
    cv2.circle(img, (80, 60), 40, (30, 30, 160), -1)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return buf.getvalue()


def test_progress_status_and_header_in_the_gcode():
    lay = Layout()
    groups = [("Pen 1", [lay.area_to_pen([[5, 5], [60, 5]]), lay.area_to_pen([[5, 20], [60, 20]])]),
              ("Pen 2", [lay.area_to_pen([[5, 40], [60, 40]])])]
    text = compile_groups(lay, groups, "t", screen=Screen(claim_pause=16))
    assert text.startswith("; HEADER_BLOCK_START") and "total estimated time:" in text
    assert "M1002 gcode_claim_action : 13" in text  # homing
    assert text.count("M1002 gcode_claim_action : 16") == 3  # the three setup pauses
    assert "M1002 gcode_claim_action : 4" in text  # pen change
    pct = [int(m) for m in re.findall(r"^M73 P(\d+)", text, re.M)]
    assert pct[0] == 0 and pct[-1] == 100 and pct == sorted(pct)
    assert "; EXECUTABLE_BLOCK_START" in text and text.rstrip().endswith("; EXECUTABLE_BLOCK_END")


def test_screen_can_be_switched_off():
    lay = Layout()
    text = compile_groups(lay, [("Pen 1", [lay.area_to_pen([[5, 5], [60, 5]])])], "t",
                          screen=Screen(progress=False, status=False))
    assert "M73" not in text and "M1002" not in text


def test_validator_allows_only_the_screen_forms():
    base = "G90\nG28\n"
    assert validate(base + "M73 P10 R3\nM1002 gcode_claim_action : 13\n", 2) == []
    assert validate(base + "M73 E5\n", 2)
    assert validate(base + "M1002 set_filament_type:PLA\n", 2)
    assert validate(base + "M1002 judge_flag g29_before_print_flag\n", 2)


def test_builtin_project_file_has_bambu_structure_thumbnails_and_time(home):
    client = TestClient(app)
    up = client.post("/api/image", files={"file": ("ball.png", png(), "image/png")}).json()
    body = {"effect": "trace", "image_id": up["id"], "layout": Layout().model_dump(), "title": "My ball", "kind": "art"}
    r = client.post("/api/package", json=body)
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    for n in ["[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "Metadata/model_settings.config",
              "Metadata/_rels/model_settings.config.rels", "Metadata/slice_info.config", "Metadata/plate_1.gcode",
              "Metadata/plate_1.gcode.md5", "Metadata/plate_1.png", "Metadata/plate_1_small.png", "Metadata/top_1.png",
              "Metadata/pick_1.png", "Metadata/plate_1.json", "Metadata/project_settings.config"]:
        assert n in names, n
    import hashlib
    gcode = z.read("Metadata/plate_1.gcode")
    assert z.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(gcode).hexdigest().upper()
    info = {m.get("key"): m.get("value") for m in ElementTree.fromstring(z.read("Metadata/slice_info.config")).iter("metadata")}
    assert info["printer_model_id"] == "N1" and int(info["prediction"]) > 60 and info["pause_count"] == "3"
    assert "My ball" in z.read("3D/3dmodel.model").decode()
    thumb = Image.open(io.BytesIO(z.read("Metadata/plate_1.png")))
    small = Image.open(io.BytesIO(z.read("Metadata/plate_1_small.png")))
    assert thumb.size == (512, 512) and small.size == (128, 128)
    rgba = np.asarray(thumb.convert("RGBA"))
    assert rgba[256, 256, 3] == 255 and rgba[2, 2, 3] == 0  # paper in the middle, transparent corners


def test_printer_settings_round_trip(home):
    client = TestClient(app)
    r = client.post("/api/printer", json={"project_file": "builtin", "thumbnail": "picture", "screen_progress": False,
                                          "screen_status": True, "claim_pause": 16, "claim_pen": 99})
    d = r.json()
    assert d["thumbnail"] == "picture" and d["screen_progress"] is False and d["claim_pause"] == 16
    assert d["claim_pen"] is None  # not one of the offered messages
