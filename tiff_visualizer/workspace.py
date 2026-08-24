"""The workspace window: napari-style tiling of stack panes in one window.

Cmd+G toggles between all stacks combined here as a resizable grid and each
stack floating in its own window. Panes are moved, never rebuilt, so position,
zoom and contrast survive every combine/split.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import settings as app_settings
from . import stack_io
from . import viewer
from .stack_io import TiffStack
from .viewer import DimBar, FileDropMixin, StackPane, StackWindow, build_menus

_workspace: "WorkspaceWindow | None" = None


def get_workspace() -> "WorkspaceWindow":
    global _workspace
    if _workspace is None:
        _workspace = WorkspaceWindow()
    return _workspace


def workspace_active() -> bool:
    if _workspace is None:
        return False
    try:
        return _workspace.isVisible() and bool(_workspace.panes)
    except RuntimeError:  # C++ side already deleted during app shutdown
        return False


def _natural_key(name: str):
    """Sort key where XY2 < XY10."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", name)]


def export_montage(parent=None):
    """Image > Export Grid Montage: the tiled grid as one PNG or GIF."""
    if not workspace_active():
        QMessageBox.information(
            parent, "Grid Montage", "Combine stacks into the grid first (Cmd+G)."
        )
        return
    _workspace._export_montage()


def shared_axes() -> bool:
    return _workspace is not None and _workspace.shared_checkbox.isChecked()


def set_shared_axes(on: bool):
    get_workspace().shared_checkbox.setChecked(on)


def combine_all():
    """Move every floating stack window's pane into the tiled workspace."""
    ws = get_workspace()
    windows = sorted(
        viewer._open_windows,
        key=lambda w: viewer._all_panes.index(w.pane) if w.pane in viewer._all_panes else 1e9,
    )
    detached = []
    for window in windows:
        if window.pane is not None:
            window.pane.saved_window_geometry = window.saveGeometry()
        pane = window.detach_pane()
        window.close()
        if pane is not None:
            detached.append(pane)
    ws.add_panes(detached)
    if ws.panes:
        ws.show()
        ws.raise_()


def split_all():
    """Give every workspace pane back its own floating window."""
    ws = _workspace
    if ws is None:
        return
    for pane in list(ws.panes):
        ws.take_pane(pane)
        StackWindow(pane).show()
    ws.hide()


def toggle_combined():
    if workspace_active():
        split_all()
    else:
        combine_all()


def combine_selected(selected: list[StackPane]):
    """Put exactly `selected` in the grid; everything else gets its own window."""
    ws = get_workspace()
    selected = [p for p in selected if p in viewer._all_panes]
    for pane in [p for p in ws.panes if p not in selected]:
        ws.take_pane(pane)
        StackWindow(pane).show()
    to_add = []
    windows = sorted(
        viewer._open_windows,
        key=lambda w: viewer._all_panes.index(w.pane) if w.pane in viewer._all_panes else 1e9,
    )
    for window in windows:
        pane = window.pane
        if pane is not None and pane in selected and pane not in ws.panes:
            pane.saved_window_geometry = window.saveGeometry()
            window.detach_pane()
            window.close()
            to_add.append(pane)
    if to_add:
        ws.add_panes(to_add)
    if ws.panes:
        ws.show()
        ws.raise_()
    else:
        ws.hide()
    from . import control_panel

    control_panel.refresh_state()


def show_combine_dialog(parent=None):
    """Alt+Cmd+G: pick which stacks go into the grid."""
    if not viewer._all_panes:
        return
    dialog = CombineSelectionDialog(parent)
    if dialog.exec() == QDialog.Accepted:
        combine_selected(dialog.selected_panes())


class CombineSelectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Combine into Grid")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Checked stacks go into the combined grid;\n"
                "unchecked stacks stay in their own windows."
            )
        )
        in_grid = set(_workspace.panes) if workspace_active() else set()
        self._rows: list[tuple[StackPane, QCheckBox]] = []
        for pane in viewer._all_panes:
            box = QCheckBox(pane.stack.name)
            box.setChecked(pane in in_grid)
            layout.addWidget(box)
            self._rows.append((pane, box))

        select_row = QHBoxLayout()
        all_button = QPushButton("Select all")
        all_button.clicked.connect(lambda: self._set_all(True))
        none_button = QPushButton("Select none")
        none_button.clicked.connect(lambda: self._set_all(False))
        select_row.addWidget(all_button)
        select_row.addWidget(none_button)
        layout.addLayout(select_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all(self, checked: bool):
        for _pane, box in self._rows:
            box.setChecked(checked)

    def selected_panes(self) -> list[StackPane]:
        return [pane for pane, box in self._rows if box.isChecked()]


class MontageDialog(QDialog):
    """Options for Image > Export Grid Montage."""

    def __init__(self, parent, panes: list[StackPane]):
        super().__init__(parent)
        self.setWindowTitle("Export Grid Montage")
        form = QFormLayout(self)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Current position (PNG)", ("png", None))
        if any(p.stack.n_frames > 1 for p in panes):
            self.mode_combo.addItem("Movie over t (GIF)", ("gif", "T"))
        if any(p.stack.n_slices > 1 and not p._mip_on() for p in panes):
            self.mode_combo.addItem("Movie over z (GIF)", ("gif", "Z"))
        self.mode_combo.currentIndexChanged.connect(self._on_mode)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(10)
        self.fps_spin.setEnabled(False)
        self.scale_combo = QComboBox()
        for label, factor in (("Full", 1), ("Half", 2), ("Quarter", 4)):
            self.scale_combo.addItem(label, factor)
        self.labels_box = QCheckBox("Stack names above tiles")
        self.labels_box.setChecked(True)
        form.addRow("Frame:", self.mode_combo)
        form.addRow("Frames/second:", self.fps_spin)
        form.addRow("Resolution:", self.scale_combo)
        form.addRow("", self.labels_box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        # Mode first so its GIF half-size default is overridden by the
        # remembered resolution rather than the other way round.
        self._remembered = {
            "mode": self.mode_combo,
            "fps": self.fps_spin,
            "scale": self.scale_combo,
            "labels": self.labels_box,
        }
        app_settings.restore_widgets("gridMontage", self._remembered)

    def accept(self):
        app_settings.save_widgets("gridMontage", self._remembered)
        super().accept()

    def _on_mode(self, *_):
        is_gif = self.mode_combo.currentData()[0] == "gif"
        self.fps_spin.setEnabled(is_gif)
        if is_gif and self.scale_combo.currentData() == 1:
            self.scale_combo.setCurrentIndex(1)  # GIFs default to half size

    def values(self) -> tuple[str, str | None, int, int, bool]:
        kind, axis = self.mode_combo.currentData()
        return (
            kind,
            axis,
            self.fps_spin.value(),
            self.scale_combo.currentData(),
            self.labels_box.isChecked(),
        )


def float_pane(pane: StackPane):
    """Pop a single pane out of the workspace into its own window."""
    ws = _workspace
    if ws is None:
        return
    ws.take_pane(pane)
    StackWindow(pane).show()
    if not ws.panes:
        ws.hide()


def show_stack(stack: TiffStack) -> StackPane:
    """Open a stack where the user is working: the workspace if it's active,
    otherwise a new floating window."""
    pane = StackPane(stack)
    if workspace_active():
        ws = get_workspace()
        ws.add_pane(pane)
        ws.raise_()
    else:
        StackWindow(pane).show()
    return pane


class WorkspaceWindow(FileDropMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.panes: list[StackPane] = []
        self.active_pane: StackPane | None = None
        self.solo_pane: StackPane | None = None
        self._init_file_drops()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self.row_container = QSplitter(Qt.Vertical)
        self.row_container.setChildrenCollapsible(False)
        # Scroll instead of growing the window when tile minimums exceed it.
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setWidget(self.row_container)
        layout.addWidget(self.scroll_area, 1)

        # Controls row: shared-axes toggle and grid arrangement.
        controls = QHBoxLayout()
        controls.setContentsMargins(4, 0, 4, 0)
        self.shared_checkbox = QCheckBox("Shared axes")
        self.shared_checkbox.setToolTip(
            "One set of c/z/t sliders drives all stacks (Cmd+Shift+G).\n"
            "Stacks with no image at a position show black."
        )
        self.shared_checkbox.setFocusPolicy(Qt.NoFocus)
        self.shared_checkbox.toggled.connect(self._apply_shared_mode)
        controls.addWidget(self.shared_checkbox)
        self.shared_channels_checkbox = QCheckBox("Shared channels")
        self.shared_channels_checkbox.setToolTip(
            "Sync Composite mode and channel visibility across all stacks."
        )
        self.shared_channels_checkbox.setFocusPolicy(Qt.NoFocus)
        self.shared_channels_checkbox.toggled.connect(self._on_shared_channels_toggled)
        controls.addWidget(self.shared_channels_checkbox)
        self.shared_view_checkbox = QCheckBox("Shared view")
        self.shared_view_checkbox.setToolTip(
            "Link pan/zoom across all tiles: zooming into a region shows the\n"
            "same region on every stack."
        )
        self.shared_view_checkbox.setFocusPolicy(Qt.NoFocus)
        self.shared_view_checkbox.toggled.connect(self._apply_shared_view)
        controls.addWidget(self.shared_view_checkbox)
        self.proj_method = "Max"
        self.mip_checkbox = QCheckBox(self._proj_label())
        self.mip_checkbox.setToolTip(viewer.projection_tooltip(self.proj_method, True))
        self.mip_checkbox.setFocusPolicy(Qt.NoFocus)
        self.mip_checkbox.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mip_checkbox.customContextMenuRequested.connect(self._show_proj_menu)
        self.mip_checkbox.toggled.connect(self._apply_mip_all)
        controls.addWidget(self.mip_checkbox)
        self.minimal_checkbox = QCheckBox("Minimalist")
        self.minimal_checkbox.setToolTip(
            "Maximal visualization efficiency: shared axes, only name + info\n"
            "above each image, no per-tile buttons, tiles packed tightly."
        )
        self.minimal_checkbox.setFocusPolicy(Qt.NoFocus)
        self.minimal_checkbox.toggled.connect(self._apply_minimal)
        controls.addWidget(self.minimal_checkbox)
        # Appears only once a tile is flagged (F), so it costs no space before.
        self.flag_checkbox = QCheckBox("★ only")
        self.flag_checkbox.setToolTip(
            "Show only flagged tiles.\nPress F on a tile to flag (★) or unflag it."
        )
        self.flag_checkbox.setFocusPolicy(Qt.NoFocus)
        self.flag_checkbox.toggled.connect(self._on_flag_filter)
        self.flag_checkbox.hide()
        controls.addWidget(self.flag_checkbox)
        controls.addStretch(1)
        controls.addWidget(QLabel("Sort:"))
        self.sort_combo = QComboBox()
        self.sort_combo.setFocusPolicy(Qt.NoFocus)
        self.sort_combo.setToolTip(
            "Tile order: Manual (drag headers to rearrange), Name (natural\n"
            "order, new tiles join sorted), Brightness (brightest first at the\n"
            "current position — reselect to re-sort)."
        )
        for label, mode in (("Manual", "manual"), ("Name", "name"), ("Brightness", "bright")):
            self.sort_combo.addItem(label, mode)
        # `activated` (not currentIndexChanged) so reselecting Brightness re-sorts.
        self.sort_combo.activated.connect(self._apply_sort)
        controls.addWidget(self.sort_combo)
        controls.addWidget(QLabel("Grid:"))
        self.grid_combo = QComboBox()
        self.grid_combo.setFocusPolicy(Qt.NoFocus)
        for label, cols in (
            ("Auto", None),
            ("1 column", 1),
            ("2 columns", 2),
            ("3 columns", 3),
            ("4 columns", 4),
            ("One row", "row"),
        ):
            self.grid_combo.addItem(label, cols)
        self.grid_combo.currentIndexChanged.connect(lambda *_: self._relayout())
        controls.addWidget(self.grid_combo)
        layout.addLayout(controls)
        self.shared_bars_box = QWidget()
        self.shared_bars_layout = QVBoxLayout(self.shared_bars_box)
        self.shared_bars_layout.setContentsMargins(0, 0, 0, 0)
        self.shared_bars_layout.setSpacing(2)
        self.shared_bars: dict[str, DimBar] = {}
        self.shared_channel_boxes: list[QCheckBox] = []
        self._syncing_shared_boxes = False
        layout.addWidget(self.shared_bars_box)
        self.shared_bars_box.hide()
        self.setCentralWidget(central)

        build_menus(self, lambda: self.active_pane)
        screen = self.screen().availableGeometry()
        self.resize(int(screen.width() * 0.85), int(screen.height() * 0.85))

        # Restore persisted mode preferences.
        from . import settings as app_settings

        s = app_settings.settings()
        self.shared_checkbox.setChecked(s.value("ws/sharedAxes", False, type=bool))
        self.shared_channels_checkbox.setChecked(s.value("ws/sharedChannels", False, type=bool))
        self.shared_view_checkbox.setChecked(s.value("ws/sharedView", False, type=bool))
        self.grid_combo.setCurrentIndex(s.value("ws/grid", 0, type=int))
        self.minimal_checkbox.setChecked(s.value("ws/minimalist", False, type=bool))
        for key, box in (
            ("ws/sharedAxes", self.shared_checkbox),
            ("ws/sharedChannels", self.shared_channels_checkbox),
            ("ws/sharedView", self.shared_view_checkbox),
            ("ws/minimalist", self.minimal_checkbox),
        ):
            box.toggled.connect(lambda on, k=key: app_settings.settings().setValue(k, on))
        self.grid_combo.currentIndexChanged.connect(
            lambda i: app_settings.settings().setValue("ws/grid", i)
        )

    # ---- pane management ----------------------------------------------

    def add_pane(self, pane: StackPane):
        self.add_panes([pane])

    def add_panes(self, panes: list[StackPane]):
        """Add many panes with a single relayout (adding one at a time is O(n²))."""
        channel_source = self.active_pane if self.panes else None
        for pane in panes:
            self.panes.append(pane)
            pane.set_tiled(True)
            pane.float_requested.connect(float_pane)
            pane.close_requested.connect(self.close_pane)
            pane.activated.connect(self._on_pane_activated)
            pane.channels_changed.connect(self._on_pane_channels_changed)
            pane.flag_toggled.connect(self._on_flag_toggled)
            pane.solo_requested.connect(self.toggle_solo)
        self.solo_pane = None  # a joining pane always becomes visible
        if self.sort_combo.currentData() == "name":
            self.panes.sort(key=lambda p: _natural_key(p.stack.name))
        self._update_flag_ui()
        self._relayout()
        if panes:
            self._set_active(panes[-1])
        self._update_title()
        self._apply_shared_mode()
        self._apply_shared_view()
        if self.mip_checkbox.isChecked():
            for pane in panes:
                if pane.mip_box is not None:
                    pane.set_proj_method(self.proj_method)
        if self.minimal_checkbox.isChecked():
            for pane in panes:
                pane.set_minimal(True)
        # New panes adopt the existing shared channel state, not the reverse.
        if self.shared_channels_checkbox.isChecked() and channel_source is not None:
            self._propagate_channels(channel_source)

    def take_pane(self, pane: StackPane):
        """Detach a pane (still alive) from the grid."""
        self._detach_pane(pane)
        self._after_detach()

    def _detach_pane(self, pane: StackPane):
        """Unhook one pane; the grid is left stale until _after_detach()."""
        self.panes.remove(pane)
        for signal, slot in (
            (pane.float_requested, float_pane),
            (pane.close_requested, self.close_pane),
            (pane.activated, self._on_pane_activated),
            (pane.channels_changed, self._on_pane_channels_changed),
            (pane.flag_toggled, self._on_flag_toggled),
            (pane.solo_requested, self.toggle_solo),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                pass
        pane.setParent(None)
        pane.set_tiled(False)
        pane.clear_shared()
        pane.viewbox.setXLink(None)
        pane.viewbox.setYLink(None)

    def _after_detach(self):
        """One rebuild for any number of removals (relayout is O(panes))."""
        if self.active_pane not in self.panes:
            self._set_active(self.panes[0] if self.panes else None)
        self._relayout()
        self._update_title()
        self._apply_shared_mode()
        self._apply_shared_view()

    def move_pane(self, source: StackPane, target: StackPane, after: bool):
        """Reorder the grid: insert source before/after target."""
        if source not in self.panes or target not in self.panes or source is target:
            return
        self.panes.remove(source)
        index = self.panes.index(target) + (1 if after else 0)
        self.panes.insert(index, source)
        if self.sort_combo.currentData() == "name":
            self.sort_combo.setCurrentIndex(0)  # a manual drag ends name order
        self._relayout()

    def close_pane(self, pane: StackPane):
        self.close_panes([pane])

    def close_panes(self, panes: list[StackPane]):
        """Close many tiles with a single grid rebuild."""
        for pane in [p for p in panes if p in self.panes]:
            self._detach_pane(pane)
            pane.unregister()
            pane.deleteLater()
        self._after_detach()
        if not self.panes:
            self.hide()

    def _grid_cols(self, n: int) -> int:
        mode = self.grid_combo.currentData()
        if mode is None:
            return math.ceil(math.sqrt(n))
        if mode == "row":
            return n
        return min(mode, n)

    def _displayed_panes(self) -> list[StackPane]:
        """The panes the grid currently shows: solo > flag filter > all."""
        if self.solo_pane is not None:
            return [self.solo_pane]
        if self.flag_checkbox.isChecked():
            flagged = [p for p in self.panes if p.flagged]
            if flagged:
                return flagged
        return list(self.panes)

    def _relayout(self):
        """Rebuild the grid: near-square, rows of horizontal splitters.
        Panes outside the current display set (soloed away / not flagged
        while the ★ filter is on) stay alive but hidden and parentless."""
        if self.solo_pane is not None and self.solo_pane not in self.panes:
            self.solo_pane = None
        for pane in self.panes:
            pane.setParent(None)
        while self.row_container.count():
            self.row_container.widget(0).setParent(None)

        shown = self._displayed_panes()
        hidden = [p for p in self.panes if p not in shown]
        n = len(shown)
        if n == 0:
            return
        cols = self._grid_cols(n)
        rows = math.ceil(n / cols)
        handle = 2 if self.minimal_checkbox.isChecked() else 5
        self.row_container.setHandleWidth(handle)
        for r in range(rows):
            row = QSplitter(Qt.Horizontal)
            row.setChildrenCollapsible(False)
            row.setHandleWidth(handle)
            for pane in shown[r * cols : (r + 1) * cols]:
                row.addWidget(pane)
                pane.show()  # may carry an explicit hide from a prior filter
            row.setSizes([10000 // max(row.count(), 1)] * row.count())
            self.row_container.addWidget(row)
        self.row_container.setSizes([10000 // rows] * rows)
        for pane in hidden:
            pane.hide()

    # ---- solo (Enter / header double-click) ----------------------------

    def toggle_solo(self, pane: StackPane):
        self.solo_pane = None if self.solo_pane is pane else pane
        self._relayout()
        self._ensure_active_displayed()

    def _ensure_active_displayed(self):
        shown = self._displayed_panes()
        if shown and self.active_pane not in shown:
            self._set_active(shown[0])
        elif self.active_pane is not None:
            self.active_pane.setFocus()

    # ---- flagging & the ★ filter ---------------------------------------

    def _on_flag_toggled(self, _pane: StackPane):
        self._update_flag_ui()
        if self.flag_checkbox.isChecked():
            self._relayout()
            self._ensure_active_displayed()

    def _on_flag_filter(self, *_):
        self._relayout()
        self._ensure_active_displayed()

    def _update_flag_ui(self):
        any_flagged = any(p.flagged for p in self.panes)
        if not any_flagged and self.flag_checkbox.isChecked():
            self.flag_checkbox.setChecked(False)  # triggers the relayout
        self.flag_checkbox.setVisible(any_flagged)

    # ---- sorting -------------------------------------------------------

    def _apply_sort(self, *_):
        mode = self.sort_combo.currentData()
        if mode == "name":
            self.panes.sort(key=lambda p: _natural_key(p.stack.name))
        elif mode == "bright":
            self.panes.sort(key=lambda p: -p.mean_intensity())
        self._relayout()

    # ---- shared axes ---------------------------------------------------

    def _apply_shared_mode(self, *_):
        on = self.shared_checkbox.isChecked() and bool(self.panes)
        self._rebuild_shared_bars()
        for pane in self.panes:
            if on:
                pane.shared_controller = self
                pane.set_bars_visible(False)
            else:
                pane.clear_shared()
        if on:
            self._on_shared_changed()
        from . import control_panel

        control_panel.refresh_state()

    def _rebuild_shared_bars(self):
        old = {letter: bar.value() for letter, bar in self.shared_bars.items()}
        while self.shared_bars_layout.count():
            item = self.shared_bars_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.shared_bars = {}
        self.shared_channel_boxes = []
        on = self.shared_checkbox.isChecked() and bool(self.panes)
        if not on:
            self.shared_bars_box.hide()
            return
        counts = {
            "c": max(p.stack.n_channels for p in self.panes),
            "z": max(p.stack.n_slices for p in self.panes),
            "t": max(p.stack.n_frames for p in self.panes),
        }
        for letter in ("c", "z", "t"):
            if counts[letter] > 1:
                bar = DimBar(letter, counts[letter], playable=letter in "zt")
                bar.set_value_silent(min(old.get(letter, 0), counts[letter] - 1))
                bar.changed.connect(self._on_shared_changed)
                if letter == "c":
                    # Like the per-pane c row: numbered visibility boxes on the
                    # right, shown when Shared channels makes them global.
                    self.shared_bars_layout.addWidget(self._build_shared_c_row(bar, counts["c"]))
                else:
                    self.shared_bars_layout.addWidget(bar)
                self.shared_bars[letter] = bar
        self._update_shared_channel_boxes()
        self.shared_bars_box.setVisible(bool(self.shared_bars))

    def _build_shared_c_row(self, bar: DimBar, count: int) -> QWidget:
        from .bc_panel import _ui_color

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 8, 0)
        row_layout.setSpacing(4)
        row_layout.addWidget(bar, 1)
        source = self.active_pane or self.panes[0]
        for ci in range(count):
            box = QCheckBox(str(ci + 1))
            box.setFocusPolicy(Qt.NoFocus)
            box.setToolTip(f"Show/hide channel {ci + 1} in all stacks\n(shortcut: {ci + 1})")
            if ci < source.stack.n_channels:
                r, g, b = _ui_color(source.stack, ci)
                box.setStyleSheet(f"color: rgb({r},{g},{b}); font-weight: bold;")
            box.setChecked(
                source.visible_channels[ci] if ci < len(source.visible_channels) else True
            )
            box.toggled.connect(lambda on, i=ci: self._on_shared_channel_box(i, on))
            row_layout.addWidget(box)
            self.shared_channel_boxes.append(box)
        return row

    def _on_shared_channel_box(self, i: int, on: bool):
        if self._syncing_shared_boxes:
            return
        for pane in self.panes:
            if i < pane.stack.n_channels:
                pane.set_channel_visible(i, on)
                break  # shared-channels propagation syncs the rest

    def _update_shared_channel_boxes(self):
        """The global boxes only make sense while Shared channels is on."""
        visible = self.shared_channels_checkbox.isChecked()
        for box in self.shared_channel_boxes:
            box.setVisible(visible)

    def _sync_shared_channel_boxes(self, source: StackPane | None = None):
        if not self.shared_channel_boxes:
            return
        source = source or self.active_pane or (self.panes[0] if self.panes else None)
        if source is None:
            return
        self._syncing_shared_boxes = True
        try:
            for i, box in enumerate(self.shared_channel_boxes):
                if i < len(source.visible_channels):
                    box.setChecked(source.visible_channels[i])
        finally:
            self._syncing_shared_boxes = False

    def shared_position(self) -> tuple[int, int, int]:
        get = lambda k: self.shared_bars[k].value() if k in self.shared_bars else 0
        return get("t"), get("z"), get("c")

    def _on_shared_changed(self, *_):
        t, z, c = self.shared_position()
        for pane in self.panes:
            pane.set_shared_position(t, z, c)

    def shared_step(self, letter: str, delta: int):
        if letter in self.shared_bars:
            self.shared_bars[letter].step(delta)

    def shared_set(self, letter: str, value: int):
        if letter in self.shared_bars:
            self.shared_bars[letter].set_value(value)

    def toggle_time_playback(self):
        bar = self.shared_bars.get("t") or self.shared_bars.get("z")
        if bar is not None:
            bar.toggle_playback()

    # ---- project all ---------------------------------------------------

    def _proj_label(self) -> str:
        return f"{stack_io.PROJECTION_ABBREV[self.proj_method]} all \u25be"

    def _show_proj_menu(self, pos):
        menu = viewer.projection_menu(
            self.mip_checkbox, self.proj_method, self.set_proj_method
        )
        menu.exec(self.mip_checkbox.mapToGlobal(pos))

    def set_proj_method(self, method: str, enable: bool = True):
        """Pick the projection every tile uses; picking one also turns it on."""
        if method not in stack_io.PROJECTION_METHODS:
            return
        self.proj_method = method
        self.mip_checkbox.setText(self._proj_label())
        self.mip_checkbox.setToolTip(viewer.projection_tooltip(method, True))
        if enable and not self.mip_checkbox.isChecked():
            self.mip_checkbox.setChecked(True)  # toggled -> _apply_mip_all
        elif self.mip_checkbox.isChecked():
            self._apply_mip_all(True)

    def _apply_mip_all(self, on: bool):
        for pane in self.panes:
            if pane.mip_box is None:
                continue
            if on:
                pane.set_proj_method(self.proj_method)
            else:
                pane.mip_box.setChecked(False)

    # ---- minimalist mode -----------------------------------------------

    def _apply_minimal(self, on: bool):
        if on:
            self.shared_checkbox.setChecked(True)
            for pane in self.panes:
                pane.lock_button.setChecked(False)  # lock button is hidden in minimal
        for pane in self.panes:
            pane.set_minimal(on)
        handle = 2 if on else 5
        self.row_container.setHandleWidth(handle)
        for i in range(self.row_container.count()):
            widget = self.row_container.widget(i)
            if isinstance(widget, QSplitter):
                widget.setHandleWidth(handle)

    # ---- shared view (linked pan/zoom) ---------------------------------

    def _apply_shared_view(self, *_):
        on = self.shared_view_checkbox.isChecked() and len(self.panes) > 1
        leader = self.panes[0].viewbox if self.panes else None
        for pane in self.panes:
            target = leader if (on and pane.viewbox is not leader) else None
            pane.viewbox.setXLink(target)
            pane.viewbox.setYLink(target)

    # ---- shared channels -----------------------------------------------

    def _on_shared_channels_toggled(self, on: bool):
        if on:
            source = self.active_pane or (self.panes[0] if self.panes else None)
            if source is not None:
                self._propagate_channels(source)
        self._update_shared_channel_boxes()

    def _on_pane_channels_changed(self, pane: StackPane):
        if self.shared_channels_checkbox.isChecked():
            self._propagate_channels(pane)

    def _propagate_channels(self, source: StackPane):
        if getattr(self, "_propagating_channels", False):
            return
        self._propagating_channels = True
        try:
            composite_on, visible = source.channel_state()
            for pane in self.panes:
                if pane is not source:
                    pane.set_channel_state(composite_on, visible)
        finally:
            self._propagating_channels = False
        self._sync_shared_channel_boxes(source)

    # ---- grid montage export -------------------------------------------

    def _montage_image(self, panes, scale=1, labels=True, t=None, z=None):
        """One montage frame: the given panes in grid order, each rendered at
        full resolution with its current contrast/channels/projection, labeled and
        letterboxed into equal cells. t/z override the position per axis."""
        from PIL import Image, ImageDraw, ImageFont

        cell_w = max(p.stack.shape_yx[1] for p in panes) // scale
        cell_h = max(p.stack.shape_yx[0] for p in panes) // scale
        label_h = max(16, cell_h // 16) if labels else 0
        cols = self._grid_cols(len(panes))
        rows = math.ceil(len(panes) / cols)
        canvas = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.load_default(size=max(11, int(label_h * 0.7)))
        except TypeError:  # Pillow < 10 has no sized default font
            font = ImageFont.load_default()
        for i, pane in enumerate(panes):
            cx = (i % cols) * cell_w
            cy = (i // cols) * (cell_h + label_h)
            if labels:
                draw.text((cx + 6, cy + 2), pane.stack.name, fill=(215, 215, 215), font=font)
            s = pane.stack
            if t is not None and t >= s.n_frames:
                continue  # black cell, like shared-axes out-of-range tiles
            if z is not None and not pane._mip_on() and z >= s.n_slices:
                continue
            tile = Image.fromarray(pane._full_res_rgb(t=t, z=z))
            if scale > 1:
                tile = tile.resize(
                    (max(tile.width // scale, 1), max(tile.height // scale, 1)), Image.LANCZOS
                )
            canvas.paste(
                tile, (cx + (cell_w - tile.width) // 2, cy + label_h + (cell_h - tile.height) // 2)
            )
        return canvas

    def _export_montage(self):
        from PIL import Image

        from . import settings as app_settings
        from .viewer import _show_status

        panes = self._displayed_panes()
        if not panes:
            return
        dialog = MontageDialog(self, panes)
        if dialog.exec() != QDialog.Accepted:
            return
        kind, axis, fps, scale, labels = dialog.values()
        suffix = "png" if kind == "png" else "gif"
        default = f"montage.{suffix}" if axis is None else f"montage_{axis}.{suffix}"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export grid montage",
            str(Path(app_settings.last_dir() or Path.home()) / default),
            "PNG (*.png)" if kind == "png" else "GIF (*.gif)",
        )
        if not path:
            return
        app_settings.set_last_dir(str(Path(path).parent))
        if kind == "png":
            self._montage_image(panes, scale, labels).save(path)
            _show_status(f"Exported montage of {len(panes)} stacks to {path}", 5000)
            return
        count = max(
            (p.stack.n_frames if axis == "T" else p.stack.n_slices) for p in panes
        )
        progress = QProgressDialog(f"Rendering {count} montage frames…", "Cancel", 0, count, self)
        progress.setWindowModality(Qt.WindowModal)
        frames = []
        for i in range(count):
            progress.setValue(i)
            QApplication.processEvents()
            if progress.wasCanceled():
                return
            frame = self._montage_image(
                panes, scale, labels,
                t=i if axis == "T" else None,
                z=i if axis == "Z" else None,
            )
            # Quantize per frame immediately: a full-RGB frame list for 48
            # tiles over a long t range would not fit in RAM.
            frames.append(frame.convert("P", palette=Image.ADAPTIVE))
        progress.setValue(count)
        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            duration=max(int(1000 / fps), 20),
            loop=0,
        )
        _show_status(f"Exported {count} montage frames to {path}", 5000)

    # ---- active pane ---------------------------------------------------

    def _on_pane_activated(self, pane: StackPane):
        self._set_active(pane)

    def _set_active(self, pane: StackPane | None):
        self.active_pane = pane
        for p in self.panes:
            p.set_active_style(p is pane)
        if pane is not None:
            pane.setFocus()

    def _update_title(self):
        n = len(self.panes)
        self.setWindowTitle(f"TIFF Visualizer — {n} stack{'s' if n != 1 else ''}")

    # ---- events --------------------------------------------------------

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and self.active_pane is not None:
            self.toggle_solo(self.solo_pane or self.active_pane)
            return
        if ev.key() == Qt.Key_Escape and self.solo_pane is not None:
            self.toggle_solo(self.solo_pane)
            return
        if self.active_pane is None or not self.active_pane.handle_key(ev):
            super().keyPressEvent(ev)

    def closeEvent(self, ev):
        # Closing the workspace is non-destructive: panes split back to windows.
        if self.panes:
            ev.ignore()
            split_all()
        else:
            super().closeEvent(ev)
