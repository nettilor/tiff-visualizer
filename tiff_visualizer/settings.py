"""Persistent app settings (QSettings)."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QComboBox, QSpinBox, QWidget


def settings() -> QSettings:
    return QSettings("SwartzLab", "TIFF Visualizer")


def restore_widgets(group: str, widgets: dict[str, QWidget]):
    """Reopen a dialog the way it was last accepted: apply the values saved
    by save_widgets(group, …) to these widgets, in dict order. Values that
    no longer fit — a combo entry this stack doesn't offer, a spin value
    outside its range — are skipped or clamped, so the dialog always opens
    in a valid state."""
    s = settings()
    for name, widget in widgets.items():
        key = f"dialogs/{group}/{name}"
        if not s.contains(key):
            continue
        if isinstance(widget, QComboBox):
            index = widget.findText(s.value(key, type=str))
            if index >= 0:
                widget.setCurrentIndex(index)
        elif isinstance(widget, QSpinBox):
            widget.setValue(s.value(key, type=int))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(s.value(key, type=bool))


def save_widgets(group: str, widgets: dict[str, QWidget]):
    """Remember these widgets' current values for restore_widgets. Pass only
    the widgets the user could actually choose from, so a stack that lacks
    an axis doesn't overwrite the remembered choice with a forced default;
    a combo left with a single entry is skipped for the same reason."""
    s = settings()
    for name, widget in widgets.items():
        key = f"dialogs/{group}/{name}"
        if isinstance(widget, QComboBox):
            if widget.count() > 1:
                s.setValue(key, widget.currentText())
        elif isinstance(widget, QSpinBox):
            s.setValue(key, widget.value())
        elif isinstance(widget, QCheckBox):
            s.setValue(key, widget.isChecked())


def last_dir() -> str:
    return settings().value("lastDir", "", type=str)


def set_last_dir(path: str):
    settings().setValue("lastDir", path)
