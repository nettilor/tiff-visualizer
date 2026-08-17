"""The app settings dialog (Cmd+,)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
)

from . import preload
from . import settings as app_settings
from . import updater

_default_font_size: int | None = None


def init_font():
    """Capture the platform default and apply any saved text size (startup)."""
    global _default_font_size
    app = QApplication.instance()
    size = app.font().pointSize()
    _default_font_size = size if size > 0 else 13
    saved = app_settings.settings().value("ui/fontSize", 0, type=int)
    if saved > 0:
        apply_font_size(saved)


def apply_font_size(size: int):
    app = QApplication.instance()
    font = app.font()
    font.setPointSize(size)
    app.setFont(font)


def show_settings(parent=None):
    SettingsDialog(parent).exec()


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        form = QFormLayout(self)

        self.preload_box = QCheckBox("Preload stacks into RAM when they fit")
        self.preload_box.setToolTip(
            "Copies opened stacks from disk into memory (within the budget)\n"
            "so playback and scrubbing never wait on the disk."
        )
        self.preload_box.setChecked(preload.enabled())
        self.preload_box.toggled.connect(self._apply)
        form.addRow(self.preload_box)

        self.budget_spin = QSpinBox()
        self.budget_spin.setRange(1, 128)
        self.budget_spin.setSuffix(" GB")
        self.budget_spin.setValue(preload.budget_gb())
        self.budget_spin.valueChanged.connect(self._apply)
        form.addRow("RAM budget:", self.budget_spin)

        self.usage_label = QLabel()
        form.addRow(self.usage_label)
        note = QLabel("Stacks over budget stay memory-mapped (still fully usable).")
        note.setStyleSheet("color: #909090;")
        form.addRow(note)

        font_row = QHBoxLayout()
        self.font_spin = QSpinBox()
        self.font_spin.setRange(9, 24)
        self.font_spin.setSuffix(" pt")
        current = QApplication.instance().font().pointSize()
        self.font_spin.setValue(current if current > 0 else 13)
        self.font_spin.valueChanged.connect(self._apply_font)
        font_row.addWidget(self.font_spin)
        reset_button = QPushButton("Default")
        reset_button.setToolTip(f"Back to the system size ({_default_font_size} pt)")
        reset_button.clicked.connect(
            lambda: self.font_spin.setValue(_default_font_size or 13)
        )
        font_row.addWidget(reset_button)
        font_row.addStretch(1)
        form.addRow("Text size:", font_row)

        self.updates_box = QCheckBox("Check for updates automatically")
        self.updates_box.setToolTip(
            "Looks at the GitHub releases page once a day and only speaks up\n"
            "when a newer version exists. The only network request this app makes.\n"
            "File > Check for Updates… asks on demand either way."
        )
        self.updates_box.setChecked(updater.auto_check_enabled())
        self.updates_box.toggled.connect(updater.set_auto_check_enabled)
        form.addRow(self.updates_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        form.addRow(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_usage)
        self._timer.start(500)
        self._update_usage()

    def _apply(self, *_):
        s = app_settings.settings()
        s.setValue("preload/enabled", self.preload_box.isChecked())
        s.setValue("preload/gb", self.budget_spin.value())
        if self.preload_box.isChecked():
            preload.preload_existing()
        self._update_usage()

    def _apply_font(self, size: int):
        apply_font_size(size)
        app_settings.settings().setValue("ui/fontSize", size)

    def _update_usage(self):
        in_ram, total = preload.stack_counts()
        gb = preload.loaded_bytes() / 1024**3
        self.usage_label.setText(
            f"In RAM now: {gb:.2f} GB of {preload.budget_gb()} GB budget "
            f"({in_ram} of {total} open stacks)"
        )
