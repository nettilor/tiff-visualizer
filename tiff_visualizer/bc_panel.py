"""Brightness & Contrast controls.

Three faces of the same controls (`BCControls`):
- `BCPanel` — the shared floating window (Cmd+Shift+C): follows the active
  pane, can be pinned, and fuses to a pane when dragged onto its left/right
  edge.
- `PaneBCDock` — a per-pane copy embedded as a column on the pane's left or
  right side (spawned by the pane's B&C header button or by drag-fusing).

All instances targeting the same pane stay in sync automatically: ranges live
on the stack, and every change refreshes via the pane's position_changed.
"""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .stack_io import auto_range, full_range

_panel: "BCPanel | None" = None


def show_bc_panel(target) -> "BCPanel":
    global _panel
    if _panel is None:
        _panel = BCPanel()
    _panel.set_target(target)
    _panel.show()
    _panel.raise_()
    return _panel


# ---- contrast undo (Cmd+Z) -------------------------------------------
# Each entry restores one operation: a list of (stack, channel, old_lo, old_hi).
_undo_stack: list[list[tuple]] = []
_MAX_UNDO = 50
_last_coalesce_key = None
_last_push_time = 0.0


def push_range_undo(entries: list[tuple], coalesce_key=None):
    """Record the pre-change ranges. Rapid consecutive edits of the same
    stack+channel (slider drags, spinbox steps) coalesce into one undo step."""
    global _last_coalesce_key, _last_push_time
    now = time.monotonic()
    if (
        coalesce_key is not None
        and coalesce_key == _last_coalesce_key
        and now - _last_push_time < 2.0
        and _undo_stack
    ):
        _last_push_time = now
        return  # keep the gesture's original 'before' snapshot
    _undo_stack.append(list(entries))
    del _undo_stack[:-_MAX_UNDO]
    _last_coalesce_key = coalesce_key
    _last_push_time = now


def undo_last_range_change():
    global _last_coalesce_key
    if not _undo_stack:
        return
    _last_coalesce_key = None
    entry = _undo_stack.pop()
    touched = set()
    for stack, c, lo, hi in entry:
        stack.ranges[c] = (lo, hi)
        stack.version += 1
        touched.add(id(stack))
    from .viewer import _all_panes

    for pane in _all_panes:
        if id(pane.stack) in touched:
            pane.refresh()


def _ui_color(stack, c: int) -> tuple[int, int, int]:
    """Channel LUT color, nudged to gray when too dark/bright to read in the UI."""
    r, g, b = stack.channel_color(c)
    if r + g + b < 60 or (r, g, b) == (255, 255, 255):
        return 160, 160, 160
    return r, g, b


class BCControls(QWidget):
    """The B&C guts: channel rows, histogram with min/max region, Auto/Reset."""

    target_lost = Signal()

    def __init__(self):
        super().__init__()
        self._target = None
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.channels_box = QWidget()
        self.channels_layout = QGridLayout(self.channels_box)
        self.channels_layout.setContentsMargins(0, 0, 0, 0)
        self.channels_layout.setSpacing(2)
        layout.addWidget(self.channels_box)
        self.channel_group = QButtonGroup(self)
        self.channel_group.idClicked.connect(self._on_channel_selected)

        self.hist_plot = pg.PlotWidget()
        self.hist_plot.setFixedHeight(120)
        self.hist_plot.plotItem.setMouseEnabled(False, False)
        self.hist_plot.plotItem.hideButtons()
        self.hist_plot.plotItem.setMenuEnabled(False)
        self.hist_plot.plotItem.hideAxis("left")
        self.hist_curve = pg.PlotCurveItem()
        self.hist_plot.addItem(self.hist_curve)
        self.region = pg.LinearRegionItem()
        self.region.setZValue(10)
        self.hist_plot.addItem(self.region)
        layout.addWidget(self.hist_plot)

        spins = QHBoxLayout()
        self.min_spin = QDoubleSpinBox()
        self.max_spin = QDoubleSpinBox()
        for label, spin in (("Min", self.min_spin), ("Max", self.max_spin)):
            spin.setKeyboardTracking(False)
            spins.addWidget(QLabel(label))
            spins.addWidget(spin, 1)
        layout.addLayout(spins)

        buttons = QHBoxLayout()
        self.auto_button = QPushButton("Auto")
        self.reset_button = QPushButton("Reset")
        buttons.addWidget(self.auto_button)
        buttons.addWidget(self.reset_button)
        layout.addLayout(buttons)
        self.apply_all_button = QPushButton("Apply to all")
        self.apply_all_button.setToolTip(
            "Apply this channel's min/max to the same channel of every open stack\n(Cmd+Z undoes)"
        )
        layout.addWidget(self.apply_all_button)

        self.region.sigRegionChanged.connect(self._on_region_changed)
        self.min_spin.valueChanged.connect(self._on_spins_changed)
        self.max_spin.valueChanged.connect(self._on_spins_changed)
        self.auto_button.clicked.connect(self._on_auto)
        self.reset_button.clicked.connect(self._on_reset)
        self.apply_all_button.clicked.connect(self._on_apply_all)

    # ---- targeting -----------------------------------------------------

    @property
    def target(self):
        return self._target

    def set_target(self, pane):
        if self._target is pane:
            return
        if self._target is not None:
            try:
                self._target.position_changed.disconnect(self._on_position_changed)
                self._target.destroyed.disconnect(self._on_target_destroyed)
            except RuntimeError:
                pass
        self._target = pane
        if pane is None:
            self.channels_box.hide()
            return
        pane.position_changed.connect(self._on_position_changed)
        pane.destroyed.connect(self._on_target_destroyed)
        self._rebuild_channel_rows()
        self._configure_spins()
        self.refresh()

    def _on_target_destroyed(self, *_):
        self._target = None
        self.target_lost.emit()

    # ---- channel rows --------------------------------------------------

    def _rebuild_channel_rows(self):
        while self.channels_layout.count():
            item = self.channels_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for button in self.channel_group.buttons():
            self.channel_group.removeButton(button)

        stack = self._target.stack
        if stack.n_channels <= 1:
            self.channels_box.hide()
            return
        self.channels_box.show()
        for c in range(stack.n_channels):
            r, g, b = _ui_color(stack, c)
            radio = QRadioButton(f"Ch {c + 1}")
            radio.setStyleSheet(f"color: rgb({r},{g},{b}); font-weight: bold;")
            radio.setToolTip(f"Select channel {c + 1} to adjust")
            self.channel_group.addButton(radio, c)
            visible = QCheckBox("visible")
            visible.setToolTip(f"Show/hide channel {c + 1} in composite\n(shortcut: {c + 1})")
            visible.setChecked(self._target.visible_channels[c])
            visible.toggled.connect(lambda on, c=c: self._on_visibility_toggled(c, on))
            self.channels_layout.addWidget(radio, c, 0)
            self.channels_layout.addWidget(visible, c, 1, alignment=Qt.AlignRight)

    def _on_channel_selected(self, c: int):
        if not self._updating and self._target is not None:
            self._target.set_channel(c)

    def _on_visibility_toggled(self, c: int, on: bool):
        if self._target is not None:
            self._target.set_channel_visible(c, on)

    # ---- range editing -------------------------------------------------

    def _configure_spins(self):
        """Limit the region and spinboxes to the image's value bounds:
        dtype range for integer images, unbounded for float."""
        stack = self._target.stack
        if np.issubdtype(stack.dtype, np.integer):
            info = np.iinfo(stack.dtype)
            lo, hi = float(info.min), float(info.max)
            self.region.setBounds((lo, hi))
            for spin in (self.min_spin, self.max_spin):
                spin.setDecimals(0)
                spin.setRange(lo, hi)
                spin.setSingleStep(1)
        else:
            self.region.setBounds((None, None))
            for spin in (self.min_spin, self.max_spin):
                spin.setDecimals(2)
                spin.setRange(-1e12, 1e12)
                spin.setSingleStep(1.0)

    def refresh(self):
        """Sync histogram, region, spins and channel selection from the target."""
        if self._target is None:
            return
        self._updating = True
        try:
            t, z, c = self._target.position()
            stack = self._target.stack
            if stack.n_channels > 1:
                button = self.channel_group.button(c)
                if button is not None:
                    button.setChecked(True)
                for ci in range(stack.n_channels):
                    item = self.channels_layout.itemAtPosition(ci, 1)
                    if item is not None:
                        item.widget().setChecked(self._target.visible_channels[ci])
            plane = np.asarray(stack.plane(t, z, c))
            counts, edges = np.histogram(plane, bins=256)
            r, g, b = _ui_color(stack, c)
            self.hist_curve.setData(
                edges,
                counts,
                stepMode="center",
                fillLevel=0,
                brush=pg.mkBrush(r, g, b, 120),
                pen=pg.mkPen(r, g, b),
            )
            self.region.setBrush(pg.mkBrush(r, g, b, 40))

            lo, hi = stack.ranges[c]
            self.region.setRegion((lo, hi))
            self.min_spin.setValue(lo)
            self.max_spin.setValue(hi)
            self.hist_plot.plotItem.setXRange(
                min(float(edges[0]), lo), max(float(edges[-1]), hi), padding=0.02
            )
        finally:
            self._updating = False

    def _apply_range(self, lo: float, hi: float):
        t_, z_, c = self._target.position()
        stack = self._target.stack
        new = (lo, hi if hi > lo else lo + 1)
        old = (float(stack.ranges[c][0]), float(stack.ranges[c][1]))
        if old == new:
            return
        push_range_undo([(stack, c, *old)], coalesce_key=(id(stack), c))
        stack.ranges[c] = new
        stack.version += 1
        self._target.refresh()

    def _on_apply_all(self):
        if self._target is None:
            return
        t_, z_, c = self._target.position()
        lo, hi = self._target.stack.ranges[c]
        from .viewer import _all_panes

        entries = []
        seen_stacks = set()
        for pane in _all_panes:
            stack = pane.stack
            if id(stack) in seen_stacks or c >= stack.n_channels:
                continue
            seen_stacks.add(id(stack))
            entries.append((stack, c, float(stack.ranges[c][0]), float(stack.ranges[c][1])))
            stack.ranges[c] = (lo, hi)
            stack.version += 1
        push_range_undo(entries)  # bulk ops never coalesce
        for pane in _all_panes:
            pane.refresh()

    def _on_region_changed(self):
        if self._updating or self._target is None:
            return
        lo, hi = self.region.getRegion()
        self._updating = True
        self.min_spin.setValue(lo)
        self.max_spin.setValue(hi)
        self._updating = False
        self._apply_range(lo, hi)

    def _on_spins_changed(self):
        if self._updating or self._target is None:
            return
        lo, hi = self.min_spin.value(), self.max_spin.value()
        if hi <= lo:
            hi = lo + 1
        self._updating = True
        self.region.setRegion((lo, hi))
        self._updating = False
        self._apply_range(lo, hi)

    def _on_position_changed(self, *_):
        self.refresh()

    def _on_auto(self):
        if self._target is None:
            return
        t, z, c = self._target.position()
        lo, hi = auto_range(np.asarray(self._target.stack.plane(t, z, c)))
        self._apply_range(lo, hi)
        self.refresh()

    def _on_reset(self):
        if self._target is None:
            return
        t, z, c = self._target.position()
        stack = self._target.stack
        lo, hi = full_range(stack.dtype, np.asarray(stack.plane(t, z, c)))
        self._apply_range(lo, hi)
        self.refresh()


class BCPanel(QWidget):
    """The shared floating B&C window: follows the active pane, pinnable, and
    fuses into a pane when dragged onto its left/right edge."""

    def __init__(self):
        super().__init__(None, Qt.Tool)
        self.setWindowTitle("B&C")
        self.setMinimumWidth(240)
        self._sized_square = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.target_label = QLabel("No stack")
        self.target_label.setStyleSheet("font-weight: bold;")
        self.pin_button = QToolButton()
        self.pin_button.setText("Pin")
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip(
            "Unpinned: the panel follows the active stack window.\n"
            "Pinned: it stays anchored to this stack."
        )
        header.addWidget(self.target_label, 1)
        header.addWidget(self.pin_button)
        layout.addLayout(header)

        self.controls = BCControls()
        self.controls.target_lost.connect(self._on_target_lost)
        layout.addWidget(self.controls)

        QApplication.instance().focusChanged.connect(self._on_focus_changed)

    def set_target(self, pane):
        self.controls.set_target(pane)
        self.target_label.setText(pane.stack.name if pane is not None else "No stack")

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._sized_square:
            self._sized_square = True
            from . import settings as app_settings

            saved = app_settings.settings().value("bc/geometry")
            if saved is not None:
                self.restoreGeometry(saved)
            else:
                # Square by default: the content's natural height sets the width.
                side = max(self.sizeHint().height(), self.minimumWidth())
                self.resize(side, side)

    def hideEvent(self, ev):
        from . import settings as app_settings

        app_settings.settings().setValue("bc/geometry", self.saveGeometry())
        super().hideEvent(ev)

    def _on_focus_changed(self, _old, new):
        if self.pin_button.isChecked() or new is None:
            return
        from .viewer import StackPane

        widget = new
        while widget is not None and not isinstance(widget, StackPane):
            widget = widget.parentWidget()
        if widget is not None and widget is not self.controls.target:
            self.set_target(widget)

    def _on_target_lost(self):
        self.pin_button.setChecked(False)
        from .viewer import _all_panes

        self.set_target(_all_panes[0] if _all_panes else None)


class PaneBCDock(QWidget):
    """A B&C column embedded on the left or right side of one pane."""

    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane
        # Flexible width: prefers ~270 px but yields in narrow grid tiles.
        self.setMinimumWidth(200)
        self.setMaximumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 4)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("B&C")
        title.setStyleSheet("font-weight: bold;")
        swap_button = QToolButton()
        swap_button.setText("⇄")
        swap_button.setToolTip("Move to the other side of the image")
        swap_button.setFocusPolicy(Qt.NoFocus)
        swap_button.clicked.connect(pane.swap_bc_side)
        close_button = QToolButton()
        close_button.setText("✕")
        close_button.setToolTip("Close this B&C panel")
        close_button.setFocusPolicy(Qt.NoFocus)
        close_button.clicked.connect(pane.close_bc_dock)
        header.addWidget(title, 1)
        header.addWidget(swap_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.controls = BCControls()
        self.controls.set_target(pane)
        layout.addWidget(self.controls)
        layout.addStretch(1)
