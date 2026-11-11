"""The mask layer — a brush and an eraser that paint a binary mask on a stack.

A mask marks a region of the image by hand: debris to leave out of an
analysis, an area to keep. Each stack has at most one, in image pixels and
the same on every channel, slice and frame — scrubbing does not move it. The
brush and the eraser are two more tools next to the selection tools (roi.py
holds the app-wide tool): a left drag paints or erases a round stroke, a
click one dab, and Alt/Option turns the brush into the eraser for a stroke
(like Fiji's brush selection tool). A selection can be added to the mask or
cut out of it whole. Export Mask writes the mask as a black/white image of
the frame's size, white = masked — the form analysis pipelines read — and
Open Mask loads one back.

Mask edits undo with Cmd+Z, sharing it with contrast changes (the latest
edit of either kind goes first), and masks are saved in sessions.
"""

from __future__ import annotations

import base64
import binascii
import math
import time
import weakref
import zlib
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import shiboken6
import tifffile
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem

from . import roi
from . import settings as app_settings

# Magenta: apart from the usual channel LUTs (red, green, blue, gray) and
# from the selection's yellow. One overlay pixel, premultiplied, as the four
# bytes R, G, B, A of QImage.Format_RGBA8888_Premultiplied read as a uint32.
# That format on purpose: Qt has a fast path for scaling ARGB32 images that
# samples a screen pixel apart from the generic one it uses for the RGB plane
# underneath, and this one takes the generic path too, so mask and image
# pixels share their edges at any zoom.
COLOR = (255, 64, 255)
ALPHA = 0.45
_PIXEL = np.array(
    [round(COLOR[0] * ALPHA), round(COLOR[1] * ALPHA), round(COLOR[2] * ALPHA), round(255 * ALPHA)],
    dtype=np.uint8,
).view(np.uint32)[0]

MIN_SIZE, MAX_SIZE, DEFAULT_SIZE = 1, 500, 20
SUFFIXES = (".png", ".tif", ".tiff")
SAVE_FILTER = "PNG image (*.png);;TIFF image (*.tif *.tiff)"
OPEN_FILTER = "Mask images (*.png *.tif *.tiff);;All files (*)"

_MAX_UNDO = 50  # per stack
_MAX_JOURNAL = 1000


# ---- the brush (app-wide, like the tool) ---------------------------------


class _BrushState(QObject):
    size_changed = Signal(int)


_state = _BrushState()
_size: int | None = None


def brush_size() -> int:
    """The brush's diameter in image pixels."""
    global _size
    if _size is None:
        saved = app_settings.settings().value("mask/brush_size", DEFAULT_SIZE, type=int)
        _size = max(MIN_SIZE, min(MAX_SIZE, saved))
    return _size


def set_brush_size(size: int):
    global _size
    size = max(MIN_SIZE, min(MAX_SIZE, int(size)))
    if size == brush_size():
        return
    _size = size
    app_settings.settings().setValue("mask/brush_size", size)
    _state.size_changed.emit(size)


def step_brush_size(direction: int):
    """[ and ]: about 15 % at a time, never less than a pixel."""
    size = brush_size()
    set_brush_size(size + direction * max(1, round(size * 0.15)))


def size_changed() -> Signal:
    """Signal(int) emitted with the new brush diameter."""
    return _state.size_changed


def last_dir() -> str:
    """Where masks were last exported to / opened from — kept apart from the
    stacks' folder, since masks usually live next to an analysis instead."""
    return app_settings.settings().value("mask/dir", "", type=str)


def set_last_dir(path: str):
    app_settings.settings().setValue("mask/dir", path)


# ---- strokes ---------------------------------------------------------------


def stroke_mask(shape, p0, p1, radius: float):
    """((y slice, x slice), bool array): the pixels a round brush of this
    radius covers while its centre moves from p0 to p1 (x, y in image
    coordinates, pixel i spanning [i, i+1)), clipped to an image of this
    (height, width); None when the stroke misses the image. A pixel is
    covered when its centre lies within the radius of the path, and the
    pixels under the two end points always are, so the smallest brush still
    leaves a connected one-pixel line."""
    h, w = shape
    (x0, y0), (x1, y1) = p0, p1
    xa = max(math.floor(min(x0, x1) - radius), 0)
    xb = min(math.ceil(max(x0, x1) + radius), w)
    ya = max(math.floor(min(y0, y1) - radius), 0)
    yb = min(math.ceil(max(y0, y1) + radius), h)
    if xb <= xa or yb <= ya:
        return None
    dx, dy = x1 - x0, y1 - y0
    xs = (np.arange(xa, xb) + 0.5 - x0)[None, :]
    ys = (np.arange(ya, yb) + 0.5 - y0)[:, None]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        dist2 = xs * xs + ys * ys
    else:
        along = np.clip((xs * dx + ys * dy) / length2, 0.0, 1.0)
        dist2 = (xs - along * dx) ** 2 + (ys - along * dy) ** 2
    hit = dist2 <= radius * radius
    for x, y in ((x0, y0), (x1, y1)):
        col, row = math.floor(x) - xa, math.floor(y) - ya
        if 0 <= col < xb - xa and 0 <= row < yb - ya:
            hit[row, col] = True
    return (slice(ya, yb), slice(xa, xb)), hit


# ---- files -------------------------------------------------------------------


def write_mask(path, mask: np.ndarray):
    """Write the mask as an 8-bit image of its own size, 0 = clear and 255 =
    masked: a TIFF for .tif/.tiff, a PNG otherwise. Raises OSError."""
    path = Path(path)
    data = np.ascontiguousarray(np.asarray(mask, dtype=bool).astype(np.uint8) * 255)
    if path.suffix.lower() in (".tif", ".tiff"):
        tifffile.imwrite(path, data, photometric="minisblack")
        return
    h, w = data.shape
    if not QImage(data.data, w, h, w, QImage.Format_Grayscale8).save(str(path), "PNG"):
        raise OSError(f"could not write {path}")


def read_mask(path) -> np.ndarray:
    """A mask image as a bool (Y, X) array: the pixels brighter than half the
    file's value range in any colour channel (alpha is ignored) — bright
    strokes on a dark ground, whatever drew them. Raises ValueError when the
    file is not an image that can be read that way."""
    path = Path(path)
    if path.suffix.lower() in (".tif", ".tiff"):
        try:
            a = tifffile.imread(path, key=0)
        except Exception as exc:  # noqa: BLE001 - tifffile raises many kinds
            raise ValueError(str(exc)) from exc
        if a.ndim == 3 and a.shape[-1] in (3, 4):
            a = a[..., :3].max(axis=-1)
        if a.ndim != 2:
            raise ValueError(f"unsupported image shape {a.shape}")
        if a.dtype == bool:
            return a.copy()
        if np.issubdtype(a.dtype, np.floating):
            return a > 0.5
        return a > np.iinfo(a.dtype).max / 2
    image = QImage(str(path))
    if image.isNull():
        raise ValueError("not an image this app can read")
    image = image.convertToFormat(QImage.Format_RGBA8888)
    w, h = image.width(), image.height()
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())
    return rows[:, : 4 * w].reshape(h, w, 4)[..., :3].max(axis=-1) > 127


# ---- undo (Cmd+Z, shared with contrast changes) ----------------------------

# One entry per undoable mask edit, oldest first: (when, the layer it was
# made on). The edits themselves live on their layers; weak references, so
# the journal never keeps a closed stack's mask alive.
_journal: list[tuple[float, weakref.ref]] = []


def _prune_journal():
    while _journal:
        layer = _journal[-1][1]()
        if layer is not None and layer.undoable():
            return
        _journal.pop()


def last_edit_time() -> float | None:
    """time.monotonic() of the latest mask edit that can still be undone."""
    _prune_journal()
    return _journal[-1][0] if _journal else None


def undo_last() -> "MaskLayer | None":
    """Take back the latest mask edit on any open stack; returns the layer
    it was made on, None when there was nothing to undo."""
    _prune_journal()
    if not _journal:
        return None
    layer = _journal.pop()[1]()
    layer.undo()
    return layer


# ---- the overlay ---------------------------------------------------------------


class _MaskItem(pg.GraphicsObject):
    """Draws the mask over the image and, while a mask tool is active, is
    the surface the brush paints on: it sits above the selection and its
    handles and claims left drags and clicks first, so a stroke that starts
    on a selection paints instead of moving or resizing it. With any other
    tool it lets every event through."""

    def __init__(self, layer: "MaskLayer"):
        super().__init__()
        self.layer = layer
        self.image: QImage | None = None
        self.setZValue(50)

    def boundingRect(self):
        h, w = self.layer.pane.stack.shape_yx
        return QRectF(0, 0, w, h)

    def paint(self, painter, option, widget=None):
        if self.image is None or not self.layer.count:
            return
        # The whole image into the whole rect, the way pyqtgraph's ImageItem
        # draws the plane underneath, so both are sampled alike. Qt clips to
        # what is exposed, so a stroke still repaints only its own box.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(self.boundingRect(), self.image)

    def hoverEvent(self, ev):
        if not ev.isExit() and self.layer.paints() and ev.acceptDrags(Qt.LeftButton):
            ev.acceptClicks(Qt.LeftButton)  # a click is one dab

    def mouseDragEvent(self, ev):
        if ev.button() != Qt.LeftButton or not self.layer.paints():
            ev.ignore()
            return
        ev.accept()
        self.layer.drag(
            ev.buttonDownPos(), ev.pos(), ev.isStart(), ev.isFinish(),
            alt=bool(ev.modifiers() & Qt.AltModifier),
        )

    def mouseClickEvent(self, ev):
        if ev.button() != Qt.LeftButton or not self.layer.paints():
            ev.ignore()
            return
        ev.accept()
        self.layer.dab(ev.pos(), alt=bool(ev.modifiers() & Qt.AltModifier))


class MaskLayer:
    """The mask of one pane: the pixels, their overlay, the strokes that
    edit them and the undo history of those edits."""

    def __init__(self, pane):
        self.pane = pane
        self.viewbox = pane.viewbox
        # Nothing is allocated until the first edit: most panes never get a mask.
        self.data: np.ndarray | None = None  # (Y, X) bool
        self.count = 0  # masked pixels
        self._pixels: np.ndarray | None = None  # (Y, X) uint32 overlay pixels the item paints from
        self._before: np.ndarray | None = None  # the mask as an open edit found it
        self._last: tuple[float, float] | None = None  # the stroke's last point while dragging
        self._erasing = False
        self._undo: list[tuple] = []  # (y slice, x slice, the pixels before the edit)
        self._detached = False
        self.item = _MaskItem(self)
        self.viewbox.addItem(self.item, ignoreBounds=True)
        self._cursor = QGraphicsEllipseItem()  # the brush's outline under the mouse
        self._cursor.setZValue(60)
        self._cursor.hide()
        self.viewbox.addItem(self._cursor, ignoreBounds=True)
        self._style_cursor(roi.tool())
        roi.tool_changed().connect(self._on_tool_changed)
        _state.size_changed.connect(self._on_size_changed)

    # ---- state ---------------------------------------------------------

    def is_empty(self) -> bool:
        return self.count == 0

    def paints(self) -> bool:
        """A mask tool is active and the pane shows an image to paint on."""
        return roi.tool() in roi.MASK_TOOLS and not self.pane._blank

    def array(self) -> np.ndarray:
        """The mask as a bool (Y, X) array of the image's size (a copy)."""
        if self.data is None:
            return np.zeros(self.pane.stack.shape_yx, dtype=bool)
        return self.data.copy()

    def region(self):
        """((y slice, x slice), bool array): the mask cut to its bounding
        box, in the form roi.Selection.mask() returns a selection; None
        without a mask."""
        if not self.count:
            return None
        rows = np.flatnonzero(self.data.any(axis=1))
        cols = np.flatnonzero(self.data.any(axis=0))
        ys = slice(int(rows[0]), int(rows[-1]) + 1)
        xs = slice(int(cols[0]), int(cols[-1]) + 1)
        return (ys, xs), self.data[ys, xs].copy()

    def status(self) -> str:
        """'mask 3,214 px (0.47%)' for the probe row; empty without a mask."""
        if not self.count:
            return ""
        return f"mask {self.count:,} px ({100 * self.count / self.data.size:.2f}%)"

    def undoable(self) -> bool:
        return bool(self._undo) and not self._detached

    def _ensure(self):
        if self.data is None:
            h, w = self.pane.stack.shape_yx
            self.data = np.zeros((h, w), dtype=bool)
            self._pixels = np.zeros((h, w), dtype=np.uint32)

    def _changed(self, ys: slice, xs: slice):
        """The mask changed inside this box: redraw it there."""
        self._pixels[ys, xs] = self.data[ys, xs].astype(np.uint32) * _PIXEL
        self.count = int(np.count_nonzero(self.data))
        h, w = self.data.shape
        # A fresh QImage over the same pixels each time, so nothing Qt may
        # have cached for the old one is reused.
        self.item.image = QImage(
            self._pixels.view(np.uint8).data, w, h, 4 * w, QImage.Format_RGBA8888_Premultiplied
        )
        self.item.update(QRectF(xs.start, ys.start, xs.stop - xs.start, ys.stop - ys.start))

    def _everywhere(self) -> tuple[slice, slice]:
        h, w = self.data.shape
        return slice(0, h), slice(0, w)

    # ---- edits (each one undo step) -------------------------------------

    def _begin(self):
        self._ensure()
        self._before = self.data.copy()

    def _commit(self) -> bool:
        """Close the open edit: remember how the changed box looked before,
        as one undo step. False when the edit changed nothing."""
        before, self._before = self._before, None
        if before is None:
            return False
        changed = before != self.data
        rows = np.flatnonzero(changed.any(axis=1))
        if rows.size == 0:
            return False
        cols = np.flatnonzero(changed.any(axis=0))
        ys = slice(int(rows[0]), int(rows[-1]) + 1)
        xs = slice(int(cols[0]), int(cols[-1]) + 1)
        self._undo.append((ys, xs, before[ys, xs].copy()))
        del self._undo[:-_MAX_UNDO]
        _journal.append((time.monotonic(), weakref.ref(self)))
        del _journal[:-_MAX_JOURNAL]
        return True

    def undo(self):
        if not self._undo:
            return
        ys, xs, pixels = self._undo.pop()
        self.data[ys, xs] = pixels
        self._changed(ys, xs)

    def add_region(self, region, subtract: bool = False) -> bool:
        """Add a region — ((y slice, x slice), bool array), as
        roi.Selection.mask() returns it — to the mask, or cut it out."""
        (ys, xs), hit = region
        self._begin()
        if subtract:
            self.data[ys, xs] &= ~hit
        else:
            self.data[ys, xs] |= hit
        self._changed(ys, xs)
        return self._commit()

    def clear(self) -> bool:
        if self.is_empty():
            return False
        self._begin()
        self.data[:] = False
        self._changed(*self._everywhere())
        return self._commit()

    def set_array(self, mask: np.ndarray, undoable: bool = True):
        """Replace the mask with this bool (Y, X) array of the image's size."""
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != tuple(self.pane.stack.shape_yx):
            raise ValueError(f"mask is {mask.shape}, the image {tuple(self.pane.stack.shape_yx)}")
        self._ensure()
        if undoable:
            self._before = self.data.copy()
        self.data[:] = mask
        self._changed(*self._everywhere())
        if undoable:
            self._commit()

    # ---- painting (called by the overlay item) ---------------------------

    def drag(self, down: QPointF, pos: QPointF, start: bool, finish: bool, alt: bool = False):
        """One step of a left drag with a mask tool, in image coordinates:
        the stroke runs from where the button went down through every
        position reported since. The eraser tool erases, and so does the
        brush for a stroke begun with Alt/Option held."""
        if start:
            self._end_stroke()
            self._begin()
            self._erasing = roi.tool() == "eraser" or alt
            self._last = (down.x(), down.y())
            self._stroke_to(self._last)  # the dab under the press
        if self._last is None:
            return  # a drag that began under another tool
        self._stroke_to((pos.x(), pos.y()))
        if finish:
            self._end_stroke()

    def dab(self, pos: QPointF, alt: bool = False):
        """A click: one round dab."""
        self.drag(pos, pos, True, True, alt)

    def _stroke_to(self, point: tuple[float, float]):
        region = stroke_mask(self.data.shape, self._last, point, brush_size() / 2)
        self._last = point
        if region is None:
            return
        (ys, xs), hit = region
        if self._erasing:
            self.data[ys, xs] &= ~hit
        else:
            self.data[ys, xs] |= hit
        self._changed(ys, xs)

    def _end_stroke(self):
        if self._last is not None:
            self._last = None
            self._commit()

    # ---- the brush outline -------------------------------------------------

    def _style_cursor(self, tool: str):
        pen = QPen(QColor("#ffffff"))
        pen.setCosmetic(True)  # one screen pixel at any zoom
        pen.setStyle(Qt.DashLine if tool == "eraser" else Qt.SolidLine)
        self._cursor.setPen(pen)

    def mouse_moved(self, pos: QPointF):
        """The mouse is at this image position: the brush's outline follows."""
        if not self.paints():
            self._cursor.hide()
            return
        r = brush_size() / 2
        self._cursor.setRect(QRectF(pos.x() - r, pos.y() - r, 2 * r, 2 * r))
        self._cursor.show()

    def cursor_left(self):
        self._cursor.hide()

    def _on_size_changed(self, size: int):
        if not shiboken6.isValid(self.pane):
            self.detach()  # closed pane whose signal connection outlived it
            return
        if self._cursor.isVisible():
            c = self._cursor.rect().center()
            self._cursor.setRect(QRectF(c.x() - size / 2, c.y() - size / 2, size, size))

    def _on_tool_changed(self, name: str):
        if not shiboken6.isValid(self.pane):
            self.detach()  # closed pane whose signal connection outlived it
            return
        self._end_stroke()
        self._style_cursor(name)
        self._cursor.hide()  # back with the next mouse move, if it paints
        # pyqtgraph decides at the last mouse move which item gets the next
        # drag; a tool picked from the keyboard since then would leave that
        # choice to the old tool, so make it look again.
        scene = self.pane.view.scene()
        if getattr(scene, "lastHoverEvent", None) is not None:
            scene.lastHoverEvent = None

    # ---- sessions --------------------------------------------------------

    def to_json(self) -> dict | None:
        if self.is_empty():
            return None
        h, w = self.data.shape
        bits = zlib.compress(np.packbits(self.data).tobytes(), 9)
        return {"shape": [h, w], "bits": base64.b64encode(bits).decode("ascii")}

    def from_json(self, data: dict | None):
        """Restore a saved mask; one saved for an image of another size (the
        file changed since) is dropped."""
        if not data:
            return
        try:
            h, w = (int(v) for v in data["shape"])
            if (h, w) != tuple(self.pane.stack.shape_yx):
                return
            raw = np.frombuffer(zlib.decompress(base64.b64decode(data["bits"])), dtype=np.uint8)
            if raw.size != (h * w + 7) // 8:
                return  # not the bits of an image this size
            mask = np.unpackbits(raw, count=h * w).reshape(h, w).astype(bool)
        except (KeyError, TypeError, ValueError, zlib.error, binascii.Error):
            return
        self.set_array(mask, undoable=False)

    # ---- teardown ----------------------------------------------------------

    def detach(self):
        """Stop following the tool and the brush; called when the pane goes away."""
        if self._detached:
            return
        self._detached = True
        self._undo.clear()
        self.item.image = None
        for signal, slot in (
            (roi.tool_changed(), self._on_tool_changed),
            (_state.size_changed, self._on_size_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass  # already disconnected
