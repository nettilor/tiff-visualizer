"""The main control window: Fiji-style command central, no shortcuts required.

A small always-open window with buttons for every main action, targeting the
active stack (last clicked). Closing it quits the app.
"""

from __future__ import annotations

import functools
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import plate as plate_mod
from . import settings as app_settings
from . import viewer, workspace
from .viewer import FileDropMixin, StackPane, build_menus


def _canonical(path) -> Path | None:
    """One spelling per file, so a stack opened through a relative or
    symlinked path (tiffviz XY05.tif, /tmp vs /private/tmp) still matches
    its folder-list entry."""
    return _resolved(str(path)) if path else None


@functools.lru_cache(maxsize=4096)
def _resolved(path: str) -> Path:
    # Cached: the open-state sync runs on every focus change, and resolving
    # each open stack's path there costs a filesystem lookup (network drives).
    return Path(path).resolve()


def _open_stack_paths() -> set[Path]:
    return {_canonical(p.stack.path) for p in viewer._all_panes if p.stack.path}


def _close_stack_by_path(path: Path):
    """Close every open pane showing this file, wherever it lives."""
    _close_stacks_by_paths({path})


def _close_stacks_by_paths(paths: set):
    """Same, for many files at once: the grid tiles go in one batch so the
    layout is rebuilt once instead of once per closed stack."""
    paths = {_canonical(p) for p in paths}
    targets = [
        p for p in list(viewer._all_panes) if p.stack.path and _canonical(p.stack.path) in paths
    ]
    ws = workspace._workspace
    tiled = [p for p in targets if ws is not None and p in ws.panes]
    if tiled:
        ws.close_panes(tiled)
    for pane in targets:
        if pane not in tiled:
            window = next((w for w in viewer._open_windows if w.pane is pane), None)
            if window is not None:
                window.close()


def _plate_bindings() -> dict[str, dict[str, str]]:
    """Which plate a folder was last shown with, and which folder a plate:
    dropping either again brings the other back (JSON in QSettings)."""
    raw = app_settings.settings().value("plates/bindings", "", type=str)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: dict(data.get(k) or {}) for k in ("by_folder", "by_plate")}


def _remember_plate(folder: Path, plate_path: Path):
    data = _plate_bindings()
    folder_key, plate_key = str(Path(folder).resolve()), str(Path(plate_path).resolve())
    for table, key, value in (("by_folder", folder_key, plate_key), ("by_plate", plate_key, folder_key)):
        d = data[table]
        d.pop(key, None)
        d[key] = value
        while len(d) > 50:  # oldest first: dicts keep insertion order
            del d[next(iter(d))]
    app_settings.settings().setValue("plates/bindings", json.dumps(data))


def _remembered_plate(folder: Path) -> Path | None:
    value = _plate_bindings()["by_folder"].get(str(Path(folder).resolve()))
    return Path(value) if value else None


def _remembered_folder(plate_path: Path) -> Path | None:
    value = _plate_bindings()["by_plate"].get(str(Path(plate_path).resolve()))
    return Path(value) if value else None


def _forget_plate(folder: Path, plate_path: Path):
    """Unlinking: neither side brings the other back any more."""
    data = _plate_bindings()
    folder_key, plate_key = str(Path(folder).resolve()), str(Path(plate_path).resolve())
    if data["by_folder"].get(folder_key) == plate_key:
        del data["by_folder"][folder_key]
    if data["by_plate"].get(plate_key) == folder_key:
        del data["by_plate"][plate_key]
    app_settings.settings().setValue("plates/bindings", json.dumps(data))


@dataclass
class _RangeDrag:
    """One press on the file list until its release: the row the range
    grows from, the state it paints (open or closed), every row's state
    before the press, and the rows painted so far."""

    anchor: int
    target: bool
    before: list[bool]
    rows: range = range(0)


class FileList(QWidget):
    """The checkbox column of a folder section, selectable like a file
    manager's list: press on a row and drag across others, or Shift+click
    after a plain click, and the whole range takes that row's new state —
    open or closed — committed as one batch on release. A click anywhere on
    a row counts, so the boxes themselves ignore the mouse (keyboard
    toggling through them still works); the hovered row is tinted.
    """

    def __init__(self, on_apply: Callable[[list[int], bool], None]):
        super().__init__()
        self._on_apply = on_apply
        self.boxes: list[QCheckBox] = []
        # Row of the last plain press: where a Shift+click range grows from.
        self._anchor: int | None = None
        self._drag: _RangeDrag | None = None
        self._hover: int | None = None
        self.setMouseTracking(True)
        self.setToolTip(
            "Click a file to open or close it.\n"
            "Drag across files, or Shift+click, to open or close a whole range."
        )
        # Keeps the list scrolling while a drag is held past its top or
        # bottom edge; move events stop once the cursor stops.
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._autoscroll)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 0, 0, 0)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)

    def add(self, box: QCheckBox):
        box.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._layout.insertWidget(len(self.boxes), box)
        self.boxes.append(box)

    def painted_rows(self) -> range:
        """Rows a held drag is previewing; a sync must leave them alone."""
        return self._drag.rows if self._drag is not None else range(0)

    def row_at(self, y: int, clamp: bool = False) -> int | None:
        """The row at this y (list coordinates). Outside the rows: the
        nearest end when clamping (a drag past the end runs to the end),
        else None (a press on the empty space below the list does nothing)."""
        if not self.boxes:
            return None
        if y < self.boxes[0].geometry().top():
            return 0 if clamp else None
        if y > self.boxes[-1].geometry().bottom():
            return len(self.boxes) - 1 if clamp else None
        for i, box in enumerate(self.boxes):
            if y <= box.geometry().bottom():
                return i
        return len(self.boxes) - 1

    # ---- mouse ---------------------------------------------------------

    def mousePressEvent(self, ev):
        row = self.row_at(ev.position().toPoint().y())
        if ev.button() != Qt.LeftButton or row is None:
            super().mousePressEvent(ev)
            return
        if not (ev.modifiers() & Qt.ShiftModifier) or self._anchor is None:
            self._anchor = row
        self._drag = _RangeDrag(
            self._anchor,
            not self.boxes[row].isChecked(),
            [box.isChecked() for box in self.boxes],
        )
        self._paint(row)
        ev.accept()

    def mouseMoveEvent(self, ev):
        y = ev.position().toPoint().y()
        if self._drag is not None:
            self._paint(self.row_at(y, clamp=True))
            self._check_autoscroll()
        self._set_hover(self.row_at(y))

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton or self._drag is None:
            super().mouseReleaseEvent(ev)
            return
        drag, self._drag = self._drag, None
        self._scroll_timer.stop()
        self._on_apply([i for i in drag.rows if drag.before[i] != drag.target], drag.target)
        ev.accept()

    def leaveEvent(self, ev):
        self._set_hover(None)
        super().leaveEvent(ev)

    def _paint(self, row: int):
        """Preview the range anchor..row in the boxes — signals blocked, the
        stacks only open or close on release — and restore rows that left it."""
        drag = self._drag
        rows = range(min(drag.anchor, row), max(drag.anchor, row) + 1)
        if rows == drag.rows:
            return
        drag.rows = rows
        for i, box in enumerate(self.boxes):
            want = drag.target if i in rows else drag.before[i]
            if box.isChecked() != want:
                box.blockSignals(True)
                box.setChecked(want)
                box.blockSignals(False)

    def _set_hover(self, row: int | None):
        if row != self._hover:
            self._hover = row
            self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._hover is not None and self._hover < len(self.boxes):
            rect = self.boxes[self._hover].geometry().adjusted(0, -1, 0, 1)
            rect.setLeft(0)
            rect.setRight(self.width() - 1)
            QPainter(self).fillRect(rect, QColor(255, 255, 255, 18))

    # ---- auto-scroll while dragging past the edges ---------------------

    def _scroll_area(self) -> QScrollArea | None:
        widget = self.parentWidget()
        while widget is not None and not isinstance(widget, QScrollArea):
            widget = widget.parentWidget()
        return widget

    def _check_autoscroll(self):
        scroll = self._scroll_area()
        if scroll is None:
            return
        y = scroll.viewport().mapFromGlobal(QCursor.pos()).y()
        if 0 <= y < scroll.viewport().height():
            self._scroll_timer.stop()
        elif not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _autoscroll(self):
        scroll = self._scroll_area()
        if self._drag is None or scroll is None:
            self._scroll_timer.stop()
            return
        pos = QCursor.pos()
        height = scroll.viewport().height()
        y = scroll.viewport().mapFromGlobal(pos).y()
        if 0 <= y < height:
            self._scroll_timer.stop()
            return
        # Faster the farther past the edge, like a file manager's list.
        overshoot = -y if y < 0 else y - height + 1
        step = min(4 + overshoot // 3, 48)
        bar = scroll.verticalScrollBar()
        bar.setValue(bar.value() + (step if y >= height else -step))
        self._paint(self.row_at(self.mapFromGlobal(pos).y(), clamp=True))


class _Pages(QStackedWidget):
    """Sized by the page on show rather than the tallest page, so the
    control window fits whichever face of the folder is up."""

    def sizeHint(self):
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self):
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()


class FolderSection(QWidget):
    """A dropped folder: header (icon, name, ✕) plus a checkbox per TIFF.
    Checking opens the stack, unchecking closes it; a drag across rows or a
    Shift+click does a whole range at once (see FileList)."""

    def __init__(self, path: Path, owner: "ControlWindow"):
        super().__init__()
        self.path = path
        self.owner = owner

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(self.style().standardIcon(QStyle.SP_DirIcon).pixmap(16, 16))
        name_label = QLabel(path.name)
        name_label.setStyleSheet("font-weight: bold;")
        name_label.setToolTip(str(path))
        close_button = QToolButton()
        close_button.setText("✕")
        close_button.setToolTip("Remove this folder list (open stacks stay open)")
        close_button.setFocusPolicy(Qt.NoFocus)
        close_button.clicked.connect(lambda: owner.remove_folder_section(self))
        # Appears once a plate layout is attached: flips between the plate
        # and the plain file list (same files, same open/close).
        self.view_button = QToolButton()
        self.view_button.setCheckable(True)
        self.view_button.setFocusPolicy(Qt.NoFocus)
        self.view_button.toggled.connect(self._show_plate)
        self.view_button.hide()
        header.addWidget(icon_label)
        header.addWidget(name_label, 1)
        header.addWidget(self.view_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.list = FileList(self._apply_rows)
        # Keyed by canonical path to match open stacks; opened and labeled as
        # listed, so a symlinked position keeps its own name. Insertion order
        # is the list's row order.
        self.checks: dict[Path, QCheckBox] = {}
        self.listed: dict[Path, Path] = {}
        open_paths = _open_stack_paths()
        for file in viewer.list_tiffs(path):
            key = _canonical(file)
            box = QCheckBox(file.name)
            box.setChecked(key in open_paths)
            # Keyboard (Space) and programmatic toggles; mouse goes through
            # the list, which commits a whole range in one batch.
            box.toggled.connect(lambda on, f=key: self._apply([f], on))
            self.list.add(box)
            self.checks[key] = box
            self.listed[key] = file

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setWidget(self.list)
        self.scroll.setMinimumHeight(80)
        # No height cap: the list stretches with the window, scrolls when short.
        # The list and, once one is attached, the plate layout share this slot.
        self.pages = _Pages()
        self.pages.addWidget(self.scroll)
        layout.addWidget(self.pages, 1)
        self.plate_panel: plate_mod.PlatePanel | None = None

        # Bulk actions under the list: check/uncheck every box in one pass.
        actions = QHBoxLayout()
        actions.setContentsMargins(8, 0, 0, 0)
        actions.setSpacing(4)
        self.open_all_button = QPushButton("Open all")
        self.open_all_button.setToolTip(f"Open every stack in {path.name}")
        self.close_all_button = QPushButton("Close all")
        self.close_all_button.setToolTip(f"Close every open stack from {path.name}")
        for b, slot in (
            (self.open_all_button, self.open_all),
            (self.close_all_button, self.close_all),
        ):
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(slot)
            actions.addWidget(b)
        actions.addStretch(1)
        # The explicit way in (a drop works too): pick a pLayout file for
        # this folder. Gone while a plate is linked — its own row has the ✕.
        self.plate_button = QPushButton("Plate layout…")
        self.plate_button.setToolTip(
            "Link a pLayout .plate file to this folder: its wells open these stacks\n"
            "(dropping a .plate file onto the list does the same)"
        )
        self.plate_button.setFocusPolicy(Qt.NoFocus)
        self.plate_button.clicked.connect(lambda: owner.open_plate_dialog(self))
        actions.addWidget(self.plate_button)
        layout.addLayout(actions)
        self.sync(open_paths)
        # The plate this folder was last shown with comes back with it.
        remembered = _remembered_plate(path)
        if remembered is not None and remembered.is_file():
            try:
                self.attach_plate(remembered)
            except ValueError:
                pass

    def open_all(self):
        """Open every stack in this folder that isn't open yet."""
        self._set_all(True)

    def close_all(self):
        """Close every pane showing a stack from this folder."""
        self._set_all(False)

    def _set_all(self, on: bool):
        self._apply([f for f, box in self.checks.items() if box.isChecked() != on], on)

    def _apply_rows(self, rows: list[int], on: bool):
        keys = list(self.checks)
        self._apply([keys[i] for i in rows], on)

    def _apply(self, files: list[Path], on: bool):
        """Open or close these files as one batch, with the control window's
        own refreshes held off until it is done; the boxes then sync once
        from what is actually open."""
        if not files:
            return
        with self.owner.bulk_update():
            if on:
                viewer.open_paths([self.listed[f] for f in files], self)
            else:
                _close_stacks_by_paths(set(files))
        if len(files) > 1:
            self.owner.statusBar().showMessage(
                f"{'Opened' if on else 'Closed'} {len(files)} stacks · {self.path.name}", 4000
            )

    def sync(self, open_paths: set):
        """Reflect reality: stacks opened/closed elsewhere update the boxes.

        Signals are blocked per box rather than muted with a flag: opening a
        stack refreshes the control window, so this can run nested inside a
        batch and must not re-enable the per-file handler behind its back.
        Rows a held drag is previewing are left to the drag.
        """
        n_open = 0
        painted = self.list.painted_rows()
        for i, (file, box) in enumerate(self.checks.items()):
            is_open = file in open_paths
            n_open += is_open
            if i not in painted and box.isChecked() != is_open:
                box.blockSignals(True)
                box.setChecked(is_open)
                box.blockSignals(False)
        self.open_all_button.setEnabled(n_open < len(self.checks))
        self.close_all_button.setEnabled(n_open > 0)
        if self.plate_panel is not None:
            self.plate_panel.set_open(open_paths)
            self._label_panes()

    # ---- plate layouts -------------------------------------------------

    def attach_plate(self, path: Path) -> "plate_mod.PlatePanel":
        """Show a pLayout .plate file as a face of this folder: its wells map
        to the listed stacks by name (plate.match_files). Replaces a plate
        attached before. Raises ValueError for a file that isn't a layout."""
        doc = plate_mod.load_plate(path)
        if self.plate_panel is not None:
            self.pages.removeWidget(self.plate_panel)
            self.plate_panel.deleteLater()
        panel = plate_mod.PlatePanel(doc)
        panel.set_files(self.listed)
        panel.toggle.connect(self._apply)
        panel.unlink.connect(self.detach_plate)
        self.plate_panel = panel
        self.pages.addWidget(panel)
        self.plate_button.hide()
        self.view_button.show()
        self.view_button.setChecked(True)
        self._show_plate(True)
        _remember_plate(self.path, path)
        self.sync(_open_stack_paths())
        return panel

    def detach_plate(self):
        """Unlink the plate (the ✕ on its row): back to the plain file list,
        and the pairing is forgotten, so neither the folder nor the plate
        brings the other back. Open stacks stay open; their headers drop
        the well conditions."""
        if self.plate_panel is None:
            return
        panel, self.plate_panel = self.plate_panel, None
        plate_path = panel.doc.path
        self.pages.removeWidget(panel)
        panel.deleteLater()
        self.view_button.setChecked(False)
        self.view_button.hide()
        self._show_plate(False)
        self.plate_button.show()
        _forget_plate(self.path, plate_path)
        for pane in viewer._all_panes:
            if pane.stack.path and _canonical(pane.stack.path) in self.checks:
                pane.header_label.setToolTip("")
        self.owner.statusBar().showMessage(f"{plate_path.name} unlinked from {self.path.name}", 4000)
        # Shrink after the layout has processed the swap.
        QTimer.singleShot(0, self.owner.adjustSize)

    def _show_plate(self, on: bool):
        if self.plate_panel is None:
            on = False
        self.pages.setCurrentWidget(self.plate_panel if on else self.scroll)
        if self.plate_panel is not None:
            self.plate_panel.view.fit_height(on)
        self.view_button.setText("☰" if on else "⊞")
        self.view_button.setToolTip("Show the file list" if on else "Show the plate layout")

    def _label_panes(self):
        """Tell every open stack which well it came from: the pane header's
        tooltip reads `B3  ·  Drug A 10 µM  ·  Drug B 0 µM`."""
        for pane in viewer._all_panes:
            if not pane.stack.path:
                continue
            text = self.plate_panel.conditions_for(_canonical(pane.stack.path))
            if text:
                pane.header_label.setToolTip(text)

_instance: "ControlWindow | None" = None


def get_control_window() -> "ControlWindow":
    global _instance
    if _instance is None:
        _instance = ControlWindow()
    return _instance


def refresh_state():
    if _instance is not None:
        _instance.refresh_state()


def bring_to_front():
    """Raise the control window above the app's own stack windows and focus it.

    Clicking the dock icon only brings the *app* forward: within it the stack
    windows keep whatever order they had, so the control window stays buried
    under them. This puts it back on top, un-minimizing it if needed.
    """
    if _instance is None:
        return
    if _instance.isMinimized():
        _instance.setWindowState(_instance.windowState() & ~Qt.WindowMinimized)
    _instance.show()
    _instance.raise_()
    _instance.activateWindow()


class ControlWindow(FileDropMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        from . import __version__

        # >0 while a bulk open/close runs: opening a stack refreshes this
        # window, and refreshing it re-syncs every folder list, so a batch of
        # 48 would otherwise do that work 48 times over.
        self._suspend_refresh = 0

        self.setWindowTitle(f"TIFF Visualizer {__version__.rsplit('.', 1)[0]}")
        self._init_file_drops()
        build_menus(self, viewer.active_pane)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.setCentralWidget(central)

        grid = QGridLayout()
        grid.setSpacing(6)
        layout.addLayout(grid)

        def button(text, tooltip, slot, row, col, colspan=1, checkable=False):
            b = QPushButton(text)
            b.setToolTip(tooltip)
            b.setCheckable(checkable)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(slot)
            grid.addWidget(b, row, col, 1, colspan)
            return b

        button("Open…", "Open TIFF stacks (Cmd+O)", self._open, 0, 0)
        button("Open folder…", "Open all TIFF stacks in a folder (Cmd+Shift+O)", self._open_folder, 0, 1)
        self.save_button = button(
            "Save As…", "Save active stack in ImageJ format (Cmd+S)", self._save, 6, 0
        )
        button("Shortcuts (?)", "Show the keyboard cheatsheet", self._cheatsheet, 6, 1)
        button("Settings…", "App settings: RAM preloading budget (Cmd+,)", self._settings, 7, 0, colspan=2)
        self.bc_button = button(
            "B&&C", "Brightness/Contrast panel (Cmd+Shift+C)", self._bc, 1, 0
        )
        self.project_button = button(
            "Projection…", "Z/T projection of the active stack (Cmd+Shift+P)", self._project, 1, 1
        )
        self.combine_button = button(
            "Combine windows", "Tile all stacks in one window / split back (Cmd+G)",
            self._toggle_combined, 2, 0, colspan=2,
        )
        self.shared_button = button(
            "Shared axes", "One set of c/z/t sliders drives all tiled stacks (Cmd+Shift+G)",
            self._toggle_shared, 3, 0, colspan=2, checkable=True,
        )
        # Selection tools (Fiji's toolbar): one app-wide tool, the menu's
        # Analyze > Selection Tool actions and these buttons mirror each other.
        from . import roi

        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons: dict[str, QToolButton] = {}
        for name in roi.TOOLS:
            b = QToolButton()
            b.setText(roi.TOOL_LABELS[name])
            b.setCheckable(True)
            b.setChecked(name == roi.tool())
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(f"{roi.TOOL_LABELS[name]} tool ({roi.TOOL_KEYS[name]})\n{roi.TOOL_HINTS[name]}")
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            # Fusion draws a checked tool button barely darker; make the
            # active tool unmistakable with the accent color.
            b.setStyleSheet(
                "QToolButton { padding: 3px 6px; border: 1px solid #3a3a3a; border-radius: 3px; }"
                "QToolButton:checked { background: #2f6fd0; color: white; border-color: #2f6fd0; }"
            )
            b.clicked.connect(lambda _checked=False, n=name: roi.set_tool(n))
            self.tool_group.addButton(b)
            self.tool_buttons[name] = b
            tools.addWidget(b)
        grid.addLayout(tools, 4, 0, 1, 2)
        roi.tool_changed().connect(self._on_tool_changed)

        self.fit_button = button("Fit", "Fit image to window (Cmd+0)", self._fit, 5, 0)
        self.actual_button = button(
            "100%", "Actual size, 1 image px = 1 screen px (Cmd+1)", self._actual, 5, 1
        )

        # Dropped-folder sections live between the buttons and the status line;
        # they absorb all extra vertical space when the window is stretched.
        self.folder_sections: dict[str, FolderSection] = {}
        self.folder_layout = QVBoxLayout()
        self.folder_layout.setSpacing(2)
        layout.addLayout(self.folder_layout, 1)

        self.active_label = QLabel()
        self.active_label.setStyleSheet("color: #909090;")
        layout.addWidget(self.active_label)

        QApplication.instance().focusChanged.connect(self._on_focus_changed)
        self.refresh_state()

        from . import settings as app_settings

        saved = app_settings.settings().value("control/geometry")
        self.restored_geometry = saved is not None
        if saved is not None:
            self.restoreGeometry(saved)
            # The saved height may include space stretched out for folder
            # lists, which never exist at startup — keep position and width,
            # compact the height.
            self.resize(self.width(), self.sizeHint().height())

    # ---- actions -------------------------------------------------------

    def _pane(self) -> StackPane | None:
        return viewer.active_pane()

    def _open(self):
        viewer.open_stack_dialog(self)
        self.refresh_state()

    def _open_folder(self):
        viewer.open_folder(self)
        self.refresh_state()

    def _cheatsheet(self):
        viewer.show_cheatsheet(self)

    def _settings(self):
        from .settings_window import show_settings

        show_settings(self)

    # ---- dropped-folder sections ---------------------------------------

    @staticmethod
    def _dropped_dirs(mime) -> list[Path]:
        if not mime.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in mime.urls() if Path(u.toLocalFile()).is_dir()]

    @staticmethod
    def _dropped_plates(mime) -> list[Path]:
        if not mime.hasUrls():
            return []
        paths = [Path(u.toLocalFile()) for u in mime.urls()]
        return [p for p in paths if p.is_file() and plate_mod.is_plate_name(p.name)]

    def dragEnterEvent(self, ev):
        mime = ev.mimeData()
        if self._dropped_dirs(mime) or self._dropped_tiff_paths(mime) or self._dropped_plates(mime):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        handled = False
        added: list[FolderSection] = []
        with self.bulk_update():
            for directory in self._dropped_dirs(ev.mimeData()):
                if (section := self.add_folder_section(directory)) is not None:
                    added.append(section)
                handled = True
            if paths := self._dropped_tiff_paths(ev.mimeData()):
                viewer.open_paths(paths, self)
                handled = True
        # A plate dropped with a folder attaches to that folder; dropped on a
        # section, to that one; else wherever open_plate works out.
        for plate_path in self._dropped_plates(ev.mimeData()):
            target = added[0] if added else self._section_at(ev.position().toPoint())
            self.open_plate(plate_path, target)
            handled = True
        if handled:
            ev.acceptProposedAction()

    def add_folder_section(self, path: Path) -> "FolderSection | None":
        """The section for this folder — made now, or the one already shown.
        None (with a status message) for a folder without TIFFs."""
        key = str(path.resolve())
        if key in self.folder_sections:
            return self.folder_sections[key]
        section = FolderSection(path, self)
        if not section.checks:
            self.statusBar().showMessage(f"No TIFF files in {path.name}", 4000)
            section.deleteLater()
            return None
        self.folder_sections[key] = section
        self.folder_layout.addWidget(section, 1)
        self.adjustSize()
        return section

    def remove_folder_section(self, section: FolderSection):
        from PySide6.QtCore import QTimer

        self.folder_sections.pop(str(section.path.resolve()), None)
        self.folder_layout.removeWidget(section)
        section.hide()
        section.setParent(None)
        section.deleteLater()
        # Shrink after the layout has processed the removal.
        QTimer.singleShot(0, self.adjustSize)

    # ---- plate layouts -------------------------------------------------

    def _section_at(self, point) -> FolderSection | None:
        w = self.childAt(point)
        while w is not None and not isinstance(w, FolderSection):
            w = w.parentWidget()
        return w

    def _section_for_plate(self, path: Path) -> FolderSection | None:
        """Where a plate goes when the drop didn't say: the only section,
        else the folder it was last used with, else one the user picks
        (which may be a section already)."""
        sections = list(self.folder_sections.values())
        if len(sections) == 1:
            return sections[0]
        remembered = _remembered_folder(path)
        if remembered is not None and remembered.is_dir():
            return self.add_folder_section(remembered)
        directory = QFileDialog.getExistingDirectory(
            self, f"Folder of stacks for {path.name}", app_settings.last_dir()
        )
        if not directory:
            return None
        app_settings.set_last_dir(directory)
        return self.add_folder_section(Path(directory))

    def open_plate(self, path: Path, section: FolderSection | None = None):
        """Attach a pLayout .plate file to a folder section (File > Open Plate
        Layout…, a drop, Finder): its wells then open the folder's stacks."""
        path = Path(path)
        if section is None:
            section = self._section_for_plate(path)
            if section is None:
                return
        try:
            panel = section.attach_plate(path)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot read plate layout", str(exc))
            return
        self.adjustSize()
        self.statusBar().showMessage(
            f"{path.name}: {panel.matched()} of {panel.plate.n_wells} wells have a stack"
            f" in {section.path.name}",
            6000,
        )

    def open_plate_dialog(self, section: FolderSection | None = None):
        """File > Open Plate Layout…, or a section's own "Plate layout…"
        button — then the plate goes to that section, no folder question."""
        title = "Open plate layout" if section is None else f"Plate layout for {section.path.name}"
        path, _ = QFileDialog.getOpenFileName(
            self, title, app_settings.last_dir(), "pLayout plate (*.plate);;All files (*)"
        )
        if path:
            app_settings.set_last_dir(str(Path(path).parent))
            self.open_plate(Path(path), section)

    def _save(self):
        if pane := self._pane():
            pane.save_as()

    def _bc(self):
        from .bc_panel import show_bc_panel

        show_bc_panel(self._pane())

    def _project(self):
        if pane := self._pane():
            pane.project()
        self.refresh_state()

    def _toggle_combined(self):
        workspace.toggle_combined()
        self.refresh_state()

    def _toggle_shared(self, checked: bool):
        workspace.set_shared_axes(checked)

    def _fit(self):
        if pane := self._pane():
            pane.zoom_fit()

    def _actual(self):
        if pane := self._pane():
            pane.zoom_actual()

    # ---- state ---------------------------------------------------------

    def _on_focus_changed(self, *_):
        self.refresh_state()

    def _on_tool_changed(self, name: str):
        self.tool_buttons[name].setChecked(True)

    @contextmanager
    def bulk_update(self):
        """Hold off state refreshes until the whole batch is done."""
        self._suspend_refresh += 1
        try:
            yield
        finally:
            self._suspend_refresh -= 1
        self.refresh_state()

    def refresh_state(self):
        if self._suspend_refresh:
            return
        pane = self._pane()
        has_pane = pane is not None
        for b in (self.save_button, self.bc_button, self.project_button,
                  self.fit_button, self.actual_button):
            b.setEnabled(has_pane)
        self.combine_button.setEnabled(has_pane or workspace.workspace_active())
        self.combine_button.setText(
            "Split windows" if workspace.workspace_active() else "Combine windows"
        )
        self.shared_button.setChecked(workspace.shared_axes())
        self.active_label.setText(f"Active: {pane.stack.name}" if pane else "No stack open")
        open_paths = _open_stack_paths()
        for section in self.folder_sections.values():
            section.sync(open_paths)

    def closeEvent(self, ev):
        from . import settings as app_settings

        app_settings.settings().setValue("control/geometry", self.saveGeometry())
        QApplication.instance().quit()
        super().closeEvent(ev)
