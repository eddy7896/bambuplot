"""AI background removal. Runs only when rembg and the small u2netp model (4.4 MB) are present."""

import cv2
import numpy as np
import pytest

from bambuplot import mask as m

rembg = pytest.importorskip("rembg")


def _have_small_model():
    return any(x["name"] == "u2netp" and x["downloaded"] for x in m.ai_models())


needs_model = pytest.mark.skipif(not _have_small_model(), reason="u2netp model not downloaded")


def test_models_are_listed_with_sizes():
    models = m.ai_models()
    assert [x["name"] for x in models][0] == "isnet-general-use"
    assert all(x["size_mb"] > 0 for x in models)


def test_unknown_model_is_refused():
    st = m.MaskState(rgb=np.full((50, 50, 3), 200, np.uint8))
    with pytest.raises(m.MaskError, match="Unknown"):
        m.ai_remove(st, "not-a-model")


@needs_model
def test_ai_removes_background_around_a_subject():
    rng = np.random.default_rng(3)
    img = cv2.resize((rng.random((6, 8, 3)) * 120 + 60).astype(np.uint8), (400, 300), interpolation=cv2.INTER_CUBIC)
    cv2.circle(img, (200, 150), 85, (220, 40, 40), -1)  # a bold red disc on a mottled background
    cv2.circle(img, (200, 150), 30, (250, 220, 60), -1)
    st = m.MaskState(rgb=img)
    label = m.ai_remove(st, "u2netp")
    assert label and st.mask is not None
    assert st.mask[150, 200] == 1
    assert st.mask[10, 10] == 0 and st.mask[290, 390] == 0
    m.undo(st)
    assert st.mask is None
