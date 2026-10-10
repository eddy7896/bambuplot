import numpy as np
import pytest

from bambuplot import effects
from bambuplot.image import Prep, prepare
from bambuplot.layout import Layout
from bambuplot.pipeline import make_plan

W, H = 120.0, 90.0


def tone():
    return prepare(None, W, H, Prep())


def inside(lines, w=W, h=H, tol=1e-6):
    pts = np.vstack(lines)
    return pts[:, 0].min() >= -tol and pts[:, 1].min() >= -tol and pts[:, 0].max() <= w + tol and pts[:, 1].max() <= h + tol


@pytest.mark.parametrize("angle", [0, 30, -60, 90])
def test_waves_at_any_angle_stay_inside_and_run_the_right_way(angle):
    lines = effects.run("waves", tone(), {"angle": angle, "continuous": False})
    assert lines and inside(lines)
    longest = max(lines, key=len)
    d = longest[-1] - longest[0]
    got = np.degrees(np.arctan2(d[1], d[0])) % 180
    assert min(abs(got - angle % 180), 180 - abs(got - angle % 180)) < 15


def test_bigger_wave_amplitude_lays_more_ink():
    t = tone()
    ink = lambda a: sum(np.hypot(*np.diff(l, axis=0).T).sum() for l in effects.run("waves", t, {"amplitude": a}))  # noqa
    assert ink(0.9) > ink(0.3)


@pytest.mark.parametrize("shape", ["circle", "square", "line", "cross"])
def test_halftone_shapes(shape):
    lines = effects.run("halftone", tone(), {"shape": shape})
    assert lines and inside(lines)


def test_spiral_off_center_and_filling_corners():
    lines = effects.run("spiral", tone(), {"center_x": 0.3, "fill_corners": True})
    pts = np.vstack(lines)
    assert inside(lines) and pts[:, 0].max() > W - 3 and pts[:, 0].min() < 3


def test_stipple_dots_with_size_are_small_filled_circles():
    lines = effects.run("stipple", tone(), {"mode": "dots", "dot_size": 1.0, "points": 300, "iterations": 2})
    assert len(lines) == 300 and inside(lines)
    assert max(np.ptp(l[:, 0]) for l in lines) <= 1.0 + 1e-6


def test_crosshatch_adds_layers():
    t = tone()
    one = effects.run("crosshatch", t, {"layers": 1})
    four = effects.run("crosshatch", t, {"layers": 4})
    assert len(four) > len(one) and inside(four)


def test_flow_lines_respect_spacing_and_skip_paper_white():
    lines = effects.run("flow", tone(), {"min_spacing": 1.0, "max_spacing": 4.0})
    assert lines and inside(lines)
    light = type(tone())(np.full((360, 480), 0.7, np.float32), W, H)  # darkness 0.3 everywhere
    assert not effects.run("flow", light, {"paper_white": 0.35})


@pytest.mark.parametrize("method", ["Floyd–Steinberg", "Atkinson", "ordered"])
def test_dither_dot_count_follows_darkness(method):
    flat = lambda v: type(tone())(np.full((360, 480), v, np.float32), W, H)  # noqa: E731
    dark = len(effects.run("dither", flat(0.2), {"cell": 2.0, "method": method}))
    light = len(effects.run("dither", flat(0.8), {"cell": 2.0, "method": method}))
    cells = int(W / 2) * int(H / 2)
    assert dark > light
    if method == "Atkinson":  # spreads only 6/8 of the error, so it runs lighter by design
        assert light < 0.2 * cells
    else:
        assert dark == pytest.approx(0.8 * cells, rel=0.1) and light == pytest.approx(0.2 * cells, rel=0.15)


def test_hilbert_visits_every_cell_once_with_unit_steps():
    pts = effects._hilbert(4)
    assert len({tuple(p) for p in pts}) == 256
    assert np.all(np.abs(np.diff(pts, axis=0)).sum(axis=1) == 1)


def test_squiggle_stays_inside():
    lines = effects.run("squiggle", tone(), {"cell": 3.0})
    assert lines and inside(lines)


def test_paper_white_blanks_light_areas():
    t = prepare(None, W, H, Prep(paper_white=0.3))
    assert (t.data[1.0 - t.data < 0.3] == 1.0).all()


def test_params_describe_units_groups_and_conditions():
    d = effects.EFFECTS["stipple"].describe()
    assert d["group"] == "Dots"
    dot = next(p for p in d["params"] if p["name"] == "dot_size")
    assert dot["unit"] == "mm" and dot["when"] == {"mode": ["dots"]}


def test_coerce_rejects_nan():
    assert effects.EFFECTS["waves"].coerce({"spacing": float("nan")})["spacing"] == 2.5
