"""Persistent app settings (QSettings)."""

from __future__ import annotations

from PySide6.QtCore import QSettings


def settings() -> QSettings:
    return QSettings("SwartzLab", "TIFF Visualizer")


def last_dir() -> str:
    return settings().value("lastDir", "", type=str)


def set_last_dir(path: str):
    settings().setValue("lastDir", path)
