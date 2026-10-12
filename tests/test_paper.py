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


def test_bigger_plate_moves_every_limit():
    from bambuplot.layout import Bed
    from bambuplot.safety import validate

    lay = Layout(bed=Bed(printer="a1", w=256, h=256))
    lay.clips = []
    lay.paper = Paper(size="custom", width=200, height=200, x=28, y=28)
    lay.area = Area(x=40, y=40, w=176, h=176)  # beyond 180, fine on a 256 plate
    assert lay.is_safe
    assert lay.edge_distances(lay.area.poly())["right"] == pytest.approx(40)
    assert validate("G90\nG28\nG1 X250 Y250\n", 2, bed_w=256, bed_h=256) == []
    assert validate("G90\nG28\nG1 X250 Y250\n", 2)  # still refused for the 180 plate


def test_template_check_follows_the_printer():
    import io
    import zipfile

    from bambuplot.package import inspect_template

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28\n")
        z.writestr("Metadata/slice_info.config",
                   '<config><plate><metadata key="printer_model_id" value="C11"/></plate></config>')
    data = buf.getvalue()
    assert inspect_template(data, ("C11", "C12"))["warning"] is None  # P1P template for the P1 entry
    assert "A1 mini" in inspect_template(data, ("N1",))["warning"]
