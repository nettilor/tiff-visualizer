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

import math
import time
import weakref

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
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

from shiboken6 import isValid

from .stack_io import auto_range, full_range, project_block

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
# Each entry restores one operation: a list of (stack ref, channel, old_lo,
# old_hi). Weak references, so undo history never keeps a closed stack's
# data in RAM; steps whose stacks have all closed are skipped.
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
    _undo_stack.append([(weakref.ref(stack), c, lo, hi) for stack, c, lo, hi in entries])
    del _undo_stack[:-_MAX_UNDO]
    _last_coalesce_key = coalesce_key
    _last_push_time = now


def undo_last_range_change():
    """Undo the latest step that still touches an open stack."""
    global _last_coalesce_key
    from .viewer import _all_panes

    _last_coalesce_key = None
    open_stacks = {id(pane.stack) for pane in _all_panes}
    while _undo_stack:
        entry = [(ref(), c, lo, hi) for ref, c, lo, hi in _undo_stack.pop()]
        entry = [e for e in entry if e[0] is not None and id(e[0]) in open_stacks]
        if entry:
            break
    else:
        return
    touched = set()
    for stack, c, lo, hi in entry:
        stack.ranges[c] = (lo, hi)
        stack.version += 1
        touched.add(id(stack))
    for pane in _all_panes:
        if id(pane.stack) in touched:
            pane.refresh()


# Past this many values a projection's histogram samples every nth pixel,
# so B&C stays live during playback of big stacks; Auto/Reset use it all.
_HIST_MAX_VALUES = 16_000_000


def displayed_plane(pane, t: int, z: int, c: int, max_values: int | None = None) -> np.ndarray:
    """Channel c's plane as the pane shows it: its z projection when that is
    on (like measurement()), otherwise slice z. A Sum projection comes back
    per slice — the Mean — since render() scales Sum's display window by the
    slice count, which leaves B&C ranges in single-slice units."""
    stack = pane.stack
    if not pane._mip_on():
        return np.asarray(stack.plane(t, z, c))
    block = stack.data[t, :, c]
    if max_values is not None and block.size > max_values:
        step = math.ceil(math.sqrt(block.size / max_values))
        block = block[:, ::step, ::step]
    method = "Mean" if pane.proj_method == "Sum" else pane.proj_method
    return project_block(np.asarray(block), method)


def _float_precision(scale: float) -> tuple[int, float, float]:
    """(decimals, step, bound) for float min/max boxes showing values of
    about this magnitude: ~4 significant digits, steps of 1%, and a range
    far past the data (a fixed ±1e12 would round 0.0012 to 0.00)."""
    exp = math.floor(math.log10(scale)) if scale > 0 and math.isfinite(scale) else 0
    return min(max(3 - exp, 2), 8), 10.0 ** (exp - 2), 10.0 ** (exp + 6)


def _is_open(pane) -> bool:
    """Still open: a closed pane has left viewer._all_panes, even while its
    window or a reference keeps it alive."""
    from .viewer import _all_panes

    return pane in _all_panes


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
        self._closing: list = []  # closed former targets awaiting Qt's delete
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
        old = self._target
        if old is not None and _is_open(old):
            try:
                old.position_changed.disconnect(self._on_position_changed)
                old.destroyed.disconnect(self._on_target_destroyed)
            except RuntimeError:
                pass
        elif old is not None:
            # A closed pane is on its way out (deleteLater pending): its
            # connections die with it, and it's held until Qt deletes it.
            self._hold_until_deleted(old)
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
        # Only the current target's deletion counts: a former target, whose
        # connections were left to die with it, must not retarget us.
        if self._target is None or _is_open(self._target):
            return
        self._target = None
        self.target_lost.emit()

    def _hold_until_deleted(self, pane):
        """Keep a closed pane's wrapper until Qt has deleted the pane. A
        closed tile is unparented with a deleteLater() pending; dropping its
        last Python reference first would make Python delete it too."""
        if not isValid(pane):
            return
        self._closing.append(pane)
        pane.destroyed.connect(lambda *_: QTimer.singleShot(0, self._forget_deleted))

    def _forget_deleted(self):
        self._closing = [p for p in self._closing if isValid(p)]

    def _target_open(self) -> bool:
        """Whether there is a target that is still open. A closed pane can
        outlive its window, so being alive isn't enough: once it has left
        viewer._all_panes the controls drop it (target_lost) rather than
        keep editing — or applying to all — a stack nobody can see."""
        if self._target is None:
            return False
        if _is_open(self._target):
            return True
        # Not set_target(None): hiding the channel rows inside the focus
        # change a close causes made PySide crash in a later garbage
        # collection. The retarget on target_lost rebuilds them anyway.
        self._hold_until_deleted(self._target)  # its connections die with it
        self._target = None
        self.target_lost.emit()
        return False

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
        dtype range for integer images, unbounded for float (whose boxes
        take their precision from the channel's range in refresh())."""
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

    def _set_float_precision(self, lo: float, hi: float):
        decimals, step, bound = _float_precision(max(abs(lo), abs(hi), abs(hi - lo)))
        for spin in (self.min_spin, self.max_spin):
            spin.setDecimals(decimals)
            spin.setRange(-bound, bound)
            spin.setSingleStep(step)

    def _min_gap(self) -> float:
        """Smallest max − min allowed: 1 for integer images, one box step
        (scaled to the data) for float ones."""
        return self.max_spin.singleStep()

    def refresh(self):
        """Sync histogram, region, spins and channel selection from the target."""
        if not self._target_open():
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
            plane = displayed_plane(self._target, t, z, c, _HIST_MAX_VALUES)
            if not np.issubdtype(plane.dtype, np.integer):
                plane = plane[np.isfinite(plane)]  # NaN/inf would break the bins
            lo, hi = (float(v) for v in stack.ranges[c])
            if not (math.isfinite(lo) and math.isfinite(hi)):
                # Shown only: a NaN range can't be drawn; editing it stores a real one.
                lo, hi = (
                    (float(plane.min()), float(plane.max())) if plane.size else (0.0, 1.0)
                )
            r, g, b = _ui_color(stack, c)
            if plane.size:
                counts, edges = np.histogram(plane, bins=256)
                self.hist_curve.setData(
                    edges,
                    counts,
                    stepMode="center",
                    fillLevel=0,
                    brush=pg.mkBrush(r, g, b, 120),
                    pen=pg.mkPen(r, g, b),
                )
                x_range = (min(float(edges[0]), lo), max(float(edges[-1]), hi))
            else:
                self.hist_curve.setData([], [])
                x_range = (lo, hi)
            self.region.setBrush(pg.mkBrush(r, g, b, 40))

            if not np.issubdtype(stack.dtype, np.integer):
                self._set_float_precision(lo, hi)
            self.region.setRegion((lo, hi))
            self.min_spin.setValue(lo)
            self.max_spin.setValue(hi)
            self.hist_plot.plotItem.setXRange(*x_range, padding=0.02)
        finally:
            self._updating = False

    def _apply_range(self, lo: float, hi: float):
        if not self._target_open() or not (math.isfinite(lo) and math.isfinite(hi)):
            return  # a NaN bound would blank the image and hang the region
        t_, z_, c = self._target.position()
        stack = self._target.stack
        new = (lo, hi if hi > lo else lo + self._min_gap())
        old = (float(stack.ranges[c][0]), float(stack.ranges[c][1]))
        if old == new:
            return
        push_range_undo([(stack, c, *old)], coalesce_key=(id(stack), c))
        stack.ranges[c] = new
        stack.version += 1
        self._target.refresh()

    def _on_apply_all(self):
        if not self._target_open():
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
        if self._updating or not self._target_open():
            return
        # Only the edited box's value is taken: the other one shows the
        # stored bound rounded to its decimals, and writing that back would
        # shift a bound the user never touched.
        t_, z_, c = self._target.position()
        lo, hi = (float(v) for v in self._target.stack.ranges[c])
        edited = self.sender()
        if edited is not self.max_spin or not math.isfinite(lo):
            lo = self.min_spin.value()
        if edited is not self.min_spin or not math.isfinite(hi):
            hi = self.max_spin.value()
        if hi <= lo:
            hi = lo + self._min_gap()
        self._updating = True
        self.region.setRegion((lo, hi))
        self._updating = False
        self._apply_range(lo, hi)

    def _on_position_changed(self, *_):
        self.refresh()

    def _on_auto(self):
        if not self._target_open():
            return
        t, z, c = self._target.position()
        lo, hi = auto_range(displayed_plane(self._target, t, z, c))
        self._apply_range(lo, hi)
        self.refresh()

    def _on_reset(self):
        if not self._target_open():
            return
        t, z, c = self._target.position()
        stack = self._target.stack
        lo, hi = full_range(stack.dtype, displayed_plane(self._target, t, z, c))
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
        self.controls._target_open()  # retargets if its stack closed while hidden
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
        # Closing a window moves focus: the moment to let go of its stack,
        # pinned or not.
        self.controls._target_open()
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
