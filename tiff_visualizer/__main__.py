"""Entry point: `tiffviz [stack.tif ...]` or `python -m tiff_visualizer [stack.tif ...]`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import control_panel
from .control_panel import get_control_window
from .theme import apply_dark_theme
from .viewer import open_paths


class ReopenDetector:
    """Tells a dock-icon click apart from every other way the app comes forward.

    macOS answers a dock click with `applicationShouldHandleReopen:`, which Qt
    force-delivers as an ApplicationStateChange even when the state is already
    Active. So a dock click reports Active twice — once for the activation
    itself, once forced right after — or once while the app never left the
    front, whereas cmd-tab and clicking one of our windows report it once.
    A second Active with no Inactive in between is therefore the reopen.
    """

    def __init__(self):
        self.active = False

    def saw_state(self, active: bool) -> bool:
        """Feed every ApplicationStateChange; True means the dock icon was clicked."""
        reopened = active and self.active
        self.active = active
        return reopened


class OpenBatcher:
    """Finder and the Dock hand a multi-file open over as one FileOpen event
    per file. Gather the burst and open it in one go — one grid relayout, one
    error report — instead of stack by stack."""

    def __init__(self, open_many, delay_ms: int = 100):
        self._paths: list[str] = []
        self._open_many = open_many
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self.flush)

    def add(self, path: str):
        self._paths.append(path)
        self._timer.start()  # restarted by each file, so it fires once the burst ends

    def flush(self):
        paths, self._paths = self._paths, []
        if paths:
            self._open_many(paths)


def open_batch(paths):
    """Open files handed to the app (argv, Finder, the Dock) as one batch.
    Absolute, so a relative path still says where the file is; the folder
    lists compare resolved paths, so symlinks match too."""
    with get_control_window().bulk_update():
        open_paths([Path(os.path.abspath(p)) for p in paths])


class TiffApplication(QApplication):
    """Handles macOS FileOpen events (e.g. `open -a "TIFF Visualizer" x.tif`)
    and dock-icon clicks, which raise the control window back to the front."""

    def __init__(self, argv):
        super().__init__(argv)
        self._reopen = ReopenDetector()
        self._file_opens = OpenBatcher(open_batch)

    def event(self, ev):
        if ev.type() == QEvent.Type.FileOpen and ev.file():
            self._file_opens.add(ev.file())
            return True
        if ev.type() == QEvent.Type.ApplicationStateChange:
            active = self.applicationState() == Qt.ApplicationState.ApplicationActive
            if self._reopen.saw_state(active):
                control_panel.bring_to_front()
        return super().event(ev)


def main() -> int:
    app = TiffApplication(sys.argv)
    app.setApplicationDisplayName("TIFF Visualizer")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "icon.png")))
    apply_dark_theme(app)
    from .settings_window import init_font

    init_font()

    control = get_control_window()
    if not control.restored_geometry:
        control.move(40, 60)
    control.show()

    def autosave_session():
        from .session import save_last_session

        save_last_session()

    app.aboutToQuit.connect(autosave_session)

    open_batch(app.arguments()[1:])  # refreshes the control window when done

    from . import updater

    updater.check_on_launch()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
