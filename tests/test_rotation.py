import re

import numpy as np
import pytest

from bambuplot.fit import center_area, fit_area
from bambuplot.gcode import UnsafeLayout
from bambuplot.image import Prep, prepare
from bambuplot.layout import Area, Layout, contains, inside
from bambuplot.pipeline import make_plan, plan_to_gcode

XY = re.compile(r"^G1 X(-?[\d.]+) Y(-?[\d.]+)")


def art_points(text):
    art = text.split("; ===== artwork =====", 1)[1]
    return np.array([[float(m.group(1)), float(m.group(2))] for l in art.splitlines() if (m := XY.match(l))])


def test_area_corners_rotate_about_center():
    a = Area(x=40, y=40, w=60, h=20, rotation=90)
    cx, cy = a.center
    world = a.to_world([[0, 0], [60, 0]])
    # The front edge (along local +X) now runs along world +Y.
    assert np.allclose(world[1] - world[0], [0, 60])
    # The front edge's midpoint sits half the height away from the center, now along +X.
    assert np.allclose(world.mean(0), [cx + 10, cy])


def test_rotated_area_poking_out_of_paper_is_refused():
    lay = Layout()
    lay.area = Area(x=22, y=24, w=136, h=132, rotation=20)  # fits square-on, not when turned
    assert not lay.is_safe
    with pytest.raises(UnsafeLayout):
        plan_to_gcode(lay, make_plan(lay, "spiral"))


@pytest.mark.parametrize("paper_rot,area_rot", [(0, 0), (30, 30), (-15, 0), (12, 47)])
def test_fit_gives_a_safe_area_at_any_rotation(paper_rot, area_rot):
    lay = Layout()
    lay.paper.rotation = paper_rot
    lay.area.rotation = area_rot
    a = fit_area(lay)
    assert a is not None and a.rotation == area_rot
    lay.area = a
    assert lay.is_safe, [c for c in lay.checks() if c.state == "bad"]
    assert a.w * a.h > 3000  # a real, usable area


def test_center_keeps_size_and_stays_safe():
    lay = Layout()
    lay.area = Area(x=30, y=40, w=60, h=40, rotation=25)
    moved = center_area(lay)
    assert moved is not None and (moved.w, moved.h, moved.rotation) == (60, 40, 25)
    lay.area = moved
    assert lay.is_safe


def test_rotated_job_stays_inside_rotated_area():
    lay = Layout()
    lay.paper.rotation = 30
    lay.area.rotation = 30
    lay.area = fit_area(lay)
    pts = art_points(plan_to_gcode(lay, make_plan(lay, "waves")))
    grown = Area(**{**lay.area.model_dump(), "w": lay.area.w + 0.05, "h": lay.area.h + 0.05,
                    "x": lay.area.x - 0.025, "y": lay.area.y - 0.025})
    assert inside(grown.poly(), pts).all()


def test_rotated_paper_overhang_is_measured_from_corners():
    lay = Layout()
    lay.paper.rotation = 45
    hang = next(c for c in lay.checks() if c.title == "Paper overhang")
    assert hang.state == "warn" and "left" in hang.detail and "right" in hang.detail


def test_prep_rotate_turns_the_image_clockwise():
    gray = np.ones((20, 20), np.float32)
    gray[:, :10] = 0.0  # left half black
    tone = prepare(gray, 20, 20, Prep(clahe=False, rotate=90))
    # After a clockwise quarter turn the black half is on top (back of the area).
    assert tone.sample(10, 17) < 0.1 and tone.sample(10, 3) > 0.9


def test_contains_helper():
    outer = Area(x=0, y=0, w=10, h=10).poly()
    assert contains(outer, Area(x=1, y=1, w=8, h=8, rotation=0).poly())
    assert not contains(outer, Area(x=1, y=1, w=8, h=8, rotation=45).poly())
