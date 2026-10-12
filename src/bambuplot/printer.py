"""Send jobs to a Bambu printer on the local network.

Needs the printer in **LAN Only mode**. Starting a print from here also needs
**Developer Mode** (set on the printer's screen, then power-cycle). Uploads
go over implicit FTPS (port 990); status and the start command go over MQTT
(port 8883). Both log in as ``bblp`` with the printer's LAN access code.

The printer uses a self-signed certificate, so TLS here encrypts but does not
verify who is answering. Only use this on a network you trust.

Settings live in ``~/.config/bambuplot/printer.json`` (readable only by you).
The access code can instead come from the ``BAMBU_ACCESS_CODE`` environment
variable, which takes priority and is never written to disk.
"""

from __future__ import annotations

import ftplib
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from typing import Literal

from pydantic import BaseModel

from . import package
from .safety import validate

CONFIG_DIR = Path(os.environ.get("BAMBUPLOT_HOME", Path.home() / ".config" / "bambuplot"))
ACCESS_ENV = "BAMBU_ACCESS_CODE"
FTPS_PORT = 990
MQTT_PORT = 8883
BUSY_STATES = {"RUNNING", "PAUSE", "PREPARE", "SLICING"}


class PrinterError(RuntimeError):
    pass


# ----------------------------------------------------------------- settings


class PrinterConfig(BaseModel):
    ip: str = ""
    serial: str = ""
    access_code: str = ""
    url_prefix: str = "file:///sdcard/"
    # Project files (.gcode.3mf): written by BambuPlot, or made from the user's sliced template.
    project_file: Literal["builtin", "template"] = "builtin"
    thumbnail: Literal["plot", "picture", "both"] = "both"
    # The printer screen during a job.
    screen_progress: bool = True
    screen_status: bool = True
    claim_pause: int | None = None
    claim_pen: int | None = 4
    claim_sheet: int | None = 16

    def code(self) -> str:
        return os.environ.get(ACCESS_ENV) or self.access_code

    def ready(self) -> list[str]:
        missing = []
        if not self.ip:
            missing.append("the printer's IP address")
        if not self.code():
            missing.append(f"the LAN access code (or set {ACCESS_ENV})")
        return missing

    def public(self) -> dict:
        """Settings safe to show in the browser: never the access code itself."""
        return {
            "ip": self.ip,
            "serial": self.serial,
            "has_access_code": bool(self.code()),
            "access_code_from_env": bool(os.environ.get(ACCESS_ENV)),
            "url_prefix": self.url_prefix,
            "template": template_info(),
            "project_file": self.project_file,
            "thumbnail": self.thumbnail,
            "screen_progress": self.screen_progress,
            "screen_status": self.screen_status,
            "claim_pause": self.claim_pause,
            "claim_pen": self.claim_pen,
            "claim_sheet": self.claim_sheet,
        }


def config_path() -> Path:
    return CONFIG_DIR / "printer.json"


def template_path() -> Path:
    return CONFIG_DIR / "template.gcode.3mf"


def load_config() -> PrinterConfig:
    p = config_path()
    if p.exists():
        try:
            return PrinterConfig.model_validate_json(p.read_text())
        except ValueError:
            pass
    return PrinterConfig()


def save_config(cfg: PrinterConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p = config_path()
    p.write_text(cfg.model_dump_json(indent=2))
    p.chmod(0o600)


def save_template(data: bytes) -> dict:
    from . import machine

    info = package.inspect_template(data, machine.load().bed.model_id)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    template_path().write_bytes(data)
    return info


def template_info() -> dict | None:
    p = template_path()
    if not p.exists():
        return None
    from . import machine  # imported here: machine uses this module's CONFIG_DIR

    try:
        return package.inspect_template(p.read_bytes(), machine.load().bed.model_id)
    except package.TemplateError as err:
        return {"error": str(err)}


# -------------------------------------------------------------------- FTPS


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
    return ctx


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP over TLS from the first byte (port 990), reusing the TLS session for data."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def upload(cfg: PrinterConfig, name: str, data: bytes, timeout: float = 30) -> None:
    """Put a file in the root of the printer's SD card."""
    ftp = ImplicitFTPS(context=_tls_context(), timeout=timeout)
    try:
        ftp.connect(cfg.ip, FTPS_PORT)
        ftp.login("bblp", cfg.code())
        ftp.prot_p()
        with ftp.transfercmd(f"STOR {name}") as conn:
            view = memoryview(data)
            for i in range(0, len(view), 65536):
                conn.sendall(view[i : i + 65536])
        ftp.voidresp()
    except ftplib.error_perm as err:
        raise PrinterError(f"The printer refused the upload ({err}). Check the access code.") from err
    except (OSError, ftplib.Error) as err:
        raise PrinterError(
            f"Couldn't upload to {cfg.ip}:{FTPS_PORT} ({err}). Is the printer on, in LAN Only mode, "
            "and on the same network?"
        ) from err
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


# -------------------------------------------------------------------- MQTT


def _mqtt_client(cfg: PrinterConfig):
    import paho.mqtt.client as mqtt

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bambuplot-{os.getpid()}-{time.time_ns() % 10**6}")
    c.username_pw_set("bblp", cfg.code())
    c.tls_set_context(_tls_context())
    c.tls_insecure_set(True)
    return c


def _session(cfg: PrinterConfig, request: dict | None, want_report: bool, timeout: float) -> dict:
    """Connect, optionally publish one request, optionally wait for a status report."""
    if not cfg.serial:
        raise PrinterError("Add the printer's serial number (Settings → Device on the printer).")
    report: dict = {}
    done = threading.Event()
    failure: list[str] = []
    topic_req = f"device/{cfg.serial}/request"

    def on_connect(client, _u, _flags, rc, _props):
        if rc.is_failure:
            failure.append(str(rc))
            done.set()
            return
        client.subscribe(f"device/{cfg.serial}/report")
        client.publish(topic_req, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
        if request is not None:
            client.publish(topic_req, json.dumps(request))
        if not want_report:
            done.set()

    def on_message(_c, _u, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        pr = data.get("print") or {}
        if "gcode_state" in pr:
            report.update(pr)
            done.set()

    client = _mqtt_client(cfg)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(cfg.ip, MQTT_PORT, keepalive=30)
    except OSError as err:
        raise PrinterError(
            f"Couldn't reach {cfg.ip}:{MQTT_PORT} ({err}). Turn on LAN Only mode and Developer Mode "
            "on the printer, then power-cycle it."
        ) from err
    client.loop_start()
    try:
        finished = done.wait(timeout)
        if request is not None:
            time.sleep(0.5)  # let the publish leave before we hang up
    finally:
        client.loop_stop()
        client.disconnect()
    if failure:
        raise PrinterError(
            f"The printer rejected the connection ({failure[0]}). Check the access code, and that "
            "Developer Mode is on."
        )
    if want_report and not finished:
        raise PrinterError(
            "Connected, but the printer didn't report its status. Check the serial number, and that "
            "Developer Mode is on."
        )
    return report


def status(cfg: PrinterConfig, timeout: float = 8) -> dict:
    r = _session(cfg, None, want_report=True, timeout=timeout)
    return {
        "state": r.get("gcode_state"),
        "file": r.get("gcode_file") or r.get("subtask_name"),
        "percent": r.get("mc_percent"),
    }


def start_request(cfg: PrinterConfig, name: str, plate_param: str | None) -> dict:
    """MQTT payload that starts ``name`` from the SD card."""
    if plate_param:  # .gcode.3mf project
        return {"print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": plate_param,
            "url": cfg.url_prefix + name,
            "subtask_name": name,
            "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
            "md5": "",
            # Our G-code replaces Bambu's start routine; keep every automatic step off.
            "timelapse": False, "bed_type": "auto", "bed_levelling": False, "flow_cali": False,
            "vibration_cali": False, "layer_inspect": False, "use_ams": False, "ams_mapping": [],
        }}
    return {"print": {"sequence_id": "0", "command": "gcode_file", "param": f"/sdcard/{name}"}}


# -------------------------------------------------------------------- jobs


def safe_name(stem: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "job"
    return f"{stem[:48]}-{time.strftime('%Y%m%d-%H%M%S')}"


def build_job(gcode: str, stem: str, min_z: float, use_template: bool = True,
              require_template: bool = False, bed: tuple[float, float] = (180.0, 180.0),
              project: tuple[bytes, str] | None = None) -> tuple[str, bytes, str | None]:
    """Validate and package a job: (file name, bytes, plate param or None for plain G-code).

    ``project``: a ready .gcode.3mf and its plate G-code path (the built-in project file).
    """
    problems = validate(gcode, min_z=min_z, bed_w=bed[0], bed_h=bed[1])
    if problems:
        raise PrinterError("Refusing to send unsafe G-code: " + "; ".join(problems[:5]))
    if project is not None:
        return safe_name(stem) + ".gcode.3mf", project[0], project[1]
    tpl = template_path()
    if use_template and tpl.exists():
        tpl_bytes = tpl.read_bytes()
        plate_param = package.inspect_template(tpl_bytes)["param"]
        return safe_name(stem) + ".gcode.3mf", package.package(gcode, tpl_bytes), plate_param
    if require_template:
        raise PrinterError(
            "Add a project template first (Printer tab). Bambu Connect only accepts .gcode.3mf files."
        )
    return safe_name(stem) + ".gcode", gcode.encode(), None


def send_job(cfg: PrinterConfig, gcode: str, stem: str, min_z: float, start: bool,
             use_template: bool = True, timeout: float = 30, bed: tuple[float, float] = (180.0, 180.0),
             project: tuple[bytes, str] | None = None) -> dict:
    """Validate, package, upload and optionally start a job. Returns what happened."""
    missing = cfg.ready()
    if missing:
        raise PrinterError("Printer settings incomplete: add " + " and ".join(missing) + ".")
    name, data, plate_param = build_job(gcode, stem, min_z, use_template, bed=bed, project=project)

    if start:
        # Never start on top of another job.
        st = status(cfg)
        if st["state"] in BUSY_STATES:
            raise PrinterError(f"The printer is busy ({st['state'].lower()}). Wait for it to finish.")

    upload(cfg, name, data, timeout=timeout)
    result = {"file": name, "bytes": len(data), "packaged": plate_param is not None, "started": False}
    if start:
        _session(cfg, start_request(cfg, name, plate_param), want_report=False, timeout=10)
        result["started"] = True
    return result


# ------------------------------------------------------------ Bambu Connect

KEEP_JOBS = 20


def jobs_dir() -> Path:
    return CONFIG_DIR / "jobs"


def connect_url(path: Path, name: str) -> str:
    """Bambu Connect's documented hand-off link (values encoded like encodeURIComponent)."""
    enc = lambda v: quote(str(v), safe="-_.!~*'()")  # noqa: E731
    return f"bambu-connect://import-file?path={enc(path)}&name={enc(name)}&version=1.0.0"


def _open_url(url: str) -> None:
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform.startswith("win"):
        os.startfile(url)  # type: ignore[attr-defined]
        return
    else:
        cmd = ["xdg-open", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise PrinterError(
            "Couldn't open Bambu Connect. Is it installed? "
            "Get it from https://wiki.bambulab.com/en/software/bambu-connect"
        )


def open_in_bambu_connect(gcode: str, stem: str, min_z: float, opener=None,
                          bed: tuple[float, float] = (180.0, 180.0), project: tuple[bytes, str] | None = None) -> dict:
    """Package the job, save it locally and hand it to Bambu Connect."""
    name, data, _ = build_job(gcode, stem, min_z, require_template=project is None, bed=bed, project=project)
    folder = jobs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / name).resolve()
    path.write_bytes(data)
    old = sorted(folder.glob("*.gcode.3mf"), key=lambda p: p.stat().st_mtime)[:-KEEP_JOBS]
    for p in old:
        p.unlink(missing_ok=True)
    url = connect_url(path, name.removesuffix(".gcode.3mf"))
    (opener or _open_url)(url)
    return {"file": name, "path": str(path), "url": url}
