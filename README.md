# BambuPlot

Turn images into pen-plotter G-code for the Bambu Lab A1 Mini, using a clip-on pen holder and paper held down with clips.

## Run it

```bash
uv sync                 # add --extra ai for AI background removal
uv run bambuplot app    # starts BambuPlot and opens it in your browser
```

Or double-click **BambuPlot.command** in Finder. It opens a Terminal window that runs the app; keep that window open while you work, and close it (or press Ctrl+C) to quit.

`bambuplot app` reuses a copy that's already running, and picks the next free port if 8000 is taken. Options: `--port 8080`, `--no-browser`.

For working on the code itself, `uv run bambuplot serve` restarts the server whenever a file changes. Then open http://127.0.0.1:8000. Two tabs:

- **Bed setup:** place the paper and clips on the 180 × 180 plate, enter your pen offset, and drag out the drawing area. Safety checks update live.
- **Artwork:** load an image, adjust tone, pick an effect (wave lines, halftone, spiral, TSP stipple) and tune it. The preview shows the actual paths; the progress slider replays the drawing order.

### Projects

BambuPlot opens on a **Projects** dashboard, laid out like the Pages and Keynote document browser:
- **Cards:** each project shows a thumbnail of its plot, when it was last edited, its effect and its picture.
- **Find and sort:** search by name or picture, and sort by Recent or Name.
- **Actions:** **New Project**, or each card's ⋯ menu for Open, Rename, Duplicate and Delete. Delete asks for confirmation. On a selected card, Enter opens it and Delete removes it.

**New Project** opens a three-step setup:
1. **Printer & Pen:**
   - **Printer and build plate:** A1 mini 180 × 180, A1 or P1 / X1 256 × 256, or a custom plate size.
   - **Pen:** tip width and holder type (rigid or spring-loaded; this sets a sensible press).
   - **Pen position:** where the pen tip sits relative to the nozzle, in plain words ("30 mm in front of, 0 mm to the right of"), with a live top-view diagram.
2. **Paper:** size (preset or custom) and orientation; placement (centered, toward the front or back); margin; and whether it's held by clips, tape on chosen edges, both, or nothing. A live plate preview shows the sheet, tape, clips and the pen's reach.
3. **Project:** name, and what to start with: a picture or SVG, the sample, or an alignment test. A summary lists every choice.

The drawing area is fitted to the paper automatically. Printer, plate and pen settings are saved for all projects, so the sheet is pre-filled next time. Only the A1 mini has been tested; the other plate sizes adjust every limit and check, but the G-code start sequence has not been tried on those printers.

**What a project keeps:** the bed layout, effect and effect settings, tone, crop and turn settings, the original picture or SVG, and the subject selection. Projects **save themselves** a moment after each change, like a Mac document. The toolbar shows *Edited* and then *Saved*, and ⌘S saves right away. Click the name in the toolbar to rename the project, and **‹ Projects** goes back to the dashboard.

**Where they live:** `~/.config/bambuplot/projects/<id>/`, with `project.json`, `source.*`, `mask.png` and `thumb.png`. The first time you open this version, the setup you had before becomes a project called *My First Plot*.

### The window

BambuPlot is laid out like a Mac app, and the page itself never scrolls.

- **Toolbar:** Open, Edit Picture, a Download menu, and Send to Printer.
- **Canvas:** the build plate fills the middle of the window.
- **Inspector:** four tabs on the right: Picture, Effect, Bed and Printer.
- **Status bar:** plot replay, the pen-up and source-picture toggles, ink/lifts/travel/time, and a safety badge that opens the full checklist.

You can also drop a file anywhere on the window to open it.

| Shortcut | Action |
|---|---|
| ⌘O | Open a picture or SVG |
| ⌘E | Edit picture (crop, select, erase) |
| ⌘S | Save the project now (it also saves itself) |
| ⇧⌘E | Export G-code |
| ⌘↩ | Send to printer |
| ⌘1–⌘4 | Picture / Effect / Bed / Printer tab |

On narrow windows, the inspector moves below the plate. It follows the system's light and dark appearance.

**Download** and **Send to Printer** are disabled while any safety check fails.

### Sources and vectorization

The Artwork tab accepts PNG, JPG and SVG files.

**SVG files** are drawn as vectors, using their own paths:
- **Lines:** stroked lines are drawn as they are.
- **Filled shapes:** these are outlined and can be hatch-filled, optionally cross-hatched. White shapes erase what's under them, so a white hole inside a black logo stays empty. A white page background is not outlined.
- **Size:** **fit** scales the art to the drawing area. **actual size** keeps the file's real millimeters and cuts off anything outside the area.
- **Not supported:** text and embedded pictures are skipped with a note. Convert text to paths in your editor first.

**PNG/JPG files** can be turned into vector lines:

| Effect | Best for | What it draws |
|---|---|---|
| Outline trace | Logos, silhouettes, posterized photos | Outlines around dark areas, hatch-filled. 2–4 tone levels add cross-hatching at new angles. |
| Centerline trace | Sketches, line art, handwriting | One stroke down the middle of each drawn line, instead of two outlines. |
| Contour lines | Photos, portraits | Lines of equal brightness, like a topographic map. |

### Layers: stack effects and pictures

The **Layers** panel at the top of the inspector holds a project's layers. Each layer has its own picture or SVG (or the sample), effect and settings, tone, crop and selection, and pen. The Picture and Effect tabs edit the selected layer.

- **＋ Add:**
  - **Another effect on this picture:** effect stacking, e.g. Flow lines for shading plus Centerline trace for outlines.
  - **New picture or SVG…**
  - **Sample picture**
- **Layer list:** the eye hides a layer from the preview and the plot. **⋯** has Duplicate, Move Up/Down and Delete. The top of the list is drawn last.
- **Effect → Layer:**
  - **Name** and **Pen** (Pen 1–6, each previewed in its own colour).
  - **Tones from / to:** e.g. 45–100% draws only the shadows.
  - **Fill the drawing area** on, or off to set the layer's frame: left/front position, width and height as a percentage of the area, and turn. A dashed box on the plate shows the frame.

**Several pens:** strokes are grouped by pen. Between pens, the job returns to the touch-off point and pauses ("swap to Pen 2 (blue)") so you can change the pen and touch it off, just like Pause 1. The border and signature use the first layer's pen. Each picture is stored once in the project, and projects from earlier versions open as a single layer.

### Border and signature

These are in **Effect → Finishing** and work with every effect, including SVG files.

- **Border around the area:** draws a frame around the drawing area. **Border inset** moves it inward.
- **Signature:** adds your own signature to the plot.
  - **Making it:** draw it once on the signature pad with a mouse, trackpad or stylus, or use **Import from Picture…** to trace a photo of your signature on white paper. It's saved for every project in `~/.config/bambuplot/signature.json`.
  - **Placing it:** choose the position (bottom right, center or left; top right or left), height and margin. If there's a border, the margin is measured from the border.
  - **Clear art behind it** removes any artwork under the signature so it stays readable.

### Subject selection and background removal

For PNG and JPG pictures, the Source panel has **Remove background** and **Select subject…**. Removed areas become paper white, so no effect puts ink there. The non-AI tools run locally with OpenCV and need no downloads.

- **Remove background (automatic):**
  - On a plain, even background (product shots, scans), it removes everything that matches the background color and touches the picture's edge.
  - On busy photos, it uses GrabCut (OpenCV's built-in subject cut-out) and assumes the subject sits a little inside the frame.
- **Select subject editor:**
  - **Box select:** drag a box around the subject and GrabCut cuts it out.
  - **Erase / Restore brushes:** paint areas out or back in. Turn on **Smart edges** to let GrabCut snap your strokes to the subject's real outline.
  - **Invert, Grow, Shrink, Undo** (⌘Z), **Reset.** Keyboard: B, E and R switch tools.
- **AI remove background** (optional, uses [rembg](https://github.com/danielgatis/rembg)): one click, and it works on busy photos.
  - Install it with `uv sync --extra ai`. Without it, the button is hidden and the app explains how to add it.
  - Pick a model in the Source panel. Each one downloads from rembg's GitHub releases the first time you use it, into `~/.rembg/models/`.

    | Model | Best for | Download |
    |---|---|---|
    | General (ISNet) | Most pictures (default) | 170 MB |
    | Finest detail (BiRefNet lite) | Hair, fur, fine edges; slower | 214 MB |
    | People (U²-Net human) | Portraits | 168 MB |
    | Small and quick (Silueta) | Fast checks | 42 MB |
    | Fastest (U²-Netp) | Rough cut-outs | 4.4 MB |
  - The result can be refined with the editor's brushes like any other selection.
- **Transparent PNGs** (already cut out) start with their transparency as the selection.
- **Use selection** switches the cut-out on and off without losing it. **Edge softness** feathers the cut.

### Cropping

Click **Crop…** in the Source panel. It opens the picture editor on the Crop tool.

- **Draw the crop:** drag a box on the picture, drag inside it to move it, or drag a corner to resize it. A thirds grid helps with composition.
- **Shape:** Free, Drawing area, Original, or Square.
- **Match area to crop:** reshapes the drawing area to the crop's proportions, keeping its width and center.
- **Reset crop:** goes back to the whole picture.
- **Placement** (in the tone controls): **Fill the area** trims whatever doesn't fit. **Fit inside** shows the whole picture with blank margins.

The crop is stored in the picture's own orientation, so **Turn image** still works on it. The plate preview shows exactly the part that will be plotted.

### Effects for photos and pictures

Every setting is a slider with a live value. Settings that only matter in some modes appear when they apply, and **Reset settings** restores the defaults. The image tone controls are sliders too, including **Paper white**, which leaves near-white areas without ink.

| Group | Effect | Highlights |
|---|---|---|
| Trace to vector lines | Outline trace, Centerline trace, Contour lines | See above |
| Single continuous line | **Wave lines** | Line angle; wave height up to 1× spacing, so neighbors overlap for full black |
| | **Spiral** | Off-center positioning; can keep spiralling into the corners |
| | **Hilbert squiggle** | One space-filling line that wiggles harder in dark areas; rounded corners |
| Hatching & engraving | **Cross-hatch** | 1–4 layers; each darker tone adds a hatch at a new angle |
| | **Flow lines** | Engraving-style lines that follow edges (or cross them), packed closer in shadows |
| Dots | **Halftone** | Circle, square, line or cross dots; filled or outline |
| | **Stipple** | Density contrast; drawn as one TSP line, pen taps, or small filled dots |
| | **Dither dots** | Floyd–Steinberg, Atkinson or ordered dithering on a pen-sized grid |

### Pen holder settings are shared

The pen offset, pen width, Z heights and speeds describe your printer, not the artwork. They live in `~/.config/bambuplot/machine.json` and apply to every project. Change them in any project and the others follow, and new projects start with them.

The offset is where the pen tip sits relative to the nozzle: +X right, +Y toward the back. A pen 30 mm in front of the nozzle is **Y −30**. With that offset, the pen can't reach the back 30 mm of the plate; the orange *pen reach* box on the plate shows the limit. If the offset is still 0, 0, a note in the safety checks reminds you to set it.

### Units

**Bed → Units** switches page and paper measurements between **mm**, **cm** and **in**. This covers paper sizes, the drawing area, edge distances, margins, clips, tape, pen offsets, the alignment readings, and the labels on the plate. Snap follows the unit: 1 mm, or 1/16 in in inches.

The choice is remembered on this computer. Projects and G-code always store millimetres, so switching never changes a layout. Pen width, Z heights and effect settings stay in mm.

### Paper size and placement

Set these in **Bed → Paper**:
- **Presets:** A5, A6, B6, Half Letter, Postcard 4 × 6 in, or Square 140.
- **Custom size…:** type a width and height, or press **Draw on Plate** and drag the sheet out on the bed. **Draw on Plate** under Drawing Area does the same for the drawing area.
- **From bed edges:** Left / Front / Right / Back show how far the sheet (or drawing area) is from each plate edge. Type any of them to move it there. Negative means it hangs over.
- **Center** puts the sheet in the middle of the plate.

**Tape** (Bed → Tape): mark which edges of the sheet are taped down (Left, Front, Right, Back), and set:
- **Tape width:** the width of your roll.
- **On paper:** how far the tape reaches onto the sheet.
- **Keep-out:** the gap to keep between the tape and any ink.

The strips show on the plate and turn with the sheet. The **Clear of tape** check refuses a drawing area that touches them, and **Fit to Free Space** stays clear of them. Clips are optional when you tape: **Remove All** clears them. If nothing holds the sheet down, you get a note.

Select the paper or the drawing area on the plate and measurement lines show the same distances. Overhang is in red. The ruler button in the status bar turns the lines on and off.

### Alignment test

Use this to find out how far off your drawings land on the sheet, and fix it.

1. Create a test: **Alignment Test** on the dashboard makes one using your most recent layout. Or, in any project, use **Bed → Alignment → Download Test / Send Test…** (also in the Download menu and the Send sheet). It plots a circle with a crosshair, a millimetre scale and a "↑ back" arrow at the centre of the drawing area.
2. Measure from the sheet's **left edge** and **front edge** to where the cross meets, and enter both. BambuPlot shows where the cross should be and how far off it landed, for example "2.5 mm right, 1.0 mm toward the front", and how to move the sheet.
3. Fix it:
   - **Update Paper Position:** tells the layout where the sheet really is.
   - **Correct Pen Offset:** use this if you placed the sheet exactly, so the error is in the holder.
4. Optionally, enter the circle's outside width and height to check that X and Y aren't stretched. More than 0.4% off points to belt tension.

Measurements are in the sheet's own frame, so a rotated sheet works too.

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

**Open in Bambu Connect (over the internet):** sends the job through your Bambu account, using [Bambu Connect](https://wiki.bambulab.com/en/software/bambu-connect), Bambu's official app for third-party software.
1. BambuPlot packages the job into your template and saves it in `~/.config/bambuplot/jobs/` (the last 20 jobs are kept).
2. It opens Bambu Connect with the job loaded.
3. In Bambu Connect, pick your printer and send it, then start the job at the printer.

This needs Bambu Connect installed and a project template. The printer must be connected to the Bambu cloud, so Developer Mode has to be off. Same job from the command line: `bambuplot connect FILE.gcode`.

It isn't known yet whether Bambu Connect accepts a job with no extrusion; try the smoke test first.

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
| `effects.py` | Effect registry: tone effects, trace effects, SVG paths |
| `vectorize.py` | Threshold, outlines, hatching, centerline skeleton tracing, contour lines |
| `svgin.py` | SVG reader: shapes, transforms, fills, visibility |
| `mask.py` | Background removal, box select (GrabCut), erase/restore brushes, undo |
| `tsp.py` | Nearest-neighbor tour and 2-opt for stipple |
| `paths.py` | Clamp, simplify, order, merge, stats |
| `gcode.py` | Start sequence with pauses, stroke compiler |
| `safety.py` | G-code allow-list validator |
| `calibrate.py` | Smoke test and press ladder |
| `package.py` | Wraps G-code in a `.gcode.3mf` from a Bambu Studio template |
| `printer.py` | LAN Only upload (FTPS), start/status (MQTT), Bambu Connect hand-off |
| `extras.py` | Border and signature: storage, tracing from a photo, placement, clearing art behind it |
| `layers.py` | Layer model, frames inside the area, pen slots and grouping |
| `projects.py` | Projects on disk: state, source file, selection, thumbnail |
| `server.py` / `web/` | FastAPI backend and the browser UI |

To add an effect, write a function in `effects.py` decorated with `@register(...)`. It must return polylines in area coordinates. The UI picks up its parameters automatically.
