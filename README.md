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

### Rotation

- **Paper and drawing area:** each can be rotated about its own center.
  - Select one and drag the round knob above it (Shift snaps to 15°).
  - Or press `[` / `]` to turn it 1°, with Shift for 15°.
  - Or type an angle in the Bed setup tab.
- **Drawing area shortcuts:** ↻ 90° / ↺ 90°, **Match paper** (copies the paper's angle, so a sheet clipped down at a slant still gets a square-on drawing), and **0°**.
- **Fit to free space** and **Center** work at any angle. They search the free space along the area's own axes.
- **Turn image** (Artwork tab) rotates the source picture in 90° steps before it is cropped to the area.

All safety checks treat the paper and area as rotated rectangles. The outline trace, test mark and calibration jobs follow the area's rotation.

## Send to printer

BambuPlot can upload jobs to the A1 Mini over your local network, and optionally start them.

**On the printer:**
1. Turn on **LAN Only mode**. Its screen shows the IP address and the access code.
2. To start jobs from your computer (not just upload them), also turn on **Developer Mode**, then restart the printer. Developer Mode disconnects the Bambu cloud and the Handy app while it's on.

**In the app, open the Printer tab:**
1. Enter the IP address, the serial number and the access code, then press **Test connection**.
2. Add a **project template**. Bambu printers start jobs from `.gcode.3mf` files. In Bambu Studio, slice any small model for the A1 mini, then choose File → Export → Export plate sliced file. BambuPlot puts each plot inside that file. Without a template, plain `.gcode` is uploaded and you start it from the printer's screen.

**To send a job:** in the Job panel, choose **Send to printer…**, pick the artwork, the smoke test or the press ladder, then either:
- **Upload only**, or
- **Upload and start**, which first needs a tick confirming the holder is empty. The printer homes right away.

You still put the pen in at Pause 1 at the printer.

**Where settings are stored:** `~/.config/bambuplot/printer.json`, readable only by you. The access code can come from the `BAMBU_ACCESS_CODE` environment variable instead; that takes priority and is never written to disk.

**Safety:**
- Every job is re-checked by the safety validator before upload.
- A job is never started while the printer reports it's busy.
- Bambu's automatic steps (bed leveling, flow calibration, AMS) are switched off in the start command.

**Network security:** the printer uses a self-signed certificate. The connection is encrypted, but the printer's identity isn't verified, so use this only on a network you trust.

**Not yet tested on real hardware:**
- the FTPS upload,
- the MQTT start command,
- the `file:///sdcard/` URL form.

If uploads work but jobs won't start, try `ftp:///` as the URL prefix (Printer tab → Advanced).

## Command line

```bash
uv run bambuplot init-layout -o layout.json          # default layout to edit
uv run bambuplot plot photo.jpg --layout layout.json --effect waves -p spacing=2 -o photo.gcode --svg photo.svg
uv run bambuplot calibrate smoke --layout layout.json -o smoke.gcode
uv run bambuplot calibrate press --layout layout.json -o press.gcode
uv run bambuplot effects                              # list effects and parameters
uv run bambuplot printer set --ip 192.168.1.50 --serial 0300AA000000000
uv run bambuplot printer template plate.gcode.3mf     # A1 mini sliced plate from Bambu Studio
uv run bambuplot printer test                         # needs BAMBU_ACCESS_CODE or the app's saved code
uv run bambuplot send photo.gcode --layout layout.json [--start]
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

Millimeters, origin at the plate's front-left corner, +X right, +Y toward the back. Angles are degrees, counter-clockwise seen from above.

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
| `layout.py` | Paper, clips, pen, Z and drawing-area model, rotated-rectangle geometry, safety checks |
| `fit.py` | Largest safe drawing area at any rotation |
| `image.py` | Load, crop to the area, CLAHE, contrast/gamma |
| `effects.py` | Effect registry: waves, halftone, spiral, stipple |
| `tsp.py` | Nearest-neighbor tour and 2-opt for stipple |
| `paths.py` | Clamp, simplify, order, merge, stats |
| `gcode.py` | Start sequence with pauses, stroke compiler |
| `safety.py` | G-code allow-list validator |
| `calibrate.py` | Smoke test and press ladder |
| `package.py` | Wraps G-code in a `.gcode.3mf` from a Bambu Studio template |
| `printer.py` | LAN Only upload (FTPS) and start/status (MQTT) |
| `server.py` / `web/` | FastAPI backend and the browser UI |

To add an effect, write a function in `effects.py` decorated with `@register(...)`. It must return polylines in area coordinates. The UI picks up its parameters automatically.
