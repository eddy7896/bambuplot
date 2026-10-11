import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bambuplot import mask as m
from bambuplot.image import Prep, prepare
from bambuplot.layout import Layout
from bambuplot.server import app


def png(rgb: np.ndarray, mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb, mode).save(buf, "PNG")
    return buf.getvalue()


def product_shot(bg=(235, 235, 230)):
    """A dark-red disc and a blue square on an even, slightly noisy background."""
    rng = np.random.default_rng(0)
    img = np.empty((300, 400, 3), np.uint8)
    img[:] = bg
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    cv2.circle(img, (150, 150), 80, (150, 20, 30), -1)
    cv2.rectangle(img, (260, 100), (340, 200), (30, 60, 160), -1)
    return img


def busy_photo():
    rng = np.random.default_rng(1)
    blotches = (rng.random((6, 8, 3)) * 255).astype(np.uint8)  # big, varied color patches
    img = cv2.resize(blotches, (400, 300), interpolation=cv2.INTER_CUBIC)
    cv2.circle(img, (200, 150), 90, (20, 200, 40), -1)  # a flat green subject
    return img


def state(img):
    return m.MaskState(rgb=m.load_rgb(png(img)))


def test_auto_removes_a_plain_background_and_keeps_both_objects():
    st = state(product_shot())
    assert m.auto(st) == "plain background"
    assert st.mask[150, 150] == 1 and st.mask[150, 300] == 1  # disc and square
    assert st.mask[10, 10] == 0 and st.mask[290, 390] == 0
    expected = (np.pi * 80**2 + 81 * 101) / (300 * 400)
    assert st.kept_fraction() == pytest.approx(expected, rel=0.08)


def test_auto_on_a_busy_photo_uses_grabcut():
    st = state(busy_photo())
    assert m.auto(st) == "photo"
    assert st.mask[150, 200] == 1 and st.mask[5, 5] == 0


def test_select_box_cuts_out_the_subject():
    st = state(busy_photo())
    m.select(st, [0.25, 0.15, 0.75, 0.85])
    assert st.mask[150, 200] == 1
    assert st.mask[150, 60] == 0  # inside the box's left side but outside the circle
    assert st.kept_fraction() == pytest.approx(np.pi * 90**2 / (300 * 400), rel=0.2)


def test_exact_brushes_erase_and_restore():
    st = state(product_shot())
    m.paint(st, [{"mode": "erase", "points": [[0.3, 0.5], [0.45, 0.5]], "size": 0.05}])
    assert st.mask[150, 150] == 0 and st.mask[10, 10] == 1
    m.paint(st, [{"mode": "keep", "points": [[0.375, 0.5]], "size": 0.05}])
    assert st.mask[150, 150] == 1


def test_smart_brush_follows_the_subject_edge():
    st = state(busy_photo())
    m.select(st, [0.25, 0.15, 0.75, 0.85])
    # Mark a bit of background inside the box as background: the subject must survive.
    m.paint(st, [{"mode": "erase", "points": [[0.28, 0.2], [0.3, 0.25]], "size": 0.02}], smart=True)
    assert st.mask[150, 200] == 1 and st.mask[60, 115] == 0


def test_invert_grow_undo_reset():
    st = state(product_shot())
    m.auto(st)
    kept = st.kept_fraction()
    m.invert(st)
    assert st.kept_fraction() == pytest.approx(1 - kept, abs=0.01)
    m.undo(st)
    assert st.kept_fraction() == pytest.approx(kept)
    m.grow(st, 5)
    assert st.kept_fraction() > kept
    m.reset(st)
    assert st.mask is None and st.kept_fraction() == 1.0


def test_png_transparency_becomes_the_selection():
    rgba = np.zeros((100, 100, 4), np.uint8)
    rgba[20:80, 20:80] = (10, 10, 10, 255)
    alpha = m.alpha_mask(png(rgba, "RGBA"))
    assert alpha is not None and alpha[50, 50] == 1 and alpha[5, 5] == 0


def test_masked_tone_is_white_where_removed_even_inverted():
    gray = np.zeros((100, 100), np.float32)  # all black picture
    keep = np.zeros_like(gray)
    keep[25:75, 25:75] = 1  # keep the middle only
    t = prepare(gray, 50, 50, Prep(clahe=False, invert=True, brightness=-0.3), mask=keep)
    assert t.sample(2, 2) == pytest.approx(1.0)  # removed corner stays paper white
    t2 = prepare(gray, 50, 50, Prep(clahe=False), mask=keep)
    assert t2.sample(25, 25) < 0.05  # kept middle is still black
    t3 = prepare(gray, 50, 50, Prep(clahe=False, use_mask=False), mask=keep)
    assert t3.sample(2, 2) < 0.05  # selection switched off


def test_api_background_removal_changes_the_plan():
    client = TestClient(app)
    data = png(product_shot())
    up = client.post("/api/image", files={"file": ("shot.png", data, "image/png")}).json()
    body = {"effect": "trace", "image_id": up["id"], "layout": Layout().model_dump(), "params": {"fill": False}}
    before = client.post("/api/plan", json=body).json()["stats"]
    sel = client.post(f"/api/image/{up['id']}/mask", json={"op": "auto"}).json()
    assert sel["has_mask"] and sel["method"] == "plain background" and sel["mask"].startswith("data:image/png")
    after = client.post("/api/plan", json=body).json()["stats"]
    assert after != before
    assert client.post(f"/api/image/{up['id']}/mask", json={"op": "undo"}).json()["has_mask"] is False
    assert client.post(f"/api/image/{up['id']}/mask", json={"op": "select", "rect": [0, 0, 0.01, 0.01]}).status_code == 422
