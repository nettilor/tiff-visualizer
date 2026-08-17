"""Entry point: `tiffviz [stack.tif ...]` or `python -m tiff_visualizer [stack.tif ...]`."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .control_panel import get_control_window
from .theme import apply_dark_theme
from .viewer import open_path


class TiffApplication(QApplication):
    """Handles macOS FileOpen events (e.g. `open -a "TIFF Visualizer" x.tif`)."""

    def event(self, ev):
        if ev.type() == QEvent.Type.FileOpen and ev.file():
            open_path(ev.file())
            return True
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

    for path in app.arguments()[1:]:
        open_path(path)
    control.refresh_state()

    from . import updater

    updater.check_on_launch()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
