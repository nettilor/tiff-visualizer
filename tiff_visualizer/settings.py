"""Persistent app settings (QSettings)."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QComboBox, QSpinBox, QWidget


def settings() -> QSettings:
    return QSettings("SwartzLab", "TIFF Visualizer")


def _combo_entry(combo: QComboBox, index: int) -> str:
    """What a combo entry is remembered by: its data when it has any, so an
    entry whose label changes — the stack montage's projection names the
    pane's method — is still found; else its text."""
    data = combo.itemData(index)
    return combo.itemText(index) if data is None else str(data)


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
            saved = s.value(key, type=str)
            entries = [_combo_entry(widget, i) for i in range(widget.count())]
            if saved in entries:
                widget.setCurrentIndex(entries.index(saved))
            elif (index := widget.findText(saved)) >= 0:  # saved by label before
                widget.setCurrentIndex(index)
        elif isinstance(widget, QSpinBox):
            widget.setValue(s.value(key, type=int))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(s.value(key, type=bool))
        elif hasattr(widget, "restore_text"):  # viewer.AxisRange
            widget.restore_text(s.value(key, type=str))


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
                s.setValue(key, _combo_entry(widget, widget.currentIndex()))
        elif isinstance(widget, QSpinBox):
            s.setValue(key, widget.value())
        elif isinstance(widget, QCheckBox):
            s.setValue(key, widget.isChecked())
        elif hasattr(widget, "remembered_text"):  # viewer.AxisRange
            s.setValue(key, widget.remembered_text())


def last_dir() -> str:
    return settings().value("lastDir", "", type=str)


def set_last_dir(path: str):
    settings().setValue("lastDir", path)
