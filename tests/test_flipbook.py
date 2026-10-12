import io
import re
import zipfile

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from bambuplot import printer
from bambuplot import flipbook as fbk
from bambuplot.flipbook import Flipbook
from bambuplot.gcode import compile_groups
from bambuplot.layout import Layout
from bambuplot.server import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    return tmp_path


def make_clip(path, seconds=2.0, fps=24):
    """A white ball rolling across a grey background."""
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for i in range(int(seconds * fps)):
        f = np.full((240, 320, 3), 90, np.uint8)
        cv2.circle(f, (30 + i * 5, 120), 40, (250, 250, 250), -1)
        w.write(f)
    w.release()
    return path.read_bytes()


def new_project(client, home):
    return client.post("/api/projects", json={"name": "Flip"}).json()["id"]


def test_cells_tile_the_area_with_gaps_and_binding():
    fb = Flipbook(cols=2, rows=3, gap=4, binding=6)
    cl = fbk.cells(fb, 120, 150)
    assert len(cl) == 6
    x, y, w, h = cl[0].page
    assert (x, round(y, 6)) == (2, 102) and (w, h) == (56, 46)  # back-left page first
    assert cl[0].art == (8, y, 50, 46)  # the binding strip is left blank
    assert cl[1].page[0] == 62 and cl[5].page[1] == 2  # reading order ends at the front-right
    one = fbk.cells(Flipbook(), 100, 80)[0]
    assert one.page == (0, 0, 100, 80)  # one page per sheet uses the whole area


def test_frame_range_and_sheets():
    fb = Flipbook(first=2, last=8, cols=2, rows=2)
    frames = fb.frames(20)
    assert frames == list(range(2, 9))
    assert [len(s) for s in fbk.sheets(frames, fb.per_sheet)] == [4, 3]
    assert Flipbook(last=99).frames(5) == [0, 1, 2, 3, 4]


def test_number_goes_in_the_binding_strip_or_clears_the_art():
    cell = fbk.cells(Flipbook(binding=10), 100, 80)[0]
    lines, box = fbk.number_for(Flipbook(binding=10), cell, "12")
    assert box is None and np.vstack(lines)[:, 0].max() <= 10  # upright in the strip
    cell = fbk.cells(Flipbook(), 100, 80)[0]
    lines, box = fbk.number_for(Flipbook(), cell, "7")
    assert box is not None  # on the picture: art is cleared behind it
    pts = np.vstack(lines)
    assert box[0] <= pts[:, 0].min() and pts[:, 0].max() <= box[2]
    kept = fbk.clear_box([np.array([[0.0, 2.0], [50.0, 2.0]])], box)
    assert all(not (box[0] < p[0] < box[2] and box[1] < p[1] < box[3]) for l in kept for p in l[1:-1])


def test_sheet_changes_and_pen_changes_in_gcode():
    lay = Layout()
    line = [np.array([[30.0, 30.0], [40.0, 30.0]])]
    groups = [("Pen 1", line), ("Pen 2", line), ("Pen 1", line), ("Pen 2", line)]
    text = compile_groups(lay, groups, "t", sheets=[1, 1, 2, 2])
    events = [e.strip() for e in re.findall(r"; ===== (pen change: [^=]+|new sheet \d of \d) =====", text)]
    assert events == ["pen change: Pen 2", "new sheet 2 of 2", "pen change: Pen 1", "pen change: Pen 2"]
    assert text.count("M400 U1") == 3 + 4
    checked = compile_groups(lay, [("Pen 1", line), ("Pen 1", line)], "t", sheets=[1, 2], check_each_sheet=True)
    assert "check Pen 1 just touches the new sheet" in checked and checked.count("M400 U1") == 3 + 2


def test_video_flipbook_end_to_end(home, tmp_path):
    client = TestClient(app)
    pid = new_project(client, home)
    data = make_clip(tmp_path / "ball.mp4")
    r = client.post(f"/api/projects/{pid}/video", files={"file": ("ball.mp4", data, "video/mp4")})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["count"] == 24 and v["clip"]["fps"] == 24  # 12 frames a second of a 2 s clip
    v = client.post(f"/api/projects/{pid}/video/extract", json={"fps": 4, "max_frames": 48}).json()
    assert v["count"] == 8
    assert client.get(f"/api/projects/{pid}/video/frames/0.jpg").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/projects/{pid}/video/frames/8.jpg").status_code == 422

    lay = Layout().model_dump()
    body = {"layout": lay, "project": pid,
            "layers": [{"id": "a", "effect": "contours", "image_id": "video", "params": {"levels": 3}}],
            "flipbook": {"enabled": True, "cols": 2, "rows": 2, "gap": 4, "frame": 5}}
    d = client.post("/api/plan", json=body).json()
    f = d["flipbook"]
    assert f["count"] == 8 and f["sheets"] == 2 and f["slot"] == 1 and f["sheet"] == 2 and len(f["cells"]) == 4
    pts = np.vstack([np.array(l) for l in d["lines"]])
    page = f["cells"][1]["page"]
    marks_x = [p for p in pts if not (page[0] - 4 <= p[0] <= page[0] + page[2] + 4)]
    assert len(marks_x) < len(pts) / 2  # the drawing sits on its own page

    # Frames move: two pages differ.
    p0 = client.post("/api/flipbook/frame", json={**body, "index": 0}).json()
    p7 = client.post("/api/flipbook/frame", json={**body, "index": 7}).json()
    c0 = np.vstack([np.array(l) for g in p0["groups"] for l in g["lines"]])[:, 0].mean()
    c7 = np.vstack([np.array(l) for g in p7["groups"] for l in g["lines"]])[:, 0].mean()
    assert c7 > c0 + 5  # the ball rolled right

    g = client.post("/api/gcode", json=body)
    assert g.status_code == 200, g.text
    assert g.text.count("new sheet 2 of 2") == 1 and g.text.count("M400 U1") == 4
    assert "2 sheets" in g.text

    pk = client.post("/api/package", json={**body, "kind": "art", "title": "Ball"})
    assert pk.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(pk.content))
    info = z.read("Metadata/slice_info.config").decode()
    assert 'key="pause_count" value="4"' in info

    # Off: the video layer plots the current frame over the whole area.
    single = client.post("/api/plan", json={**body, "flipbook": {"enabled": False, "frame": 3}}).json()
    assert "flipbook" not in single and single["lines"]

    # Saved and reopened with the project.
    st = {"layout": lay, "layers": body["layers"], "flipbook": body["flipbook"]}
    assert client.put(f"/api/projects/{pid}", json={"state": st}).status_code == 200
    opened = client.get(f"/api/projects/{pid}").json()
    assert opened["video"]["count"] == 8 and opened["state"]["flipbook"]["cols"] == 2
    assert client.get(f"/api/projects/{pid}/thumb.png").status_code == 200

    assert client.delete(f"/api/projects/{pid}/video").json()["ok"]
    gone = client.post("/api/plan", json=body)
    assert gone.status_code == 404


def test_video_upload_rejects_non_video(home):
    client = TestClient(app)
    pid = new_project(client, home)
    r = client.post(f"/api/projects/{pid}/video", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 422
    r = client.post(f"/api/projects/{pid}/video", files={"file": ("broken.mp4", b"not a video", "video/mp4")})
    assert r.status_code == 422 and client.get(f"/api/projects/{pid}/video").json() is None


def test_pages_too_small_are_refused(home, tmp_path):
    client = TestClient(app)
    pid = new_project(client, home)
    client.post(f"/api/projects/{pid}/video", files={"file": ("b.mp4", make_clip(tmp_path / "b.mp4", 0.5), "video/mp4")})
    body = {"layout": Layout().model_dump(), "project": pid, "layers": [{"id": "a", "image_id": "video"}],
            "flipbook": {"enabled": True, "cols": 8, "rows": 8, "gap": 10}}
    r = client.post("/api/plan", json=body)
    assert r.status_code == 422 and "too small" in r.json()["detail"]
