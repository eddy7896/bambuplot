import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bambuplot import printer
from bambuplot.gcode import compile_groups
from bambuplot.image import Prep, prepare
from bambuplot.layers import Place, group_in_order, place_lines
from bambuplot.layout import Layout
from bambuplot.server import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    return tmp_path


def png(color=(30, 30, 160)):
    img = np.full((120, 160, 3), 240, np.uint8)
    cv2.circle(img, (80, 60), 40, color, -1)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return buf.getvalue()


def test_full_frame_leaves_lines_alone():
    line = np.array([[0.0, 0], [10, 5]])
    assert place_lines([line], Place(), 100, 50)[0] is line


def test_half_frame_moves_lines_into_the_right_half():
    lines = [np.array([[0.0, 0], [50, 50]])]  # frame is 50 × 50 (half of a 100 × 50 area)
    out = place_lines(lines, Place(x=0.5, w=0.5), 100, 50)
    assert np.allclose(out[0], [[50, 0], [100, 50]])


def test_rotated_frame_is_clipped_to_the_area():
    lines = [np.array([[0.0, 25], [100, 25]])]
    out = place_lines(lines, Place(rotation=45), 100, 50)
    pts = np.vstack(out)
    assert pts.min() >= -1e-9 and pts[:, 0].max() <= 100 + 1e-9 and pts[:, 1].max() <= 50 + 1e-9


def test_group_in_order_keeps_layer_order():
    a, b, c, d = (np.zeros((2, 2)),) * 4
    groups = group_in_order([(1, [a]), (1, [b]), (2, [c]), (3, []), (1, [d])])
    assert [p for p, _ in groups] == [1, 2, 1] and len(groups[0][1]) == 2


def test_layers_plot_one_after_another(home):
    """Same pen, two layers side by side: every stroke of the first is drawn before the second starts."""
    client = TestClient(app)
    lay = Layout().model_dump()
    w = lay["area"]["w"]
    layers = [
        {"id": "a", "effect": "waves", "pen": 1, "place": {"x": 0.5, "w": 0.5}},
        {"id": "b", "effect": "spiral", "pen": 1, "place": {"x": 0.0, "w": 0.5}},
    ]
    d = client.post("/api/plan", json={"layout": lay, "layers": layers}).json()
    assert d["stats"]["pen_changes"] == 0
    xs = [np.array(l)[:, 0].mean() for l in d["lines"]]
    sides = [x > w / 2 for x in xs]
    first_b = sides.index(False)
    assert all(sides[:first_b]) and not any(sides[first_b:])  # all of a (right half), then all of b


def test_returning_to_a_pen_keeps_layer_order(home):
    client = TestClient(app)
    lay = Layout().model_dump()
    layers = [{"id": "a", "effect": "spiral", "pen": 1},
              {"id": "b", "effect": "waves", "pen": 2},
              {"id": "c", "effect": "halftone", "pen": 1}]
    d = client.post("/api/plan", json={"layout": lay, "layers": layers}).json()
    assert [g["pen"] for g in d["groups"]] == [1, 2, 1]
    assert d["stats"]["pen_changes"] == 2 and d["stats"]["pens"] == 2


def test_tone_band_keeps_only_the_chosen_tones():
    gray = np.tile(np.linspace(0, 1, 100, dtype=np.float32), (10, 1))  # dark → light
    t = prepare(gray, 100, 10, Prep(clahe=False, band_lo=0.6, band_hi=1.0))  # shadows only
    dark = 1 - t.data
    assert dark[:, -20:].max() == 0  # light end blanked
    assert dark[:, :20].min() > 0.6  # shadows kept


def test_two_pens_pause_for_a_pen_change():
    lay = Layout()
    a = lay.area
    groups = [("Pen 1 (black)", [lay.area_to_pen([[5, 5], [20, 5]])]), ("Pen 3 (red)", [lay.area_to_pen([[5, 9], [20, 9]])])]
    text = compile_groups(lay, groups, "test")
    assert text.count("M400 U1") == 4  # three start pauses + one pen change
    assert "swap to Pen 3 (red)" in text and "pens: Pen 1 (black), Pen 3 (red)" in text


def test_api_layers_plan_gcode_and_hidden_layers(home):
    client = TestClient(app)
    lay = Layout().model_dump()
    layers = [
        {"id": "a", "effect": "spiral", "pen": 1},
        {"id": "b", "effect": "waves", "pen": 2, "place": {"x": 0.5, "w": 0.5}},
        {"id": "c", "effect": "halftone", "pen": 3, "visible": False},
    ]
    d = client.post("/api/plan", json={"layout": lay, "layers": layers}).json()
    assert [l["id"] for l in d["layers"]] == ["a", "b"] and d["stats"]["pen_changes"] == 1
    assert d["layers"][1]["color"] != d["layers"][0]["color"]
    xs = np.vstack([np.array(l) for l in d["layers"][1]["lines"]])[:, 0]
    assert xs.min() >= lay["area"]["w"] / 2 - 1e-6  # layer b sits in the right half
    g = client.post("/api/gcode", json={"layout": lay, "layers": layers})
    assert g.status_code == 200 and "swap to Pen 2 (blue)" in g.text
    none = client.post("/api/gcode", json={"layout": lay, "layers": [{"id": "x", "visible": False}]})
    assert none.status_code == 422


def test_project_keeps_one_picture_per_layer(home):
    client = TestClient(app)
    p = client.post("/api/projects", json={"name": "Layers"}).json()
    up1 = client.post("/api/image", files={"file": ("blue.png", png(), "image/png")}).json()
    up2 = client.post("/api/image", files={"file": ("red.png", png((200, 30, 30)), "image/png")}).json()
    layers = [{"id": "a", "effect": "trace", "image_id": up1["id"], "pen": 1},
              {"id": "b", "effect": "flow", "image_id": up2["id"], "pen": 2}]
    r = client.put(f"/api/projects/{p['id']}", json={"state": {"layers": layers, "active": 1}})
    assert r.status_code == 200 and r.json()["has_thumb"]
    opened = client.get(f"/api/projects/{p['id']}").json()
    assert set(opened["images"]) == {up1["id"], up2["id"]}
    assert opened["state"]["layers"][1]["effect"] == "flow" and opened["state"]["active"] == 1
    assert client.get(f"/api/projects/{p['id']}/sources/{up2['id']}").content == png((200, 30, 30))
    # Dropping a layer removes its picture from the project.
    client.put(f"/api/projects/{p['id']}", json={"state": {"layers": layers[:1]}})
    assert set(client.get(f"/api/projects/{p['id']}").json()["images"]) == {up1["id"]}
