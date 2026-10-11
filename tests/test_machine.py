import pytest
from fastapi.testclient import TestClient

from bambuplot import machine, printer
from bambuplot.server import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_machine_settings_are_shared_and_seed_new_layouts(home):
    client = TestClient(app)
    m = client.get("/api/machine").json()
    m["pen"]["offset_y"] = -30
    assert client.put("/api/machine", json=m).json()["pen"]["offset_y"] == -30
    assert machine.load().pen.offset_y == -30
    assert client.get("/api/defaults").json()["layout"]["pen"]["offset_y"] == -30
