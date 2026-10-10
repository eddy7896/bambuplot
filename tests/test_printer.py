import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from bambuplot import package, printer
from bambuplot.calibrate import smoke_test
from bambuplot.layout import Layout
from bambuplot.server import app

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config><plate><metadata key="index" value="1"/><metadata key="printer_model_id" value="{model}"/></plate></config>"""


def make_template(model="N1", plates=(1,)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/slice_info.config", SLICE_INFO.format(model=model))
        z.writestr("Metadata/plate_1.png", b"\x89PNG")
        for n in plates:
            z.writestr(f"Metadata/plate_{n}.gcode", "; sliced by Bambu Studio\nG1 X1 Y1 E1\n")
            z.writestr(f"Metadata/plate_{n}.gcode.md5", "OLD")
    return buf.getvalue()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv(printer.ACCESS_ENV, raising=False)
    return tmp_path


@pytest.fixture
def cfg():
    return printer.PrinterConfig(ip="192.168.1.50", serial="0300AA000000000", access_code="12345678")


# ----------------------------------------------------------------- package


def test_package_swaps_gcode_and_md5_and_keeps_the_rest():
    out = package.package("G90\nG28\n", make_template())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert z.read("Metadata/plate_1.gcode").decode() == "G90\nG28\n"
        assert z.read("Metadata/plate_1.gcode.md5").decode() == package.md5_hex("G90\nG28\n")
        assert z.read("Metadata/plate_1.png") == b"\x89PNG"
        assert "3D/3dmodel.model" in z.namelist()


def test_package_drops_other_plates():
    out = package.package("G28\n", make_template(plates=(1, 2)))
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert "Metadata/plate_2.gcode" not in z.namelist()


def test_template_checks():
    assert package.inspect_template(make_template())["printer_model"] == "A1 mini"
    assert "not the A1 mini" in package.inspect_template(make_template(model="C12"))["warning"]
    with pytest.raises(package.TemplateError):
        package.inspect_template(b"not a zip")
    with pytest.raises(package.TemplateError, match="slice"):
        package.inspect_template(make_template(plates=()))


# ------------------------------------------------------------------ config


def test_access_code_never_leaves_in_public_view_and_file_is_private(home, cfg):
    printer.save_config(cfg)
    assert (printer.config_path().stat().st_mode & 0o777) == 0o600
    pub = printer.load_config().public()
    assert pub["has_access_code"] and "12345678" not in json.dumps(pub)


def test_env_access_code_wins(home, monkeypatch):
    monkeypatch.setenv(printer.ACCESS_ENV, "ENVCODE1")
    c = printer.PrinterConfig(ip="1.2.3.4", access_code="FILECODE")
    assert c.code() == "ENVCODE1" and c.public()["access_code_from_env"]


# -------------------------------------------------------------------- send


@pytest.fixture
def fake_net(monkeypatch):
    calls = {"uploads": [], "requests": [], "state": "IDLE"}
    monkeypatch.setattr(printer, "upload", lambda c, name, data, timeout=30: calls["uploads"].append((name, data)))
    monkeypatch.setattr(printer, "status", lambda c, timeout=8: {"state": calls["state"], "file": None, "percent": 0})
    monkeypatch.setattr(printer, "_session", lambda c, req, want_report, timeout: calls["requests"].append(req) or {})
    return calls


def test_send_plain_gcode_upload_only(home, cfg, fake_net):
    lay = Layout()
    res = printer.send_job(cfg, smoke_test(lay), "smoke", min_z=lay.z.down, start=False)
    assert res["file"].endswith(".gcode") and not res["packaged"] and not res["started"]
    assert fake_net["uploads"] and not fake_net["requests"]


def test_send_with_template_packages_and_starts_with_automation_off(home, cfg, fake_net):
    printer.save_template(make_template())
    lay = Layout()
    res = printer.send_job(cfg, smoke_test(lay), "smoke", min_z=lay.z.down, start=True)
    assert res["file"].endswith(".gcode.3mf") and res["packaged"] and res["started"]
    req = fake_net["requests"][0]["print"]
    assert req["command"] == "project_file" and req["param"] == "Metadata/plate_1.gcode"
    assert req["url"] == "file:///sdcard/" + res["file"]
    assert not req["bed_levelling"] and not req["use_ams"] and not req["flow_cali"]


def test_send_refuses_unsafe_gcode_before_uploading(home, cfg, fake_net):
    with pytest.raises(printer.PrinterError, match="unsafe"):
        printer.send_job(cfg, "G90\nG28\nM104 S220\n", "bad", min_z=2.0, start=False)
    assert not fake_net["uploads"]


def test_send_refuses_to_start_on_a_busy_printer(home, cfg, fake_net):
    fake_net["state"] = "RUNNING"
    lay = Layout()
    with pytest.raises(printer.PrinterError, match="busy"):
        printer.send_job(cfg, smoke_test(lay), "smoke", min_z=lay.z.down, start=True)
    assert not fake_net["uploads"]


def test_send_needs_settings(home, fake_net):
    with pytest.raises(printer.PrinterError, match="IP address"):
        printer.send_job(printer.PrinterConfig(), "G90\nG28\n", "x", min_z=2, start=False)


def test_api_send_and_settings(home, fake_net):
    client = TestClient(app)
    r = client.post("/api/printer", json={"ip": "192.168.1.50", "serial": "SN1", "access_code": "12345678"})
    assert r.status_code == 200 and "12345678" not in r.text
    r = client.post("/api/printer", json={"ip": "192.168.1.51", "serial": "SN1"})  # keeps the saved code
    assert r.json()["has_access_code"]
    r = client.post("/api/send", json={"kind": "smoke", "layout": Layout().model_dump()})
    assert r.status_code == 200 and r.json()["file"].startswith("bambuplot-smoke")


# ----------------------------------------------------------- Bambu Connect


def test_connect_needs_a_template(home):
    lay = Layout()
    with pytest.raises(printer.PrinterError, match="template"):
        printer.open_in_bambu_connect(smoke_test(lay), "smoke", min_z=lay.z.down, opener=lambda u: None)


def test_connect_saves_package_and_opens_documented_url(home):
    printer.save_template(make_template())
    opened = []
    lay = Layout()
    res = printer.open_in_bambu_connect(smoke_test(lay), "my smoke", min_z=lay.z.down, opener=opened.append)
    path = home / "jobs" / res["file"]
    assert path.exists() and res["file"].endswith(".gcode.3mf")
    with zipfile.ZipFile(path) as z:
        assert "M400 U1" in z.read("Metadata/plate_1.gcode").decode()
    url = opened[0]
    assert url.startswith("bambu-connect://import-file?path=%2F") and url.endswith("&version=1.0.0")
    assert "%2Fjobs%2F" in url and "&name=my-smoke-" in url


def test_connect_refuses_unsafe_gcode(home):
    printer.save_template(make_template())
    with pytest.raises(printer.PrinterError, match="unsafe"):
        printer.open_in_bambu_connect("G90\nG28\nG1 X300 Y10\n", "bad", min_z=2, opener=lambda u: None)


def test_api_package_download(home):
    client = TestClient(app)
    body = {"kind": "smoke", "layout": Layout().model_dump()}
    assert client.post("/api/package", json=body).status_code == 422  # no template yet
    printer.save_template(make_template())
    r = client.post("/api/package", json=body)
    assert r.status_code == 200 and ".gcode.3mf" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "G28" in z.read("Metadata/plate_1.gcode").decode()
