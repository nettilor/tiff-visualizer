"""Timings for the paths that touch many panes at once: opening a batch of
stacks, combining into / splitting out of the grid, solo, closing tiles and
windows. Run from the repo root on the real platform (windows will flash on
screen) — the offscreen platform skips native windows and painting, which
is where most of the time goes:

    N=48 .venv/bin/python -u tests/bench_layout.py
    N=24 PROFILE=split,closetile .venv/bin/python -u tests/bench_layout.py

N stacks are opened from example_stacks/ (reused round-robin). PROFILE names
phases to print a cProfile breakdown for: open, combine, solo, unsolo,
closetile, closetile_shared, split, closewin, closefolder, closeall.
Reference numbers (M-series Mac, 48 stacks, 2026-10-28, before -> after the
incremental grid layout): split 4.7 s -> 0.9 s, close one tile 200 -> 26 ms,
un-solo 190 -> 10 ms, close all 2.8 s -> 40 ms.
"""

import cProfile
import io
import os
import pstats
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
N = int(os.environ.get("N", "24"))
PROFILE = set(os.environ.get("PROFILE", "").split(","))
TOP = int(os.environ.get("TOP", "18"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

TMP = tempfile.mkdtemp(prefix="tiffviz_bench_")
from tiff_visualizer import settings as app_settings  # noqa: E402

# A throwaway settings file: the user's QSettings stay untouched.
app_settings.settings = lambda: QSettings(os.path.join(TMP, "s.ini"), QSettings.IniFormat)
app_settings.settings().setValue("preload/enabled", os.environ.get("PRELOAD", "0") == "1")

from tiff_visualizer import control_panel, session, viewer, workspace  # noqa: E402
from tiff_visualizer.theme import apply_dark_theme  # noqa: E402

app = QApplication(sys.argv)
apply_dark_theme(app)
ctrl = control_panel.get_control_window()
ctrl.show()

stacks = sorted(p for p in (REPO / "example_stacks").iterdir() if p.suffix.lower() in (".tif", ".tiff"))
paths = [stacks[i % len(stacks)] for i in range(N)]


def settle(ms=150):
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        app.processEvents()


def timed(label, fn, tag=None):
    """Time fn, then the events it leaves behind (deferred deletes, pool
    renders landing, paints)."""
    settle(300)
    prof = cProfile.Profile() if tag in PROFILE else None
    t0 = time.perf_counter()
    if prof:
        prof.enable()
    fn()
    t_call = time.perf_counter() - t0
    settle(10)
    if prof:
        prof.disable()
    dt = time.perf_counter() - t0
    print(f"{label:52s} call {t_call * 1000:8.1f} ms   +events {dt * 1000:8.1f} ms", flush=True)
    if prof:
        for sort in ("tottime", "cumulative"):
            out = io.StringIO()
            pstats.Stats(prof, stream=out).sort_stats(sort).print_stats(TOP)
            lines = [
                line for line in out.getvalue().splitlines()
                if line.strip() and not line.startswith("Ordered") and "function calls" not in line
            ]
            print(f"--- {label}: top by {sort} ---\n" + "\n".join(lines) + "\n", flush=True)


timed(f"open {N} stacks (floating windows)", lambda: viewer.open_paths(paths, ctrl), "open")
assert len(viewer._all_panes) == N
timed(f"combine_all ({N})", workspace.toggle_combined, "combine")
ws = workspace._workspace
timed("solo a tile (Enter)", lambda: ws.toggle_solo(ws.panes[5]), "solo")
timed("un-solo (Esc)", lambda: ws.toggle_solo(ws.panes[5]), "unsolo")
pane = ws.panes[3]
timed("close one grid tile (plain)", lambda: ws.close_pane(pane), "closetile")
timed("shared axes ON", lambda: workspace.set_shared_axes(True), "shared")
pane = ws.panes[3]
timed("close one grid tile (shared axes)", lambda: ws.close_pane(pane), "closetile_shared")
timed("shared axes OFF", lambda: workspace.set_shared_axes(False))
timed(f"split_all ({len(ws.panes)})", workspace.toggle_combined, "split")
assert not workspace.workspace_active()
window = next(iter(viewer._open_windows))
timed("close one floating window", window.close, "closewin")
timed("combine_all again", workspace.toggle_combined, "combine2")
section = ctrl.add_folder_section(REPO / "example_stacks")
settle(100)
key = next(iter(section.checks))
timed(
    "close via folder list (one file, all its panes)",
    lambda: control_panel._close_stacks_by_paths({key}),
    "closefolder",
)
timed(f"session.close_all ({len(viewer._all_panes)} panes)", session.close_all, "closeall")
print("panes left:", len(viewer._all_panes), flush=True)
os._exit(0)
