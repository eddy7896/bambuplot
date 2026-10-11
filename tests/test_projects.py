import io

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bambuplot import printer
from bambuplot import projects as proj
from bambuplot.layout import Layout
from bambuplot.server import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    return tmp_path


def png_bytes():
    img = np.full((120, 160, 3), 235, np.uint8)
    cv2.circle(img, (80, 60), 40, (30, 30, 160), -1)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return buf.getvalue()


def test_create_list_rename_duplicate_delete(home):
    a = proj.create("Cat portrait")
    b = proj.create("Cat portrait")  # same name gets a number
    assert b["name"] == "Cat portrait 2"
    assert [p["id"] for p in proj.list_projects()][0] == b["id"]  # newest first
    proj.rename(a["id"], "  Fox   sketch ")
    assert proj.load(a["id"])["name"] == "Fox sketch"
    c = proj.duplicate(a["id"])
    assert c["name"] == "Fox sketch copy" and c["id"] != a["id"]
    proj.delete(a["id"])
    assert {p["id"] for p in proj.list_projects()} == {b["id"], c["id"]}


def test_ids_cannot_escape_the_projects_folder(home):
    for bad in ["../../etc", "", "ABCDEF123456", "a" * 13]:
        with pytest.raises(proj.ProjectError):
            proj.load(bad)


def test_source_is_stored_once_and_cleared(home):
    p = proj.create("x")
    data = png_bytes()
    assert proj.set_source(p["id"], data, "photo.png") is True
    assert proj.set_source(p["id"], data, "photo.png") is False  # unchanged
    got, info = proj.source(p["id"])
    assert got == data and info["name"] == "photo.png" and info["kind"] == "raster"
    proj.set_source(p["id"], None)
    assert proj.source(p["id"]) is None


def test_api_round_trip_keeps_state_picture_selection_and_thumbnail(home):
    client = TestClient(app)
    p = client.post("/api/projects", json={"name": "Round trip"}).json()
    up = client.post("/api/image", files={"file": ("dot.png", png_bytes(), "image/png")}).json()
    client.post(f"/api/image/{up['id']}/mask", json={"op": "auto"})
    lay = Layout()
    lay.area.w = 120
    state = {"layout": lay.model_dump(), "effect": "trace", "params": {"trace": {"levels": 2}},
             "prep": {"contrast": 1.4, "crop": [0.1, 0.1, 0.9, 0.9]}}
    saved = client.put(f"/api/projects/{p['id']}", json={"state": state, "image_id": up["id"]})
    assert saved.status_code == 200 and saved.json()["has_thumb"]
    assert client.get(f"/api/projects/{p['id']}/thumb.png").headers["content-type"] == "image/png"

    opened = client.get(f"/api/projects/{p['id']}").json()
    st = opened["state"]
    assert st["effect"] == "trace" and st["params"]["trace"]["levels"] == 2
    assert st["layout"]["area"]["w"] == 120 and st["prep"]["contrast"] == 1.4
    assert st["prep"]["crop"] == [0.1, 0.1, 0.9, 0.9]
    assert opened["image"]["name"] == "dot.png" and opened["image"]["selection"]["has_mask"]
    assert client.get(f"/api/projects/{p['id']}/source").content == png_bytes()


def test_api_unknown_project_is_404(home):
    client = TestClient(app)
    assert client.get("/api/projects/0123456789ab").status_code == 404
    assert client.delete("/api/projects/../x").status_code in (404, 405)


def test_saving_without_a_picture_removes_the_old_one(home):
    client = TestClient(app)
    p = client.post("/api/projects", json={}).json()
    up = client.post("/api/image", files={"file": ("dot.png", png_bytes(), "image/png")}).json()
    client.put(f"/api/projects/{p['id']}", json={"state": {}, "image_id": up["id"]})
    client.put(f"/api/projects/{p['id']}", json={"state": {}, "image_id": None})
    assert client.get(f"/api/projects/{p['id']}").json()["image"] is None
