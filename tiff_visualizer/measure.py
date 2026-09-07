"""Intensity measurements (Fiji's Analyze > Measure, Cmd+M) and the table
that collects them.

A measurement is the mean (MFI), min and max of one channel of one stack
inside the pane's selection (the whole image without one) at the pane's
current position — the projected plane when the pane's z projection is on. Every measurement is appended to one
app-wide list shown in the floating Measurements window, from which rows
can be copied tab-separated for a spreadsheet or saved as CSV.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import settings as app_settings

COLUMNS = ("#", "Stack", "c", "z", "t", "ROI", "Area", "Mean", "Min", "Max")
_NUMERIC = {0, 2, 3, 4, 6, 7, 8, 9}


def _fmt(value: float, integer: bool) -> str:
    """Fiji's table convention: whole numbers plain, everything else 3 decimals."""
    return f"{value:.0f}" if integer else f"{value:.3f}"


@dataclass
class Measurement:
    stack: str
    c: int  # 1-based, as the header shows them
    z: str  # slice number (1-based) or the projection abbreviation ("MIP")
    t: int  # 1-based
    roi: str  # "whole image" or e.g. "rect 120,80 64×64" (shape + bounding box)
    area: int  # pixels measured
    mean: float
    min: float
    max: float
    integer: bool = True  # min/max are whole numbers (integer data or Max/Min/Median projection)

    def where(self) -> str:
        return f"c {self.c} · z {self.z} · t {self.t}"

    def summary(self) -> str:
        return (
            f"mean {self.mean:.3f}, min {_fmt(self.min, self.integer)},"
            f" max {_fmt(self.max, self.integer)} over {self.area} px"
        )

    def cells(self) -> tuple[str, ...]:
        return (
            self.stack,
            str(self.c),
            self.z,
            str(self.t),
            self.roi,
            str(self.area),
            f"{self.mean:.3f}",
            _fmt(self.min, self.integer),
            _fmt(self.max, self.integer),
        )


_measurements: list[Measurement] = []
_table: "MeasurementsTable | None" = None


def measurements() -> list[Measurement]:
    return list(_measurements)


def record(measurement: Measurement):
    """Append a measurement and make sure the table is on screen. The
    table never takes focus here, so the next Cmd+M still hits the stack."""
    table = show_table(activate=False)  # a fresh table builds itself from the list
    _measurements.append(measurement)
    table.append_row(measurement)


def clear():
    _measurements.clear()
    if _table is not None:
        _table.rebuild()


def remove(indices) -> int:
    """Drop the measurements at these (0-based) row indices; returns how many."""
    doomed = {i for i in indices if 0 <= i < len(_measurements)}
    if doomed:
        _measurements[:] = [m for i, m in enumerate(_measurements) if i not in doomed]
        if _table is not None:
            _table.rebuild()
    return len(doomed)


def rows(indices=None) -> list[tuple[str, ...]]:
    """Table rows (numbered) for all measurements or a subset of row indices."""
    chosen = range(len(_measurements)) if indices is None else sorted(indices)
    return [(str(i + 1), *_measurements[i].cells()) for i in chosen]


def to_tsv(indices=None) -> str:
    """Tab-separated text with a header line — what a spreadsheet expects on paste."""
    return "\n".join("\t".join(r) for r in (COLUMNS, *rows(indices)))


def save_csv(path: str | Path):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        writer.writerows(rows())


def show_table(activate: bool = True) -> "MeasurementsTable":
    global _table
    if _table is None:
        _table = MeasurementsTable()
    _table.show()
    _table.raise_()
    if activate:
        _table.activateWindow()
    return _table


def _status(message: str, msecs: int = 4000):
    from .viewer import _show_status

    _show_status(message, msecs)


class _Table(QTableWidget):
    """The grid itself; Cmd+C copies whole rows (not one cell, Qt's default),
    Delete drops the selected rows and Cmd+M measures the active stack."""

    def __init__(self, owner: "MeasurementsTable"):
        super().__init__(0, len(COLUMNS))
        self._owner = owner

    def keyPressEvent(self, ev):
        if ev.matches(QKeySequence.Copy):
            self._owner.copy_rows()
        elif ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self._owner.delete_rows()
        elif ev.key() == Qt.Key_M and ev.modifiers() & Qt.ControlModifier:
            from .viewer import active_pane

            pane = active_pane()
            if pane is not None:
                pane.measure()
        else:
            super().keyPressEvent(ev)


class MeasurementsTable(QWidget):
    """Fiji's Results window: one row per measurement, oldest first."""

    def __init__(self):
        super().__init__(None, Qt.Tool)
        self.setWindowTitle("Measurements")
        # Cmd+M pops the table up next to the stacks without stealing focus.
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._restored_geometry = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.table = _Table(self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setHighlightSections(False)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip(
            "Copy the selected rows (all rows when none are selected) as\n"
            "tab-separated text with a header line, ready to paste into a\n"
            "spreadsheet (Cmd+C)"
        )
        self.copy_button.clicked.connect(self.copy_rows)
        self.save_button = QPushButton("Save As…")
        self.save_button.setToolTip("Save all rows as a CSV file")
        self.save_button.clicked.connect(self.save_as)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Remove every measurement\n(Delete removes just the selected rows)")
        self.clear_button.clicked.connect(self.clear_all)
        for b in (self.copy_button, self.save_button, self.clear_button):
            b.setFocusPolicy(Qt.NoFocus)
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.count_label = QLabel()
        self.count_label.setStyleSheet("color: #909090;")
        buttons.addWidget(self.count_label)
        layout.addLayout(buttons)
        self.rebuild()

    # ---- rows ----------------------------------------------------------

    def append_row(self, measurement: Measurement):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._fill_row(row, (str(row + 1), *measurement.cells()))
        self.table.scrollToBottom()
        self._update_state()

    def rebuild(self):
        self.table.setRowCount(0)
        for r in rows():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._fill_row(row, r)
        self._update_state()

    def _fill_row(self, row: int, cells):
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col in _NUMERIC:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, col, item)

    def _update_state(self):
        n = self.table.rowCount()
        self.count_label.setText(f"{n} measurement{'s' if n != 1 else ''}")
        for b in (self.copy_button, self.save_button, self.clear_button):
            b.setEnabled(n > 0)

    def selected_rows(self) -> list[int]:
        return sorted(i.row() for i in self.table.selectionModel().selectedRows())

    # ---- actions -------------------------------------------------------

    def copy_rows(self):
        if not _measurements:
            return
        selected = self.selected_rows()
        QApplication.clipboard().setText(to_tsv(selected or None))
        n = len(selected) if selected else len(_measurements)
        _status(f"Copied {n} measurement{'s' if n != 1 else ''} to the clipboard")

    def delete_rows(self):
        removed = remove(self.selected_rows())
        if removed:
            _status(f"Removed {removed} measurement{'s' if removed != 1 else ''}")

    def clear_all(self):
        if _measurements and (
            QMessageBox.question(
                self,
                "Clear measurements",
                f"Remove all {len(_measurements)} measurements?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            != QMessageBox.Yes
        ):
            return
        clear()

    def save_as(self):
        if not _measurements:
            return
        start = Path(app_settings.last_dir() or Path.home()) / "Measurements.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save measurements as CSV", str(start), "CSV (*.csv)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        app_settings.set_last_dir(str(Path(path).parent))
        try:
            save_csv(path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        _status(f"Saved {len(_measurements)} measurements to {path}", 5000)

    # ---- window --------------------------------------------------------

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._restored_geometry:
            self._restored_geometry = True
            saved = app_settings.settings().value("measure/geometry")
            if saved is not None:
                self.restoreGeometry(saved)
            else:
                self.resize(640, 320)

    def hideEvent(self, ev):
        app_settings.settings().setValue("measure/geometry", self.saveGeometry())
        super().hideEvent(ev)
