import numpy as np
import pytest
from fastapi.testclient import TestClient

from bambuplot.calibrate import alignment_correction, alignment_test
from bambuplot.effects import alignment_target
from bambuplot.layout import Layout
from bambuplot.server import app


def test_target_is_centered_with_a_circle_cross_and_arrow():
    lines = alignment_target(100, 80, 25, ticks=True)
    circle = lines[0]
    assert np.allclose(circle.mean(axis=0), [50, 40], atol=0.2)
    assert np.allclose(np.hypot(circle[:, 0] - 50, circle[:, 1] - 40), 25)
    pts = np.vstack(lines)
    assert pts[:, 0].min() >= 0 and pts[:, 0].max() <= 100 and pts[:, 1].max() <= 80


def test_radius_shrinks_to_fit_a_small_area():
    circle = alignment_target(30, 30, 60, ticks=False)[0]
    assert np.hypot(*(circle[0] - [15, 15])) <= 11 + 1e-9


def test_alignment_job_is_safe_gcode():
    text = alignment_test(Layout())
    assert "alignment test" in text and text.count("M400 U1") == 3


def test_perfect_measurement_means_no_correction():
    lay = Layout()
    c = alignment_correction(lay, 74.0, 105.0)  # area center 90,90 on a sheet at 16,-15
    assert c["error_x"] == pytest.approx(0) and c["error_y"] == pytest.approx(0)


def test_correction_signs():
    lay = Layout()
    c = alignment_correction(lay, 76.0, 104.0)  # landed 2 mm right and 1 mm toward the front
    assert c["move_sheet"] == pytest.approx([2, -1])
    assert c["paper_xy"] == pytest.approx([lay.paper.x - 2, lay.paper.y + 1])
    assert c["pen_offset"] == pytest.approx([2, -1])


def test_correction_on_a_turned_sheet_is_converted_to_the_plate():
    lay = Layout()
    lay.paper.rotation = 90  # sheet's +X now points to the plate's +Y
    exp = alignment_correction(lay, 0, 0)
    c = alignment_correction(lay, exp["expected_left"] + 3, exp["expected_front"])
    assert c["error_bed_x"] == pytest.approx(0, abs=1e-9) and c["error_bed_y"] == pytest.approx(3)


def test_applying_the_paper_fix_makes_the_next_check_exact():
    lay = Layout()
    c = alignment_correction(lay, 77.5, 103.5)
    lay.paper.x, lay.paper.y = c["paper_xy"]
    again = alignment_correction(lay, 77.5, 103.5)
    assert again["error_x"] == pytest.approx(0) and again["error_y"] == pytest.approx(0)


def test_api():
    client = TestClient(app)
    lay = Layout().model_dump()
    assert client.post("/api/alignment", json={"layout": lay}).json() == {"expected_left": 74.0, "expected_front": 105.0}
    c = client.post("/api/alignment", json={"layout": lay, "left": 75, "front": 105}).json()
    assert c["error_x"] == pytest.approx(1)
    assert client.post("/api/calibrate/align", json=lay).status_code == 200
