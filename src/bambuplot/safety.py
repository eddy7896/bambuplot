"""Last line of defense: scan finished G-code before it can reach the printer.

The validator uses an allow-list. Anything that could heat, extrude, change
tools or leave the plate is rejected, whatever produced it.
"""

from __future__ import annotations

from .layout import BED_H, BED_W

ALLOWED = {"G0", "G1", "G21", "G28", "G90", "M204", "M400"}
MAX_Z = 60.0
EPS = 1e-6


class GcodeSafetyError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Unsafe G-code:\n" + "\n".join(problems))


def validate(text: str, min_z: float, bed_w: float = BED_W, bed_h: float = BED_H) -> list[str]:
    """Return a list of problems; empty means the program is safe to run."""
    problems: list[str] = []
    homed = absolute = False
    for n, raw in enumerate(text.splitlines(), 1):
        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        words = code.split()
        cmd = words[0].upper()
        if cmd not in ALLOWED:
            problems.append(f"line {n}: {cmd} is not allowed")
            continue
        params: dict[str, float] = {}
        for word in words[1:]:
            try:
                params[word[0].upper()] = float(word[1:])
            except (ValueError, IndexError):
                problems.append(f"line {n}: can't read '{word}'")
        if "E" in params:
            problems.append(f"line {n}: extrusion (E) is not allowed")
        if cmd == "G90":
            absolute = True
        elif cmd == "G28":
            homed = True
        elif cmd in ("G0", "G1"):
            if not (homed and absolute):
                problems.append(f"line {n}: move before G28 and G90")
            if "X" in params and not (-EPS <= params["X"] <= bed_w + EPS):
                problems.append(f"line {n}: X{params['X']:g} is off the plate")
            if "Y" in params and not (-EPS <= params["Y"] <= bed_h + EPS):
                problems.append(f"line {n}: Y{params['Y']:g} is off the plate")
            if "Z" in params and not (min_z - EPS <= params["Z"] <= MAX_Z):
                problems.append(f"line {n}: Z{params['Z']:g} is outside {min_z:g}–{MAX_Z:g}")
        if len(problems) >= 50:
            problems.append("… stopped after 50 problems")
            break
    return problems
