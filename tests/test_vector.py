import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from bambuplot import vectorize as vz
from bambuplot.effects import EFFECTS
from bambuplot.layout import Layout
from bambuplot.pipeline import make_plan, plan_to_gcode
from bambuplot.server import app
from bambuplot.svgin import SvgError, load_svg


def length(l):
    return float(np.hypot(*np.diff(l, axis=0).T).sum())


def white(n=400):
    return np.full((n, n), 255, np.uint8)


# ------------------------------------------------------------------ raster


def test_outlines_of_a_black_square_match_its_perimeter():
    img = white()
    cv2.rectangle(img, (100, 100), (299, 299), 0, -1)  # 200 px = 50 mm at 4 px/mm
    lines = vz.outlines(vz.threshold(img / 255.0, 0.5), 100, 100)
    assert len(lines) == 1 and length(lines[0]) == pytest.approx(200, rel=0.03)


def test_hatch_stays_inside_the_mask():
    mask = np.zeros((400, 400), bool)
    mask[100:300, 100:300] = True  # area mm 25..75 both ways
    for l in vz.hatch(mask, 100, 100, spacing=1.0, angle=30):
        assert l.min() >= 24.8 and l.max() <= 75.2


def test_centerline_of_a_thick_stroke_is_one_line_down_the_middle():
    img = white()
    cv2.line(img, (40, 200), (360, 200), 0, 15)  # 15 px thick, 80 mm long
    lines = vz.centerlines(vz.threshold(img / 255.0, 0.5), 100, 100, min_len=2)
    assert len(lines) == 1
    assert length(lines[0]) == pytest.approx(80, abs=4)
    assert np.allclose(lines[0][:, 1], 50, atol=0.5)  # along the middle


def test_centerline_crossing_lines_keep_their_total_length():
    img = white()
    cv2.line(img, (40, 200), (360, 200), 0, 9)
    cv2.line(img, (200, 40), (200, 360), 0, 9)
    lines = vz.centerlines(vz.threshold(img / 255.0, 0.5), 100, 100, min_len=2)
    assert sum(length(l) for l in lines) == pytest.approx(160, abs=8)


@pytest.mark.parametrize("effect", ["trace", "centerline", "contours"])
def test_trace_effects_make_safe_gcode(effect):
    img = white(600)
    cv2.circle(img, (300, 300), 200, 0, 25)
    lay = Layout()
    plan = make_plan(lay, effect, gray=img / 255.0)
    assert plan.lines
    assert "M400 U1" in plan_to_gcode(lay, plan)


def test_trace_levels_add_hatch_angles():
    gray = np.tile(np.linspace(0, 1, 400, dtype=np.float32), (400, 1))
    lay = Layout()
    one = make_plan(lay, "trace", {"levels": 1, "outline": False}, gray=gray)
    three = make_plan(lay, "trace", {"levels": 3, "outline": False}, gray=gray)
    assert three.stats["draw_m"] > one.stats["draw_m"]


# --------------------------------------------------------------------- svg

SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="80mm" height="40mm" viewBox="0 0 80 40">
<rect width="80" height="40" fill="white"/>
<g transform="rotate(5 40 20)"><rect x="5" y="5" width="30" height="30" fill="black"/>
<rect x="15" y="15" width="10" height="10" fill="#fff"/></g>
<path d="M45 35 C 55 5, 65 5, 75 35" stroke="#000" fill="none"/>
<circle cx="60" cy="25" r="3" fill="none" stroke="#000" visibility="hidden"/>
<text x="1" y="5">label</text></svg>"""


def test_svg_reads_strokes_fills_and_skips_hidden_and_text():
    art = load_svg(SVG)
    assert len(art.strokes) == 1 and len(art.fills) == 3
    assert any("text" in w for w in art.warnings)
    assert art.size_mm() == pytest.approx((80, 40), abs=0.5)


def test_svg_only_text_gives_a_helpful_error():
    with pytest.raises(SvgError, match="convert the text|Convert the text"):
        load_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><text>hi</text></svg>')


def test_svg_fit_stays_in_area_and_white_background_is_not_outlined():
    lay = Layout()
    plan = make_plan(lay, "svg", {"hatch_fills": False}, svg=load_svg(SVG))
    pts = np.vstack(plan.lines)
    assert pts.min() >= -1e-6 and pts[:, 0].max() <= lay.area.w + 1e-6 and pts[:, 1].max() <= lay.area.h + 1e-6
    # Outlines: black square + white hole (it covers black art) + the curve. Not the page background.
    closed = [l for l in plan.lines if np.allclose(l[0], l[-1])]
    assert len(closed) == 2


def test_svg_hatch_skips_the_white_hole():
    lay = Layout()
    art = load_svg(SVG)
    plan = make_plan(lay, "svg", {"outline_fills": False, "strokes": False, "fit": "actual size"}, svg=art)
    # Actual size: 1 svg unit = 1 mm, centered. The hole's middle (40-20, ...) has no ink.
    cx, cy = lay.area.w / 2 - 20, lay.area.h / 2  # hole center in area mm
    near = [l for l in plan.lines if np.any(np.hypot(l[:, 0] - cx, l[:, 1] - cy) < 3)]
    assert not near


def test_svg_actual_size_larger_than_area_is_clipped():
    big = b'<svg xmlns="http://www.w3.org/2000/svg" width="400mm" height="20mm" viewBox="0 0 400 20"><path d="M0 10 H400" stroke="black"/></svg>'
    lay = Layout()
    plan = make_plan(lay, "svg", {"fit": "actual size"}, svg=load_svg(big))
    assert plan.lines and np.vstack(plan.lines)[:, 0].max() <= lay.area.w + 1e-6


def test_effects_and_sources_must_match():
    lay = Layout()
    with pytest.raises(ValueError):
        make_plan(lay, "svg")
    with pytest.raises(ValueError):
        make_plan(lay, "waves", svg=load_svg(SVG))
    assert EFFECTS["svg"].source == "svg" and EFFECTS["trace"].source == "raster"


def test_api_svg_upload_and_plan():
    client = TestClient(app)
    up = client.post("/api/image", files={"file": ("logo.svg", SVG, "image/svg+xml")}).json()
    assert up["kind"] == "svg" and up["warnings"]
    body = {"effect": "svg", "image_id": up["id"], "layout": Layout().model_dump()}
    assert client.post("/api/plan", json=body).json()["lines"]
    wrong = client.post("/api/plan", json={**body, "effect": "waves"})
    assert wrong.status_code == 422
