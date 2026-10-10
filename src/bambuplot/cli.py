"""Command line: bambuplot serve | plot | calibrate | effects | init-layout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import effects
from .calibrate import press_ladder, smoke_test
from .gcode import UnsafeLayout
from .image import Prep, load_gray
from .layout import Layout
from .pipeline import make_plan, plan_to_gcode
from .safety import GcodeSafetyError
from .svg import to_svg


def _layout(path: str | None) -> Layout:
    if not path:
        return Layout()
    return Layout.model_validate_json(Path(path).read_text())


def _params(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--param needs name=value, got '{item}'")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cmd_serve(args):
    import uvicorn

    print(f"BambuPlot running at http://{args.host}:{args.port}")
    uvicorn.run("bambuplot.server:app", host=args.host, port=args.port, reload=args.reload)


def cmd_plot(args):
    layout = _layout(args.layout)
    gray = load_gray(args.image) if args.image else None
    prep = Prep(clahe=not args.no_clahe, invert=args.invert)
    plan = make_plan(layout, args.effect, _params(args.param), prep, gray)
    s = plan.stats
    print(f"{s['paths']} paths · {s['draw_m']} m of ink · {s['travel_m']} m travel · ~{s['minutes']} min")
    if args.svg:
        Path(args.svg).write_text(to_svg(layout, plan.pen_lines(layout)))
        print(f"preview → {args.svg}")
    gcode = plan_to_gcode(layout, plan, title=f"{args.effect} from {Path(args.image).name if args.image else 'sample'}")
    Path(args.out).write_text(gcode)
    print(f"G-code  → {args.out}")


def cmd_calibrate(args):
    layout = _layout(args.layout)
    text = smoke_test(layout) if args.kind == "smoke" else press_ladder(layout)
    Path(args.out).write_text(text)
    print(f"{args.kind} G-code → {args.out}")


def cmd_effects(args):
    for e in effects.EFFECTS.values():
        print(f"{e.name:<10} {e.label} — {e.description}")
        for p in e.params:
            print(f"    {p.name:<18} {p.kind:<6} default {p.default!r}  {p.help}")


def cmd_init_layout(args):
    Path(args.out).write_text(json.dumps(Layout().model_dump(), indent=2))
    print(f"default layout → {args.out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bambuplot", description="Images to pen-plotter G-code for the A1 Mini.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve)

    p = sub.add_parser("plot", help="turn an image into G-code")
    p.add_argument("image", nargs="?", help="JPG/PNG; omit to use the built-in sample")
    p.add_argument("--layout", help="layout JSON from the web app or init-layout")
    p.add_argument("--effect", default="waves", choices=list(effects.EFFECTS))
    p.add_argument("-p", "--param", action="append", help="effect parameter, e.g. -p spacing=2")
    p.add_argument("--no-clahe", action="store_true")
    p.add_argument("--invert", action="store_true")
    p.add_argument("-o", "--out", default="plot.gcode")
    p.add_argument("--svg", help="also write a preview SVG")
    p.set_defaults(fn=cmd_plot)

    c = sub.add_parser("calibrate", help="smoke test or press-depth ladder")
    c.add_argument("kind", choices=["smoke", "press"])
    c.add_argument("--layout")
    c.add_argument("-o", "--out", default="calibrate.gcode")
    c.set_defaults(fn=cmd_calibrate)

    e = sub.add_parser("effects", help="list effects and their parameters")
    e.set_defaults(fn=cmd_effects)

    i = sub.add_parser("init-layout", help="write the default layout JSON")
    i.add_argument("-o", "--out", default="layout.json")
    i.set_defaults(fn=cmd_init_layout)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (UnsafeLayout, GcodeSafetyError) as err:
        print(f"Refused: {err}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
