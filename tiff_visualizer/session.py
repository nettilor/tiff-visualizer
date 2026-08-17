"""Session save/restore: which stacks are open, their view state and layout."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import QByteArray, QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox

from . import settings as app_settings
from . import viewer, workspace
from .stack_io import load_stack
from .viewer import StackPane, StackWindow


def _geometry_to_str(geo: QByteArray) -> str:
    return bytes(geo.toBase64()).decode()


def _geometry_from_str(s: str) -> QByteArray:
    return QByteArray.fromBase64(s.encode())


def capture() -> dict:
    ws = workspace._workspace
    ws_active = workspace.workspace_active()
    stacks = []
    # Grid panes first, in grid order, so restoring rebuilds the same layout.
    panes = list(viewer._all_panes)
    if ws_active:
        panes = list(ws.panes) + [p for p in panes if p not in ws.panes]
    for pane in panes:
        if pane.stack.path is None:
            continue  # unsaved derived stacks (projections) can't be reopened
        t, z, c = pane.position()
        entry = {
            "path": str(pane.stack.path),
            "position": [t, z, c],
            "ranges": [list(map(float, r)) for r in pane.stack.ranges],
            "visible_channels": list(pane.visible_channels),
            "composite": pane._composite_on(),
            "mip": pane._mip_on(),
            "locked": pane.shared_locked,
            "flagged": pane.flagged,
            "in_grid": ws_active and pane in ws.panes,
        }
        window = next((w for w in viewer._open_windows if w.pane is pane), None)
        if window is not None:
            entry["geometry"] = _geometry_to_str(window.saveGeometry())
        stacks.append(entry)

    data = {"version": 1, "stacks": stacks}
    if ws is not None:
        data["workspace"] = {
            "visible": ws_active,
            "geometry": _geometry_to_str(ws.saveGeometry()),
            "grid": ws.grid_combo.currentIndex(),
            "shared_axes": ws.shared_checkbox.isChecked(),
            "shared_channels": ws.shared_channels_checkbox.isChecked(),
            "shared_view": ws.shared_view_checkbox.isChecked(),
            "minimalist": ws.minimal_checkbox.isChecked(),
            "mip_all": ws.mip_checkbox.isChecked(),
            "flag_filter": ws.flag_checkbox.isChecked(),
            "shared_position": list(ws.shared_position()),
        }
    return data


def close_all():
    ws = workspace._workspace
    if ws is not None:
        for pane in list(ws.panes):
            ws.close_pane(pane)
    for window in list(viewer._open_windows):
        window.close()


def restore(data: dict, parent=None):
    close_all()
    missing = []
    grid_panes = []
    for entry in data.get("stacks", []):
        path = Path(entry["path"])
        if not path.exists():
            missing.append(path.name)
            continue
        try:
            stack = load_stack(path)
        except Exception:  # noqa: BLE001
            missing.append(path.name)
            continue
        pane = StackPane(stack)
        saved_ranges = entry.get("ranges", [])
        for c in range(min(len(saved_ranges), stack.n_channels)):
            stack.ranges[c] = saved_ranges[c]
        visible = entry.get("visible_channels", [])
        for c in range(min(len(visible), stack.n_channels)):
            pane.visible_channels[c] = bool(visible[c])
        if pane.composite_box is not None:
            pane.composite_box.blockSignals(True)
            pane.composite_box.setChecked(entry.get("composite", stack.composite))
            pane.composite_box.blockSignals(False)
        if pane.mip_box is not None and entry.get("mip"):
            pane.mip_box.setChecked(True)
        t, z, c = entry.get("position", [0, 0, 0])
        for letter, value in (("t", t), ("z", z), ("c", c)):
            if letter in pane.bars:
                pane.bars[letter].set_value_silent(min(value, pane.bars[letter].count - 1))
        pane.refresh()
        if entry.get("flagged"):
            pane.set_flagged(True)
        if "geometry" in entry:
            pane.saved_window_geometry = _geometry_from_str(entry["geometry"])
        if entry.get("in_grid"):
            grid_panes.append((pane, entry.get("locked", False)))
        else:
            window = StackWindow(pane)
            window.show()

    ws_info = data.get("workspace", {})
    if grid_panes:
        ws = workspace.get_workspace()
        ws.shared_checkbox.setChecked(ws_info.get("shared_axes", False))
        ws.shared_channels_checkbox.setChecked(ws_info.get("shared_channels", False))
        ws.shared_view_checkbox.setChecked(ws_info.get("shared_view", False))
        ws.minimal_checkbox.setChecked(ws_info.get("minimalist", False))
        ws.mip_checkbox.setChecked(ws_info.get("mip_all", False))
        ws.grid_combo.setCurrentIndex(ws_info.get("grid", 0))
        ws.add_panes([p for p, _locked in grid_panes])
        for pane, locked in grid_panes:
            if locked:
                pane.lock_button.setChecked(True)
        ws.flag_checkbox.setChecked(ws_info.get("flag_filter", False))
        if "geometry" in ws_info:
            ws.restoreGeometry(_geometry_from_str(ws_info["geometry"]))
        t, z, c = ws_info.get("shared_position", [0, 0, 0])
        for letter, value in (("t", t), ("z", z), ("c", c)):
            if letter in ws.shared_bars:
                ws.shared_bars[letter].set_value(min(value, ws.shared_bars[letter].count - 1))
        ws.show()
    from . import control_panel

    control_panel.refresh_state()
    if missing:
        QMessageBox.warning(
            parent, "Session restored with gaps", "Could not reopen:\n" + "\n".join(missing)
        )


# ---- file plumbing -----------------------------------------------------


def _last_session_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path / "last_session.json"


def save_session_to(path: str | Path):
    Path(path).write_text(json.dumps(capture(), indent=2))


def save_last_session():
    try:
        if viewer._all_panes:
            save_session_to(_last_session_path())
    except Exception:  # noqa: BLE001 - never block app exit
        pass


def save_session_dialog(parent=None):
    start = str(Path(app_settings.last_dir() or Path.home()) / "session.tiffviz.json")
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save session", start, "TIFF Visualizer session (*.tiffviz.json *.json)"
    )
    if not path:
        return
    save_session_to(path)


def open_session_dialog(parent=None):
    path, _ = QFileDialog.getOpenFileName(
        parent,
        "Open session",
        app_settings.last_dir(),
        "TIFF Visualizer session (*.tiffviz.json *.json)",
    )
    if not path:
        return
    restore(json.loads(Path(path).read_text()), parent)


def restore_last_session(parent=None):
    path = _last_session_path()
    if not path.exists():
        QMessageBox.information(parent, "No session", "No previous session was found.")
        return
    restore(json.loads(path.read_text()), parent)
