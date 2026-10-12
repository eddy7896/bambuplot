"""Printer-wide settings shared by every project: the pen holder, Z heights and speeds.

These describe the hardware, not the artwork, so they live in
``~/.config/bambuplot/machine.json`` instead of inside each project. Opening a
project applies them; changing them in any project updates the shared file.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from . import printer
from .layout import Bed, Feed, Pen, ZSettings


class Machine(BaseModel):
    bed: Bed = Field(default_factory=Bed)
    pen: Pen = Field(default_factory=Pen)
    z: ZSettings = Field(default_factory=ZSettings)
    feed: Feed = Field(default_factory=Feed)


def path():
    return printer.CONFIG_DIR / "machine.json"


def load() -> Machine:
    p = path()
    if p.exists():
        try:
            return Machine.model_validate_json(p.read_text())
        except ValueError:
            pass
    return Machine()


def save(m: Machine) -> Machine:
    printer.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path().with_suffix(".tmp")
    tmp.write_text(m.model_dump_json(indent=2))
    tmp.replace(path())
    return m
