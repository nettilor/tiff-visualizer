"""pLayout plate layouts as a face of a folder list.

pLayout saves plain JSON `.plate` files: factors (each with coloured levels)
and plates, each holding one row-major array of level IDs per factor. Its XY
Position Fill names wells the way a Keyence microscope numbers its imaging
positions — an "XY" factor whose levels run XY01, XY02, … — and the stack
builder names each hyperstack by that position (XY05.tif). So a plate and a
folder of stacks map onto each other by name alone: nothing here depends on
pLayout's code, and pLayout never needs to know.

PlatePanel is the plate as it appears in a folder section of the control
window: wells coloured by one factor, or by replicate group (wells sharing
the chosen factors' values); a click on a well opens or closes its stack, a
click on a replicate group opens them all.
"""

from __future__ import annotations

import colorsys
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

REPLICATES = "replicates"  # the colour mode next to the factor ids
_MOD = "Cmd" if sys.platform == "darwin" else "Ctrl"  # the single-well click
XY_FACTOR_NAME = "XY"  # what pLayout's XY Position Fill calls its factor
_XY_TOKEN = re.compile(r"(?<![A-Za-z0-9])XY(\d+)(?![0-9])", re.IGNORECASE)
# A well label on its own in a file name (B02, H12, AF24) — "P06-022" in a
# project code is not well P6.
_WELL_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,2})(\d{1,2})(?![0-9-])")


def is_plate_name(name: str) -> bool:
    return name.lower().endswith(".plate") and not name.startswith(".")


def xy_number(text: str) -> int | None:
    """The imaging-position number in XY05, xy5 or 2026_XY05_Z001 — or None."""
    m = _XY_TOKEN.search(text or "")
    return int(m.group(1)) if m else None


def row_label(row: int) -> str:
    """0 → A, 25 → Z, 26 → AA (1536-well plates run A..AF), like pLayout."""
    out, n = "", row
    while True:
        out = chr(65 + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def row_index(label: str) -> int | None:
    s = label.strip().upper()
    if not s or not all("A" <= ch <= "Z" for ch in s):
        return None
    n = 0
    for ch in s:
        n = n * 26 + ord(ch) - 64
    return n - 1


def well_label(row: int, col: int) -> str:
    return f"{row_label(row)}{col + 1}"


# ---- the document ------------------------------------------------------


@dataclass(frozen=True)
class Level:
    id: str
    name: str
    color: str  # "#RRGGBB"


@dataclass
class Factor:
    id: str
    name: str
    unit: str
    levels: dict[str, Level]  # by id, in file order
    # pLayout's right-click → "Hide in Overview": left out of its Overview
    # text and grouping, and saved with the document — so left out of the
    # replicate grouping here by default too.
    hidden_in_overview: bool = False

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.unit})" if self.unit else self.name

    @property
    def is_xy(self) -> bool:
        """pLayout's imaging-position factor: named XY, or every level an XY number."""
        if self.name.strip().upper() == XY_FACTOR_NAME:
            return True
        return bool(self.levels) and all(xy_number(lv.name) is not None for lv in self.levels.values())


@dataclass
class Plate:
    name: str
    rows: int
    cols: int
    assignments: dict[str, list[str | None]]  # factor id → level id per well, row-major
    notes: dict[int, str]  # well index → note

    @property
    def n_wells(self) -> int:
        return self.rows * self.cols

    def level_id(self, factor_id: str, well: int) -> str | None:
        values = self.assignments.get(factor_id)
        return values[well] if values and well < len(values) else None


@dataclass
class PlateLayout:
    path: Path
    factors: list[Factor]
    plates: list[Plate]

    def factor(self, factor_id: str) -> Factor | None:
        return next((f for f in self.factors if f.id == factor_id), None)

    def xy_factor(self) -> Factor | None:
        return next((f for f in self.factors if f.is_xy), None)

    def level(self, factor: Factor, plate: Plate, well: int) -> Level | None:
        lid = plate.level_id(factor.id, well)
        return factor.levels.get(lid) if lid else None

    def conditions(self, plate: Plate, well: int) -> list[tuple[Factor, Level]]:
        """Every non-XY factor's value in this well, in factor order."""
        out = []
        for f in self.factors:
            if f.is_xy:
                continue
            level = self.level(f, plate, well)
            if level is not None:
                out.append((f, level))
        return out

    def conditions_text(self, plate: Plate, well: int) -> str:
        """`Drug A 10 µM · Drug B 0 µM` — what the well holds, in one line."""
        return "  ·  ".join(
            f"{f.name} {lv.name}{' ' + f.unit if f.unit else ''}" for f, lv in self.conditions(plate, well)
        )


def _hex(value) -> str:
    s = str(value or "").strip()
    return s if re.fullmatch(r"#[0-9A-Fa-f]{6}", s) else "#808080"


def load_plate(path: str | Path) -> PlateLayout:
    """Read a .plate file. Lenient like pLayout's own decoder: missing fields
    take defaults, so a layout from a newer pLayout still opens."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name}: not a pLayout file ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("plates"), list):
        raise ValueError(f"{path.name}: not a pLayout file (no plates)")
    factors = []
    for f in data.get("factors") or []:
        if not isinstance(f, dict):
            continue
        levels = {}
        for lv in f.get("levels") or []:
            if isinstance(lv, dict):
                lid = str(lv.get("id", ""))
                levels[lid] = Level(lid, str(lv.get("name", "")), _hex(lv.get("colorHex")))
        factors.append(
            Factor(
                str(f.get("id", "")),
                str(f.get("name", "")),
                str(f.get("unit") or ""),
                levels,
                hidden_in_overview=f.get("hiddenInOverview") is True,
            )
        )
    plates = []
    for p in data["plates"]:
        if not isinstance(p, dict):
            continue
        fmt = p.get("format") or {}
        try:
            rows, cols = int(fmt.get("rows", 0)), int(fmt.get("cols", 0))
        except (TypeError, ValueError):
            continue
        if rows <= 0 or cols <= 0:
            continue
        n = rows * cols
        assignments = {}
        for fid, values in (p.get("assignments") or {}).items():
            vals = [v if isinstance(v, str) else None for v in (values or [])][:n]
            assignments[str(fid)] = vals + [None] * (n - len(vals))
        notes = {}
        for k, v in (p.get("wellNotes") or {}).items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n and isinstance(v, str) and v.strip():
                notes[idx] = v.strip()
        plates.append(Plate(str(p.get("name") or f"Plate {len(plates) + 1}"), rows, cols, assignments, notes))
    if not plates:
        raise ValueError(f"{path.name}: no plates in this layout")
    return PlateLayout(path, factors, plates)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", text) if t]


def _plate_number(doc: PlateLayout, plate: Plate) -> int:
    """`Plate 2` → 2, `p1` → 1, an unnumbered name → its 1-based position."""
    m = re.search(r"\d+", plate.name)
    return int(m.group()) if m else doc.plates.index(plate) + 1


def file_plate(doc: PlateLayout, stem: str) -> Plate | None:
    """Which of the document's plates a file name says it belongs to, or
    None when it names none. A plate's name as a run of tokens in the file
    name wins (`p2_XY01`, `Plate_1_XY01`); else `plate3`/`p3` by number, but
    only a number the document has plates for, so the `P06` of a project
    code never counts."""
    if len(doc.plates) < 2:
        return None
    tokens = _tokens(stem)
    for plate in doc.plates:
        name = _tokens(plate.name)
        if name and any(tokens[i : i + len(name)] == name for i in range(len(tokens) - len(name) + 1)):
            return plate
    for t in tokens:
        if (m := re.fullmatch(r"(?:plate|p)(\d+)", t)) and 1 <= int(m.group(1)) <= len(doc.plates):
            n = int(m.group(1))
            return next((p for p in doc.plates if _plate_number(doc, p) == n), None)
    return None


def match_files(doc: PlateLayout, plate: Plate, files: dict) -> tuple[dict[int, list], str]:
    """Which listed files belong to each well: ({well index: [keys]}, how).

    `files` maps a key (the folder list's canonical path) to the listed path,
    whose name is what gets matched. Wells go by their XY level when the
    plate has one (XY5 and XY05 agree; several files on one position all
    belong to the well), else by a well label in the file name (B02.tif).
    In a layout with several plates, a file that names a plate (`p2_XY01`)
    counts only for that plate; files naming none stand in where no file
    names this one.
    """
    by_xy: dict[int, list] = {}
    by_xy_untagged: dict[int, list] = {}
    by_well: dict[tuple[int, int], list] = {}
    by_well_untagged: dict[tuple[int, int], list] = {}
    for key, path in files.items():
        stem = Path(path).stem
        owner = file_plate(doc, stem)
        if owner is not None and owner is not plate:
            continue
        xy_table, well_table = (by_xy, by_well) if owner is plate else (by_xy_untagged, by_well_untagged)
        if (n := xy_number(stem)) is not None:
            xy_table.setdefault(n, []).append(key)
        if m := _WELL_TOKEN.search(stem):
            r = row_index(m.group(1))
            if r is not None:
                well_table.setdefault((r, int(m.group(2)) - 1), []).append(key)
    xy = doc.xy_factor()
    use_xy = xy is not None and any(plate.assignments.get(xy.id) or [])
    out: dict[int, list] = {}
    for well in range(plate.n_wells):
        if use_xy:
            level = doc.level(xy, plate, well)
            n = xy_number(level.name) if level else None
            keys = (by_xy.get(n) or by_xy_untagged.get(n, [])) if n is not None else []
        else:
            rc = divmod(well, plate.cols)
            keys = by_well.get(rc) or by_well_untagged.get(rc, [])
        if keys:
            out[well] = list(keys)
    return out, ("XY position" if use_xy else "well name")


def default_group_factors(doc: PlateLayout) -> list[str]:
    """The factors that make wells replicates of each other until the user
    says otherwise: every factor pLayout shows in its Overview — so one hidden
    there (right-click → Hide in Overview) stays out here — except the XY
    position, which would make every well its own group. All of them when
    that leaves none."""
    shown = [f.id for f in doc.factors if not f.is_xy and not f.hidden_in_overview]
    return shown or [f.id for f in doc.factors if not f.is_xy] or [f.id for f in doc.factors]


def _group_color(index: int) -> QColor:
    """Distinct hues for replicate groups: golden-angle steps, so neighbours
    in numbering never look alike."""
    r, g, b = colorsys.hsv_to_rgb((index * 0.381966) % 1.0, 0.58, 0.84)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


# ---- the widget --------------------------------------------------------


class PlateView(QWidget):
    """The wells. Each well is a toggle for the stack(s) mapped to it: a
    press-and-drag covers a rectangle, like a drag across the file list.
    In replicate mode a click takes the whole group; Cmd+click (Ctrl+click
    on Windows, Alt+click anywhere) takes the one well."""

    toggle = Signal(list, bool)  # (file keys, open?)
    MAX_CELL = 44.0
    MAX_TEXT_PT = 12.0
    MIN_TEXT_PT = 7.0  # smaller than this is not readable: the label falls back

    def __init__(self, doc: PlateLayout, parent: QWidget | None = None):
        super().__init__(parent)
        self.doc = doc
        self.plate = doc.plates[0]
        self.files: dict[int, list] = {}  # well → file keys
        self.names: dict = {}  # file key → shown name
        self.wells_of: dict = {}  # file key → wells
        self.open_keys: set = set()
        self.mode: str = REPLICATES
        self.group_factors: list[str] = default_group_factors(doc)
        self._groups: dict[int, int] = {}  # well → replicate group number
        self._hover: int | None = None
        self._anchor: int | None = None
        self._single = False
        self._on = True
        self._preview: set[int] = set()
        self._fits = True
        self._laid_out = False
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(180)
        self._regroup()

    # ---- state ---------------------------------------------------------

    def set_plate(self, index: int):
        self.plate = self.doc.plates[index]
        self._regroup()
        self.updateGeometry()
        self.update()

    def set_files(self, files: dict[int, list], names: dict):
        self.files = files
        self.names = names
        self.wells_of = {}
        for well, keys in files.items():
            for k in keys:
                self.wells_of.setdefault(k, []).append(well)
        self.update()

    def set_open(self, open_keys: set):
        self.open_keys = set(open_keys)
        self.update()

    def set_mode(self, mode: str):
        self.mode = mode
        self.update()

    def set_group_factors(self, factor_ids: list[str]):
        self.group_factors = list(factor_ids)
        self._regroup()
        self.update()

    def _regroup(self):
        """Replicate groups: wells with the same value for every grouping
        factor, numbered in reading order. Wells blank in all of them stay
        out, like pLayout's grouping."""
        keys: dict[tuple, int] = {}
        self._groups = {}
        for well in range(self.plate.n_wells):
            values = tuple(self.plate.level_id(f, well) for f in self.group_factors)
            if not values or all(v is None for v in values):
                continue
            self._groups[well] = keys.setdefault(values, len(keys))

    def group_of(self, well: int) -> int | None:
        return self._groups.get(well)

    def label_lines(self, well: int) -> list[str]:
        """What the well's label says, one line per factor: in replicate
        mode the grouping factors' values — what makes the well a replicate
        of its neighbours — in factor order (a blank one shows as –), else
        the colour factor's value. Empty for a well with nothing to say."""
        if self.mode == REPLICATES:
            if well not in self._groups:
                return []
            lines = []
            for fid in self.group_factors:
                factor = self.doc.factor(fid)
                level = self.doc.level(factor, self.plate, well) if factor else None
                lines.append(level.name if level is not None else "–")
            return lines
        factor = self.doc.factor(self.mode)
        level = self.doc.level(factor, self.plate, well) if factor else None
        return [level.name] if level is not None else []

    def _label_fonts(self, cell: float) -> list[tuple]:
        """Candidate label fonts for this cell size, largest first, each
        with its metrics — built once per paint, not per well."""
        top = max(min(cell * 0.32, self.MAX_TEXT_PT), self.MIN_TEXT_PT)
        sizes = []
        pt = top
        while pt > self.MIN_TEXT_PT + 0.5:
            sizes.append(pt)
            pt -= 1.0
        sizes.append(self.MIN_TEXT_PT)
        fonts = []
        for pt in sizes:
            font = self.font()
            font.setPointSizeF(pt)
            fonts.append((font, QFontMetrics(font)))
        return fonts

    @staticmethod
    def _fit(lines: list[str], width: float, height: float, fonts: list[tuple]):
        """(font, metrics) — the largest at which every line fits the box, else None."""
        for font, fm in fonts:
            if len(lines) * fm.height() <= height and all(
                fm.horizontalAdvance(line) <= width for line in lines
            ):
                return font, fm
        return None

    def fitted_label(self, well: int, width: float, height: float, fonts=None):
        """The label drawn in a well body of this size: (font, metrics,
        lines). The conditions as stacked lines when they are readable at
        that size, else on one line, else — in replicate mode — the group
        number, which fits any well; a lone factor value that still does
        not fit is elided. None when the well says nothing."""
        lines = self.label_lines(well)
        if not lines:
            return None
        fonts = fonts or self._label_fonts(width)
        fit = self._fit(lines, width, height, fonts)
        if fit is None and len(lines) > 1:
            lines = ["  ·  ".join(lines)]
            fit = self._fit(lines, width, height, fonts)
        if fit is None and self.mode == REPLICATES:
            lines = [str(self._groups[well] + 1)]
            fit = self._fit(lines, width, height, fonts)
        if fit is None:
            font, fm = fonts[-1]
            lines = [fm.elidedText(lines[-1], Qt.ElideRight, int(width))]
            fit = (font, fm)
        return fit[0], fit[1], lines

    def well_state(self, well: int) -> str:
        """none (no stack mapped) / closed / partial / open."""
        keys = self.files.get(well)
        if not keys:
            return "none"
        n = sum(k in self.open_keys for k in keys)
        return "open" if n == len(keys) else "partial" if n else "closed"

    def conditions_for(self, key) -> str:
        """`B3  ·  Drug A 10 µM  ·  Drug B 0 µM` for a mapped file, else ''."""
        wells = self.wells_of.get(key)
        if not wells:
            return ""
        r, c = divmod(wells[0], self.plate.cols)
        text = self.doc.conditions_text(self.plate, wells[0])
        return f"{well_label(r, c)}  ·  {text}" if text else well_label(r, c)

    def tooltip_for(self, well: int) -> str:
        r, c = divmod(well, self.plate.cols)
        head = well_label(r, c)
        xy = self.doc.xy_factor()
        if xy is not None and (level := self.doc.level(xy, self.plate, well)) is not None:
            head += f"  ·  {level.name}"
        lines = [head]
        for f, lv in self.doc.conditions(self.plate, well):
            lines.append(f"{f.name}: {lv.name}{' ' + f.unit if f.unit else ''}")
        if self.mode == REPLICATES and (g := self._groups.get(well)) is not None:
            n = sum(1 for other in self._groups.values() if other == g)
            lines.append(f"Replicate group {g + 1}  ·  {n} wells")
        if note := self.plate.notes.get(well):
            lines.append(f"Note: {note}")
        keys = self.files.get(well)
        if keys:
            for k in keys:
                lines.append(f"{self.names.get(k, Path(str(k)).name)}  ({'open' if k in self.open_keys else 'closed'})")
        else:
            lines.append("no stack in this folder")
        return "\n".join(lines)

    # ---- geometry ------------------------------------------------------

    def _metrics_for(self, width: int) -> tuple[float, float, float]:
        """(left gutter, top gutter, well pitch) at this width."""
        fm = QFontMetrics(self.font())
        left = fm.horizontalAdvance(row_label(self.plate.rows - 1)) + 8
        top = fm.height() + 4
        cell = min((width - left - 2) / self.plate.cols, self.MAX_CELL)
        return left, top, max(cell, 4.0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        left, top, cell = self._metrics_for(width)
        return int(top + cell * self.plate.rows) + 2

    def sizeHint(self) -> QSize:
        return QSize(320, self.heightForWidth(320))

    def minimumSizeHint(self) -> QSize:
        return QSize(180, self.heightForWidth(180))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._laid_out = True
        if self._fits:
            self.setMinimumHeight(self.heightForWidth(self.width()))

    def fit_height(self, shown: bool = True):
        """Keep the whole plate on screen: the minimum height follows the
        width, so the window grows rather than clipping rows. Dropped while
        the file list is showing instead, so that page can be short. Before
        the layout has placed the view its width is a default, not a size,
        so the first real resize sets the minimum instead."""
        self._fits = shown
        if not shown:
            self.setMinimumHeight(0)
        elif self._laid_out:
            self.setMinimumHeight(self.heightForWidth(self.width()))

    def well_rect(self, well: int) -> QRectF:
        left, top, cell = self._metrics_for(self.width())
        r, c = divmod(well, self.plate.cols)
        return QRectF(left + c * cell, top + r * cell, cell, cell)

    def well_at(self, pos, clamp: bool = False) -> int | None:
        left, top, cell = self._metrics_for(self.width())
        c = (pos.x() - left) / cell
        r = (pos.y() - top) / cell
        if clamp:
            c = min(max(c, 0.0), self.plate.cols - 1e-6)
            r = min(max(r, 0.0), self.plate.rows - 1e-6)
        if c < 0 or r < 0 or c >= self.plate.cols or r >= self.plate.rows:
            return None
        return int(r) * self.plate.cols + int(c)

    # ---- painting ------------------------------------------------------

    def _face(self, well: int) -> tuple[QColor | None, str]:
        """Fill colour and label for the current colour mode."""
        if self.mode == REPLICATES:
            g = self._groups.get(well)
            return (None, "") if g is None else (_group_color(g), str(g + 1))
        factor = self.doc.factor(self.mode)
        level = self.doc.level(factor, self.plate, well) if factor else None
        return (None, "") if level is None else (QColor(level.color), level.name)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        left, top, cell = self._metrics_for(self.width())
        rows, cols = self.plate.rows, self.plate.cols
        base = self.font()
        base_pt = base.pointSizeF() if base.pointSizeF() > 0 else 12.0

        gutter_font = self.font()
        gutter_font.setPointSizeF(max(min(cell * 0.3, base_pt), 6.0))
        p.setFont(gutter_font)
        p.setPen(QColor("#909090"))
        for c in range(cols):
            p.drawText(QRectF(left + c * cell, 0, cell, top), Qt.AlignCenter, str(c + 1))
        for r in range(rows):
            p.drawText(QRectF(0, top + r * cell, left - 4, cell), Qt.AlignVCenter | Qt.AlignRight, row_label(r))

        hover_group = (
            self._groups.get(self._hover)
            if self.mode == REPLICATES and self._hover is not None
            else None
        )
        # Square wells with a small rounding, like pLayout's: the full cell
        # is room for text, where a circle wasted its corners.
        pad = cell * 0.06
        radius = min(3.0, (cell - 2 * pad) / 6)
        fonts = self._label_fonts(cell)
        labels: dict[tuple, tuple] = {}  # every cell is the same size: fit each text once
        text_w = cell - 2 * pad - 3
        text_h = cell - 2 * pad - 2
        for well in range(rows * cols):
            rect = self.well_rect(well).adjusted(pad, pad, -pad, -pad)
            color, _text = self._face(well)
            state = self.well_state(well)
            fill = QColor(color) if color is not None else QColor("#2a2a2a")
            if state == "none":
                fill.setAlpha(60)
            border = QPen(QColor("#3a3a3a"), 1)
            in_hover = well == self._hover or (hover_group is not None and self._groups.get(well) == hover_group)
            if well in self._preview:
                border = QPen(QColor("#2f6fd0"), 2.5)
                if not self._on:
                    border.setStyle(Qt.DashLine)
            elif state == "open":
                border = QPen(QColor("#ffffff"), 2.5)
            elif state == "partial":
                border = QPen(QColor("#ffffff"), 2.5)
                border.setStyle(Qt.DashLine)
            elif in_hover and state != "none":
                border = QPen(QColor("#e0e0e0"), 2)
            p.setPen(border)
            p.setBrush(fill)
            p.drawRoundedRect(rect, radius, radius)
            if cell < 18:
                continue
            key = tuple(self.label_lines(well)) + (self.mode == REPLICATES and self._groups.get(well),)
            if key not in labels:
                labels[key] = self.fitted_label(well, text_w, text_h, fonts)
            label = labels[key]
            if label is None:
                continue
            font, fm, lines = label
            if state == "none":
                tc = QColor(240, 240, 240, 120)
            else:
                lum = 0.299 * fill.red() + 0.587 * fill.green() + 0.114 * fill.blue()
                tc = QColor("#101010") if lum > 150 else QColor("#f0f0f0")
            p.setFont(font)
            p.setPen(tc)
            line_h = fm.height()
            y = rect.center().y() - line_h * len(lines) / 2
            for i, line in enumerate(lines):
                p.drawText(QRectF(rect.x(), y + i * line_h, rect.width(), line_h), Qt.AlignCenter, line)
        p.end()

    # ---- mouse ---------------------------------------------------------

    def _expand(self, wells: set[int]) -> set[int]:
        """Grow a selection to whole replicate groups (replicate mode only)."""
        if self.mode != REPLICATES or self._single:
            return wells
        groups = {self._groups[w] for w in wells if w in self._groups}
        return wells | {w for w, g in self._groups.items() if g in groups}

    def _targets(self, a: int, b: int) -> set[int]:
        """Mapped wells in the rectangle spanned by two wells, grown to groups."""
        cols = self.plate.cols
        ra, ca = divmod(a, cols)
        rb, cb = divmod(b, cols)
        wells = {
            r * cols + c
            for r in range(min(ra, rb), max(ra, rb) + 1)
            for c in range(min(ca, cb), max(ca, cb) + 1)
        }
        return {w for w in self._expand(wells) if w in self.files}

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return super().mousePressEvent(ev)
        well = self.well_at(ev.position())
        if well is None:
            return
        self._anchor = well
        # Qt reports the Mac's Cmd as ControlModifier.
        self._single = bool(ev.modifiers() & (Qt.ControlModifier | Qt.AltModifier))
        targets = self._targets(well, well)
        # Opening wins while anything in the selection is still closed.
        self._on = any(k not in self.open_keys for w in targets for k in self.files[w])
        self._preview = targets
        self.update()

    def mouseMoveEvent(self, ev):
        if self._anchor is not None:
            well = self.well_at(ev.position(), clamp=True)
            if well is not None:
                self._preview = self._targets(self._anchor, well)
                self.update()
            return
        well = self.well_at(ev.position())
        if well != self._hover:
            self._hover = well
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton or self._anchor is None:
            return super().mouseReleaseEvent(ev)
        keys = list(dict.fromkeys(k for w in sorted(self._preview) for k in self.files[w]))
        self._anchor = None
        self._preview = set()
        self.update()
        if keys:
            self.toggle.emit(keys, self._on)

    def leaveEvent(self, ev):
        self._hover = None
        self.update()
        super().leaveEvent(ev)

    def event(self, ev):
        if ev.type() == QEvent.ToolTip:
            well = self.well_at(ev.pos())
            if well is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(ev.globalPos(), self.tooltip_for(well), self)
            return True
        return super().event(ev)


class PlatePanel(QWidget):
    """A plate file in a folder section: which plate (when the file holds
    several), what colours the wells, the matched-stack count, the wells."""

    toggle = Signal(list, bool)
    unlink = Signal()  # the ✕ on the plate's row: detach it from the folder

    def __init__(self, doc: PlateLayout, parent: QWidget | None = None):
        super().__init__(parent)
        self.doc = doc
        self._files: dict = {}
        self.view = PlateView(doc)
        self.view.toggle.connect(self.toggle)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(f"⊞ {doc.path.name}")
        name.setToolTip(str(doc.path))
        name.setStyleSheet("color: #c0c0c0;")
        head.addWidget(name, 1)
        self.plate_combo = QComboBox()
        for p in doc.plates:
            self.plate_combo.addItem(p.name)
        self.plate_combo.setToolTip("Which plate of this layout to show")
        self.plate_combo.setVisible(len(doc.plates) > 1)
        self.plate_combo.currentIndexChanged.connect(self._plate_changed)
        head.addWidget(self.plate_combo)
        # The explicit way out, next to the plate's name: the folder section
        # goes back to its file list and forgets the pairing.
        self.unlink_button = QToolButton()
        self.unlink_button.setText("✕")
        self.unlink_button.setToolTip(
            "Unlink this plate layout from the folder\n(back to the file list; open stacks stay open)"
        )
        self.unlink_button.setFocusPolicy(Qt.NoFocus)
        self.unlink_button.clicked.connect(self.unlink)
        head.addWidget(self.unlink_button)
        box.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Colour:"))
        self.mode_combo = QComboBox()
        for f in doc.factors:
            self.mode_combo.addItem(f.display_name, f.id)
        self.mode_combo.addItem("Replicates", REPLICATES)
        self.mode_combo.setToolTip(
            "Colour wells by one factor's values, or by replicate group:\n"
            "wells sharing the Group-by factors' values get one colour, show\n"
            "those values (their group number when too small to read) and\n"
            f"open together with a single click ({_MOD}+click: one well)."
        )
        default = next((f.id for f in doc.factors if not f.is_xy), None)
        if default is None and doc.factors:
            default = doc.factors[0].id
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(default or REPLICATES))
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode_combo, 1)
        self.group_button = QToolButton()
        self.group_button.setPopupMode(QToolButton.InstantPopup)
        self.group_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        # The style's corner arrow would overprint the text; the caret is in the text.
        self.group_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        self.group_button.setFocusPolicy(Qt.NoFocus)
        self.group_button.setToolTip(
            "Which factors have to match for wells to count as replicates,\n"
            "and what their labels show. The XY position starts unchecked\n"
            "(it would make every well its own group), as does any factor\n"
            "hidden in pLayout's Overview."
        )
        self.group_menu = QMenu(self.group_button)
        self.group_actions: list[QAction] = []
        grouped = set(self.view.group_factors)
        for f in doc.factors:
            action = QAction(f.display_name, self.group_menu)
            action.setCheckable(True)
            action.setChecked(f.id in grouped)
            action.setData(f.id)
            action.toggled.connect(lambda _on, a=action: self._groups_changed(a))
            self.group_menu.addAction(action)
            self.group_actions.append(action)
        self.group_button.setMenu(self.group_menu)
        row.addWidget(self.group_button)
        box.addLayout(row)

        self.status = QLabel()
        self.status.setStyleSheet("color: #909090;")
        box.addWidget(self.status)
        box.addWidget(self.view)
        box.addStretch(1)
        self._update_group_button()
        self._mode_changed()

    @property
    def plate(self) -> Plate:
        return self.view.plate

    def set_files(self, files: dict):
        """`files` is the folder section's listing: canonical path → listed path."""
        self._files = dict(files)
        mapping, how = match_files(self.doc, self.plate, self._files)
        self.view.set_files(mapping, {k: Path(p).name for k, p in self._files.items()})
        n = self.plate.n_wells
        self.status.setText(f"{len(mapping)} of {n} wells have a stack  ·  matched by {how}")

    def set_open(self, open_keys: set):
        self.view.set_open(open_keys)

    def conditions_for(self, key) -> str:
        return self.view.conditions_for(key)

    def matched(self) -> int:
        return len(self.view.files)

    def _plate_changed(self, index: int):
        self.view.set_plate(index)
        self.set_files(self._files)

    def _mode_changed(self, *_):
        mode = self.mode_combo.currentData()
        self.view.set_mode(mode)
        self.group_button.setVisible(mode == REPLICATES and bool(self.group_actions))

    def _groups_changed(self, action: QAction):
        ids = [a.data() for a in self.group_actions if a.isChecked()]
        if not ids:  # keep at least one factor, like pLayout
            action.blockSignals(True)
            action.setChecked(True)
            action.blockSignals(False)
            ids = [action.data()]
        self.view.set_group_factors(ids)
        self._update_group_button()

    def _update_group_button(self):
        checked = [a for a in self.group_actions if a.isChecked()]
        if len(checked) == len(self.group_actions):
            text = "all factors"
        elif len(checked) > 2:
            text = f"{len(checked)} of {len(self.group_actions)} factors"
        else:
            text = ", ".join(a.text() for a in checked)
        self.group_button.setText(f"Group by: {text} ▾")
