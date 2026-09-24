# Changelog

## 1.6.0 — 2026-09-24

### Exports
- **Montage labels say where every tile came from**: Export Grid Montage
  labels each tile `XY05.tif  ·  c 1+2  z 5  t 12` — the displayed
  channels, z slice and t frame, with the projection (`z MIP`, `z AVG`, …)
  in place of the slice when it is on. GIF frames follow the movie's
  position, and frames past a stack's end read "no image". When a cell is
  too narrow for one line, every tile switches to name over position, so
  the sheet and every GIF frame keep one size; labels are clipped to their
  cell instead of running into the neighbor's. Export Stack Montage gets a
  title line with the stack name, channels and projection. Both are on by
  default, each with a checkbox to turn it off.
- **t and z export ranges**: Export Stack Montage has t and z ranges next
  to the every-nth steps (the z range is greyed out while z collapses to a
  projection, which covers every slice as on screen); Export Movie and grid
  montage GIFs get a range for the animated axis. Ranges are remembered only
  while narrowed, so a full range stays full on a longer stack.
- Default file names say what is inside: `XY05_t3_z2-6.gif` (instead of
  naming only the current frame), `XY05_montage_t2-4.png` / `…_MIP.png`,
  `montage_t4-8.gif`.
- Export Movie shows a cancelable progress bar and no longer holds every
  frame in RAM; with nothing to animate it says so instead of writing a
  one-frame GIF.
- Copy and Export PNG refuse a grid tile that is black on screen (shared
  position past its end), and movies and grid GIFs take the displayed
  position instead of exporting a stale frame.
- Every export reports success in the window you are working in, and a
  failed write (read-only folder, full disk) in a dialog.
- Export Stack Montage remembers "collapse z" whatever the projection
  method, and no longer offers a collapse that would leave a single tile.

### Files
- **Save As could destroy the original file**: saving over a file that was
  still memory-mapped — after an earlier Save As, from a relative path, or
  with the same file open twice — truncated it. Every open copy of the
  target now moves into RAM first, and Save As waits for background
  preloads and renders still reading it.
- Read-only TIFFs open.
- A NaN pixel no longer turns a float image black: NaN renders black and is
  left out of display ranges and measurements, like Fiji.
- RGB TIFFs open composite in red, green and blue.
- Fiji's single-channel display range (min/max) is read and written back;
  8- and 16-bit palette LUTs are used; ImageJ "color" mode survives a save,
  and a multi-channel stack saved with Composite unchecked is written in
  "color" mode — one channel at a time in its LUT color, as shown.
- Single-channel stacks saved by the app reopen.
- Symlinked positions keep their own names in headers, montage labels and
  export names.
- Float images: Reset spans the data, and a flat plane gets a range scaled
  to its values instead of +1.

### Grid
- Locked tiles keep their own sliders when tiles join or leave, float
  without jumping to the shared position, and locks clear (without moving
  anything) when shared axes turn off.
- A tile with its projection on stays drawn when the shared z is past its
  slice count, and its arrow keys still step the shared z.
- **Playback renders off the UI thread**: shared playback prefetches through
  the render pool (11 stacks: ~144 ms → ~1 ms of UI time per tick, no
  dropped ticks) and keeps playing at its speed when tiles join or leave.
- The grid keeps the live pixel value: the bottom controls row mirrors the
  probe of whichever tile the cursor is over, so Minimalist mode still
  shows it.
- "★ only" clears when the last flagged tile closes or floats.
- Stacks combined into an empty grid sync under Shared channels.
- Turning Shared axes off leaves Minimalist; any drag returns the sort to
  Manual; Brightness sort ranks what each tile displays, projections
  included.

### Viewer
- The pixel readout updates when the plane changes under a still cursor.
- With z collapsed to a projection, arrows, wheel and Space no longer move
  the hidden slice, and the header drops its slice label.
- Save As and Projection keep the pane's Composite state; a projection
  opens with the source's visible channels.
- Closing a floating window frees its stack (it stayed in RAM and dropped
  out of the preload budget's count).

### Sessions & selections
- Restored contrast shows immediately instead of the load-time one until
  you scrub.
- Per-tile projection settings survive a restore under MIP all.
- Sessions store absolute paths and still open older relative ones.
- Ellipses are measured and saved as the true ellipse (they were a
  24-sided polygon: ~1.2% of the area off, shrinking ~2 px per save and
  restore).
- Replacing, removing or clearing a selection mid-drag can no longer crash
  the app.

### Brightness & Contrast
- Histogram, Auto and Reset use the displayed plane — the projection when
  it is on.
- Float images show enough decimals, and nudges step by an amount scaled
  to the data.
- The panel retargets when its stack closes, Apply to all never pushes a
  closed stack's range, and undo history no longer keeps closed stacks in
  RAM.

### App
- Files opened together from Finder or the Dock open as one batch.
- Folder lists, Open Folder… and dropped folders sort naturally (XY2 before
  XY10) and skip macOS `._` files; their checkboxes match stacks opened
  through relative or symlinked paths.
- A manual Check for Updates during the launch check still answers;
  `darwin`-named release assets count as macOS.
- Regression suite: 89 checks.

## 1.5.0 — 2026-09-06

### Measurements
- **Measure (Cmd+M)**, Fiji's Analyze → Measure: the active stack's current
  channel at its current z/t position — the projected plane when the pane's
  z projection is on — measured inside its selection, or over the whole
  image without one. Works on floating windows and on the active grid tile
  alike, shared axes included; the host window's status bar shows the values.
- **Measurements table** (Analyze → Measurements Table): one row per Cmd+M
  with stack, c/z/t, ROI (shape plus bounding box, e.g. `rect 120,80 64×64`
  or `whole image`), Area, Mean (MFI), Min and Max — whole numbers plain,
  everything else with three decimals like Fiji. Pops up on the first
  measurement without stealing focus. Copy (Cmd+C) puts the selected rows,
  or all rows when none are selected, on the clipboard tab-separated with a
  header line for spreadsheets; Save As… writes a CSV; Delete removes the
  selected rows; Clear (or Analyze → Clear Measurements) empties it.

### Selections
- **Rectangle, ellipse, polygon and freehand selections**, Fiji-style: one
  app-wide tool, picked from the new tool row in the control window,
  Analyze → Selection Tool, or the keys H / R / E / P / D. The Hand pans;
  with a shape tool a left drag draws — rectangle and ellipse drag out a
  whole-pixel box clipped to the image, freehand traces an outline, polygon
  takes a click per corner and closes on a double-click or a click on the
  first corner. The probe row shows the shape and size while drawing.
- Each stack keeps one selection in Fiji yellow: drawing a new one replaces
  it, dragging inside it with a shape tool moves it (the hand always pans,
  even over a whole-image selection), handles resize it or move polygon
  corners, a click outside clears it. Esc abandons a shape mid-draw, then
  clears; Cmd+A selects the whole image; Cmd+Shift+A or right-click →
  Remove ROI clear. Selections stay put while scrubbing z/t or switching
  channels, so one region can be followed through a timelapse, and they
  are saved in sessions.
- Cmd+M rasterizes the shape to a pixel mask exactly as drawn; a selection
  dragged entirely off the image reports "Nothing to measure".

### App
- **Dock icon raises the control window**: clicking the app's dock icon
  brings the control window above the stack and grid windows and focuses
  it, un-minimizing it first if needed. Only real dock clicks do it —
  cmd-tab or clicking a stack window leave the window order alone.
- **LZW- and Deflate-compressed TIFFs open in the bundled app**: the
  imagecodecs codec modules are now collected into the macOS and Windows
  builds (they are imported dynamically, so PyInstaller missed them and
  such files failed with "cannot import name 'lzw_decode'").
- Regression suite: 44 checks.

## 1.4.0 — 2026-08-24

### Viewing
- **Z-projection type on the fly**: the per-pane projection checkbox is
  labeled with the method it is running — **MIP** (max, as before), MIN, AVG,
  MED or SUM — and right-clicking it offers Fiji's five methods (Max
  intensity, Min intensity, Average, Median, Sum slices). Picking one also
  switches the projection on, so swapping mid-scrub is one right-click. The
  header reads `z AVG`, PNG/GIF exports are named `…_AVG` / `…_MED`, the
  pixel probe reports projected values, and the Stack Montage's "collapse z"
  option follows the pane's method. **MIP all** in the grid controls gained
  the same menu and applies the choice to every tile. Sum scales its display
  window with the slice count, so it reads like the mean on screen while the
  probe still reports true sums. The method is saved in sessions.

### Control window
- **Open all / Close all** under each dropped folder's file list: open every
  stack in the folder, or close every pane showing one of them (grid tiles
  included). Each button greys out when it has nothing to do, and the status
  bar reports how many stacks were opened or closed.

### Performance
- Opening or closing many stacks at once is no longer O(n²): stacks are added
  to the grid in one batch (`viewer.open_paths`), tiles are closed in one
  batch (`WorkspaceWindow.close_panes`), and the control window suspends its
  refreshes for the duration — ~7× faster at 11 stacks, more as the count
  grows. Open folder…, multi-file Open… and file drops share the same path.
- Failures while opening a batch are collected into one dialog instead of one
  per file.

### App
- Regression suite: 39 checks.

## 1.3.0 — 2026-08-24

### Export
- **Montage grid layout**: Export Stack Montage gains a Layout choice for a
  single varying axis (t, z, or t with z collapsed to a MIP) — Auto grid
  (near-square, as before), One row, One column, or Custom columns × rows.
  The grid boxes always show the effective grid and unlock in Custom, where
  editing one dimension only grows the other when the typed grid would drop
  tiles; oversized grids leave black cells, and the size estimate says how
  many are empty. With both t and z varying the sheet stays t across × z
  down.
- **Dialogs remember their last-used options**: Export Stack Montage, Export
  Grid Montage, Export Movie and Projection reopen exactly as last accepted
  (Cancel changes nothing), so repeating an export across positions is just
  OK, OK, OK. Choices a stack doesn't offer are skipped and values clamp to
  its range; a stack lacking an axis never overwrites the remembered setting
  for it; a projection range is remembered only when it was narrowed, so a
  full range stays full on a taller stack.

### App
- Regression suite: 37 checks.

## 1.2.0 — 2026-08-17

First public release on GitHub.

### Grid workspace
- **Solo / focus tile**: Enter or a double-click on a tile's header fills the
  workspace with that tile — same position, zoom and contrast; Esc or Enter
  drops it back. Hidden tiles skip rendering while soloed, so scrubbing gets
  single-window fast.
- **Flagging & triage**: `F` flags the active stack with an amber ★ in its
  header; a "★ only" filter appears in the grid controls once anything is
  flagged (and clears itself when the last flag goes). Flags persist in
  sessions; **View → Copy Flagged Names** puts the list on the clipboard.
- **Sorting**: Manual / Name (natural order, so XY2 comes before XY10) /
  Brightness (one-shot, brightest first by mean intensity of the visible
  channels at the current position). Dragging a tile returns to Manual;
  sessions preserve the grid order.
- **Minimalist mode**: forces shared axes on and strips every per-tile
  control, leaving name + info above each image with 2 px dividers — the
  whole window given to pixels.

### Channels
- **Number keys 1–9** on the active stack: in Composite each digit toggles
  that channel's visibility, otherwise it jumps the c bar to that channel.
- **Numbered channel boxes** beside a shortened c bar, colored by each
  channel's LUT; two-way sync with the B&C panels and shared-channels mode.
  With shared axes + shared channels they sit next to the shared c bar.
- Fixed: the B&C channel radios could not switch channel in a shared-axes
  grid — the click was immediately snapped back, leaving no way to adjust
  another channel's contrast except the shared slider.

### Export
- **Export Stack Montage…** (Cmd+Alt+M): one PNG contact sheet from a single
  stack, t across columns and z down rows with labels framing the sheet; a
  single varying axis wraps into a near-square grid; z can collapse to a max
  projection. Every-nth t/z steps, full/half/quarter resolution with a live
  output-size estimate, channels as displayed or one file per channel.
- **Export Grid Montage…** (Cmd+Shift+M): the displayed tiles in grid order as
  one labeled montage — PNG at the current position or GIF over t/z. Per-tile
  contrast, channels and MIP honored; differing sizes letterboxed; respects
  "★ only" and solo, so you export what you see.

### App
- **Update checking**: the app looks at its GitHub releases page at most once
  a day and stays silent unless there is something newer — and says nothing
  at all offline. **File → Check for Updates…** (in the TIFF Visualizer menu
  on macOS) asks on demand; a checkbox in Settings turns the daily look off.
  An offered update downloads to ~/Downloads and opens, ready to drag into
  Applications; nothing is installed behind your back. It is the only network
  request the app ever makes.
- Fixed: the control window opened absurdly tall when it had last been
  stretched for a folder list — it now keeps the saved position and width but
  compacts the height, since folder lists never exist at launch.
- New app icon (v3): a bold 3×3 grid, columns in Italian-flag order, fading
  downward like z-slices.
- Permanent regression suite (`tests/regression.py`): 35 checks over the whole
  feature matrix, run before releases.

## 1.1.0 — 2026-08-13

### Grid workspace
- **Shared view**: link pan/zoom across all tiles — zoom into a region on one
  stack and every tile shows the same region.
- **Per-tile lock** (🔓/🔒): pin a tile out of shared axes; it keeps its
  position (with its own bars back) while the others scrub.
- **MIP all**: max-project every tile over z with one checkbox; per-tile MIP
  toggle also available on each pane.
- **Rearrange by dragging**: tile header strips are drag handles; a blue
  insertion bar shows exactly where the tile will land (left half = before,
  right half = after).
- **Combine Selected…** (Alt+Cmd+G): checkbox dialog to choose exactly which
  stacks tile into the grid; the rest stay floating.
- Grid scrolls inside the window instead of forcing it beyond the screen;
  tiled panes accept a smaller minimum size than floating windows.

### Playback & performance
- **Play buttons** on every z/t bar (and the shared bars), right-click for
  2–30 fps; **Space** toggles time playback anywhere.
- **Parallel tile rendering**: grid ticks render across CPU cores; the UI
  stays responsive during 48-stack playback (main-thread cost per tick
  ~110 ms → ~23 ms).
- **RAM preloading** (Settings, Cmd+,): stacks are copied into memory within a
  configurable GB budget so playback never waits on the disk; live usage
  readout; over-budget stacks stay memory-mapped.
- Rendered-plane cache enlarged to hold a full t-loop; adjacent planes
  prefetched at idle.
- Compressed TIFFs load in a background thread (uncompressed stays instant
  via memory-mapping).

### Files & sessions
- **Folder swap lists**: drop a folder onto the control window to get a
  checkbox per TIFF — check to open, uncheck to close; checkboxes track
  stacks opened/closed elsewhere; the list stretches with the window.
- **Open Folder…** (Cmd+Shift+O) opens every TIFF in a directory.
- **Sessions** (Cmd+Alt+S/O/R): save/open/restore-last — stacks, positions,
  contrast, channel state, MIP/locks, window geometries and grid arrangement;
  auto-saved on quit.
- Drag & drop of files onto the control window and grid; the macOS app
  accepts drops on its Dock icon and appears in Finder's "Open With".

### Viewing & export
- **Live MIP** per pane: max projection over z while scrubbing t.
- **Copy View** (Cmd+C), **Export View as PNG** (Cmd+E), **Export Movie GIF**
  over T or Z with fps choice (Cmd+Shift+E) — full resolution, current
  contrast/channels.
- **Apply to all** in B&C: one channel's min/max to every open stack;
  **Cmd+Z** undoes any contrast change (slider drags coalesce; one undo
  reverts a whole apply-to-all).
- B&C drag-to-fuse removed (too clumsy); the per-tile B&C button attaches
  fused panels.

### App
- **Settings window** (Cmd+,): RAM preload budget and **text size** (9–24 pt,
  applied live app-wide).
- Persistent settings: last folder, window geometries, grid preferences.
- **Keyboard cheatsheet** on "?".
- New app icon: dark grid-of-tiles design in the channel colors.

## 1.0.0 — 2026-08-13

First release as a self-contained app (macOS .app via PyInstaller;
`packaging/build_windows.bat` builds the Windows equivalent).

- ImageJ/Fiji hyperstack TIFFs as the native format: dimensions, LUTs,
  display ranges and slice labels read and written losslessly; multi-GB
  stacks open instantly via memory-mapping.
- Per-stack floating windows and the combined grid workspace (Cmd+G) with
  shared axes and shared channels; panes move between modes with state and
  window positions preserved.
- Fiji-style navigation: c/z/t bars, wheel/keys with Shift/Alt modifiers,
  pinch zoom at the cursor, zoom-out capped at fit.
- Brightness & Contrast: shared follow-focus window (pinnable) and per-pane
  fused panels; histogram with draggable range, Auto/Reset, bounds clamped
  to the image dtype.
- Z/T projections (Max/Min/Mean/Median/Sum) with Fiji naming; Save As in
  ImageJ format.
- Always-on control window, black theme for microscopy, flat app icon.
