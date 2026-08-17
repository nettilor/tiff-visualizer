"""The main control window: Fiji-style command central, no shortcuts required.

A small always-open window with buttons for every main action, targeting the
active stack (last clicked). Closing it quits the app.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import viewer, workspace
from .viewer import FileDropMixin, StackPane, build_menus


def _close_stack_by_path(path: Path):
    """Close every open pane showing this file, wherever it lives."""
    for pane in list(viewer._all_panes):
        if pane.stack.path == path:
            ws = workspace._workspace
            if ws is not None and pane in ws.panes:
                ws.close_pane(pane)
            else:
                window = next((w for w in viewer._open_windows if w.pane is pane), None)
                if window is not None:
                    window.close()


class FolderSection(QWidget):
    """A dropped folder: header (icon, name, ✕) plus a checkbox per TIFF.
    Checking opens the stack, unchecking closes it."""

    def __init__(self, path: Path, owner: "ControlWindow"):
        super().__init__()
        self.path = path
        self.owner = owner
        self._syncing = False

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
        header.addWidget(icon_label)
        header.addWidget(name_label, 1)
        header.addWidget(close_button)
        layout.addLayout(header)

        files_widget = QWidget()
        files_layout = QVBoxLayout(files_widget)
        files_layout.setContentsMargins(8, 0, 0, 0)
        files_layout.setSpacing(2)
        self.checks: dict[Path, QCheckBox] = {}
        open_paths = {p.stack.path for p in viewer._all_panes}
        for file in sorted(path.iterdir()):
            if file.suffix.lower() not in (".tif", ".tiff"):
                continue
            box = QCheckBox(file.name)
            box.setChecked(file in open_paths)
            box.toggled.connect(lambda on, f=file: self._on_toggled(f, on))
            files_layout.addWidget(box)
            self.checks[file] = box
        files_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setWidget(files_widget)
        self.scroll.setMinimumHeight(80)
        # No height cap: the list stretches with the window, scrolls when short.
        layout.addWidget(self.scroll, 1)

    def _on_toggled(self, file: Path, on: bool):
        if self._syncing:
            return
        if on:
            viewer.open_path(file, self)
        else:
            _close_stack_by_path(file)
        self.owner.refresh_state()

    def sync(self, open_paths: set):
        """Reflect reality: stacks opened/closed elsewhere update the boxes."""
        self._syncing = True
        try:
            for file, box in self.checks.items():
                box.setChecked(file in open_paths)
        finally:
            self._syncing = False

_instance: "ControlWindow | None" = None


def get_control_window() -> "ControlWindow":
    global _instance
    if _instance is None:
        _instance = ControlWindow()
    return _instance


def refresh_state():
    if _instance is not None:
        _instance.refresh_state()


class ControlWindow(FileDropMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        from . import __version__

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
            "Save As…", "Save active stack in ImageJ format (Cmd+S)", self._save, 5, 0
        )
        button("Shortcuts (?)", "Show the keyboard cheatsheet", self._cheatsheet, 5, 1)
        button("Settings…", "App settings: RAM preloading budget (Cmd+,)", self._settings, 6, 0, colspan=2)
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
        self.fit_button = button("Fit", "Fit image to window (Cmd+0)", self._fit, 4, 0)
        self.actual_button = button(
            "100%", "Actual size, 1 image px = 1 screen px (Cmd+1)", self._actual, 4, 1
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

    def dragEnterEvent(self, ev):
        if self._dropped_dirs(ev.mimeData()) or self._dropped_tiff_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        handled = False
        for directory in self._dropped_dirs(ev.mimeData()):
            self.add_folder_section(directory)
            handled = True
        for path in self._dropped_tiff_paths(ev.mimeData()):
            viewer.open_path(path, self)
            handled = True
        if handled:
            ev.acceptProposedAction()
        self.refresh_state()

    def add_folder_section(self, path: Path):
        key = str(path.resolve())
        if key in self.folder_sections:
            return
        section = FolderSection(path, self)
        if not section.checks:
            self.statusBar().showMessage(f"No TIFF files in {path.name}", 4000)
            section.deleteLater()
            return
        self.folder_sections[key] = section
        self.folder_layout.addWidget(section, 1)
        self.adjustSize()

    def remove_folder_section(self, section: FolderSection):
        from PySide6.QtCore import QTimer

        self.folder_sections.pop(str(section.path.resolve()), None)
        self.folder_layout.removeWidget(section)
        section.hide()
        section.setParent(None)
        section.deleteLater()
        # Shrink after the layout has processed the removal.
        QTimer.singleShot(0, self.adjustSize)

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

    def refresh_state(self):
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
        open_paths = {p.stack.path for p in viewer._all_panes}
        for section in self.folder_sections.values():
            section.sync(open_paths)

    def closeEvent(self, ev):
        from . import settings as app_settings

        app_settings.settings().setValue("control/geometry", self.saveGeometry())
        QApplication.instance().quit()
        super().closeEvent(ev)
