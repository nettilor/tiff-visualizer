# Changelog

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
