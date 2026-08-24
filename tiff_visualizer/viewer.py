"""Stack panes and windows.

A StackPane is a self-contained view of one stack (header, image, dimension
bars). It can live in its own floating StackWindow (Fiji-style) or tiled with
other panes inside the shared workspace window (napari-style); combining and
splitting moves the same pane between the two.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

import numpy as np
import pyqtgraph as pg
from collections import OrderedDict

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QDrag, QImage, QKeySequence
from PySide6.QtWidgets import QApplication, QMenu

from . import settings as app_settings
from PySide6.QtWidgets import (
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
    QScrollBar,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import stack_io
from .stack_io import TiffStack, load_stack

# Menu text for the live z-projection methods; the abbreviations in
# parentheses are what the toggle itself shows once a method is picked.
PROJ_MENU_TEXT = {
    "Max": "Max intensity",
    "Min": "Min intensity",
    "Mean": "Average",
    "Median": "Median",
    "Sum": "Sum slices",
}


def projection_menu(parent, current: str, on_pick) -> QMenu:
    """Right-click menu of z-projection methods for a projection toggle."""
    menu = QMenu(parent)
    for method in stack_io.PROJECTION_METHODS:
        action = menu.addAction(
            f"{PROJ_MENU_TEXT[method]} ({stack_io.PROJECTION_ABBREV[method]})"
        )
        action.setCheckable(True)
        action.setChecked(method == current)
        action.triggered.connect(lambda _checked=False, m=method: on_pick(m))
    return menu


def projection_tooltip(method: str, all_stacks: bool = False) -> str:
    scope = "all stacks in the grid" if all_stacks else "the stack"
    return (
        f"{PROJ_MENU_TEXT[method]} projection over z on {scope}\n"
        "Right-click to change the projection type"
    )

pg.setConfigOptions(imageAxisOrder="row-major", background="#000000")

PANE_MIME = "application/x-tiffviz-pane"

_zoom_clamp_active = False

# Registries: floating windows (so they aren't garbage collected) and all live
# panes in creation order (for B&C retargeting when one closes).
_open_windows: set["StackWindow"] = set()
_all_panes: list["StackPane"] = []
_active_pane: "StackPane | None" = None


def active_pane() -> "StackPane | None":
    """The last-focused live pane, falling back to the most recently opened."""
    if _active_pane is not None and _active_pane in _all_panes:
        return _active_pane
    return _all_panes[-1] if _all_panes else None


class StackViewBox(pg.ViewBox):
    """ViewBox where the wheel scrolls the stack (Fiji-style): plain = z,
    Shift = t, Alt/Option = c; Cmd/Ctrl+wheel zooms."""

    wheel_stepped = Signal(str, int)  # (dimension letter, steps)

    def __init__(self):
        super().__init__(lockAspect=True, invertY=True)
        self._wheel_accum = 0
        self._wheel_letter = "z"

    def wheelEvent(self, ev, axis=None):
        mods = ev.modifiers()
        if mods & Qt.ControlModifier:
            super().wheelEvent(ev, axis)
            return
        ev.accept()
        letter = "t" if mods & Qt.ShiftModifier else "c" if mods & Qt.AltModifier else "z"
        if letter != self._wheel_letter:
            self._wheel_letter = letter
            self._wheel_accum = 0
        self._wheel_accum += ev.delta()
        step = int(self._wheel_accum / 120)
        if step:
            self._wheel_accum -= step * 120
            self.wheel_stepped.emit(letter, -step)


class StackGraphicsView(pg.GraphicsView):
    """GraphicsView that turns macOS trackpad pinch gestures into zoom,
    centered on the cursor."""

    def __init__(self, viewbox: StackViewBox):
        super().__init__()
        self._vb = viewbox
        self.setCentralItem(viewbox)

    def viewportEvent(self, ev):
        if (
            ev.type() == QEvent.Type.NativeGesture
            and ev.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            value = ev.value()
            if value and value > -0.9:
                # The gesture event's own position() is unreliable on macOS;
                # the actual mouse cursor is where the user is pointing.
                cursor = self.viewport().mapFromGlobal(QCursor.pos())
                self._pinch_zoom(1.0 / (1.0 + value), cursor)
            return True
        return super().viewportEvent(ev)

    def _pinch_zoom(self, factor: float, viewport_point):
        """Scale the view by factor, keeping the image point under
        viewport_point fixed in place."""
        center = self._vb.mapSceneToView(self.mapToScene(viewport_point))
        self._vb._resetTarget()
        self._vb.scaleBy((factor, factor), center)


class DimBar(QWidget):
    """One Fiji-style dimension scroll bar: letter, scrollbar, 'current/total'.

    playable adds a play/pause button (right-click it for the fps menu)."""

    changed = Signal(int)

    def __init__(self, letter: str, count: int, playable: bool = False):
        super().__init__()
        self.count = count
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)
        if playable:
            self._fps = 10
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._advance)
            self.play_button = QToolButton()
            self.play_button.setText("▶")
            self.play_button.setFixedWidth(24)
            self.play_button.setCheckable(True)
            self.play_button.setFocusPolicy(Qt.NoFocus)
            self.play_button.setToolTip("Play through this dimension\n(right-click for speed)")
            self.play_button.toggled.connect(self._toggle_play)
            self.play_button.setContextMenuPolicy(Qt.CustomContextMenu)
            self.play_button.customContextMenuRequested.connect(self._show_speed_menu)
            layout.addWidget(self.play_button)
        else:
            self.play_button = None
        name = QLabel(letter)
        name.setFixedWidth(12)
        self.bar = QScrollBar(Qt.Horizontal)
        self.bar.setRange(0, count - 1)
        self.bar.setPageStep(1)
        self.bar.setFocusPolicy(Qt.NoFocus)
        self.pos_label = QLabel()
        # Minimum, not fixed: must grow with larger UI text sizes.
        self.pos_label.setMinimumWidth(52)
        self.pos_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(name)
        layout.addWidget(self.bar, 1)
        layout.addWidget(self.pos_label)
        self.bar.valueChanged.connect(self._on_change)
        self._on_change(0)

    def _on_change(self, value: int):
        self.pos_label.setText(f"{value + 1}/{self.count}")
        self.changed.emit(value)

    def value(self) -> int:
        return self.bar.value()

    def set_value(self, value: int):
        self.bar.setValue(value)

    def set_value_silent(self, value: int):
        """Move the bar without emitting `changed` (no refresh cascade)."""
        self.bar.blockSignals(True)
        self.bar.setValue(value)
        self.bar.blockSignals(False)
        self.pos_label.setText(f"{self.bar.value() + 1}/{self.count}")

    def step(self, delta: int):
        self.bar.setValue(self.bar.value() + delta)

    # ---- playback ------------------------------------------------------

    def _toggle_play(self, on: bool):
        self.play_button.setText("⏸" if on else "▶")
        if on:
            self._timer.start(max(int(1000 / self._fps), 10))
        else:
            self._timer.stop()

    def _advance(self):
        self.bar.setValue((self.bar.value() + 1) % self.count)

    def _set_fps(self, fps: int):
        self._fps = fps
        if self._timer.isActive():
            self._timer.start(max(int(1000 / fps), 10))

    def _show_speed_menu(self, pos):
        menu = QMenu(self)
        for fps in (2, 5, 10, 20, 30):
            action = menu.addAction(f"{fps} fps")
            action.setCheckable(True)
            action.setChecked(fps == self._fps)
            action.triggered.connect(lambda _=False, f=fps: self._set_fps(f))
        menu.exec(self.play_button.mapToGlobal(pos))

    def stop_playback(self):
        if self.play_button is not None and self.play_button.isChecked():
            self.play_button.setChecked(False)

    def toggle_playback(self):
        if self.play_button is not None:
            self.play_button.setChecked(not self.play_button.isChecked())

    def hideEvent(self, ev):
        self.stop_playback()
        super().hideEvent(ev)


class StackPane(QWidget):
    """One stack's complete view; portable between a window and the workspace grid."""

    position_changed = Signal(int, int, int)  # (t, z, c)
    channels_changed = Signal(object)  # self, on composite/visibility change
    activated = Signal(object)  # self, on focus-in
    float_requested = Signal(object)  # workspace title-bar buttons
    close_requested = Signal(object)
    flag_toggled = Signal(object)  # self, on flag/unflag (F)
    solo_requested = Signal(object)  # self, on header double-click in the grid

    def __init__(self, stack: TiffStack):
        super().__init__()
        self.stack = stack
        self.visible_channels = [True] * stack.n_channels
        self.flagged = False
        # Set while hidden (soloed-away / filtered tiles skip rendering);
        # the pane re-renders once on its next showEvent.
        self._needs_refresh = False
        # Floating-window geometry, kept across combine/split cycles.
        self.saved_window_geometry = None
        # Set by the workspace in shared-axes mode: steps are forwarded there
        # and _shared_pos may put this pane out of range (rendered black).
        self.shared_controller = None
        self._shared_pos: tuple[int, int, int] | None = None
        self._blank = False
        self.shared_locked = False
        self._plane_cache: OrderedDict = OrderedDict()
        self._prefetch_scheduled = False
        self._render_request = 0  # invalidates in-flight async renders
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(240, 220)
        self.setAcceptDrops(True)  # pane-reorder drags; file drops pass through
        self._drag_start: QPoint | None = None
        self._tiled = False
        self._minimal = False
        # Insertion indicator shown while another tile is dragged over this one.
        self._drop_indicator = QWidget(self)
        self._drop_indicator.setStyleSheet("background: #2f6fd0; border-radius: 2px;")
        self._drop_indicator.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 2)
        layout.setSpacing(2)
        self._layout = layout

        # One compact top row: stack name (tiled mode), slice label + position,
        # and float/close buttons (tiled mode).
        top_row = QHBoxLayout()
        top_row.setContentsMargins(8, 2, 4, 2)
        top_row.setSpacing(8)
        self._top_row = top_row
        self.flag_label = QLabel("★")
        self.flag_label.setStyleSheet("color: #f5c34d;")
        self.flag_label.setToolTip("Flagged (press F to unflag)")
        self.flag_label.hide()
        self.title_label = QLabel(stack.name)
        self.title_label.setMinimumWidth(1)
        self.title_label.hide()
        self.header_label = QLabel()
        self.header_label.setMinimumWidth(1)
        self.header_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.float_button = QToolButton()
        self.float_button.setText("⧉")
        self.float_button.setToolTip("Float this stack in its own window")
        self.float_button.setFocusPolicy(Qt.NoFocus)
        self.float_button.clicked.connect(lambda: self.float_requested.emit(self))
        self.float_button.hide()
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("Close this stack")
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.clicked.connect(lambda: self.close_requested.emit(self))
        self.close_button.hide()
        self.lock_button = QToolButton()
        self.lock_button.setText("🔓")
        self.lock_button.setCheckable(True)
        self.lock_button.setToolTip(
            "Lock this stack out of shared axes:\nit keeps its position while the others scrub"
        )
        self.lock_button.setFocusPolicy(Qt.NoFocus)
        self.lock_button.toggled.connect(self._on_lock_toggled)
        self.lock_button.hide()
        self.bc_button = QToolButton()
        self.bc_button.setText("B&C")
        self.bc_button.setToolTip(
            "Attach a Brightness/Contrast panel to this stack's side\n(click again to close it)"
        )
        self.bc_button.setFocusPolicy(Qt.NoFocus)
        self.bc_button.clicked.connect(self.toggle_bc_dock)
        top_row.addWidget(self.flag_label)
        top_row.addWidget(self.title_label)
        top_row.addWidget(self.header_label, 1)
        top_row.addWidget(self.lock_button)
        top_row.addWidget(self.bc_button)
        top_row.addWidget(self.float_button)
        top_row.addWidget(self.close_button)
        layout.addLayout(top_row)

        # Middle: the image view, with an optional B&C dock on either side.
        self.bc_dock = None
        self.bc_side = "right"
        self._middle = QHBoxLayout()
        self._middle.setContentsMargins(0, 0, 0, 0)
        self._middle.setSpacing(2)
        self.viewbox = StackViewBox()
        self.view = StackGraphicsView(self.viewbox)
        self.view.setFocusProxy(self)  # clicks on the image focus/activate the pane
        self.image_item = pg.ImageItem()
        self.viewbox.addItem(self.image_item)
        self._middle.addWidget(self.view, 1)
        layout.addLayout(self._middle, 1)

        self.bars: dict[str, DimBar] = {}
        self.channel_boxes: list[QCheckBox] = []
        for letter, count in (
            ("c", stack.n_channels),
            ("z", stack.n_slices),
            ("t", stack.n_frames),
        ):
            if count > 1:
                bar = DimBar(letter, count, playable=letter in "zt")
                bar.changed.connect(self.refresh)
                if letter == "c":
                    # The c bar shares its row with per-channel visibility
                    # toggles (numbered, like the B&C window's 'visible' boxes).
                    from .bc_panel import _ui_color

                    row = QHBoxLayout()
                    row.setContentsMargins(0, 0, 8, 0)
                    row.setSpacing(4)
                    row.addWidget(bar, 1)
                    for ci in range(count):
                        box = QCheckBox(str(ci + 1))
                        box.setChecked(True)
                        box.setFocusPolicy(Qt.NoFocus)
                        box.setToolTip(
                            f"Show/hide channel {ci + 1} in composite\n(shortcut: {ci + 1})"
                        )
                        r, g, b = _ui_color(stack, ci)
                        box.setStyleSheet(f"color: rgb({r},{g},{b}); font-weight: bold;")
                        box.toggled.connect(lambda on, i=ci: self._on_channel_box(i, on))
                        row.addWidget(box)
                        self.channel_boxes.append(box)
                    layout.addLayout(row)
                else:
                    layout.addWidget(bar)
                self.bars[letter] = bar

        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 0, 8, 0)
        self.probe_label = QLabel()
        bottom.addWidget(self.probe_label, 1)
        self.mip_box = None
        self.proj_method = "Max"
        if stack.n_slices > 1:
            # Label doubles as the readout of which projection is live.
            self.mip_box = QCheckBox(self._proj_label())
            self.mip_box.setToolTip(projection_tooltip(self.proj_method))
            self.mip_box.setFocusPolicy(Qt.NoFocus)
            self.mip_box.setContextMenuPolicy(Qt.CustomContextMenu)
            self.mip_box.customContextMenuRequested.connect(self._show_proj_menu)
            self.mip_box.toggled.connect(self._on_mip_toggled)
            bottom.addWidget(self.mip_box)
        self.composite_box = None
        if stack.n_channels > 1:
            self.composite_box = QCheckBox("Composite")
            self.composite_box.setChecked(stack.composite)
            self.composite_box.setFocusPolicy(Qt.NoFocus)
            self.composite_box.toggled.connect(self._on_composite_toggled)
            bottom.addWidget(self.composite_box)
        layout.addLayout(bottom)

        self.viewbox.wheel_stepped.connect(self._step)
        self.view.scene().sigMouseMoved.connect(self._on_mouse_moved)

        self._last_stride = 1
        self._clamping_zoom = False
        from .preload import maybe_preload

        maybe_preload(stack)
        self.refresh()
        self.viewbox.autoRange(padding=0)
        self.viewbox.sigRangeChanged.connect(self._enforce_min_zoom)
        self.viewbox.sigRangeChanged.connect(self._maybe_restride)
        _all_panes.append(self)

    def set_tiled(self, tiled: bool):
        self._tiled = tiled
        if not tiled:
            self.lock_button.setChecked(False)
            self._minimal = False
        # Tiles accept a smaller minimum than floating windows; past that the
        # workspace grid scrolls rather than forcing the window off-screen.
        self.setMinimumSize(*((170, 150) if tiled else (240, 220)))
        # In the grid the header strip is a drag handle for rearranging tiles,
        # so text selection is disabled there; floating windows keep it.
        self.header_label.setTextInteractionFlags(
            Qt.NoTextInteraction if tiled else Qt.TextSelectableByMouse
        )
        cursor = Qt.OpenHandCursor if tiled else Qt.ArrowCursor
        self.title_label.setCursor(cursor)
        self.header_label.setCursor(cursor)
        self._apply_chrome()

    def set_minimal(self, on: bool):
        """Minimalist grid mode: only name + info above the image, no buttons."""
        self._minimal = on
        self._apply_chrome()

    def _apply_chrome(self):
        tiled, minimal = self._tiled, self._minimal
        self.title_label.setVisible(tiled)
        for widget in (self.float_button, self.close_button, self.lock_button):
            widget.setVisible(tiled and not minimal)
        self.bc_button.setVisible(not minimal)
        self.probe_label.setVisible(not minimal)
        if self.mip_box is not None:
            self.mip_box.setVisible(not minimal)
        if self.composite_box is not None:
            self.composite_box.setVisible(not minimal)
        self._layout.setSpacing(0 if minimal else 2)
        self._layout.setContentsMargins(0, 0, 0, 0 if minimal else 2)
        self._top_row.setContentsMargins(
            4 if minimal else 8, 0 if minimal else 2, 4, 0 if minimal else 2
        )

    # ---- grid rearranging (drag the header strip) ----------------------

    def _header_strip_contains(self, pos: QPoint) -> bool:
        return pos.y() <= self.header_label.geometry().bottom() + 4

    def mousePressEvent(self, ev):
        if (
            ev.button() == Qt.LeftButton
            and self.title_label.isVisible()
            and self._header_strip_contains(ev.position().toPoint())
        ):
            self._drag_start = ev.position().toPoint()
            self.setFocus()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_start is not None:
            moved = (ev.position().toPoint() - self._drag_start).manhattanLength()
            if moved > QApplication.startDragDistance():
                self._drag_start = None
                self._start_pane_drag()
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_start = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if (
            ev.button() == Qt.LeftButton
            and self._tiled
            and self._header_strip_contains(ev.position().toPoint())
        ):
            self.solo_requested.emit(self)
            return
        super().mouseDoubleClickEvent(ev)

    def _start_pane_drag(self):
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(PANE_MIME, str(id(self)).encode())
        drag.setMimeData(mime)
        pixmap = self.grab().scaledToWidth(240, Qt.SmoothTransformation)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, 12))
        drag.exec(Qt.MoveAction)

    @staticmethod
    def _drag_source(mime) -> "StackPane | None":
        source_id = int(bytes(mime.data(PANE_MIME)).decode())
        return next((p for p in _all_panes if id(p) == source_id), None)

    def _update_drop_indicator(self, ev):
        if self._drag_source(ev.mimeData()) is self:
            self._drop_indicator.hide()
            return
        after = ev.position().x() > self.width() / 2
        w = 6
        self._drop_indicator.setGeometry(self.width() - w if after else 0, 0, w, self.height())
        self._drop_indicator.raise_()
        self._drop_indicator.show()

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(PANE_MIME) and self.title_label.isVisible():
            ev.acceptProposedAction()
            self._update_drop_indicator(ev)
        else:
            ev.ignore()  # file drops bubble up to the workspace window

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasFormat(PANE_MIME) and self.title_label.isVisible():
            ev.acceptProposedAction()
            self._update_drop_indicator(ev)
        else:
            ev.ignore()

    def dragLeaveEvent(self, ev):
        self._drop_indicator.hide()

    def dropEvent(self, ev):
        self._drop_indicator.hide()
        if not ev.mimeData().hasFormat(PANE_MIME):
            ev.ignore()
            return
        source = self._drag_source(ev.mimeData())
        if source is not None and source is not self:
            after = ev.position().x() > self.width() / 2
            from .workspace import get_workspace

            get_workspace().move_pane(source, self, after)
        ev.acceptProposedAction()

    # ---- per-pane B&C dock ---------------------------------------------

    def toggle_bc_dock(self):
        if self.bc_dock is None:
            self.show_bc_dock(self.bc_side)
        else:
            self.close_bc_dock()

    def show_bc_dock(self, side: str = "right"):
        from .bc_panel import PaneBCDock

        if self.bc_dock is None:
            self.bc_dock = PaneBCDock(self)
        else:
            self._middle.removeWidget(self.bc_dock)
        self.bc_side = side
        if side == "left":
            self._middle.insertWidget(0, self.bc_dock)
        else:
            self._middle.addWidget(self.bc_dock)
        self.bc_dock.show()

    def swap_bc_side(self):
        if self.bc_dock is not None:
            self.show_bc_dock("left" if self.bc_side == "right" else "right")

    def close_bc_dock(self):
        if self.bc_dock is not None:
            self._middle.removeWidget(self.bc_dock)
            self.bc_dock.hide()
            self.bc_dock.setParent(None)
            self.bc_dock.deleteLater()
            self.bc_dock = None

    def set_active_style(self, active: bool):
        style = "font-weight: bold;"
        if active:
            style += " color: palette(highlight);"
        self.title_label.setStyleSheet(style)

    # ---- flagging (F) --------------------------------------------------

    def toggle_flag(self):
        self.set_flagged(not self.flagged)

    def set_flagged(self, on: bool):
        self.flagged = on
        self.flag_label.setVisible(on)
        self.flag_toggled.emit(self)

    def unregister(self):
        global _active_pane
        if self in _all_panes:
            _all_panes.remove(self)
        if _active_pane is self:
            _active_pane = None

    def focusInEvent(self, ev):
        global _active_pane
        _active_pane = self
        self.activated.emit(self)
        super().focusInEvent(ev)

    # ---- navigation ----------------------------------------------------

    def position(self) -> tuple[int, int, int]:
        get = lambda k: self.bars[k].value() if k in self.bars else 0
        return get("t"), get("z"), get("c")

    def set_channel(self, c: int):
        """Select the channel to display/adjust. In shared-axes mode this drives
        the shared c bar — writing to the pane's own (hidden) bar would just be
        snapped back by _sync_shared, leaving B&C unable to change channel."""
        if self.shared_controller is not None and not self.shared_locked:
            self.shared_controller.shared_set("c", c)
        elif "c" in self.bars:
            self.bars["c"].set_value(c)

    def set_channel_visible(self, c: int, visible: bool):
        self.visible_channels[c] = visible
        self.refresh()
        self.channels_changed.emit(self)

    def _channel_key(self, c: int) -> bool:
        """Number-key channel switching. Returns False (key unhandled) when the
        stack has no such channel, so a stray digit doesn't swallow the event."""
        if c >= self.stack.n_channels:
            return False
        if self._composite_on():
            self.set_channel_visible(c, not self.visible_channels[c])
        else:
            self.set_channel(c)
        return True

    def _on_channel_box(self, c: int, on: bool):
        if self.visible_channels[c] != on:
            self.set_channel_visible(c, on)

    def _sync_channel_boxes(self):
        for i, box in enumerate(self.channel_boxes):
            if box.isChecked() != self.visible_channels[i]:
                box.blockSignals(True)
                box.setChecked(self.visible_channels[i])
                box.blockSignals(False)

    def _on_composite_toggled(self, *_):
        self.refresh()
        self.channels_changed.emit(self)

    def channel_state(self) -> tuple[bool, list[bool]]:
        return self._composite_on(), list(self.visible_channels)

    def set_channel_state(self, composite_on: bool, visible: list[bool]):
        """Adopt another pane's composite/visibility state (shared channels).
        Indices beyond this stack's channel count are ignored."""
        if self.composite_box is not None:
            self.composite_box.blockSignals(True)
            self.composite_box.setChecked(composite_on)
            self.composite_box.blockSignals(False)
        for i in range(min(len(visible), self.stack.n_channels)):
            self.visible_channels[i] = visible[i]
        self.refresh()

    def _step(self, letter: str, delta: int):
        if self.shared_controller is not None and not self.shared_locked:
            self.shared_controller.shared_step(letter, delta)
        elif letter in self.bars:
            self.bars[letter].step(delta)

    def _on_lock_toggled(self, locked: bool):
        self.shared_locked = locked
        self.lock_button.setText("🔒" if locked else "🔓")
        if self.shared_controller is not None:
            if locked:
                self.set_bars_visible(True)  # navigate this pane independently
            else:
                self.set_bars_visible(False)
                self.set_shared_position(*self.shared_controller.shared_position())
        self.refresh()

    # ---- shared-axes mode (driven by the workspace) --------------------

    def set_bars_visible(self, visible: bool):
        for bar in self.bars.values():
            bar.setVisible(visible)
        for box in self.channel_boxes:
            box.setVisible(visible)

    def set_shared_position(self, t: int, z: int, c: int):
        self._shared_pos = (t, z, c)
        if not self.isVisible():
            # Soloed-away / filtered tiles don't render; catch up on show.
            self._needs_refresh = True
            return
        # Shared ticks touch many panes at once: render off the UI thread.
        self.refresh(async_render=True)

    def showEvent(self, ev):
        if self._needs_refresh:
            self._needs_refresh = False
            self.refresh(async_render=True)
        super().showEvent(ev)

    def clear_shared(self):
        self.shared_controller = None
        self._shared_pos = None
        self.set_bars_visible(True)
        self.refresh()

    def _sync_shared(self):
        """Apply the shared position; mark blank where this stack has no image."""
        self._blank = False
        if self.shared_controller is None or self._shared_pos is None or self.shared_locked:
            return
        t, z, c = self._shared_pos
        s = self.stack
        if t >= s.n_frames or z >= s.n_slices:
            self._blank = True
            return
        for letter, value in (("t", t), ("z", z), ("c", min(c, s.n_channels - 1))):
            if letter in self.bars:
                self.bars[letter].set_value_silent(value)
        if not self._composite_on() and c >= s.n_channels:
            self._blank = True

    def _toggle_time_playback(self):
        """Space: play/pause the time axis (z for stacks without one)."""
        if self.shared_controller is not None and not self.shared_locked:
            self.shared_controller.toggle_time_playback()
            return
        bar = self.bars.get("t") or self.bars.get("z")
        if bar is not None:
            bar.toggle_playback()

    def handle_key(self, ev) -> bool:
        key = ev.key()
        mods = ev.modifiers()
        if key == Qt.Key_Space:
            self._toggle_time_playback()
            return True
        if key == Qt.Key_F and not mods:
            self.toggle_flag()
            return True
        # 1-9: in composite, flip that channel on/off; otherwise jump to it.
        if Qt.Key_1 <= key <= Qt.Key_9 and not mods:
            return self._channel_key(key - Qt.Key_1)
        # ←/→ scroll z; Shift makes them scroll t, Alt/Option makes them scroll c.
        horiz = "t" if mods & Qt.ShiftModifier else "c" if mods & Qt.AltModifier else "z"
        steps = {
            Qt.Key_Left: (horiz, -1),
            Qt.Key_Right: (horiz, 1),
            Qt.Key_Down: ("t", -1),
            Qt.Key_Up: ("t", 1),
            Qt.Key_Comma: ("c", -1),
            Qt.Key_Period: ("c", 1),
        }
        if key in steps:
            self._step(*steps[key])
            return True
        return False

    def keyPressEvent(self, ev):
        if not self.handle_key(ev):
            super().keyPressEvent(ev)

    # ---- rendering -----------------------------------------------------

    def _composite_on(self) -> bool:
        return self.composite_box.isChecked() if self.composite_box else False

    def _mip_on(self) -> bool:
        return self.mip_box.isChecked() if self.mip_box else False

    def _proj_abbrev(self) -> str:
        return stack_io.PROJECTION_ABBREV[self.proj_method]

    def _proj_label(self) -> str:
        return f"{self._proj_abbrev()} \u25be"

    def _show_proj_menu(self, pos: QPoint):
        menu = projection_menu(self.mip_box, self.proj_method, self.set_proj_method)
        menu.exec(self.mip_box.mapToGlobal(pos))

    def set_proj_method(self, method: str, enable: bool = True):
        """Switch the live z projection; picking a method also switches it on."""
        if self.mip_box is None or method not in stack_io.PROJECTION_METHODS:
            return
        self.proj_method = method
        self.mip_box.setText(self._proj_label())
        self.mip_box.setToolTip(projection_tooltip(method))
        if enable and not self.mip_box.isChecked():
            self.mip_box.setChecked(True)  # toggled -> refresh
        elif self.mip_box.isChecked():
            self.refresh()

    def _on_mip_toggled(self, on: bool):
        if "z" in self.bars:
            self.bars["z"].setEnabled(not on)
        self.refresh()

    def _display_channels(self) -> list[int]:
        t_, z_, c = self.position()
        if self._composite_on():
            return [i for i, on in enumerate(self.visible_channels) if on]
        return [c]

    def _render_stride(self) -> int:
        """Downsample when the view shows the image much smaller than 1:1."""
        view_rect = self.viewbox.viewRect()
        widget_px = max(self.view.width(), 1) * self.view.devicePixelRatioF()
        if view_rect.width() <= 0 or widget_px <= 0:
            return 1
        img_px_per_screen_px = view_rect.width() / widget_px
        stride = 1
        while stride * 2 <= img_px_per_screen_px and stride < 8:
            stride *= 2
        return stride

    def _maybe_restride(self, *_):
        if self._render_stride() != self._last_stride:
            self.refresh()

    def _enforce_min_zoom(self, *_):
        """Zooming out stops at fit-to-view: a smaller-than-view image snaps
        back to the fit level. (Resizing the window/tile itself still works —
        fit is computed from the live view size.)"""
        global _zoom_clamp_active
        if self._clamping_zoom or _zoom_clamp_active:
            return
        view_rect = self.viewbox.viewRect()
        if view_rect.width() <= 0:
            return
        widget_w = max(self.view.width(), 1)
        widget_h = max(self.view.height(), 1)
        h, w = self.stack.shape_yx
        px_per_img_px = widget_w / view_rect.width()
        fit_px_per_img_px = min(widget_w / w, widget_h / h)
        if px_per_img_px < fit_px_per_img_px * 0.999:
            # Global guard: with view-linked panes of differing sizes, per-pane
            # clamps could otherwise cascade into a feedback loop.
            self._clamping_zoom = True
            _zoom_clamp_active = True
            try:
                self.viewbox.autoRange(padding=0)
            finally:
                self._clamping_zoom = False
                _zoom_clamp_active = False

    def _render_key(self, t: int, z: int, stride: int) -> tuple:
        mip = self._mip_on()
        return (t, -1 if mip else z, tuple(self._display_channels()), stride,
                self.proj_method if mip else None, self.stack.version)

    def _store_cache(self, key: tuple, rgb: np.ndarray):
        self._plane_cache[key] = rgb
        # Big enough that a whole t-loop stays cached during playback.
        limit = max(16, self.stack.n_frames + 4)
        while len(self._plane_cache) > limit:
            self._plane_cache.popitem(last=False)

    def _apply_rgb(self, rgb: np.ndarray):
        h, w = self.stack.shape_yx
        self.image_item.setImage(rgb, autoLevels=False, levels=(0, 255))
        # Keep the item spanning full image coordinates regardless of stride.
        self.image_item.setRect(QRectF(0, 0, w, h))

    def _cached_render(self, t: int, z: int, stride: int) -> np.ndarray:
        """LRU cache over rendered planes; key includes the stack's range
        version so contrast edits invalidate naturally."""
        key = self._render_key(t, z, stride)
        rgb = self._plane_cache.get(key)
        if rgb is None:
            rgb = self.stack.render(
                t, z, self._display_channels(), stride, self._mip_on(), self.proj_method
            )
            self._store_cache(key, rgb)
        else:
            self._plane_cache.move_to_end(key)
        return rgb

    def refresh(self, *_, async_render: bool = False):
        self._render_request += 1
        self._sync_shared()
        t, z, c = self.position()
        stride = self._render_stride()
        self._last_stride = stride
        if self._blank:
            self._apply_rgb(np.zeros((2, 2, 3), dtype=np.uint8))
        else:
            key = self._render_key(t, z, stride)
            rgb = self._plane_cache.get(key)
            if rgb is not None:
                self._plane_cache.move_to_end(key)
                self._apply_rgb(rgb)
            elif async_render:
                from . import render_pool

                # Previous image stays on screen until the worker's result
                # lands; stale results are dropped via the request id.
                render_pool.submit(
                    self,
                    self.stack,
                    (t, z, list(self._display_channels()), stride, self._mip_on(),
                     self.proj_method),
                    key,
                    self._render_request,
                )
            else:
                self._apply_rgb(self._cached_render(t, z, stride))
        self._update_header()
        self._sync_channel_boxes()
        self.position_changed.emit(t, z, c)
        if not self._prefetch_scheduled and not self._blank:
            self._prefetch_scheduled = True
            QTimer.singleShot(60, self._prefetch_neighbors)

    def _prefetch_neighbors(self):
        """Pre-render adjacent z/t planes at idle so scrubbing feels instant."""
        self._prefetch_scheduled = False
        if self._blank or not self.isVisible():
            return
        t, z, c_ = self.position()
        stride = self._render_stride()
        for dt, dz in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            t2, z2 = t + dt, z + dz
            if 0 <= t2 < self.stack.n_frames and 0 <= z2 < self.stack.n_slices:
                self._cached_render(t2, z2, stride)

    def _update_header(self):
        s = self.stack
        if self._blank:
            self.header_label.setText(f"{s.name}  ·  no image at this position")
            return
        t, z, c = self.position()
        h, w = s.shape_yx
        parts = []
        label = s.label(t, z, c)
        if label:
            parts.append(label)
        pos = []
        if s.n_channels > 1:
            pos.append(f"c {c + 1}/{s.n_channels}")
        if s.n_slices > 1:
            pos.append(
                f"z {self._proj_abbrev()}"
                if self._mip_on()
                else f"z {z + 1}/{s.n_slices}"
            )
        if s.n_frames > 1:
            pos.append(f"t {t + 1}/{s.n_frames}")
        parts.append(" ".join(pos) if pos else "single image")
        bits = s.dtype.itemsize * 8
        depth = f"{bits}-bit float" if np.issubdtype(s.dtype, np.floating) else f"{bits}-bit"
        parts.append(f"{w}×{h} · {depth}")
        self.header_label.setText("  ·  ".join(parts))

    def _on_mouse_moved(self, scene_pos):
        if self._blank:
            self.probe_label.setText("")
            return
        pos = self.viewbox.mapSceneToView(scene_pos)
        x, y = int(pos.x()), int(pos.y())
        h, w = self.stack.shape_yx
        if 0 <= x < w and 0 <= y < h:
            t, z, c = self.position()
            values = self.stack.values_at(t, z, y, x, self._mip_on(), self.proj_method)
            fmt = (lambda v: f"{v:g}") if np.issubdtype(self.stack.dtype, np.floating) else str
            if self._composite_on():
                val = " ".join(fmt(v) for v in values)
            else:
                val = fmt(values[c])
            self.probe_label.setText(f"x={x} y={y}  value: {val}")
        else:
            self.probe_label.setText("")

    def mean_intensity(self) -> float:
        """Mean of the visible channels at the current position (subsampled);
        the metric behind the workspace's brightness sort."""
        if self._blank:
            return float("-inf")
        t, z, c = self.position()
        channels = [ci for ci in self._display_channels() if ci < self.stack.n_channels]
        if not channels:
            return float("-inf")
        data = self.stack.data
        return float(np.mean([np.mean(data[t, z, ci, ::8, ::8]) for ci in channels]))

    # ---- actions (invoked from menus of whichever window hosts us) -----

    def save_as(self):
        start_dir = self.stack.path.parent if self.stack.path else Path.home()
        default = str(start_dir / self.stack.name)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save stack (ImageJ format)", default, "TIFF (*.tif *.tiff)"
        )
        if not path:
            return
        if not path.lower().endswith((".tif", ".tiff")):
            path += ".tif"
        try:
            self.stack.save(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.title_label.setText(self.stack.name)
        host = self.window()
        if isinstance(host, StackWindow):
            host.setWindowTitle(self.stack.name)
        if isinstance(host, QMainWindow):
            host.statusBar().showMessage(f"Saved {path}", 5000)

    def project(self):
        if self.stack.n_slices <= 1 and self.stack.n_frames <= 1:
            return
        dialog = ProjectionDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        axis, method, start, stop = dialog.values()
        from .workspace import show_stack

        show_stack(stack_io.project(self.stack, axis, method, start, stop))

    def zoom(self, factor: float):
        self.viewbox.scaleBy((factor, factor))

    def zoom_fit(self):
        self.viewbox.autoRange(padding=0)

    def zoom_actual(self):
        vb = self.viewbox
        px_w, px_h = vb.rect().width(), vb.rect().height()
        center = vb.viewRect().center()
        vb.setRange(
            QRectF(center.x() - px_w / 2, center.y() - px_h / 2, px_w, px_h), padding=0
        )

    # ---- export --------------------------------------------------------

    def _full_res_rgb(self, t: int | None = None, z: int | None = None) -> np.ndarray:
        t0, z0, c_ = self.position()
        return self.stack.render(
            t if t is not None else t0,
            z if z is not None else z0,
            self._display_channels(),
            stride=1,
            mip=self._mip_on(),
            method=self.proj_method,
        )

    def _rgb_to_qimage(self, rgb: np.ndarray) -> QImage:
        h, w, _ = rgb.shape
        rgb = np.ascontiguousarray(rgb)
        return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()

    def _export_name(self, suffix: str) -> str:
        t, z, c_ = self.position()
        base = Path(self.stack.name).stem
        pos = f"_t{t + 1}" + (
            f"_{self._proj_abbrev()}" if self._mip_on() else f"_z{z + 1}"
        )
        return f"{base}{pos}.{suffix}"

    def copy_view(self):
        QApplication.clipboard().setImage(self._rgb_to_qimage(self._full_res_rgb()))
        host = self.window()
        if isinstance(host, QMainWindow):
            host.statusBar().showMessage("View copied to clipboard", 3000)

    def export_png(self):
        start = Path(app_settings.last_dir() or Path.home()) / self._export_name("png")
        path, _ = QFileDialog.getSaveFileName(self, "Export view as PNG", str(start), "PNG (*.png)")
        if not path:
            return
        app_settings.set_last_dir(str(Path(path).parent))
        self._rgb_to_qimage(self._full_res_rgb()).save(path)

    def export_movie(self):
        dialog = ExportMovieDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        axis, fps = dialog.values()
        start = Path(app_settings.last_dir() or Path.home()) / self._export_name("gif")
        path, _ = QFileDialog.getSaveFileName(self, "Export movie (GIF)", str(start), "GIF (*.gif)")
        if not path:
            return
        app_settings.set_last_dir(str(Path(path).parent))
        from PIL import Image

        t, z, c_ = self.position()
        count = self.stack.n_frames if axis == "T" else self.stack.n_slices
        frames = []
        for i in range(count):
            rgb = self._full_res_rgb(t=i if axis == "T" else t, z=z if axis == "T" else i)
            frames.append(Image.fromarray(rgb))
        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            duration=max(int(1000 / fps), 20),
            loop=0,
        )
        host = self.window()
        if isinstance(host, QMainWindow):
            host.statusBar().showMessage(f"Exported {count} frames to {path}", 5000)

    # ---- stack montage (t across, z down) ------------------------------

    def _stack_montage_image(
        self, t_idx, z_idx, channels, stride, labels, mip=False, on_tile=None, grid=None,
        method="Max",
    ):
        """One montage sheet of this stack: t across columns, z down rows;
        a single varying axis wraps into a near-square grid instead, or into
        the (cols, rows) ``grid`` when given (row-major; surplus cells stay
        black). Returns a PIL image, or None if on_tile() reported a cancel."""
        from PIL import Image, ImageDraw, ImageFont

        s = self.stack
        both = len(t_idx) > 1 and len(z_idx) > 1
        if both:
            rows, cols = len(z_idx), len(t_idx)
            cells = [
                (t_idx[c], z_idx[r], r, c, None) for r in range(rows) for c in range(cols)
            ]
        else:
            over_t = len(t_idx) > 1
            seq = t_idx if over_t else z_idx
            cols, rows = _montage_grid(len(seq), grid)
            letter = "t" if over_t else "z"
            cells = [
                (
                    v if over_t else t_idx[0],
                    z_idx[0] if over_t else v,
                    i // cols,
                    i % cols,
                    f"{letter} {v + 1}",
                )
                for i, v in enumerate(seq)
            ]
        first = s.render(cells[0][0], cells[0][1], channels, stride, mip, method)
        cell_h, cell_w = first.shape[:2]
        # t/z headers frame the sheet when both axes vary; a wrapped single
        # axis labels each tile's corner instead.
        header_h = max(16, cell_h // 16) if (labels and both) else 0
        gutter_w = int(header_h * 2.4)
        canvas = Image.new(
            "RGB", (gutter_w + cols * cell_w, header_h + rows * cell_h), (0, 0, 0)
        )
        draw = ImageDraw.Draw(canvas)
        text_size = max(11, int(max(16, cell_h // 16) * 0.7))
        try:
            font = ImageFont.load_default(size=text_size)
        except TypeError:  # Pillow < 10 has no sized default font
            font = ImageFont.load_default()
        gray = (215, 215, 215)
        if labels and both:
            for c in range(cols):
                draw.text((gutter_w + c * cell_w + 5, 2), f"t {t_idx[c] + 1}", fill=gray, font=font)
            for r in range(rows):
                draw.text((4, header_h + r * cell_h + 3), f"z {z_idx[r] + 1}", fill=gray, font=font)
        rendered = first
        for t, z, r, c, corner in cells:
            rgb = (
                rendered
                if rendered is not None
                else s.render(t, z, channels, stride, mip, method)
            )
            rendered = None
            x, y = gutter_w + c * cell_w, header_h + r * cell_h
            canvas.paste(Image.fromarray(rgb), (x, y))
            if labels and corner:
                draw.text((x + 5, y + 3), corner, fill=gray, font=font,
                          stroke_width=2, stroke_fill=(0, 0, 0))
            if on_tile is not None and not on_tile():
                return None
        return canvas

    def export_stack_montage(self):
        """Image > Export Stack Montage (Cmd+Alt+M)."""
        s = self.stack
        if s.n_frames <= 1 and s.n_slices <= 1:
            _show_status("Nothing to montage: single t/z position")
            return
        dialog = StackMontageDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        t_step, z_step, mip, stride, per_channel, labels, grid = dialog.values()
        t0, z0, _c0 = self.position()
        t_idx = list(range(0, s.n_frames, t_step)) if s.n_frames > 1 else [t0]
        z_idx = list(range(0, s.n_slices, z_step)) if s.n_slices > 1 and not mip else [z0]
        base = Path(s.name).stem
        start = Path(app_settings.last_dir() or Path.home()) / f"{base}_montage.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export stack montage", str(start), "PNG (*.png)"
        )
        if not path:
            return
        app_settings.set_last_dir(str(Path(path).parent))
        channel_sets = (
            [[c] for c in range(s.n_channels)] if per_channel else [self._display_channels()]
        )
        total = len(t_idx) * len(z_idx) * len(channel_sets)
        progress = QProgressDialog(
            f"Rendering {total} montage tiles…", "Cancel", 0, total, self.window()
        )
        progress.setWindowModality(Qt.WindowModal)
        done = 0

        def tick():
            nonlocal done
            done += 1
            progress.setValue(done)
            QApplication.processEvents()
            return not progress.wasCanceled()

        written = []
        for i, channels in enumerate(channel_sets):
            img = self._stack_montage_image(
                t_idx, z_idx, channels, stride, labels, mip, tick, grid, self.proj_method
            )
            if img is None:  # canceled
                return
            out = Path(path)
            if per_channel:
                out = out.with_name(f"{out.stem}_C{i + 1}{out.suffix}")
            img.save(out)
            written.append(out.name)
        progress.setValue(total)
        _show_status(
            "Exported " + (", ".join(written) if per_channel else f"stack montage to {path}"),
            6000,
        )


def _montage_grid(n: int, grid=None) -> tuple[int, int]:
    """(cols, rows) for n tiles laid out row-major: near-square by default,
    or the requested grid grown just enough (extra rows) to hold every tile."""
    if grid is None:
        cols = math.ceil(math.sqrt(n))
        return cols, math.ceil(n / cols)
    cols = max(1, int(grid[0]))
    return cols, max(1, int(grid[1]), math.ceil(n / cols))


class StackMontageDialog(QDialog):
    """Options for Image > Export Stack Montage."""

    LAYOUTS = (
        ("Auto grid (near-square)", "auto"),
        ("One row", "row"),
        ("One column", "column"),
        ("Custom columns × rows", "custom"),
    )

    def __init__(self, pane: "StackPane"):
        super().__init__(pane.window())
        self.pane = pane
        self.setWindowTitle("Export Stack Montage")
        s = pane.stack
        form = QFormLayout(self)
        self.t_spin = QSpinBox()
        self.t_spin.setRange(1, max(s.n_frames - 1, 1))
        if s.n_frames > 1:
            form.addRow("Every nth t:", self.t_spin)
        self.z_combo = QComboBox()
        self.z_combo.addItem("All slices as rows", False)
        self.z_combo.addItem(
            f"{PROJ_MENU_TEXT[pane.proj_method]} projection "
            f"({stack_io.PROJECTION_ABBREV[pane.proj_method]})",
            True,
        )
        if pane._mip_on():
            self.z_combo.setCurrentIndex(1)
        self.z_spin = QSpinBox()
        self.z_spin.setRange(1, max(s.n_slices - 1, 1))
        if s.n_slices > 1:
            form.addRow("z:", self.z_combo)
            form.addRow("Every nth z:", self.z_spin)
        # Layout applies when a single axis varies (t, z, or t with z
        # collapsed to a projection); with both varying the sheet is always t
        # across × z down. The spinboxes always show the effective grid and
        # are editable only for the custom layout.
        self.layout_combo = QComboBox()
        for label, key in self.LAYOUTS:
            self.layout_combo.addItem(label, key)
        form.addRow("Layout:", self.layout_combo)
        self.cols_spin = QSpinBox()
        self.rows_spin = QSpinBox()
        grid_row = QWidget()
        grid_layout = QHBoxLayout(grid_row)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.addWidget(self.cols_spin)
        grid_layout.addWidget(QLabel("×"))
        grid_layout.addWidget(self.rows_spin)
        grid_layout.addWidget(QLabel("columns × rows"))
        grid_layout.addStretch()
        form.addRow("Grid:", grid_row)
        self._syncing = False
        self.scale_combo = QComboBox()
        for label, factor in (("Full", 1), ("Half", 2), ("Quarter", 4)):
            self.scale_combo.addItem(label, factor)
        form.addRow("Resolution:", self.scale_combo)
        self.channels_combo = None
        if s.n_channels > 1:
            self.channels_combo = QComboBox()
            self.channels_combo.addItem("As displayed (visible channels)", False)
            self.channels_combo.addItem("One file per channel", True)
            form.addRow("Channels:", self.channels_combo)
        self.labels_box = QCheckBox("t/z position labels")
        self.labels_box.setChecked(True)
        form.addRow("", self.labels_box)
        self.estimate = QLabel()
        self.estimate.setStyleSheet("color: #909090;")
        form.addRow("", self.estimate)
        for signal in (
            self.t_spin.valueChanged,
            self.z_spin.valueChanged,
            self.z_combo.currentIndexChanged,
            self.layout_combo.currentIndexChanged,
            self.scale_combo.currentIndexChanged,
        ):
            signal.connect(self._update_estimate)
        self.cols_spin.valueChanged.connect(
            lambda _v: self._grid_edited(self.cols_spin, self.rows_spin)
        )
        self.rows_spin.valueChanged.connect(
            lambda _v: self._grid_edited(self.rows_spin, self.cols_spin)
        )
        self._update_estimate()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        # Reopen with the last accepted options; a pane with its z
        # projection on still preselects the collapsed export, in the pane's
        # own method, since that's what's on screen.
        app_settings.restore_widgets("stackMontage", self._remembered())
        if pane._mip_on():
            self.z_combo.setCurrentIndex(1)

    def _remembered(self) -> dict[str, QWidget]:
        """The options carried from one export to the next: only those this
        stack lets the user choose (a t-less stack must not reset the t
        step). Restore order matters: the layout comes before the grid boxes
        it unlocks."""
        s = self.pane.stack
        widgets: dict[str, QWidget] = {}
        if s.n_frames > 1:
            widgets["tStep"] = self.t_spin
        if s.n_slices > 1:
            widgets["z"] = self.z_combo
            widgets["zStep"] = self.z_spin
        widgets["layout"] = self.layout_combo
        widgets["cols"] = self.cols_spin
        widgets["rows"] = self.rows_spin
        widgets["scale"] = self.scale_combo
        if self.channels_combo is not None:
            widgets["channels"] = self.channels_combo
        widgets["labels"] = self.labels_box
        return widgets

    def accept(self):
        widgets = self._remembered()
        if not self.cols_spin.isEnabled():  # keep the last *custom* grid
            del widgets["cols"], widgets["rows"]
        app_settings.save_widgets("stackMontage", widgets)
        super().accept()

    def _counts(self) -> tuple[int, int]:
        """(nt, nz): tiles along each axis for the current options."""
        s = self.pane.stack
        mip = self.z_combo.currentData()
        nt = len(range(0, s.n_frames, self.t_spin.value())) if s.n_frames > 1 else 1
        nz = len(range(0, s.n_slices, self.z_spin.value())) if s.n_slices > 1 and not mip else 1
        return nt, nz

    def _grid_edited(self, edited: QSpinBox, other: QSpinBox):
        """Custom grid: never lose a tile — grow the other dimension when the
        edited one leaves too few cells, otherwise keep both as typed."""
        if self._syncing:
            return
        nt, nz = self._counts()
        n = max(nt, nz)
        if edited.value() * other.value() < n:
            self._syncing = True
            other.setValue(math.ceil(n / edited.value()))
            self._syncing = False
        self._update_estimate()

    def _grid(self) -> tuple[int, int, bool]:
        """(cols, rows, both): the sheet's grid for the current options and
        whether it is the fixed t×z layout."""
        nt, nz = self._counts()
        both = nt > 1 and nz > 1
        n = max(nt, nz)
        mode = self.layout_combo.currentData()
        if both:
            return nt, nz, True
        if mode == "row":
            return n, 1, False
        if mode == "column":
            return 1, n, False
        if mode == "custom":
            wanted = (min(self.cols_spin.value(), n), min(self.rows_spin.value(), n))
            return (*_montage_grid(n, wanted), False)
        return (*_montage_grid(n), False)

    def _update_estimate(self, *_):
        s = self.pane.stack
        mip = self.z_combo.currentData()
        self.z_spin.setEnabled(not mip)
        cols, rows, both = self._grid()
        n = max(self._counts())
        self.layout_combo.setEnabled(not both)
        custom = not both and self.layout_combo.currentData() == "custom"
        self.cols_spin.setEnabled(custom)
        self.rows_spin.setEnabled(custom)
        self._syncing = True
        for spin, value in ((self.cols_spin, cols), (self.rows_spin, rows)):
            spin.setRange(1, max(n, value))
            spin.setValue(value)
        self._syncing = False
        stride = self.scale_combo.currentData()
        h, w = s.shape_yx
        cell_w, cell_h = len(range(0, w, stride)), len(range(0, h, stride))
        text = f"{cols}×{rows} tiles · ≈{cols * cell_w}×{rows * cell_h} px"
        empty = cols * rows - n
        if not both and empty > 0:
            text += f" · {empty} empty"
        self.estimate.setText(text)

    def values(self) -> tuple[int, int, bool, int, bool, bool, tuple[int, int] | None]:
        per_channel = (
            self.channels_combo.currentData() if self.channels_combo is not None else False
        )
        cols, rows, both = self._grid()
        grid = None if both or self.layout_combo.currentData() == "auto" else (cols, rows)
        return (
            self.t_spin.value(),
            self.z_spin.value(),
            bool(self.z_combo.currentData()),
            self.scale_combo.currentData(),
            bool(per_channel),
            self.labels_box.isChecked(),
            grid,
        )


class ExportMovieDialog(QDialog):
    def __init__(self, pane: "StackPane"):
        super().__init__(pane.window())
        self.setWindowTitle("Export Movie (GIF)")
        form = QFormLayout(self)
        self.axis_combo = QComboBox()
        if pane.stack.n_frames > 1:
            self.axis_combo.addItem("T")
        if pane.stack.n_slices > 1 and not pane._mip_on():
            self.axis_combo.addItem("Z")
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(10)
        form.addRow("Axis:", self.axis_combo)
        form.addRow("Frames/second:", self.fps_spin)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._remembered = {"axis": self.axis_combo, "fps": self.fps_spin}
        app_settings.restore_widgets("movie", self._remembered)

    def accept(self):
        app_settings.save_widgets("movie", self._remembered)
        super().accept()

    def values(self) -> tuple[str, int]:
        return self.axis_combo.currentText(), self.fps_spin.value()


def build_menus(window: QMainWindow, active_pane: Callable[[], StackPane | None]):
    """Shared menu bar for stack windows and the workspace window."""

    def _add(menu, text, shortcut, slot) -> QAction:
        action = QAction(text, window)
        action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _with_pane(method):
        def run():
            pane = active_pane()
            if pane is not None:
                method(pane)

        return run

    file_menu = window.menuBar().addMenu("&File")
    _add(file_menu, "&Open...", QKeySequence.Open, lambda: open_stack_dialog(window))
    _add(file_menu, "Open &Folder...", "Ctrl+Shift+O", lambda: open_folder(window))
    _add(file_menu, "&Save As...", QKeySequence.Save, _with_pane(StackPane.save_as))
    file_menu.addSeparator()

    def _session(fn_name):
        def run():
            from . import session

            getattr(session, fn_name)(window)

        return run

    _add(file_menu, "Save Sessio&n...", "Ctrl+Alt+S", _session("save_session_dialog"))
    _add(file_menu, "Open Sessi&on...", "Ctrl+Alt+O", _session("open_session_dialog"))
    _add(file_menu, "&Restore Last Session", "Ctrl+Alt+R", _session("restore_last_session"))
    file_menu.addSeparator()

    def open_settings():
        from .settings_window import show_settings

        show_settings(window)

    settings_action = _add(file_menu, "Se&ttings...", QKeySequence.Preferences, open_settings)
    settings_action.setMenuRole(QAction.PreferencesRole)

    def check_for_updates():
        from . import updater

        updater.check_now()

    # ApplicationSpecificRole puts it in the "TIFF Visualizer" menu on macOS,
    # next to Settings, where a Mac user looks for it; elsewhere it stays here.
    update_action = _add(file_menu, "Check for &Updates...", "", check_for_updates)
    update_action.setMenuRole(QAction.ApplicationSpecificRole)
    file_menu.addSeparator()
    _add(file_menu, "&Close Window", QKeySequence.Close, window.close)

    image_menu = window.menuBar().addMenu("&Image")

    def show_bc(pane):
        from .bc_panel import show_bc_panel

        show_bc_panel(pane)

    _add(image_menu, "&Brightness/Contrast...", "Ctrl+Shift+C", _with_pane(show_bc))

    def undo_contrast():
        from .bc_panel import undo_last_range_change

        undo_last_range_change()

    _add(image_menu, "&Undo Contrast Change", QKeySequence.Undo, undo_contrast)
    _add(image_menu, "&Projection...", "Ctrl+Shift+P", _with_pane(StackPane.project))
    image_menu.addSeparator()
    _add(image_menu, "&Copy View", QKeySequence.Copy, _with_pane(StackPane.copy_view))
    _add(image_menu, "&Export View as PNG...", "Ctrl+E", _with_pane(StackPane.export_png))
    _add(image_menu, "Export &Movie (GIF)...", "Ctrl+Shift+E", _with_pane(StackPane.export_movie))
    _add(
        image_menu,
        "Export Stack Mo&ntage...",
        "Ctrl+Alt+M",
        _with_pane(StackPane.export_stack_montage),
    )

    def export_grid_montage():
        from .workspace import export_montage

        export_montage(window)

    _add(image_menu, "Export &Grid Montage...", "Ctrl+Shift+M", export_grid_montage)

    view_menu = window.menuBar().addMenu("&View")
    _add(view_menu, "Zoom &In", QKeySequence.ZoomIn, _with_pane(lambda p: p.zoom(1 / 1.25)))
    _add(view_menu, "Zoom &Out", QKeySequence.ZoomOut, _with_pane(lambda p: p.zoom(1.25)))
    _add(view_menu, "&Actual Size", "Ctrl+1", _with_pane(StackPane.zoom_actual))
    _add(view_menu, "&Fit to Window", "Ctrl+0", _with_pane(StackPane.zoom_fit))
    view_menu.addSeparator()

    def toggle():
        from .workspace import toggle_combined

        toggle_combined()

    combine_action = _add(view_menu, "&Combine into One Window", "Ctrl+G", toggle)

    def combine_selected_dialog():
        from .workspace import show_combine_dialog

        show_combine_dialog(window)

    _add(view_menu, "Combine &Selected...", "Ctrl+Alt+G", combine_selected_dialog)

    def set_shared(checked: bool):
        from .workspace import set_shared_axes

        set_shared_axes(checked)

    shared_action = QAction("Shared A&xes in Grid", window)
    shared_action.setCheckable(True)
    shared_action.setShortcut(QKeySequence("Ctrl+Shift+G"))
    shared_action.triggered.connect(set_shared)
    view_menu.addAction(shared_action)

    def update_view_menu():
        from .workspace import shared_axes, workspace_active

        combine_action.setText(
            "Split into Separate &Windows" if workspace_active() else "&Combine into One Window"
        )
        shared_action.setChecked(shared_axes())

    view_menu.aboutToShow.connect(update_view_menu)

    def copy_flagged_names():
        names = [p.stack.name for p in _all_panes if p.flagged]
        if names:
            QApplication.clipboard().setText("\n".join(names))
            _show_status(f"Copied {len(names)} flagged stack name(s)")
        else:
            _show_status("No flagged stacks (press F on a stack to flag it)")

    _add(view_menu, "Copy Flagged &Names", "", copy_flagged_names)
    view_menu.addSeparator()
    _add(view_menu, "&Keyboard Shortcuts", "?", lambda: show_cheatsheet(window))


class StackWindow(QMainWindow):
    """A floating window hosting a single pane (Fiji-style)."""

    def __init__(self, pane: StackPane):
        super().__init__()
        self.pane = pane
        pane.set_tiled(False)
        self.setCentralWidget(pane)
        pane.show()  # clear any explicit hide from grid solo/flag filtering
        self.setWindowTitle(pane.stack.name)
        build_menus(self, lambda: self.pane)
        self._size_to_image()
        if pane.saved_window_geometry is not None:
            self.restoreGeometry(pane.saved_window_geometry)
        pane.setFocus()
        _open_windows.add(self)

    def detach_pane(self) -> StackPane:
        """Remove the pane so it survives this window's close (for combining)."""
        pane = self.takeCentralWidget()
        self.pane = None
        return pane

    def keyPressEvent(self, ev):
        if self.pane is None or not self.pane.handle_key(ev):
            super().keyPressEvent(ev)

    def _size_to_image(self):
        h, w = self.pane.stack.shape_yx
        screen = self.screen().availableGeometry()
        extra_h = 84 + 24 * len(self.pane.bars)  # header + status bar + dim bars
        self.resize(
            min(w + 20, int(screen.width() * 0.9)),
            min(h + extra_h, int(screen.height() * 0.9)),
        )

    def closeEvent(self, ev):
        _open_windows.discard(self)
        if self.pane is not None:
            self.pane.unregister()
        super().closeEvent(ev)


class ProjectionDialog(QDialog):
    """Fiji's 'Z Project...': choose axis, method and slice range."""

    def __init__(self, pane: StackPane):
        super().__init__(pane.window())
        self.setWindowTitle("Projection")
        self._stack = pane.stack
        stack = pane.stack

        form = QFormLayout(self)
        self.axis_combo = QComboBox()
        if stack.n_slices > 1:
            self.axis_combo.addItem("Z")
        if stack.n_frames > 1:
            self.axis_combo.addItem("T")
        self.method_combo = QComboBox()
        self.method_combo.addItems(stack_io.PROJECTION_METHODS)
        self.start_spin = QSpinBox()
        self.stop_spin = QSpinBox()
        form.addRow("Axis:", self.axis_combo)
        form.addRow("Method:", self.method_combo)
        form.addRow("Start:", self.start_spin)
        form.addRow("Stop:", self.stop_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.axis_combo.currentTextChanged.connect(self._update_range)
        self._update_range()
        self._remembered = {"axis": self.axis_combo, "method": self.method_combo}
        app_settings.restore_widgets("projection", self._remembered)
        saved = app_settings.settings().value("dialogs/projection/range", "", type=str)
        if "-" in saved:  # "start-stop", 1-based; the spins clamp to this axis
            start, stop = (int(v) for v in saved.split("-", 1))
            self.start_spin.setValue(start)
            self.stop_spin.setValue(stop)

    def _axis_count(self) -> int:
        return (
            self._stack.n_slices
            if self.axis_combo.currentText() == "Z"
            else self._stack.n_frames
        )

    def accept(self):
        app_settings.save_widgets("projection", self._remembered)
        start, stop = sorted((self.start_spin.value(), self.stop_spin.value()))
        narrowed = (start, stop) != (1, self._axis_count())
        app_settings.settings().setValue(
            "dialogs/projection/range", f"{start}-{stop}" if narrowed else "full"
        )
        super().accept()

    def _update_range(self, *_):
        count = self._axis_count()
        for spin in (self.start_spin, self.stop_spin):
            spin.setRange(1, count)
        self.start_spin.setValue(1)
        self.stop_spin.setValue(count)

    def values(self) -> tuple[str, str, int, int]:
        start = self.start_spin.value() - 1
        stop = self.stop_spin.value() - 1
        if stop < start:
            start, stop = stop, start
        return self.axis_combo.currentText(), self.method_combo.currentText(), start, stop


class FileDropMixin:
    """Accept .tif/.tiff files dropped onto the window and open them."""

    def _init_file_drops(self):
        self.setAcceptDrops(True)

    @staticmethod
    def _dropped_tiff_paths(mime) -> list[str]:
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls()]
        return [p for p in paths if p.lower().endswith((".tif", ".tiff"))]

    def dragEnterEvent(self, ev):
        if self._dropped_tiff_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        paths = self._dropped_tiff_paths(ev.mimeData())
        if paths:
            open_paths(paths, self)
            ev.acceptProposedAction()


class _StackLoader(QThread):
    """Background loader for compressed TIFFs (which can't be memory-mapped)."""

    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            self.loaded.emit(load_stack(self.path))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


_loaders: set[_StackLoader] = set()


def _show_status(message: str, msecs: int = 4000):
    from . import control_panel

    if control_panel._instance is not None:
        control_panel._instance.statusBar().showMessage(message, msecs)


def _on_async_render_done(pane: StackPane, key: tuple, rgb, request_id: int):
    """Main-thread landing for pool-rendered planes."""
    if pane not in _all_panes:
        return  # pane closed while rendering
    pane._store_cache(key, rgb)
    if request_id == pane._render_request:  # still the frame we want
        pane._apply_rgb(rgb)


from . import render_pool  # noqa: E402

render_pool.set_handler(_on_async_render_done)


def open_path(path: str | Path, parent: QWidget | None = None) -> StackPane | None:
    path = Path(path)
    try:
        slow = stack_io.needs_decode(path)
    except Exception as exc:  # noqa: BLE001 - surface any load failure to the user
        QMessageBox.critical(parent, "Cannot open stack", f"{path}\n\n{exc}")
        return None
    from .workspace import show_stack

    if not slow:  # memory-mapped: effectively instant, keep it synchronous
        try:
            return show_stack(load_stack(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(parent, "Cannot open stack", f"{path}\n\n{exc}")
            return None

    _show_status(f"Loading {path.name}…", 60000)
    loader = _StackLoader(path)
    _loaders.add(loader)

    def on_loaded(stack):
        from . import control_panel

        show_stack(stack)
        _show_status(f"Loaded {path.name}", 3000)
        control_panel.refresh_state()

    loader.loaded.connect(on_loaded)
    loader.failed.connect(
        lambda msg: QMessageBox.critical(parent, "Cannot open stack", f"{path}\n\n{msg}")
    )
    loader.finished.connect(lambda: _loaders.discard(loader))
    loader.start()
    return None


def open_paths(paths, parent: QWidget | None = None) -> list[StackPane]:
    """Open many stacks at once.

    Memory-mappable stacks are loaded here and handed to the grid in a single
    batch — adding them one at a time relayouts the whole grid per stack,
    which is O(n²) for a folder of 48. Compressed stacks still load in the
    background one by one, and failures are reported once at the end instead
    of one dialog per file.
    """
    from .workspace import get_workspace, show_stack, workspace_active

    stacks, deferred, failed = [], [], []
    for path in paths:
        path = Path(path)
        try:
            if stack_io.needs_decode(path):
                deferred.append(path)
            else:
                stacks.append(load_stack(path))
        except Exception as exc:  # noqa: BLE001 - collect, report once
            failed.append(f"{path.name}: {exc}")
    if workspace_active() and stacks:
        panes = [StackPane(stack) for stack in stacks]
        ws = get_workspace()
        ws.add_panes(panes)
        ws.raise_()
    else:
        panes = [show_stack(stack) for stack in stacks]
    for path in deferred:
        open_path(path, parent)
    if failed:
        QMessageBox.critical(
            parent, "Cannot open stack", "\n".join(failed[:10])
        )
    return panes


def open_stack_dialog(parent: QWidget | None = None):
    paths, _ = QFileDialog.getOpenFileNames(
        parent,
        "Open TIFF stack",
        app_settings.last_dir(),
        "TIFF images (*.tif *.tiff);;All files (*)",
    )
    if paths:
        app_settings.set_last_dir(str(Path(paths[0]).parent))
    open_paths(paths, parent)


def open_folder(parent: QWidget | None = None, directory: str | Path | None = None):
    """Open every TIFF stack in a folder at once."""
    if directory is None:
        directory = QFileDialog.getExistingDirectory(
            parent, "Open folder of TIFF stacks", app_settings.last_dir()
        )
        if not directory:
            return
    app_settings.set_last_dir(str(directory))
    open_paths(
        [p for p in sorted(Path(directory).iterdir()) if p.suffix.lower() in (".tif", ".tiff")],
        parent,
    )


_CHEATSHEET = """
<h3 style='margin-top:0'>Keyboard &amp; Mouse</h3>
<table cellspacing='0' cellpadding='3'>
<tr><td><b>wheel / ← →</b></td><td>scroll z</td>
    <td width='30'></td><td><b>Cmd+G</b></td><td>combine / split all</td></tr>
<tr><td><b>Shift + wheel/←→</b></td><td>scroll t (also ↑ ↓)</td>
    <td></td><td><b>Alt+Cmd+G</b></td><td>combine selected…</td></tr>
<tr><td><b>Alt + wheel/←→</b></td><td>scroll channel (also , .)</td>
    <td></td><td><b>Cmd+Shift+G</b></td><td>shared axes in grid</td></tr>
<tr><td><b>1 … 9</b></td><td>channel on/off (jumps to it when not composite)</td>
    <td></td><td></td><td></td></tr>
<tr><td><b>pinch / Cmd+wheel</b></td><td>zoom at cursor</td>
    <td></td><td><b>Cmd+Shift+C</b></td><td>brightness / contrast</td></tr>
<tr><td><b>Cmd+0 / Cmd+1</b></td><td>fit / actual size</td>
    <td></td><td><b>Cmd+Z</b></td><td>undo contrast change</td></tr>
<tr><td><b>Cmd+O / Cmd+Shift+O</b></td><td>open files / folder</td>
    <td></td><td><b>Cmd+Shift+P</b></td><td>projection…</td></tr>
<tr><td><b>Cmd+S</b></td><td>save as (ImageJ format)</td>
    <td></td><td><b>Cmd+C / Cmd+E</b></td><td>copy view / export PNG</td></tr>
<tr><td><b>Cmd+Alt+S / O / R</b></td><td>save / open / restore session</td>
    <td></td><td><b>Cmd+Shift+E</b></td><td>export movie (GIF)</td></tr>
<tr><td><b>Space</b></td><td>play / pause time</td>
    <td></td><td><b>Cmd+,</b></td><td>settings</td></tr>
<tr><td><b>Enter / Esc</b></td><td>solo the active tile / back to grid</td>
    <td></td><td><b>Cmd+Shift+M</b></td><td>export grid montage</td></tr>
<tr><td><b>F</b></td><td>flag / unflag the active stack (★)</td>
    <td></td><td><b>Cmd+Alt+M</b></td><td>export stack montage (t×z)</td></tr>
</table>
<p>Grid tiles: drag the <b>header strip</b> to rearrange (double-click it to
solo) · the <b>★ only</b> box shows just the flagged tiles · <b>Sort</b>
reorders tiles by name or brightness · <b>🔓</b> locks a tile out of shared
axes · <b>▶</b> on z/t bars plays (right-click it for speed) · the
<b>B&amp;C</b> button on a tile attaches a contrast panel to its side.</p>
"""


def show_cheatsheet(parent: QWidget | None = None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Keyboard Shortcuts")
    layout = QVBoxLayout(dialog)
    label = QLabel(_CHEATSHEET)
    label.setTextFormat(Qt.RichText)
    layout.addWidget(label)
    dialog.exec()
