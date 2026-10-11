import numpy as np
import pytest

from bambuplot.fit import fit_area
from bambuplot.layout import Area, Layout, Paper, Tape, contains, inside


def taped(**edges):
    lay = Layout(paper=Paper(size="custom", width=150, height=150, x=15, y=15))
    lay.clips = []
    lay.tape = Tape(**edges)
    return lay


def test_no_tape_no_check():
    assert all(c.title != "Clear of tape" for c in Layout().checks())


def test_area_over_the_tape_is_refused():
    lay = taped(left=True, width=18, on_paper=8, keep_out=2)
    lay.area = Area(x=24, y=40, w=60, h=60)  # tape on paper ends at x=23, keep-out to 25
    check = next(c for c in lay.checks() if c.title == "Clear of tape")
    assert check.state == "bad" and "left" in check.detail
    lay.area = Area(x=26, y=40, w=60, h=60)
    assert next(c for c in lay.checks() if c.title == "Clear of tape").state == "ok"


def test_tape_strip_geometry_follows_the_paper_edges():
    lay = taped(front=True, width=20, on_paper=6)
    (edge, poly), = lay.tape_polys()
    (x0, y0), (x1, y1) = poly.min(0), poly.max(0)
    assert edge == "front"
    assert y0 == pytest.approx(15 - 14) and y1 == pytest.approx(15 + 6)  # 14 off the sheet, 6 on it
    assert x0 < 15 and x1 > 165  # runs past both corners


def test_tape_turns_with_the_paper():
    lay = taped(left=True)
    lay.paper.rotation = 90
    (_, poly), = lay.tape_polys()
    (x0, y0), (x1, y1) = poly.min(0), poly.max(0)
    assert (x1 - x0) > (y1 - y0)  # the left strip now lies horizontally


def test_fit_avoids_tape_on_all_edges():
    lay = taped(left=True, front=True, right=True, back=True, width=18, on_paper=8, keep_out=2)
    a = fit_area(lay)
    lay.area = a
    assert lay.is_safe
    assert a.x >= 15 + 8 + 2 - 1e-6 and a.x2 <= 165 - 10 + 1e-6


def test_nothing_holding_the_paper_is_noted():
    lay = taped()
    assert any(c.title == "Nothing holds the paper" for c in lay.checks())
    lay.tape.back = True
    assert all(c.title != "Nothing holds the paper" for c in lay.checks())
