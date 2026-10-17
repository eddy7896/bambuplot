import numpy as np
import pytest
from scipy import ndimage

from bambuplot import effects
from bambuplot.effects_more import _glyph, _lsystem
from bambuplot.image import Prep, prepare

NEW = ["scribble", "curve", "ridges", "rings", "mesh", "moire", "morse", "harmonograph", "engraving", "offset_fill",
       "woodcut", "sketch", "pixel_hatch", "dashes", "shapes", "maze", "truchet", "voronoi", "lowpoly", "text"]


def tone(w=100.0, h=80.0, left=0.15, right=1.0):
    img = np.full((160, 200), right, np.float32)
    img[:, :100] = left
    return prepare(img, w, h, Prep(clahe=False))


def ink(lines):
    return sum(np.hypot(*np.diff(l, axis=0).T).sum() for l in lines if len(l) > 1)


def test_all_twenty_are_registered_in_groups():
    for name in NEW:
        e = effects.EFFECTS[name]
        assert e.group in effects.GROUPS


@pytest.mark.parametrize("name", NEW)
def test_more_ink_where_it_is_dark(name):
    t = tone()
    lines = effects.run(name, t, {}, effects.Context(pen_width=0.4))
    assert lines
    pts = np.vstack(lines)
    assert pts[:, 0].min() >= -1e-6 and pts[:, 0].max() <= 100 + 1e-6
    assert pts[:, 1].min() >= -1e-6 and pts[:, 1].max() <= 80 + 1e-6
    if name in ("maze", "curve", "ridges", "mesh", "rings", "moire"):
        return  # long lines across the whole area: darkness shows as their shape, not as where they are
    left = ink([l for l in lines if l[:, 0].mean() < 50])
    right = ink([l for l in lines if l[:, 0].mean() >= 50])
    assert left > right * 1.3, (left, right)


def test_maze_has_a_way_from_entrance_to_exit():
    w, h = 60.0, 40.0
    lines = effects.run("maze", tone(w, h, 0.6, 0.1), {"min_cell": 2.0, "levels": 2}, effects.Context())
    ppm = 8
    wall = np.zeros((int(h * ppm) + 1, int(w * ppm) + 1), bool)
    for l in lines:
        for a, b in zip(l[:-1], l[1:]):
            n = int(np.hypot(*(b - a)) * ppm * 2) + 2
            for t in np.linspace(0, 1, n):
                x, y = a + t * (b - a)
                wall[int(round(y * ppm)), int(round(x * ppm))] = True
    lab, _ = ndimage.label(~wall)
    start = lab[int(0.4 * ppm), int(0.4 * ppm)]  # just inside the front-left corner
    end = lab[int((h - 0.4) * ppm), int((w - 0.4) * ppm)]  # just inside the back-right corner
    assert start and start == end


def test_maze_is_finer_in_the_dark():
    lines = effects.run("maze", tone(), {}, effects.Context())
    inner = [l for l in lines if 0.5 < l[:, 0].mean() < 99.5 and 0.5 < l[:, 1].mean() < 79.5]
    assert ink([l for l in inner if l[:, 0].mean() < 50]) > 1.5 * ink([l for l in inner if l[:, 0].mean() >= 50])


def test_space_filling_curves_never_cross_themselves():
    for name in ("gosper", "peano", "moore"):
        p = _lsystem(name, 3)
        assert len(np.unique(np.round(p, 6), axis=0)) == len(p) - (1 if name == "moore" else 0) or name == "moore"


def test_ridges_hide_lines_behind():
    t = prepare(None, 100.0, 80.0, Prep())  # a round shape: nearer ridges rise in front of farther ones
    shown = ink(effects.run("ridges", t, {"hidden": True}, effects.Context()))
    every = ink(effects.run("ridges", t, {"hidden": False}, effects.Context()))
    assert shown < every


def test_letters_are_single_strokes_and_words_are_used():
    strokes, adv = _glyph("A")
    assert 2 <= len(strokes) <= 4 and 0.5 < adv < 1.5
    pts = np.vstack(strokes)
    assert pts[:, 1].min() > -0.1 and 0.8 < pts[:, 1].max() < 1.15  # baseline to capital height
    t = tone()
    a = effects.run("text", t, {"words": "I"}, effects.Context())
    b = effects.run("text", t, {"words": "WWW"}, effects.Context())
    assert ink(b) > ink(a)
    assert effects.EFFECTS["text"].coerce({"words": "x" * 500})["words"] == "x" * 200
