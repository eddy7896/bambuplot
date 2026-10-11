import pytest

from bambuplot.layout import BED_H, BED_W, PAPER_SIZES, Area, Layout, Paper


def test_all_presets_have_portrait_dimensions_and_landscape_swaps():
    for key, (w, h) in PAPER_SIZES.items():
        assert w <= h
        assert Paper(size=key, orientation="landscape").dims() == (h, w)


def test_custom_size_uses_width_and_height():
    p = Paper(size="custom", width=120, height=90, orientation="portrait")
    assert p.dims() == (120, 90)


def test_unknown_size_falls_back_to_a5():
    assert Paper(size="A0").size == "A5"


@pytest.mark.parametrize("w,h", [(5, 100), (100, 500)])
def test_custom_size_limits(w, h):
    with pytest.raises(ValueError):
        Paper(size="custom", width=w, height=h)


def test_edge_distances_and_overhang():
    lay = Layout(paper=Paper(size="A5", x=16, y=-15))
    d = lay.edge_distances(lay.paper.poly())
    assert d["left"] == pytest.approx(16) and d["right"] == pytest.approx(BED_W - 16 - 148)
    assert d["front"] == pytest.approx(-15) and d["back"] == pytest.approx(BED_H - 195)  # -15: hangs over


def test_edge_distances_of_a_rotated_area_use_its_outer_corners():
    lay = Layout()
    lay.area = Area(x=40, y=40, w=100, h=100, rotation=45)
    d = lay.edge_distances(lay.area.poly())
    half_diag = 50 * 2**0.5
    assert d["left"] == pytest.approx(90 - half_diag) and d["back"] == pytest.approx(90 - half_diag)


def test_custom_paper_safety_checks():
    lay = Layout(paper=Paper(size="custom", width=170, height=170, x=5, y=5))
    lay.clips = []
    lay.area = Area(x=15, y=15, w=150, h=150)
    assert lay.is_safe
    lay.area = Area(x=15, y=15, w=165, h=150)  # past the paper margin on the right
    assert not lay.is_safe
