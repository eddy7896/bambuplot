"""Command line: bambuplot serve | plot | calibrate | effects | init-layout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import effects, printer
from .calibrate import DEFAULT_PRESSES, press_ladder, smoke_test
from .gcode import UnsafeLayout
from .image import Prep, load_gray
from .layout import Layout
from .pipeline import make_plan, plan_to_gcode
from .package import TemplateError
from .safety import GcodeSafetyError
from .svgin import SvgError, load_svg
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
    # Reload on code changes so the page and the server never drift apart.
    pkg = str(Path(__file__).parent)
    uvicorn.run("bambuplot.server:app", host=args.host, port=args.port, reload=not args.no_reload,
                reload_dirs=[pkg], access_log=args.verbose)


def cmd_plot(args):
    layout = _layout(args.layout)
    is_svg = bool(args.image) and args.image.lower().endswith(".svg")
    svg = load_svg(Path(args.image).read_bytes()) if is_svg else None
    gray = load_gray(args.image) if args.image and not is_svg else None
    effect = args.effect or ("svg" if is_svg else "waves")
    for w in svg.warnings if svg else []:
        print("note:", w)
    prep = Prep(clahe=not args.no_clahe, invert=args.invert)
    plan = make_plan(layout, effect, _params(args.param), prep, gray, svg=svg)
    s = plan.stats
    print(f"{s['paths']} paths · {s['draw_m']} m of ink · {s['travel_m']} m travel · ~{s['minutes']} min")
    if args.svg:
        Path(args.svg).write_text(to_svg(layout, plan.pen_lines(layout)))
        print(f"preview → {args.svg}")
    gcode = plan_to_gcode(layout, plan, title=f"{effect} from {Path(args.image).name if args.image else 'sample'}")
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


def cmd_printer(args):
    cfg = printer.load_config()
    if args.action == "show":
        print(json.dumps(cfg.public(), indent=2))
    elif args.action == "set":
        if args.ip:
            cfg.ip = args.ip
        if args.serial:
            cfg.serial = args.serial
        if args.url_prefix:
            cfg.url_prefix = args.url_prefix
        printer.save_config(cfg)
        print(f"saved → {printer.config_path()}")
        if not cfg.code():
            print(f"Add the access code by setting {printer.ACCESS_ENV}, or in the app's Printer tab.")
    elif args.action == "template":
        if not args.file:
            raise SystemExit("bambuplot printer template FILE.gcode.3mf")
        info = printer.save_template(Path(args.file).read_bytes())
        print(f"template saved ({info['printer_model'] or 'unknown printer'}, uses {info['plate']})")
        if info["warning"]:
            print("warning:", info["warning"])
    elif args.action == "test":
        st = printer.status(cfg)
        print(f"printer is {st['state']}" + (f", last file {st['file']}" if st["file"] else ""))


def cmd_send(args):
    layout = _layout(args.layout)
    text = Path(args.gcode).read_text()
    min_z = min(layout.z.down, layout.z.touch - max(DEFAULT_PRESSES))
    res = printer.send_job(printer.load_config(), text, Path(args.gcode).stem, min_z=min_z,
                           start=args.start, use_template=not args.plain)
    print(f"uploaded {res['file']} ({res['bytes']:,} bytes)")
    print("started — go to the printer for Pause 1" if res["started"] else "start it from the printer's screen")


def cmd_connect(args):
    layout = _layout(args.layout)
    min_z = min(layout.z.down, layout.z.touch - max(DEFAULT_PRESSES))
    res = printer.open_in_bambu_connect(Path(args.gcode).read_text(), Path(args.gcode).stem, min_z=min_z)
    print(f"saved {res['path']}")
    print("opened in Bambu Connect: send it to the printer there, then start it at the printer")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bambuplot", description="Images to pen-plotter G-code for the A1 Mini.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-reload", action="store_true", help="don't restart when the code changes")
    s.add_argument("--verbose", action="store_true", help="log every request")
    s.set_defaults(fn=cmd_serve)

    p = sub.add_parser("plot", help="turn an image into G-code")
    p.add_argument("image", nargs="?", help="JPG/PNG/SVG; omit to use the built-in sample")
    p.add_argument("--layout", help="layout JSON from the web app or init-layout")
    p.add_argument("--effect", choices=list(effects.EFFECTS), help="default: svg for SVG files, else waves")
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

    pr = sub.add_parser("printer", help="printer connection settings")
    pr.add_argument("action", choices=["show", "set", "template", "test"])
    pr.add_argument("file", nargs="?", help="for 'template': an A1 mini .gcode.3mf from Bambu Studio")
    pr.add_argument("--ip")
    pr.add_argument("--serial")
    pr.add_argument("--url-prefix")
    pr.set_defaults(fn=cmd_printer)

    sd = sub.add_parser("send", help="upload a G-code file to the printer (and optionally start it)")
    sd.add_argument("gcode")
    sd.add_argument("--layout", help="layout used to make the file (for the safety check)")
    sd.add_argument("--start", action="store_true", help="start printing right away (needs Developer Mode)")
    sd.add_argument("--plain", action="store_true", help="upload plain .gcode even if a template is set")
    sd.set_defaults(fn=cmd_send)

    bc = sub.add_parser("connect", help="package a G-code file and open it in Bambu Connect")
    bc.add_argument("gcode")
    bc.add_argument("--layout", help="layout used to make the file (for the safety check)")
    bc.set_defaults(fn=cmd_connect)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (UnsafeLayout, GcodeSafetyError) as err:
        print(f"Refused: {err}", file=sys.stderr)
        sys.exit(2)
    except (printer.PrinterError, TemplateError) as err:
        print(f"Printer: {err}", file=sys.stderr)
        sys.exit(3)
    except (SvgError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
