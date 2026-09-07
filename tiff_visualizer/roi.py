"""Selections (ROIs) and the tools that draw them — Fiji's rectangle, oval,
polygon and freehand selections, one per stack.

One app-wide tool decides what a left drag on an image does: the hand pans,
the shape tools draw. A pane keeps at most one selection: drawing a new one
replaces it, a click outside it clears it, dragging inside moves it and the
handles resize it (rectangle/ellipse) or move vertices (polygon). Cmd+M
measures inside it. Selections are saved in sessions.
"""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
import shiboken6
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QGraphicsPathItem

TOOLS = ("hand", "rect", "ellipse", "polygon", "freehand")
TOOL_LABELS = {
    "hand": "Hand",
    "rect": "Rectangle",
    "ellipse": "Ellipse",
    "polygon": "Polygon",
    "freehand": "Freehand",
}
TOOL_KEYS = {"hand": "H", "rect": "R", "ellipse": "E", "polygon": "P", "freehand": "D"}
TOOL_HINTS = {
    "hand": "Drag pans the image, selection or not",
    "rect": "Drag on the image to draw a rectangle",
    "ellipse": "Drag on the image to draw an ellipse",
    "polygon": "Click the corners; double-click or click the first corner to close",
    "freehand": "Drag on the image to trace an outline",
}

COLOR = "#ffd400"  # Fiji's selection yellow
_PEN = pg.mkPen(COLOR, width=1)
_HOVER_PEN = pg.mkPen("#ffef9a", width=2)
_HANDLE_PEN = pg.mkPen(COLOR)


class _ToolState(QObject):
    changed = Signal(str)


_state = _ToolState()
_tool = "hand"


def tool() -> str:
    return _tool


def set_tool(name: str):
    global _tool
    if name not in TOOLS or name == _tool:
        return
    _tool = name
    _state.changed.emit(name)


def tool_changed() -> Signal:
    """Signal(str) emitted with the new tool's name."""
    return _state.changed


# ---- the selection items ------------------------------------------------

_COMMON = dict(
    pen=_PEN,
    hoverPen=_HOVER_PEN,
    handlePen=_HANDLE_PEN,
    rotatable=False,
    removable=True,  # right-click > Remove ROI
    snapSize=1.0,
    translateSnap=True,  # moving keeps whole-pixel positions, like Fiji
)


class RectSelection(pg.RectROI):
    kind = "rect"

    def __init__(self, x, y, w, h, bounds: QRectF):
        super().__init__((x, y), (w, h), maxBounds=bounds, scaleSnap=True, **_COMMON)
        # pg adds the bottom-right scale handle; the other corners too.
        self.addScaleHandle([0, 0], [1, 1])
        self.addScaleHandle([1, 0], [0, 1])
        self.addScaleHandle([0, 1], [1, 0])


class EllipseSelection(pg.EllipseROI):
    kind = "ellipse"

    def __init__(self, x, y, w, h, bounds: QRectF):
        super().__init__((x, y), (w, h), maxBounds=bounds, scaleSnap=True, **_COMMON)

    def _addHandles(self):  # pg's default is a rotate + one diagonal handle
        for pos, center in (([1, 1], [0, 0]), ([0, 0], [1, 1]), ([1, 0], [0, 1]), ([0, 1], [1, 0])):
            self.addScaleHandle(pos, center)


class PolygonSelection(pg.PolyLineROI):
    kind = "polygon"

    def __init__(self, points):
        super().__init__([tuple(p) for p in points], closed=True, **_COMMON)

    def points(self) -> list[tuple[float, float]]:
        pts = (self.mapToView(h["item"].pos()) for h in self.handles)
        return [(pt.x(), pt.y()) for pt in pts]


class FreehandSelection(pg.ROI):
    """A traced outline; movable as a whole, no handles."""

    kind = "freehand"

    def __init__(self, points):
        pts = np.asarray(points, dtype=float)
        origin = pts.min(axis=0)
        super().__init__(pos=tuple(origin), size=tuple(pts.max(axis=0) - origin), resizable=False, **_COMMON)
        self._points = pts - origin
        self._path = QPainterPath(QPointF(*self._points[0]))
        for x, y in self._points[1:]:
            self._path.lineTo(x, y)
        self._path.closeSubpath()

    def points(self) -> list[tuple[float, float]]:
        origin = np.array([self.pos().x(), self.pos().y()])
        return [tuple(p) for p in (self._points + origin).tolist()]

    def boundingRect(self):
        return self._path.boundingRect()

    def shape(self):
        return self._path

    def paint(self, p, opt, widget):
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(self.currentPen)
        p.drawPath(self._path)


# ---- per-pane selection state -------------------------------------------


def _fmt_rect(r: QRectF) -> str:
    return f"{int(round(r.x()))},{int(round(r.y()))} {int(round(r.width()))}×{int(round(r.height()))}"


class Selection:
    """The one selection of a pane, plus the in-progress drawing state."""

    def __init__(self, pane):
        self.pane = pane
        self.viewbox = pane.viewbox
        self.roi = None
        self._drawing: str | None = None  # tool of the shape being drawn
        self._start = QPointF()
        self._points: list[tuple[float, float]] = []
        self._preview = QGraphicsPathItem()
        self._preview.setPen(_PEN)
        self._preview.setZValue(20)
        self._preview.hide()
        self.viewbox.addItem(self._preview, ignoreBounds=True)
        self.viewbox.selection = self
        _state.changed.connect(self._on_tool_changed)
        self._on_tool_changed(tool())

    # ---- state ---------------------------------------------------------

    @property
    def kind(self) -> str | None:
        return self.roi.kind if self.roi is not None else None

    def drawing(self) -> bool:
        return self._drawing is not None

    def _bounds(self) -> QRectF:
        h, w = self.pane.stack.shape_yx
        return QRectF(0, 0, w, h)

    def _set_roi(self, item):
        self.clear()
        self.viewbox.addItem(item, ignoreBounds=True)
        item.sigRemoveRequested.connect(lambda *_: self.clear())
        # Like Fiji, only a shape tool moves a selection by dragging inside
        # it; the hand always pans, even over a whole-image selection.
        item.translatable = tool() != "hand"
        self.roi = item

    def clear(self):
        if self.roi is not None:
            self.viewbox.removeItem(self.roi)
            self.roi = None

    def set_rect(self, x, y, w, h, ellipse: bool = False):
        """A whole-pixel rectangle/ellipse clipped to the image; nothing
        when the clipped box is empty."""
        r = QRectF(x, y, w, h).normalized().intersected(self._bounds())
        x0, y0 = int(round(r.left())), int(round(r.top()))
        x1, y1 = int(round(r.right())), int(round(r.bottom()))
        if x1 <= x0 or y1 <= y0:
            return
        cls = EllipseSelection if ellipse else RectSelection
        self._set_roi(cls(x0, y0, x1 - x0, y1 - y0, self._bounds()))

    def set_polygon(self, points):
        pts = _dedupe(points)
        if len(pts) >= 3:
            self._set_roi(PolygonSelection(pts))

    def set_freehand(self, points):
        pts = _dedupe(points)
        if len(pts) >= 3:
            self._set_roi(FreehandSelection(pts))

    def select_all(self):
        h, w = self.pane.stack.shape_yx
        self.set_rect(0, 0, w, h)

    def _path(self) -> QPainterPath:
        return self.roi.mapToView(self.roi.shape())

    def describe(self) -> str:
        if self.roi is None:
            return "whole image"
        return f"{self.roi.kind} {_fmt_rect(self._path().boundingRect())}"

    def values(self, plane: np.ndarray):
        """(pixel values inside the selection, pixel count, description) for
        one plane, or None when the selection lies outside the image."""
        if self.roi is None:
            return plane.ravel(), plane.size, "whole image"
        region = self.mask()
        if region is None:
            return None
        (ys, xs), mask = region
        values = plane[ys, xs][mask]
        if values.size == 0:
            return None
        return values, int(values.size), self.describe()

    def mask(self):
        """((y slice, x slice), bool mask) covering the selection's bounding
        box clipped to the image, or None when that box is empty. The shape
        is rasterized exactly as drawn, so what you see is what gets measured."""
        if self.roi is None:
            return None
        path = self._path()
        br = path.boundingRect()
        h, w = self.pane.stack.shape_yx
        x0, y0 = max(math.floor(br.left()), 0), max(math.floor(br.top()), 0)
        x1, y1 = min(math.ceil(br.right()), w), min(math.ceil(br.bottom()), h)
        if x1 <= x0 or y1 <= y0:
            return None
        img = QImage(x1 - x0, y1 - y0, QImage.Format_Grayscale8)
        img.fill(0)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)
        painter.setBrush(Qt.white)
        painter.translate(-x0, -y0)
        painter.drawPath(path)
        painter.end()
        buf = np.frombuffer(img.constBits(), dtype=np.uint8)
        mask = buf.reshape(img.height(), img.bytesPerLine())[:, : x1 - x0] > 0
        return (slice(y0, y1), slice(x0, x1)), mask.copy()

    # ---- sessions ------------------------------------------------------

    def to_json(self) -> dict | None:
        roi = self.roi
        if roi is None:
            return None
        if roi.kind in ("rect", "ellipse"):
            r = self._path().boundingRect()
            return {"kind": roi.kind, "rect": [r.x(), r.y(), r.width(), r.height()]}
        return {"kind": roi.kind, "points": [list(p) for p in roi.points()]}

    def from_json(self, data: dict | None):
        if not data:
            self.clear()
            return
        kind = data.get("kind")
        if kind in ("rect", "ellipse"):
            self.set_rect(*data["rect"], ellipse=kind == "ellipse")
        elif kind == "polygon":
            self.set_polygon(data["points"])
        elif kind == "freehand":
            self.set_freehand(data["points"])

    # ---- drawing (called by the pane's ViewBox) ------------------------

    def detach(self):
        """Stop following tool changes; called when the pane goes away."""
        try:
            _state.changed.disconnect(self._on_tool_changed)
        except (RuntimeError, TypeError):
            pass  # already disconnected

    def _on_tool_changed(self, name: str):
        if not shiboken6.isValid(self.pane):
            self.detach()  # closed pane whose signal connection outlived it
            return
        self.cancel()
        if self.roi is not None:
            self.roi.translatable = name != "hand"
        self.pane.view.viewport().setCursor(Qt.ArrowCursor if name == "hand" else Qt.CrossCursor)

    def cancel(self) -> bool:
        """Abandon an in-progress shape; True if there was one."""
        if self._drawing is None:
            return False
        self._drawing = None
        self._points = []
        self._preview.hide()
        return True

    def handle_escape(self) -> bool:
        """Esc: abandon the shape being drawn, else clear the selection."""
        if self.cancel():
            return True
        if self.roi is not None:
            self.clear()
            return True
        return False

    def contains(self, pos: QPointF) -> bool:
        return self.roi is not None and self._path().contains(pos)

    def drag(self, ev) -> bool:
        """A left drag on the image with a rectangle/ellipse/freehand tool
        draws that shape; returns False to let the ViewBox pan otherwise."""
        t = tool()
        if t in ("hand", "polygon") or ev.button() != Qt.LeftButton:
            return False
        pos = self.viewbox.mapToView(ev.pos())
        if ev.isStart():
            self.clear()
            self._drawing = t
            self._start = self.viewbox.mapToView(ev.buttonDownPos())
            self._points = [(pos.x(), pos.y())]
        if self._drawing is None:
            return False
        ev.accept()
        if t == "freehand":
            last = self._points[-1]
            if abs(pos.x() - last[0]) + abs(pos.y() - last[1]) >= 0.5:
                self._points.append((pos.x(), pos.y()))
            self._show_preview(_polyline_path(self._points, close=True))
            status = f"freehand {_fmt_rect(self._preview.path().boundingRect())}"
            if ev.isFinish():
                points = self._points
                self.cancel()
                self.set_freehand(points)
        else:
            rect = self._drag_rect(pos)
            path = QPainterPath()
            if t == "ellipse":
                path.addEllipse(rect)
            else:
                path.addRect(rect)
            self._show_preview(path)
            status = f"{t} {_fmt_rect(rect)}"
            if ev.isFinish():
                self.cancel()
                self.set_rect(rect.x(), rect.y(), rect.width(), rect.height(), ellipse=t == "ellipse")
        self.pane.probe_label.setText(status)
        return True

    def _drag_rect(self, pos: QPointF) -> QRectF:
        r = QRectF(self._start, pos).normalized().intersected(self._bounds())
        x0, y0 = round(r.left()), round(r.top())
        x1, y1 = round(r.right()), round(r.bottom())
        return QRectF(x0, y0, max(x1 - x0, 0), max(y1 - y0, 0))

    def click(self, ev) -> bool:
        """Polygon tool: clicks add corners, a double-click or a click on the
        first corner closes. Other shape tools: a click outside the selection
        clears it (Fiji). The hand tool leaves clicks alone."""
        t = tool()
        if t == "hand" or ev.button() != Qt.LeftButton:
            return False
        pos = self.viewbox.mapToView(ev.pos())
        ev.accept()
        if t != "polygon":
            if self.roi is not None and not self.contains(pos):
                self.clear()
            return True
        if ev.double():
            self._close_polygon()
            return True
        if not self._points:
            self.clear()
            self._drawing = "polygon"
        elif len(self._points) >= 3 and self._near_first(pos):
            self._close_polygon()
            return True
        self._points.append((round(pos.x()), round(pos.y())))
        self.mouse_moved(pos)
        return True

    def _near_first(self, pos: QPointF) -> bool:
        px = self.viewbox.viewPixelSize()[0]  # image units per screen pixel
        x0, y0 = self._points[0]
        return math.hypot(pos.x() - x0, pos.y() - y0) <= 6 * px

    def _close_polygon(self):
        points = self._points
        if self.cancel() and len(_dedupe(points)) >= 3:
            self.set_polygon(points)

    def mouse_moved(self, pos: QPointF):
        """Rubber band from the last polygon corner to the cursor."""
        if self._drawing != "polygon":
            return
        self._show_preview(_polyline_path(self._points + [(pos.x(), pos.y())], close=len(self._points) > 1))
        n = len(self._points)
        self.pane.probe_label.setText(
            f"polygon: {n} corner{'s' if n != 1 else ''}"
            + (" — double-click or click the first corner to close" if n >= 3 else "")
        )

    def _show_preview(self, path: QPainterPath):
        self._preview.setPath(path)
        self._preview.show()


def _dedupe(points) -> list[tuple[float, float]]:
    """Drop consecutive duplicate vertices (and a closing repeat of the first)."""
    out: list[tuple[float, float]] = []
    for p in points:
        p = (float(p[0]), float(p[1]))
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _polyline_path(points, close: bool) -> QPainterPath:
    path = QPainterPath()
    if not points:
        return path
    path.moveTo(*points[0])
    for p in points[1:]:
        path.lineTo(*p)
    if close and len(points) > 2:
        path.closeSubpath()
    return path
