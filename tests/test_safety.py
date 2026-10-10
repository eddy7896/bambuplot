import re

import numpy as np
import pytest

from bambuplot.calibrate import press_ladder, smoke_test
from bambuplot.gcode import Stroke, UnsafeLayout, compile_strokes
from bambuplot.layout import Area, Layout
from bambuplot.pipeline import make_plan, plan_to_gcode
from bambuplot.safety import GcodeSafetyError, validate

MOVE = re.compile(r"^G1 (.*?)\s*(;|$)")


def moves(text):
    for line in text.splitlines():
        m = MOVE.match(line)
        if m:
            yield {w[0]: float(w[1:]) for w in m.group(1).split() if w[0] in "XYZ"}


def test_validator_rejects_heating_extrusion_and_unknown_codes():
    base = "G90\nG28\n"
    assert validate(base + "M104 S200\n", min_z=2)
    assert validate(base + "G1 X10 Y10 E5\n", min_z=2)
    assert validate(base + "M620 S0\n", min_z=2)
    assert validate(base + "G1 X10 Y10 Z3\n", min_z=2) == []


def test_validator_rejects_moves_off_plate_or_too_low_or_before_homing():
    assert validate("G90\nG28\nG1 X181 Y10\n", min_z=2)
    assert validate("G90\nG28\nG1 X10 Y-0.5\n", min_z=2)
    assert validate("G90\nG28\nG1 Z1.9\n", min_z=2)
    assert validate("G90\nG1 X10 Y10\nG28\n", min_z=2)


from bambuplot.effects import EFFECTS

RASTER = [n for n, e in EFFECTS.items() if e.source == "raster"]
FAST = {"stipple": {"points": 400, "iterations": 2, "optimize_seconds": 0.2}, "dither": {"cell": 2.0}}


@pytest.mark.parametrize("effect", RASTER)
def test_every_effect_stays_inside_area_and_above_pen_down(effect):
    layout = Layout()
    params = FAST.get(effect, {})
    text = plan_to_gcode(layout, make_plan(layout, effect, params))
    a = layout.area
    art = text.split("; ===== artwork =====", 1)[1]
    for m in moves(art):
        if "X" in m:
            assert a.x - 1e-6 <= m["X"] <= a.x2 + 1e-6
            assert a.y - 1e-6 <= m["Y"] <= a.y2 + 1e-6
        if "Z" in m:
            assert m["Z"] >= layout.z.down - 1e-9


def test_pen_offset_shifts_nozzle_coordinates():
    layout = Layout()
    layout.pen.offset_x, layout.pen.offset_y = 10.0, -12.0
    text = compile_strokes(layout, [Stroke(np.array([[50.0, 60.0], [70.0, 60.0]]))])
    assert "G1 X40.00 Y72.00 F5000" in text  # nozzle = pen - offset


def test_unsafe_layout_is_refused():
    layout = Layout()
    layout.area = Area(x=22, y=-5, w=100, h=100)  # off the front of the plate, over the clips
    with pytest.raises(UnsafeLayout):
        compile_strokes(layout, [Stroke(np.array([[30.0, 30.0], [40.0, 30.0]]))])


def test_offset_that_pushes_nozzle_off_plate_is_refused():
    layout = Layout()
    layout.pen.offset_x = 30.0  # pen is right of the nozzle, so the left strip is unreachable
    assert not layout.is_safe
    with pytest.raises(UnsafeLayout):
        smoke_test(layout)


def test_stroke_outside_plate_is_caught_by_validator():
    layout = Layout()
    with pytest.raises(GcodeSafetyError):
        compile_strokes(layout, [Stroke(np.array([[30.0, 30.0], [200.0, 30.0]]))])


def test_start_sequence_has_three_pauses_and_homes_first():
    text = smoke_test(Layout())
    code = [l.split(";")[0].strip() for l in text.splitlines() if l.split(";")[0].strip()]
    assert code.count("M400 U1") == 3
    assert code.index("G28") < next(i for i, c in enumerate(code) if c.startswith("G1"))


def test_press_ladder_goes_no_deeper_than_requested():
    layout = Layout()
    text = press_ladder(layout, presses=(0.0, 0.1, 0.2))
    zs = [m["Z"] for m in moves(text) if "Z" in m]
    assert min(zs) == pytest.approx(layout.z.touch - 0.2)
