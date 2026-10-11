import numpy as np
import pytest

from bambuplot.image import Prep, prepare


def quadrants():
    """200 × 100 picture: top-left quarter black, the rest white."""
    g = np.ones((100, 200), np.float32)
    g[:50, :100] = 0
    return g


def test_crop_picks_the_chosen_part():
    t = prepare(quadrants(), 50, 50, Prep(clahe=False, crop=[0, 0, 0.5, 0.5]))
    assert t.data.mean() == pytest.approx(0, abs=0.01)
    t = prepare(quadrants(), 50, 50, Prep(clahe=False, crop=[0.5, 0.5, 1, 1]))
    assert t.data.mean() == pytest.approx(1, abs=0.01)


def test_crop_is_in_original_orientation_even_when_turned():
    # Crop the black corner, then turn it: it must still be all black.
    t = prepare(quadrants(), 40, 40, Prep(clahe=False, crop=[0, 0, 0.5, 0.5], rotate=90))
    assert t.data.mean() == pytest.approx(0, abs=0.01)


def test_fit_keeps_the_whole_picture_with_white_margins():
    t = prepare(quadrants(), 50, 50, Prep(clahe=False, fit="fit"))
    rows = t.data.shape[0]
    assert t.data[: rows // 5].mean() == pytest.approx(1)  # blank band above
    assert t.data[-rows // 5:].mean() == pytest.approx(1)  # and below
    assert t.data[rows // 2 - 2, :5].mean() < 0.05  # black corner is still there, not trimmed


def test_fill_trims_instead():
    t = prepare(quadrants(), 50, 50, Prep(clahe=False, fit="fill"))
    assert t.data[:5].mean() < 0.6  # no blank band: the picture reaches the top


def test_fit_margins_stay_white_even_inverted():
    t = prepare(quadrants(), 50, 50, Prep(clahe=False, fit="fit", invert=True))
    assert t.data[:3].mean() == pytest.approx(1)


@pytest.mark.parametrize("raw,expected", [
    ([0.5, 0.6, 0.2, 0.1], [0.2, 0.1, 0.5, 0.6]),
    ([0, 0, 1, 1], None),
    ([-1, -1, 2, 2], None),
    ([0.1, 0.1, 0.105, 0.5], None),
    (["a"], None),
])
def test_crop_values_are_cleaned(raw, expected):
    assert Prep(crop=raw).crop == expected
