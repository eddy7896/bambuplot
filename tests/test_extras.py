import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bambuplot import extras, printer
from bambuplot.extras import Extras
from bambuplot.layout import Layout
from bambuplot.pipeline import make_plan, plan_to_gcode
from bambuplot.server import app

SIG = [[[0, 0], [1, 2], [2, 0], [3, 2]], [[0.5, 1], [2.5, 1]]]  # a zig-zag "W" with a cross stroke


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_normalize_scales_to_height_one_from_origin():
    n = extras.normalize([[[10, 20], [14, 24]], [[12, 22], [16, 20]]])
    pts = np.vstack([np.array(s) for s in n])
    assert pts.min(axis=0) == pytest.approx([0, 0]) and pts[:, 1].max() == pytest.approx(1)


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        extras.normalize([[], [[float("nan"), 1]]])


def test_border_is_a_closed_rectangle_at_the_inset():
    lay = Layout()
    plan = make_plan(lay, "spiral", extras=Extras(border=True, border_inset=2))
    a = lay.area
    closed = [l for l in plan.lines if np.allclose(l[0], l[-1]) and len(l) == 5]
    assert closed and closed[0].min() == pytest.approx(2) and closed[0][:, 0].max() == pytest.approx(a.w - 2)


@pytest.mark.parametrize("corner", ["br", "bl", "bc", "tr", "tl"])
def test_signature_lands_in_the_chosen_corner(corner):
    sig = [np.array(s, float) for s in extras.normalize(SIG)]
    ex = Extras(signature=True, sig_corner=corner, sig_height=10, sig_margin=4)
    placed, (x0, y0, x1, y1) = extras.place_signature(sig, 120, 80, ex)
    assert y1 - y0 == pytest.approx(10)
    if corner in ("br", "tr"):
        assert x1 == pytest.approx(116)
    if corner in ("bl", "tl"):
        assert x0 == pytest.approx(4)
    if corner.startswith("t"):
        assert y1 == pytest.approx(76)
    else:
        assert y0 == pytest.approx(4)


def test_signature_shrinks_to_fit_a_narrow_area():
    sig = [np.array(s, float) for s in extras.normalize(SIG)]
    placed, (x0, _, x1, _) = extras.place_signature(sig, 20, 80, Extras(signature=True, sig_height=30, sig_margin=2))
    assert x1 - x0 <= 16 + 1e-9


def test_clear_behind_removes_art_under_the_signature_only():
    sig = [np.array(s, float) for s in extras.normalize(SIG)]
    art = [np.array([[0, 6], [100, 6]])]  # a line through the bottom margin, under a bottom-right signature
    ex = Extras(signature=True, sig_corner="br", sig_height=10, sig_margin=2, sig_clear=True)
    out = extras.apply(art, 100, 50, ex, sig)
    _, (x0, y0, x1, y1) = extras.place_signature(sig, 100, 50, ex)
    kept = [l for l in out if len(l) == 2 and np.allclose(l[:, 1], 6)]
    assert len(kept) == 2  # the line stops before the signature and picks up after it
    for l in kept:
        assert not ((l[:, 0] > x0 - 1) & (l[:, 0] < x1 + 1)).any()
    keep_all = extras.apply(art, 100, 50, ex.model_copy(update={"sig_clear": False}), sig)
    assert any(np.allclose(l, art[0]) for l in keep_all)


def test_signature_in_a_real_job_is_safe(home):
    extras.save(SIG)
    lay = Layout()
    plan = make_plan(lay, "waves", extras=Extras(signature=True, border=True), signature=extras.load())
    assert "M400 U1" in plan_to_gcode(lay, plan)


def test_trace_signature_from_a_picture():
    img = np.full((120, 360), 255, np.uint8)
    cv2.putText(img, "Faheem", (10, 85), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 2.4, 0, 5)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    strokes = extras.trace_image(buf.getvalue())
    pts = np.vstack([np.array(s) for s in strokes])
    assert len(strokes) >= 3 and pts[:, 1].max() == pytest.approx(1) and pts[:, 0].max() > 2


def test_api_signature_and_plan(home):
    client = TestClient(app)
    assert client.get("/api/signature").json()["has"] is False
    assert client.put("/api/signature", json={"strokes": SIG}).json()["has"] is True
    body = {"effect": "spiral", "layout": Layout().model_dump(), "extras": {"signature": True, "border": True}}
    with_sig = client.post("/api/plan", json=body).json()["stats"]["paths"]
    without = client.post("/api/plan", json={**body, "extras": {}}).json()["stats"]["paths"]
    assert with_sig > without
    client.delete("/api/signature")
    assert client.get("/api/signature").json()["has"] is False
