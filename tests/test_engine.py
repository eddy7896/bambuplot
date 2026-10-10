import io

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from bambuplot import effects
from bambuplot.image import Prep, cover_crop, prepare
from bambuplot.layout import Layout, Rect
from bambuplot.paths import merge, order_paths, rdp
from bambuplot.server import app
from bambuplot.tsp import nn_tour, path_length, two_opt


def test_tone_rows_run_back_to_front():
    gray = np.zeros((40, 40), np.float32)
    gray[:20] = 1.0  # top half of the image is white
    tone = prepare(gray, 40, 40, Prep(clahe=False))
    assert tone.sample(20, 35) > 0.9  # back of the area (high Y) = top of image
    assert tone.sample(20, 5) < 0.1


def test_cover_crop_matches_aspect():
    out = cover_crop(np.zeros((100, 300)), 1.0)
    assert out.shape == (100, 100)


def test_coerce_clamps_and_fills_defaults():
    e = effects.EFFECTS["waves"]
    v = e.coerce({"spacing": "0.1", "bogus": 1})
    assert v["spacing"] == 0.8 and "bogus" not in v and v["continuous"] is True


def test_order_paths_reverses_when_end_is_closer():
    a = np.array([[0.0, 0], [10, 0]])
    b = np.array([[30.0, 0], [11, 0]])
    out = order_paths([b, a], start=(0, 0))
    assert np.allclose(out[0][0], [0, 0]) and np.allclose(out[1][0], [11, 0])


def test_merge_joins_touching_paths():
    a = np.array([[0.0, 0], [10, 0]])
    b = np.array([[10.1, 0], [20, 0]])
    assert len(merge([a, b], tol=0.2)) == 1


def test_rdp_drops_collinear_points():
    line = np.column_stack([np.linspace(0, 10, 50), np.zeros(50)])
    assert len(rdp(line, 0.01)) == 2


def test_two_opt_never_makes_tour_longer():
    rng = np.random.default_rng(1)
    pts = rng.uniform(0, 100, (300, 2))
    nn = nn_tour(pts)
    better = two_opt(pts, nn, time_limit=1.0)
    assert sorted(better) == list(range(300))
    assert path_length(pts[better]) <= path_length(pts[nn]) + 1e-9


def test_api_plan_and_gcode_round_trip():
    client = TestClient(app)
    buf = io.BytesIO()
    Image.new("L", (64, 48), 90).save(buf, "PNG")
    img = client.post("/api/image", files={"file": ("g.png", buf.getvalue(), "image/png")}).json()
    body = {"effect": "waves", "image_id": img["id"], "layout": Layout().model_dump()}
    plan = client.post("/api/plan", json=body)
    assert plan.status_code == 200 and plan.json()["lines"]
    g = client.post("/api/gcode", json=body)
    assert g.status_code == 200 and "M400 U1" in g.text


def test_api_refuses_unsafe_layout():
    client = TestClient(app)
    lay = Layout()
    lay.area = Rect(x=0, y=0, w=50, h=50)
    r = client.post("/api/gcode", json={"effect": "spiral", "layout": lay.model_dump()})
    assert r.status_code == 422 and r.json()["detail"]["problems"]
