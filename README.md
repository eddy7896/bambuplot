# BambuPlot

Turn images into pen-plotter G-code for the Bambu Lab A1 Mini, using a clip-on pen holder and paper held down with clips.

## Run it

```bash
uv sync
uv run bambuplot serve
```

Open http://127.0.0.1:8000. Two tabs:

- **Bed setup:** place the paper and clips on the 180 × 180 plate, enter your pen offset, and drag out the drawing area. Safety checks update live.
- **Artwork:** load an image, adjust tone, pick an effect (wave lines, halftone, spiral, TSP stipple) and tune it. The preview shows the actual paths; the progress slider replays the drawing order.

**Download G-code** is disabled while any safety check fails.

## Command line

```bash
uv run bambuplot init-layout -o layout.json          # default layout to edit
uv run bambuplot plot photo.jpg --layout layout.json --effect waves -p spacing=2 -o photo.gcode --svg photo.svg
uv run bambuplot calibrate smoke --layout layout.json -o smoke.gcode
uv run bambuplot calibrate press --layout layout.json -o press.gcode
uv run bambuplot effects                              # list effects and parameters
```

Layout files saved from the web app work with `--layout`.

## How a job runs on the printer

1. `G28` homes with **no pen in the holder**.
2. The head rises above the clips and moves to the touch-off point (the red cross).
3. **Pause 1:** clip the paper, push the pen down until the tip touches, tighten the holder.
4. The pen hovers 5 mm up and traces the drawing area.
5. **Pause 2:** check the outline is where you want it.
6. A small L test mark is drawn.
7. **Pause 3:** check the mark; re-seat the pen if it's faint.
8. The artwork plots, then the head lifts clear of the clips.

Pen-down height is `touch − press`; pen-up is `touch + hop`. With a rigid holder, keep `press` at 0.1–0.2 mm. Print the press ladder to choose yours.

## First time on new hardware

Run the smoke test before any artwork. Confirm that:

- the printer accepts a plain `.gcode` file from SD card,
- a cold `G28` behaves with the holder fitted,
- `M400 U1` waits for Resume without moving the head.

None of this has been verified on a real A1 Mini yet.

## Coordinates

Millimeters, origin at the plate's front-left corner, +X right, +Y toward the back.

- **Pen** coordinates: where the tip touches the paper.
- **Nozzle** coordinates (what the G-code contains): pen − offset.
- Effects work in **area** coordinates, with the origin at the drawing area's front-left corner.

## Safety

Every file passes through `safety.validate` before it is written. It is an allow-list (`G0 G1 G21 G28 G90 M204 M400`) that rejects:

- extrusion, heaters and any other command,
- moves outside 0–180 mm,
- Z below pen-down,
- moves before homing.

## Layout of the code

| Module | Job |
|---|---|
| `layout.py` | Paper, clips, pen, Z and drawing-area model plus safety checks |
| `image.py` | Load, crop to the area, CLAHE, contrast/gamma |
| `effects.py` | Effect registry: waves, halftone, spiral, stipple |
| `tsp.py` | Nearest-neighbor tour and 2-opt for stipple |
| `paths.py` | Clamp, simplify, order, merge, stats |
| `gcode.py` | Start sequence with pauses, stroke compiler |
| `safety.py` | G-code allow-list validator |
| `calibrate.py` | Smoke test and press ladder |
| `server.py` / `web/` | FastAPI backend and the browser UI |

To add an effect, write a function in `effects.py` decorated with `@register(...)`. It must return polylines in area coordinates. The UI picks up its parameters automatically.
