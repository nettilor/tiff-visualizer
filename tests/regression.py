"""Full regression suite. Run from the repo root:

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/regression.py

Exercises the whole feature matrix against example_stacks/. Exits 0 on
success (via os._exit to sidestep flaky Qt offscreen teardown).
"""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

TMP = tempfile.mkdtemp(prefix="tiffviz_test_")
# Any stack in the folder will do for the single-stack checks that do not
# depend on a particular file; the folder's contents change over time.
STACKS = sorted(p for p in Path("example_stacks").iterdir() if p.suffix.lower() in (".tif", ".tiff"))
SPARE_STACK = str(STACKS[-1])

from tiff_visualizer import settings as app_settings  # noqa: E402

app_settings.settings = lambda: QSettings(os.path.join(TMP, "s.ini"), QSettings.IniFormat)
app_settings.settings().setValue("preload/enabled", False)

from tiff_visualizer import (  # noqa: E402
    bc_panel,
    control_panel,
    session,
    stack_io,
    viewer,
    workspace,
)
from tiff_visualizer.bc_panel import show_bc_panel  # noqa: E402
from tiff_visualizer.viewer import PANE_MIME  # noqa: E402

app = QApplication(sys.argv)
PASSED = 0


def ok(name):
    global PASSED
    PASSED += 1
    print(f"  ✓ {name}")


def wait_until(cond, timeout_ms=6000):
    for _ in range(timeout_ms // 50):
        QTest.qWait(50)
        if cond():
            return True
    return False


class FakeDrop:
    def __init__(self, mime, x=10, y=10):
        self._m, self._x, self._y = mime, x, y
        self.accepted = False

    def mimeData(self):
        return self._m

    def position(self):
        from PySide6.QtCore import QPointF

        return QPointF(self._x, self._y)

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        pass


print("model layer")
s = stack_io.load_stack("example_stacks/XY05.tif")
assert s.data.shape == (8, 9, 4, 720, 960) and type(s.data).__name__ == "memmap"
assert s.composite and len(s.labels) == 288
ok("hyperstack load + metadata")
rgb = s.render(1, 4, [0, 1, 2, 3])
assert rgb.shape == (720, 960, 3) and rgb.max() > 0
assert s.render(1, 4, [0], stride=4).shape == (180, 240, 3)
mip = s.render(1, 0, [1], mip=True)
assert (mip >= s.render(1, 3, [1])).all()
ok("render: composite, stride, MIP")
proj = stack_io.project(s, "Z", "Max", 0, 8)
assert proj.data.shape == (8, 1, 4, 720, 960) and proj.name == "MAX_XY05.tif"
rt = os.path.join(TMP, "rt.tif")
s.ranges[1] = (5.0, 99.0)
s.save(rt)
r = stack_io.load_stack(rt)
assert np.allclose(r.ranges, s.ranges) and np.array_equal(r.luts, s.luts)
assert not stack_io.needs_decode(rt)
ok("projection, ImageJ save round-trip, needs_decode")

print("panes & navigation")
p1 = workspace.show_stack(stack_io.load_stack("example_stacks/XY05.tif"))
p2 = workspace.show_stack(stack_io.load_stack("example_stacks/XY06.tif"))
p3 = workspace.show_stack(stack_io.load_stack("example_stacks/XY07.tif"))
app.processEvents()
key = lambda k, m=Qt.NoModifier: QKeyEvent(QEvent.Type.KeyPress, k, m)
p1.handle_key(key(Qt.Key_Right))
p1.handle_key(key(Qt.Key_Right, Qt.ShiftModifier))
p1.handle_key(key(Qt.Key_Right, Qt.AltModifier))
assert p1.position() == (1, 1, 1)
ok("arrow keys with modifiers")
fit = p1.view.width() / p1.viewbox.viewRect().width()
p1.view._pinch_zoom(1 / 1.5, QPoint(p1.view.width() // 2, p1.view.height() // 2))
assert p1.view.width() / p1.viewbox.viewRect().width() > fit * 1.4
for _ in range(4):
    p1.zoom(1.3)
    app.processEvents()
assert abs(p1.view.width() / p1.viewbox.viewRect().width() - fit) / fit < 0.02
ok("pinch zoom + zoom-out cap at fit")
assert len(p1.channel_boxes) == 4
p1.channel_boxes[3].setChecked(False)
assert p1._display_channels() == [0, 1, 2]
p1.mip_box.setChecked(True)
assert "MIP" in p1.header_label.text() and not p1.bars["z"].isEnabled()
p1.mip_box.setChecked(False)
ok("channel boxes + MIP toggle")
# Right-click menu on the projection box swaps method on the fly; picking one
# while it is off also switches it on, and the label/header follow the method.
assert "MIP" in p1.mip_box.text()
menu = viewer.projection_menu(p1.mip_box, p1.proj_method, p1.set_proj_method)
labels = [a.text() for a in menu.actions()]
assert len(labels) == len(stack_io.PROJECTION_METHODS) and "Average (AVG)" in labels
assert [a.isChecked() for a in menu.actions()] == [True, False, False, False, False]
menu.actions()[2].trigger()  # Mean
assert p1.mip_box.isChecked() and p1.proj_method == "Mean"
assert "AVG" in p1.mip_box.text() and "z AVG" in p1.header_label.text()
assert p1._export_name("png").endswith("_AVG.png")
mean_rgb = p1._full_res_rgb()
p1.set_proj_method("Max")
assert not np.array_equal(mean_rgb, p1._full_res_rgb())  # cache keyed on method
p1.set_proj_method("Sum")
# Sum grows the display window with the slice count, so it reads like the mean
# while the pixel probe reports true sums.
assert np.abs(p1._full_res_rgb().astype(int) - mean_rgb.astype(int)).max() <= 2
t_, z_, c_ = p1.position()
sums = p1.stack.values_at(t_, z_, 5, 5, True, "Sum")
means = p1.stack.values_at(t_, z_, 5, 5, True, "Mean")
assert np.allclose(sums, means * p1.stack.n_slices, rtol=1e-4)
p1.set_proj_method("Max")
p1.mip_box.setChecked(False)
ok("z-projection methods: menu, live swap, label/header/export naming")
was_composite = p1._composite_on()
p1.channel_boxes[3].setChecked(True)
p1.composite_box.setChecked(True)
p1.handle_key(key(Qt.Key_2))
assert p1.visible_channels == [True, False, True, True]
p1.handle_key(key(Qt.Key_2))
assert p1.visible_channels == [True] * 4 and p1.channel_boxes[1].isChecked()
p1.composite_box.setChecked(False)  # single-channel view: digits jump instead
p1.handle_key(key(Qt.Key_4))
assert p1.position()[2] == 3
assert not p1.handle_key(key(Qt.Key_9))  # no 9th channel: key left unhandled
assert p1.position()[2] == 3
p1.handle_key(key(Qt.Key_2))
assert p1.position()[2] == 1
p1.composite_box.setChecked(was_composite)
ok("number keys 1-9 switch channels on/off")
p1.bars["t"].play_button.setChecked(True)
assert p1.bars["t"]._timer.isActive()
p1.handle_key(key(Qt.Key_Space))
assert not p1.bars["t"]._timer.isActive()
ok("playback + spacebar")

print("grid workspace")
w1 = next(w for w in viewer._open_windows if w.pane is p1)
w1.setGeometry(60, 60, 400, 350)
workspace.combine_all()
app.processEvents()
ws = workspace.get_workspace()
assert len(ws.panes) == 3 and p1.title_label.isVisibleTo(p1)
mime = QMimeData()
mime.setData(PANE_MIME, str(id(p3)).encode())
p1.dragEnterEvent(FakeDrop(mime))
assert p1._drop_indicator.isVisible()
p1.dropEvent(FakeDrop(mime))
assert ws.panes[0] is p3 and not p1._drop_indicator.isVisible()
ok("combine + drag reorder with indicator")
ws.grid_combo.setCurrentText("1 column")
assert ws.row_container.count() == 3
ws.grid_combo.setCurrentText("Auto")
ok("grid arrangements")
workspace.set_shared_axes(True)
app.processEvents()
ws.shared_bars["z"].set_value(5)
app.processEvents()
QTest.qWait(400)
assert p2.position()[1] == 5
p2.lock_button.setChecked(True)
ws.shared_bars["z"].set_value(2)
app.processEvents()
QTest.qWait(400)
assert p2.position()[1] == 5 and p1.position()[1] == 2
p2.lock_button.setChecked(False)
ok("shared axes + per-tile lock")
ws._set_active(p1)
ws.shared_channels_checkbox.setChecked(True)
p1.set_channel_visible(2, False)
app.processEvents()
assert p2.visible_channels[2] is False
assert len(ws.shared_channel_boxes) == 4
assert all(b.isVisibleTo(ws.shared_bars_box) for b in ws.shared_channel_boxes)
assert ws.shared_channel_boxes[2].isChecked() is False  # boxes mirror state
pj = workspace.show_stack(stack_io.load_stack("example_stacks/XY09.tif"))
app.processEvents()
assert pj.visible_channels[2] is False  # newcomer adopts shared state
assert ws.shared_channel_boxes[2].isChecked() is False  # boxes survive the join
ws.close_pane(pj)
app.processEvents()
ws.shared_channel_boxes[2].setChecked(True)
app.processEvents()
assert p1.visible_channels[2] is True and p2.visible_channels[2] is True
ws.shared_channels_checkbox.setChecked(False)
ok("shared channels propagation + shared c-bar boxes")
ws.shared_view_checkbox.setChecked(True)
p1.viewbox.setRange(xRange=(200, 600), padding=0)
app.processEvents()
assert abs(p2.viewbox.viewRect().left() - 200) < 2
ws.shared_view_checkbox.setChecked(False)
ok("shared view linking")
ws.mip_checkbox.setChecked(True)
assert all(p.mip_box.isChecked() for p in ws.panes)
ws.set_proj_method("Median")
assert all(p.proj_method == "Median" and p.mip_box.isChecked() for p in ws.panes)
assert "MED all" in ws.mip_checkbox.text()
ws.mip_checkbox.setChecked(False)
assert not any(p.mip_box.isChecked() for p in ws.panes)
ws.set_proj_method("Min")  # picking a method with the box off turns it back on
assert ws.mip_checkbox.isChecked() and all(p.proj_method == "Min" for p in ws.panes)
ws.mip_checkbox.setChecked(False)
ws.set_proj_method("Max", False)
for p in ws.panes:
    p.set_proj_method("Max", False)
ok("MIP all + projection method across the grid")
ws.minimal_checkbox.setChecked(True)
app.processEvents()
assert ws.shared_checkbox.isChecked()
assert not p1.close_button.isVisibleTo(p1) and p1.title_label.isVisibleTo(p1)
assert not p1.composite_box.isVisibleTo(p1)
assert not p1.probe_label.isVisibleTo(p1) and ws.probe_label.isVisibleTo(ws)
# The grid's bottom bar mirrors the hovered tile's pixel readout (the only
# readout in minimalist mode), named after the stack.
p1._on_mouse_moved(p1.viewbox.mapViewToScene(QPointF(10.5, 20.5)))
assert p1.probe_label.text().startswith("x=10 y=20  value: ")
assert ws.probe_label.text() == f"{p1.stack.name}:  {p1.probe_label.text()}"
p2._on_mouse_moved(p2.viewbox.mapViewToScene(QPointF(-5, -5)))  # off the image
assert ws.probe_label.text() == ""
ws.minimal_checkbox.setChecked(False)
app.processEvents()
assert p1.close_button.isVisibleTo(p1)
ok("minimalist mode on/off + bottom-bar pixel probe")
ws._set_active(p1)
QTest.keyClick(ws, Qt.Key_Return)
app.processEvents()
assert ws.solo_pane is p1
assert p1.isVisible() and not p2.isVisible() and not p3.isVisible()
ws.shared_bars["z"].set_value(3)  # scrub while soloed: hidden tiles defer rendering
app.processEvents()
assert p2._needs_refresh
QTest.keyClick(ws, Qt.Key_Escape)
app.processEvents()
QTest.qWait(400)
assert ws.solo_pane is None and p2.isVisible() and p3.isVisible()
assert p2.position()[1] == 3  # hidden tile caught up on show
p3.solo_requested.emit(p3)  # the header double-click path
assert ws.solo_pane is p3
ws.toggle_solo(p3)
app.processEvents()
ok("solo tile (Enter/Esc + double-click) with deferred hidden renders")
p1.handle_key(key(Qt.Key_F))
assert p1.flagged and p1.flag_label.isVisibleTo(p1)
assert ws.flag_checkbox.isVisibleTo(ws)
p2.set_flagged(True)
ws.flag_checkbox.setChecked(True)
app.processEvents()
assert p1.isVisible() and p2.isVisible() and not p3.isVisible()
p2.set_flagged(False)  # unflag while filtered: tile leaves the view
app.processEvents()
assert not p2.isVisible()
p1.set_flagged(False)  # last flag gone: filter auto-off, checkbox hides
app.processEvents()
assert not ws.flag_checkbox.isChecked() and not ws.flag_checkbox.isVisibleTo(ws)
assert p2.isVisible() and p3.isVisible()
ok("flag (F) + ★-only filter")
assert workspace._natural_key("XY2.tif") < workspace._natural_key("XY10.tif")
ws.sort_combo.setCurrentIndex(1)
ws._apply_sort()
assert [q.stack.name for q in ws.panes] == ["XY05.tif", "XY06.tif", "XY07.tif"]
ws.sort_combo.setCurrentIndex(2)
ws._apply_sort()
assert ws.panes == sorted([p1, p2, p3], key=lambda q: -q.mean_intensity())
ws.sort_combo.setCurrentIndex(1)
ws._apply_sort()
ws.move_pane(ws.panes[-1], ws.panes[0], after=False)  # a drag ends name order
assert ws.sort_combo.currentIndex() == 0
ok("tile sorting: natural name + brightness, drag resets to manual")
img = ws._montage_image(list(ws.panes), scale=2, labels=True)
cell_w, cell_h = 960 // 2, 720 // 2
label_h = max(16, cell_h // 16)
assert img.size == (2 * cell_w, 2 * (cell_h + label_h))  # 3 tiles -> auto 2x2
assert np.asarray(img).max() > 0
ok("grid montage frame")
# Provenance labels: each tile's channels, z (its projection when on) and t
# join its name, on a second line for every tile once any cell is too narrow.
assert viewer.position_caption(p1.stack, [0, 2], t=11, z=4) == "c 1+3  z 5  t 12"
assert viewer.position_caption(p1.stack, [1], t=0, proj="AVG") == "c 2  z AVG  t 1"
assert viewer.position_caption(p1.stack, [1]) == "c 2"  # varying axes left out
assert viewer.caption_lines("a.tif", "c 1") == ["a.tif  ·  c 1"]
assert viewer.caption_lines("a.tif", "c 1", split=True) == ["a.tif", "c 1"]
assert viewer.caption_lines("", "c 1", split=True) == ["c 1"]
wide = ws._montage_image(list(ws.panes), scale=2, labels=True, info=True)
assert wide.size == img.size  # 480 px cells hold name + position on one line
assert np.asarray(wide)[:label_h].sum() > np.asarray(img)[:label_h].sum()
narrow = ws._montage_image(list(ws.panes), scale=8, labels=True, info=True)
assert narrow.size == (2 * 120, 2 * (90 + 2 * 16))  # split: name / position
# GIF frames keep one size, including frames past a stack's end (black, "no image").
past_end = ws._montage_image(list(ws.panes), scale=8, labels=True, t=8, info=True)
assert past_end.size == narrow.size and np.asarray(past_end)[32:122, :120].max() == 0
# A tile blank on screen (shared position past its end) exports black too —
# in a PNG, and in GIF frames over t while the shared z is past its slices
# (its bars would still hold a stale slice) — unless a projection makes z moot.
held = p3._shared_pos
p3._shared_pos = (p3.stack.n_frames, 0, 0)  # shared t past its last frame
blank_sheet = np.asarray(ws._montage_image([p3], scale=8, labels=True, info=True))
assert blank_sheet[32:].max() == 0 and blank_sheet[:32].max() > 0
p3._shared_pos = (0, p3.stack.n_slices + 2, 0)  # shared z past its last slice
assert np.asarray(ws._montage_image([p3], scale=8, labels=True, t=0, info=True))[32:].max() == 0
p3.mip_box.setChecked(True)
assert np.asarray(ws._montage_image([p3], scale=8, labels=True, t=0, info=True))[32:].max() > 0
p3.mip_box.setChecked(False)
p3._shared_pos = held
p3.refresh()
assert not p3._blank
ok("grid montage: channel/z/t labels, split when narrow, stable GIF layout")
workspace.set_shared_axes(False)
workspace.split_all()
app.processEvents()
n1 = next(w for w in viewer._open_windows if w.pane is p1)
assert (n1.geometry().x(), n1.geometry().y()) == (60, 60)
ok("split restores window geometry")

print("async grid rendering")
workspace.combine_all()
workspace.set_shared_axes(True)
app.processEvents()
for p in ws.panes:
    p._plane_cache.clear()
ws.shared_bars["z"].set_value(7)
app.processEvents()
assert wait_until(
    lambda: all(
        np.array_equal(
            p.image_item.image, p.stack.render(0, 7, p._display_channels(), p._last_stride)
        )
        for p in ws.panes
    )
), "async renders did not converge"
ok("parallel renders converge")
panel = show_bc_panel(p1)
app.processEvents()
panel.controls.channel_group.button(2).click()  # "Ch 3" radio
app.processEvents()
assert p1.position()[2] == 2 and ws.shared_bars["c"].value() == 2
assert p2.position()[2] == 2  # shared, so every tile follows
p1.lock_button.setChecked(True)
panel.controls.channel_group.button(0).click()
app.processEvents()
assert p1.position()[2] == 0 and ws.shared_bars["c"].value() == 2  # locked: own bar only
p1.lock_button.setChecked(False)
app.processEvents()
ok("B&C channel radio switches channel under shared axes")
workspace.set_shared_axes(False)
workspace.split_all()
app.processEvents()

print("B&C")
panel = show_bc_panel(p1)
app.processEvents()
panel.controls.region.setRegion((-50, 400))
lo, hi = panel.controls.region.getRegion()
assert lo >= 0 and hi <= 255
ok("range clamped to dtype bounds")
p1.set_channel(0)
app.processEvents()
panel.controls.max_spin.setValue(77)
assert tuple(p1.stack.ranges[0]) == (0.0, 77.0)
panel.controls._on_apply_all()
assert tuple(p2.stack.ranges[0]) == (0.0, 77.0)
bc_panel.undo_last_range_change()
assert tuple(p2.stack.ranges[0]) != (0.0, 77.0)
bc_panel.undo_last_range_change()
ok("apply-to-all + undo")
p1.toggle_bc_dock()
assert p1.bc_dock is not None
p1.swap_bc_side()
assert p1.bc_side == "left"
p1.close_bc_dock()
assert p1.bc_dock is None
ok("fused dock lifecycle")

print("measurements")
from tiff_visualizer import measure  # noqa: E402

measure.clear()
p1.set_channel(1)
p1.bars["z"].set_value(3)
p1.bars["t"].set_value(2)
app.processEvents()
plane = np.asarray(p1.stack.data[2, 3, 1])
m = p1.measurement()
assert (m.stack, m.c, m.z, m.t) == ("XY05.tif", 2, "4", 3)
assert m.mean == float(plane.mean()) and m.min == float(plane.min()) and m.max == float(plane.max())
assert m.roi == "whole image" and m.area == plane.size
assert m.integer and m.cells()[6] == f"{plane.mean():.3f}" and m.cells()[8] == str(plane.max())
p1.mip_box.setChecked(True)
mp = p1.measurement()
assert mp.z == "MIP" and mp.max >= m.max and mp.mean >= m.mean
p1.set_proj_method("Mean")
assert not p1.measurement().integer  # AVG projection is float
p1.set_proj_method("Max", enable=False)
p1.mip_box.setChecked(False)
ok("measure: active channel at position, projected plane when MIP is on")
# Cmd+M is the Analyze > Measure action of every window; it fills the table
# without taking focus from the stack, and a blank tile measures nothing.
measure_action = next(a for a in p1.window().findChildren(QAction) if a.text() == "&Measure")
assert measure_action.shortcut().toString() == "Ctrl+M"
p1.window().activateWindow()
measure_action.trigger()
measure_action.trigger()
app.processEvents()
table = measure._table
assert table is not None and table.isVisible() and table.table.rowCount() == 2
assert len(measure.measurements()) == 2 and table.testAttribute(Qt.WA_ShowWithoutActivating)
assert table.table.item(1, 0).text() == "2" and table.table.item(1, 1).text() == "XY05.tif"
p1._blank = True
p1.measure()
p1._blank = False
assert len(measure.measurements()) == 2
p2.measure()
tsv = measure.to_tsv().splitlines()
assert tsv[0].split("\t") == list(measure.COLUMNS) and len(tsv) == 4
assert tsv[3].split("\t")[:2] == ["3", "XY06.tif"]
assert measure.to_tsv([2]).splitlines()[1].startswith("3\tXY06.tif")
csv_path = os.path.join(TMP, "m.csv")
measure.save_csv(csv_path)
assert open(csv_path).read().splitlines()[0] == ",".join(measure.COLUMNS)
table.table.selectRow(0)
table.delete_rows()
assert [m.stack for m in measure.measurements()] == ["XY05.tif", "XY06.tif"]
assert table.table.item(1, 0).text() == "2"  # renumbered
measure.clear()
assert table.table.rowCount() == 0 and not table.copy_button.isEnabled()
table.close()
ok("measurements table: Cmd+M appends, copy/CSV, delete rows, clear")

print("selections")
from tiff_visualizer import roi  # noqa: E402

sel = p1.selection
plane = np.asarray(p1.stack.data[2, 3, 1])
sel.set_rect(10, 20, 64, 48)
(ys, xs), mask = sel.mask()
assert (ys, xs) == (slice(20, 68), slice(10, 74)) and mask.all()
m = p1.measurement()
sub = plane[20:68, 10:74]
assert m.roi == "rect 10,20 64×48" and m.area == 64 * 48
assert m.mean == float(sub.mean()) and m.min == sub.min() and m.max == sub.max()
sel.set_rect(-30, -30, 50, 50)  # clipped to the image
assert sel.describe() == "rect 0,0 20×20"
sel.set_rect(900, 700, 200, 200, ellipse=True)
assert sel.kind == "ellipse" and sel.describe() == "ellipse 900,700 60×20"
sel.set_rect(100, 100, 100, 60, ellipse=True)
area = p1.measurement().area
assert abs(area - np.pi * 50 * 30) / (np.pi * 50 * 30) < 0.02  # rasterized ellipse
sel.set_polygon([(0, 0), (100, 0), (0, 100), (0, 0)])  # right triangle, closing repeat dropped
assert sel.kind == "polygon" and len(sel.roi.points()) == 3
assert abs(p1.measurement().area - 5000) < 100
sel.set_freehand([(50, 50), (150, 50), (150, 150), (50, 150)])
assert sel.kind == "freehand" and p1.measurement().area == 100 * 100
sel.set_polygon([(0, 0), (1, 1)])  # too few corners: nothing
assert sel.kind == "freehand"
sel.set_polygon([(2000, 2000), (2100, 2000), (2000, 2100)])  # entirely outside the image
assert p1.measurement() is None
sel.select_all()
assert sel.describe() == "rect 0,0 960×720" and p1.measurement().area == plane.size
ok("selection masks: rect, ellipse, polygon, freehand, clipping, select all")

# Drawing through the ViewBox: a rectangle by drag, a polygon by clicks.
class FakeMouse:
    def __init__(self, vb, x, y, start=False, finish=False, double=False, x0=None, y0=None):
        self._vb, self._start, self._finish, self._double = vb, start, finish, double
        self._pos = vb.mapFromView(QPointF(x, y))
        self._down = vb.mapFromView(QPointF(x if x0 is None else x0, y if y0 is None else y0))
        self.accepted = False

    def button(self):
        return Qt.LeftButton

    def pos(self):
        return self._pos

    def buttonDownPos(self, *_):
        return self._down

    def isStart(self):
        return self._start

    def isFinish(self):
        return self._finish

    def double(self):
        return self._double

    def accept(self):
        self.accepted = True


vb = p1.viewbox
assert not sel.drag(FakeMouse(vb, 5, 5, start=True))  # hand tool: the ViewBox pans
rect_action = next(a for a in p1.window().findChildren(QAction) if a.text() == "Rectangle Tool")
assert rect_action.shortcut().toString() == "R"
rect_action.trigger()
assert roi.tool() == "rect"
ctrl = control_panel.get_control_window()
assert ctrl.tool_buttons["rect"].isChecked()
sel.drag(FakeMouse(vb, 30.4, 40.6, start=True))
sel.drag(FakeMouse(vb, 90.2, 70.1, x0=30.4, y0=40.6))
assert sel.drawing() and sel._preview.isVisible() and p1.probe_label.text() == "rect 30,41 60×29"
sel.drag(FakeMouse(vb, 94.4, 100.6, finish=True, x0=30.4, y0=40.6))
assert not sel.drawing() and sel.describe() == "rect 30,41 64×60"
assert sel.click(FakeMouse(vb, 50, 50)) and sel.roi is not None  # inside: kept
assert sel.click(FakeMouse(vb, 500, 500)) and sel.roi is None  # outside: cleared
roi.set_tool("polygon")
for x, y in ((10, 10), (110, 10), (110, 110)):
    sel.click(FakeMouse(vb, x, y))
assert sel.drawing() and "3 corners" in p1.probe_label.text()
sel.click(FakeMouse(vb, 10, 110))
sel.click(FakeMouse(vb, 10, 110, double=True))
assert not sel.drawing() and sel.kind == "polygon" and p1.measurement().area == 100 * 100
roi.set_tool("freehand")
sel.drag(FakeMouse(vb, 0, 0, start=True))
sel.drag(FakeMouse(vb, 200, 0, x0=0, y0=0))
sel.drag(FakeMouse(vb, 200, 100, x0=0, y0=0))
sel.drag(FakeMouse(vb, 0, 100, finish=True, x0=0, y0=0))
assert sel.kind == "freehand" and p1.measurement().area == 200 * 100
roi.set_tool("polygon")
sel.click(FakeMouse(vb, 10, 10))
assert sel.drawing() and p1.handle_key(key(Qt.Key_Escape)) and not sel.drawing()
assert sel.roi is None  # starting a new shape dropped the freehand one
sel.select_all()
assert p1.handle_key(key(Qt.Key_Escape)) and sel.roi is None
assert not p1.handle_key(key(Qt.Key_Escape))  # nothing to clear: not handled
sel.select_all()
assert sel.roi.translatable  # polygon tool still active: drag inside moves it
roi.set_tool("hand")
assert p1.view.viewport().cursor().shape() == Qt.ArrowCursor
assert not sel.roi.translatable  # the hand pans even over a whole-image selection
sel.clear()
ok("drawing tools: drag rect, click polygon, trace freehand, click outside, Esc")

print("sessions")
workspace.combine_all()
app.processEvents()
ws.minimal_checkbox.setChecked(True)
app.processEvents()
p1.bars["t"].set_value(3)
p1.set_flagged(True)
p1.set_proj_method("Median")
p1.selection.set_polygon([(5, 5), (60, 5), (30, 50)])
p2.selection.set_rect(10, 20, 30, 40, ellipse=True)
data = session.capture()
assert next(e for e in data["stacks"] if e["path"].endswith("XY05.tif"))["roi"]["kind"] == "polygon"
assert next(e for e in data["stacks"] if e["path"].endswith("XY05.tif"))["proj"] == "Median"
assert data["workspace"]["minimalist"] is True
grid_order = [q.stack.name for q in workspace.get_workspace().panes]
assert [Path(e["path"]).name for e in data["stacks"]] == grid_order  # grid order kept
assert next(e for e in data["stacks"] if e["path"].endswith("XY05.tif"))["flagged"] is True
session.close_all()
app.processEvents()
assert len(viewer._all_panes) == 0
session.restore(data)
app.processEvents()
assert len(viewer._all_panes) == 3 and workspace.workspace_active()
assert workspace.get_workspace().minimal_checkbox.isChecked()
restored = next(p for p in viewer._all_panes if p.stack.name == "XY05.tif")
assert not restored.close_button.isVisibleTo(restored)  # minimalist applied
ws = workspace.get_workspace()
assert [q.stack.name for q in ws.panes] == grid_order
assert restored.flagged and ws.flag_checkbox.isVisibleTo(ws)
assert restored.proj_method == "Median" and restored.mip_box.isChecked()
assert "MED" in restored.mip_box.text() and "z MED" in restored.header_label.text()
assert restored.selection.describe() == "polygon 5,5 55×45"
restored2 = next(p for p in viewer._all_panes if p.stack.name == "XY06.tif")
assert restored2.selection.describe() == "ellipse 10,20 30×40"
assert next(p for p in viewer._all_panes if p.stack.name == "XY07.tif").selection.roi is None
restored.mip_box.setChecked(False)
restored.set_proj_method("Max", False)
ws.minimal_checkbox.setChecked(False)
ok("session round-trip incl. minimalist, grid order, flags and selections")
session.close_all()
app.processEvents()

print("control window & folders")
ctrl = control_panel.get_control_window()
ctrl.show()
app.processEvents()
folder = Path("example_stacks").resolve()
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(folder))])
ctrl.dropEvent(FakeDrop(mime))
app.processEvents()
section = next(iter(ctrl.folder_sections.values()))
n_tiffs = len([f for f in folder.iterdir() if f.suffix.lower() in (".tif", ".tiff")])
assert len(section.checks) == n_tiffs
box = section.checks[folder / "XY10.tif"]
box.setChecked(True)
app.processEvents()
assert any(p.stack.name == "XY10.tif" for p in viewer._all_panes)
assert section.open_all_button.isEnabled() and section.close_all_button.isEnabled()
box.setChecked(False)
app.processEvents()
assert not viewer._all_panes
assert not section.close_all_button.isEnabled()  # nothing open to close
ctrl.remove_folder_section(section)
ok("folder swap list open/close")

ctrl.dropEvent(FakeDrop(mime))
app.processEvents()
section = next(iter(ctrl.folder_sections.values()))
# Open all with the grid active: adding a tile refreshes the control window,
# which re-syncs this list mid-batch — that must not re-fire the per-file
# handler and open every stack a second time.
section.checks[sorted(section.checks)[0]].setChecked(True)
app.processEvents()
workspace.combine_all()
app.processEvents()
section.open_all()
app.processEvents()
assert len(viewer._all_panes) == n_tiffs, len(viewer._all_panes)  # no duplicates
assert all(b.isChecked() for b in section.checks.values())
assert not section.open_all_button.isEnabled()  # everything is open already
section.close_all()  # closing must reach panes tiled in the grid too
app.processEvents()
assert not viewer._all_panes and not any(b.isChecked() for b in section.checks.values())
assert section.open_all_button.isEnabled() and not section.close_all_button.isEnabled()
ctrl.remove_folder_section(section)
ok("folder list open all / close all")

# A tall saved geometry (window stretched for folder lists) must not carry
# over to a fresh start, where no folder sections exist yet.
ctrl.resize(ctrl.width(), 560)
app.processEvents()
app_settings.settings().setValue("control/geometry", ctrl.saveGeometry())
fresh = control_panel.ControlWindow()
fresh.show()
app.processEvents()
assert fresh.height() <= fresh.sizeHint().height() + 2, fresh.height()
assert fresh.height() < 500, fresh.height()
fresh.hide()
fresh.deleteLater()
app.processEvents()
ok("control window height compacts on startup")

# Dock-icon clicks: macOS reports the reopen as a *second* Active state with no
# Inactive in between (the plain activation, then Qt's forced one from
# applicationShouldHandleReopen:), so only those raise the control window —
# cmd-tab and clicking a stack window report Active once and must leave the
# window order alone.
from tiff_visualizer.__main__ import ReopenDetector  # noqa: E402

d = ReopenDetector()
assert not d.saw_state(True)  # launch
assert d.saw_state(True)  # dock click while the app is already frontmost
assert not d.saw_state(False)  # user switches away
assert not d.saw_state(True)  # cmd-tab back / click on a stack window
assert d.saw_state(True)  # dock click from the background: activation + forced repeat
assert not d.saw_state(False) and not d.saw_state(True)
ctrl.showMinimized()
app.processEvents()
control_panel.bring_to_front()
app.processEvents()
assert ctrl.isVisible() and not ctrl.isMinimized()
ok("dock-icon reopen detection + control window raise")

print("preload")
app_settings.settings().setValue("preload/enabled", True)
app_settings.settings().setValue("preload/gb", 4)
p = workspace.show_stack(stack_io.load_stack(SPARE_STACK))
assert wait_until(lambda: p.stack.in_memory), "preload did not complete"
ok("RAM preload within budget")
app_settings.settings().setValue("preload/enabled", False)

print("export")
rgb = p._full_res_rgb()
img = p._rgb_to_qimage(rgb)
assert img.width() == 960
img.save(os.path.join(TMP, "x.png"))
from PIL import Image

frames = [Image.fromarray(p._full_res_rgb(t=i)) for i in range(3)]
gif = os.path.join(TMP, "x.gif")
frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
assert Image.open(gif).n_frames == 3
ok("PNG + GIF export")

# Stack montage: t across columns, z down rows (XY11 is 8t x 9z, 720x960).
mont = p._stack_montage_image([0, 2, 4], [0, 4], p._display_channels(), 4, True)
header = max(16, 180 // 16)
gutter = int(header * 2.4)
assert mont.size == (gutter + 3 * 240, header + 2 * 180)
assert np.asarray(mont).max() > 0
# One varying axis wraps into a near-square grid (4 tiles -> 2x2), no gutters.
wrap = p._stack_montage_image([0, 1, 2, 3], [0], [0], 2, True)
assert wrap.size == (2 * 480, 2 * 360)
# MIP collapses z: each tile must dominate its single-slice counterpart.
mip_sheet = np.asarray(p._stack_montage_image([0, 1], [0], [1], 4, False, mip=True))
flat_sheet = np.asarray(p._stack_montage_image([0, 1], [0], [1], 4, False, mip=False))
assert (mip_sheet >= flat_sheet).all() and mip_sheet.sum() > flat_sheet.sum()
# Explicit layouts for a single varying axis: one row, one column, and a
# custom grid that grows rows so every tile fits (surplus cells stay black).
row = p._stack_montage_image([0, 1, 2, 3], [0], [0], 2, True, grid=(4, 1))
assert row.size == (4 * 480, 360)
col = p._stack_montage_image([0, 1, 2, 3], [0], [0], 2, True, grid=(1, 4))
assert col.size == (480, 4 * 360)
custom = np.asarray(p._stack_montage_image([0, 1, 2, 3], [0], [0], 2, True, grid=(3, 1)))
assert custom.shape[:2] == (2 * 360, 3 * 480)
assert custom[360:, 480:].max() == 0 and custom[360:, :480].max() > 0
assert viewer._montage_grid(8) == (3, 3) and viewer._montage_grid(8, (5, 5)) == (5, 5)
# The title line (stack name, channels, projection) sits above the t headers;
# a sheet too narrow for one line gets name and position on two.
titled = p._stack_montage_image([0, 2, 4], [0, 4], p._display_channels(), 4, True, title=True)
assert titled.size == (mont.width, mont.height + header)
assert (np.asarray(titled)[header:] == np.asarray(mont)).all()
one_ch = p._stack_montage_image([0, 1], [0], [1], 8, True, mip=True, grid=(1, 2), title=True)
assert one_ch.size == (120, 16 + 2 * 90)  # "XY43.tif  ·  c 2  z MIP" fits
tall = p._stack_montage_image([0, 1], [0], [0, 1, 2, 3], 8, True, mip=True, grid=(1, 2), title=True)
assert tall.size == (120, 2 * 16 + 2 * 90)  # "c 1+2+3+4  z MIP" does not
ok("stack montage: t×z sheet, wrapped axis, MIP option, row/column/custom grids, title")

# Montage dialog: the layout applies only while a single axis varies; the
# grid boxes show the effective grid and are editable in Custom, where the
# other dimension grows only when the typed one would drop tiles.
d = viewer.StackMontageDialog(p)
assert not d.layout_combo.isEnabled() and d.values()[6] is None  # t×z is fixed
assert d.values()[7] is True  # the provenance title is on by default
d.z_combo.setCurrentIndex(1)  # MIP collapses z -> only t varies (8 tiles)
assert d.layout_combo.isEnabled() and d._grid() == (3, 3, False)
assert d.values()[6] is None and d.estimate.text().endswith("1 empty")
d.layout_combo.setCurrentIndex(1)  # one row
assert d.values()[6] == (8, 1) and not d.cols_spin.isEnabled()
d.layout_combo.setCurrentIndex(2)  # one column
assert d.values()[6] == (1, 8)
d.layout_combo.setCurrentIndex(3)  # custom
assert d.cols_spin.isEnabled() and d.rows_spin.isEnabled()
d.cols_spin.setValue(4)  # 4×8 still fits everything -> rows kept
assert d.values()[6] == (4, 8)
d.rows_spin.setValue(1)  # 4×1 would drop tiles -> cols grows to 8
assert d.values()[6] == (8, 1)
d.cols_spin.setValue(3)  # 3×1 -> rows grows to 3
assert d.values()[6] == (3, 3) and "empty" in d.estimate.text()
d.t_spin.setValue(3)  # every 3rd t -> 3 tiles in a 3×3 grid, 6 empty
assert d.values()[6] == (3, 3) and d.estimate.text().endswith("6 empty")
d.z_combo.setCurrentIndex(0)  # back to t×z -> layout ignored again
assert d.values()[6] is None and not d.cols_spin.isEnabled()
# t/z ranges narrow the sheet; reversed boxes still read low-high, and a
# collapsed z ignores its range (the projection covers every slice).
d.t_spin.setValue(1)
d.t_range.start_spin.setValue(3)
d.t_range.stop_spin.setValue(6)
d.z_range.start_spin.setValue(9)
d.z_range.stop_spin.setValue(4)
assert d._counts() == (4, 6) and d.values()[8:] == ((2, 5), (3, 8))
assert d.estimate.text().startswith("4×6 tiles")
d.z_combo.setCurrentIndex(1)
assert not d.z_range.isEnabled() and d._counts() == (4, 1)
d.deleteLater()
r = viewer.AxisRange(8)
assert r.values() == (0, 7) and not r.narrowed() and r.remembered_text() == "full"
r.restore_text("3-12")  # saved on a longer stack: clamps to this one
assert r.values() == (2, 7) and r.span() == "3-8" and r.indices(2) == [2, 4, 6]
r.restore_text("full")
assert not r.narrowed()
r.restore_text("junk")
assert not r.narrowed()
ok("stack montage dialog: layout modes, linked grid boxes, t×z lockout, t/z ranges")

# Dialogs reopen with the options last accepted; Cancel forgets nothing.
d = viewer.StackMontageDialog(p)
d.z_combo.setCurrentIndex(1)  # MIP -> t only, every 2nd t = 4 tiles
d.t_spin.setValue(2)
d.layout_combo.setCurrentIndex(3)
d.cols_spin.setValue(4)
d.rows_spin.setValue(1)
d.scale_combo.setCurrentIndex(2)
d.labels_box.setChecked(False)
d.title_box.setChecked(False)
d.accept()
d = viewer.StackMontageDialog(p)
assert d.values() == (2, 1, True, 4, False, False, (4, 1), False, (0, 7), (0, 8)), d.values()
d.scale_combo.setCurrentIndex(0)
d.reject()
assert viewer.StackMontageDialog(p).values()[3] == 4  # Cancel did not save
# A pane whose MIP is on preselects the MIP export over a remembered "all slices".
d = viewer.StackMontageDialog(p)
d.z_combo.setCurrentIndex(0)
d.accept()
assert viewer.StackMontageDialog(p).values()[2] is False
p.mip_box.setChecked(True)
assert viewer.StackMontageDialog(p).values()[2] is True
p.mip_box.setChecked(False)
# Ranges are remembered only while narrowed.
d = viewer.StackMontageDialog(p)
d.z_range.start_spin.setValue(2)
d.z_range.stop_spin.setValue(5)
d.accept()
d = viewer.StackMontageDialog(p)
assert d.values()[8:] == ((0, 7), (1, 4))
assert app_settings.settings().value("dialogs/stackMontage/tRange") == "full"
d.z_range.restore_text("full")
d.accept()
# Movie: axis + fps + the animated axis's range.
d = viewer.ExportMovieDialog(p)
d.axis_combo.setCurrentText("Z")
d.fps_spin.setValue(25)
assert d.ranges["Z"].isEnabled() and not d.ranges["T"].isEnabled()
d.ranges["Z"].start_spin.setValue(3)
d.ranges["Z"].stop_spin.setValue(5)
d.accept()
assert viewer.ExportMovieDialog(p).values() == ("Z", 25, (2, 4))
d = viewer.ExportMovieDialog(p)
d.axis_combo.setCurrentText("T")
assert d.values() == ("T", 25, (0, 7)) and d.ranges["T"].isEnabled()
d.reject()
p.mip_box.setChecked(True)  # MIP hides Z: a forced single-entry axis is no choice
d = viewer.ExportMovieDialog(p)
assert d.axis_combo.count() == 1 and "Z" not in d.ranges
d.accept()
p.mip_box.setChecked(False)
assert viewer.ExportMovieDialog(p).values() == ("Z", 25, (2, 4))  # z range kept
# Grid montage: the remembered resolution beats the GIF half-size default.
d = workspace.MontageDialog(None, [p])
assert d.info_box.isChecked()  # channel/z/t labels on by default
d.mode_combo.setCurrentIndex(1)  # movie over t -> scale bumps to Half
assert d.scale_combo.currentData() == 2
d.scale_combo.setCurrentIndex(0)
d.fps_spin.setValue(7)
d.labels_box.setChecked(False)
d.info_box.setChecked(False)
assert d.ranges["T"].isEnabled() and not d.ranges["Z"].isEnabled()
d.ranges["T"].start_spin.setValue(4)
d.accept()
d = workspace.MontageDialog(None, [p])
assert d.values() == ("gif", "T", 7, 1, False, False, (3, 7))
d.mode_combo.setCurrentIndex(0)  # a PNG has no range
assert d.values()[6] is None and not d.ranges["T"].isEnabled()
d.reject()
# Projection: axis + method, and the slice range only when it was narrowed.
d = viewer.ProjectionDialog(p)
d.method_combo.setCurrentText("Sum")
d.start_spin.setValue(3)
d.stop_spin.setValue(5)
d.accept()
d = viewer.ProjectionDialog(p)
assert d.values() == ("Z", "Sum", 2, 4)
d.start_spin.setValue(1)
d.stop_spin.setValue(9)  # full range again
d.accept()
assert app_settings.settings().value("dialogs/projection/range") == "full"
d = viewer.ProjectionDialog(p)
assert d.values() == ("Z", "Sum", 0, 8)
d.axis_combo.setCurrentText("T")
d.stop_spin.setValue(50)  # clamps to the 8 frames
d.accept()
assert viewer.ProjectionDialog(p).values() == ("T", "Sum", 0, 7)
ok("dialogs remember last accepted options (montage, grid montage, movie, projection)")

# Exports honor the ranges end to end, and default names say what's inside.
from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QDialog, QFileDialog  # noqa: E402

offered_names = []


def save_as(name):
    def pick(_parent, _title, start, _filter):
        offered_names.append(Path(start).name)
        return os.path.join(TMP, name), ""
    return staticmethod(pick)


real_save = QFileDialog.getSaveFileName
t0, z0, _c0 = p.position()
stem = Path(p.stack.name).stem
try:
    viewer.ExportMovieDialog.exec = lambda self: QDialog.Accepted  # remembered: Z 3-5
    QFileDialog.getSaveFileName = save_as("movie.gif")
    p.export_movie()
    assert Image.open(os.path.join(TMP, "movie.gif")).n_frames == 3
    assert offered_names[-1] == f"{stem}_t{t0 + 1}_z3-5.gif"
    viewer.StackMontageDialog.exec = lambda self: (
        self.t_range.restore_text("2-4"), self.t_spin.setValue(1), QDialog.Accepted
    )[-1]
    QFileDialog.getSaveFileName = save_as("sheet.png")
    p.export_stack_montage()
    sheet = Image.open(os.path.join(TMP, "sheet.png"))
    stride = viewer.StackMontageDialog(p).values()[3]
    assert offered_names[-1] == f"{stem}_montage_t2-4.png"
    # t 2-4 across, all 9 z down; labels and title were remembered off above.
    assert sheet.size == (3 * 960 // stride, 9 * 720 // stride), sheet.size
    workspace.MontageDialog.exec = lambda self: QDialog.Accepted  # remembered: GIF t 4-8
    QFileDialog.getSaveFileName = save_as("grid.gif")
    workspace.combine_all()
    app.processEvents()
    workspace.get_workspace()._export_montage()
    assert Image.open(os.path.join(TMP, "grid.gif")).n_frames == 5
    assert offered_names[-1] == "montage_t4-8.gif"
    workspace.split_all()
finally:
    QFileDialog.getSaveFileName = real_save
    for cls in (viewer.ExportMovieDialog, viewer.StackMontageDialog, workspace.MontageDialog):
        cls.exec = QDialog.exec
ok("exports: movie, stack montage and grid GIF follow their ranges and name them")

print("updates")
from datetime import datetime, timedelta  # noqa: E402

from tiff_visualizer import updater  # noqa: E402

V = updater.parse_version
assert V("v1.3") == V("1.3") == V("1.3.0") == (1, 3)
assert V("1.10") > V("1.9") and V("1.2.1") > V("1.2") and V("2.0") > V("1.99")
assert V("1.2.3-beta") is None and V("") is None and V("v") is None and V("latest") is None
assert updater.display_version("v1.3") == "1.3" and updater.display_version("1.3") == "1.3"
rel = updater.Release("v1.2.0", "u", "", ())
assert updater.verdict(rel, "1.1.0", None, False) == "update"
assert updater.verdict(rel, "1.2", None, False) == "up-to-date"  # 1.2 == 1.2.0
assert updater.verdict(rel, "1.3.0", None, False) == "up-to-date"
assert updater.verdict(rel, None, None, True) == "up-to-date"
# A malformed tag must never produce an alert.
assert updater.verdict(updater.Release("nightly", "u", "", ()), "1.1.0", None, True) == "up-to-date"
# Skipping is honored by the daily check and ignored when the user asks by hand.
assert updater.verdict(rel, "1.1.0", "v1.2", False) == "skipped"
assert updater.verdict(rel, "1.1.0", "v1.2", True) == "update"
assert updater.verdict(rel, "1.1.0", "v1.1.5", False) == "update"
ok("version parsing + update verdict")

assets = (
    ("TIFF-Visualizer-1.2.0-windows.zip", "w"),
    ("TIFF-Visualizer-1.2.0-macos.zip", "z"),
    ("TIFF Visualizer-1.2.0.dmg", "d"),
)
r = updater.Release("v1.2.0", "u", "", assets)
assert r.asset_for_platform("darwin")[1] == "d"  # dmg beats the mac zip
assert r.asset_for_platform("win32")[1] == "w"
assert updater.Release("v1", "u", "", assets[:1]).asset_for_platform("darwin") is None
assert updater.Release("v1", "u", "", assets[1:2]).asset_for_platform("darwin")[1] == "z"
assert updater.Release("v1", "u", "", ()).asset_for_platform("darwin") is None
assert not updater.is_due(datetime.now() - timedelta(hours=3))
assert updater.is_due(datetime.now() - timedelta(hours=21)) and updater.is_due(None)
assert updater.notes_excerpt("").strip() == ""
assert updater.notes_excerpt("a\n\nb\nc\nd\ne\nf").endswith("…")
ok("asset pick per platform + check interval")

downloads = Path(TMP) / "downloads"
downloads.mkdir()
(downloads / "app.dmg").write_text("older")
assert updater.free_destination("app.dmg", downloads).name == "app 2.dmg"
assert updater.free_destination("noext", downloads).name == "noext"
payload = Path(TMP) / "payload.bin"
payload.write_bytes(b"x" * 500_000)
seen = []
target = updater.free_destination("app.dmg", downloads)
updater.download(payload.as_uri(), target, on_progress=lambda a, b: seen.append((a, b)))
assert target.read_bytes() == payload.read_bytes() and seen and seen[-1][1] == 500_000
assert not list(downloads.glob("*.part"))
ok("download to a free Downloads slot")

# The whole path minus the network and the alert: worker thread, queued signal
# back to the main thread, verdict, and the once-a-day stamp.
offered = []
updater._offer = lambda release, manual: offered.append((release.tag, manual))
updater.fetch_latest = lambda *a, **k: updater.Release("v99.0", "u", "notes", ())
app_settings.settings().remove("updates/lastCheck")
assert updater.auto_check_enabled()
updater._check(manual=False)
assert wait_until(lambda: bool(offered)) and offered[0] == ("v99.0", False)
assert not updater.is_due(updater._last_check())  # stamped, so no second look today
failures = []
updater._say_check_failed = lambda error: failures.append(error)
updater.fetch_latest = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
updater._check(manual=False)
assert not wait_until(lambda: bool(failures), 800)  # silent when the check is automatic
updater._check(manual=True)
assert wait_until(lambda: bool(failures))
ok("check flow: offer on newer, silent when offline")

print("bug-fix pass")
# ---- bug-fix pass: stack_io ----
import stat  # noqa: E402
import warnings  # noqa: E402

import tifffile  # noqa: E402

io_dir = Path(TMP) / "io_fixes"
io_dir.mkdir()
hyper = np.random.default_rng(0).integers(0, 4000, (2, 3, 2, 64, 80), dtype=np.uint16)
# Save As over a file that is still memory-mapped: this stack after an
# earlier Save As (A -> B -> A), or another open copy of the same file.
file_a, file_b = io_dir / "A.tif", io_dir / "B.tif"
tifffile.imwrite(file_a, hyper, imagej=True, metadata={"axes": "TZCYX"})
sa = stack_io.load_stack(file_a)
assert isinstance(sa.data, np.memmap)
sa.save(file_b)
sa.save(file_a)
assert np.array_equal(np.asarray(stack_io.load_stack(file_a).data), hyper)
copy1, copy2 = stack_io.load_stack(file_b), stack_io.load_stack(file_b)
copy1.save(file_b)
assert not isinstance(copy2.data, np.memmap) and np.array_equal(copy2.data, hyper)
assert np.array_equal(np.asarray(stack_io.load_stack(file_b).data), hyper)
ok("save over a memory-mapped file (A→B→A, same file open twice) keeps the data")
# Read-only files open, mapped read-only.
ro = io_dir / "ro.tif"
tifffile.imwrite(ro, hyper, imagej=True, metadata={"axes": "TZCYX"})
os.chmod(ro, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
sro = stack_io.load_stack(ro)
assert isinstance(sro.data, np.memmap) and not sro.data.flags.writeable
assert np.array_equal(np.asarray(sro.data), hyper)
os.chmod(ro, stat.S_IRUSR | stat.S_IWUSR)
# Paths are absolute, so a relative path still says where the file is, but
# not symlink-resolved (TMP sits under /var -> /private/var on macOS): a link
# keeps its own name, and "same file?" checks resolve.
rel = os.path.relpath(ro)
assert stack_io.load_stack(rel).path == Path(os.path.abspath(ro)) == sro.path
assert sro.path.is_absolute() and os.path.samefile(sro.path, Path(ro).resolve())
ok("read-only TIFFs open (read-only map), stack paths are absolute")
# A NaN pixel no longer turns a float image black: ranges skip NaN, and
# NaN renders as the bottom of the range without a cast warning.
floats = np.random.default_rng(1).random((64, 80)).astype(np.float32) * 10
floats[5, 7] = np.nan
flt = io_dir / "nan.tif"
tifffile.imwrite(flt, floats)
sn = stack_io.load_stack(flt)
finite = floats[np.isfinite(floats)]
assert np.allclose(sn.ranges[0], np.percentile(finite, [0.35, 99.65]))
assert stack_io.full_range(floats.dtype, floats) == (float(finite.min()), float(finite.max()))
assert stack_io.auto_range(np.full((4, 4), np.nan, np.float32)) == (0.0, 1.0)
with warnings.catch_warnings():
    warnings.simplefilter("error")
    rgb_nan = sn.render(0, 0, [0])
assert (rgb_nan[5, 7] == 0).all() and rgb_nan.max() > 0
ok("NaN pixels: finite ranges, render as 0 without warnings")
# RGB TIFFs open as a red/green/blue composite.
green = np.zeros((64, 80, 3), np.uint8)
green[..., 1] = 200
tifffile.imwrite(io_dir / "rgb.tif", green, photometric="rgb")
srgb = stack_io.load_stack(io_dir / "rgb.tif")
assert srgb.n_channels == 3 and srgb.composite
assert tuple(srgb.render(0, 0, [0, 1, 2])[10, 10]) == (0, 200, 0)
# 8-bit palette files use their ColorMap as the LUT.
cmap = np.zeros((3, 256), np.uint16)
cmap[1] = np.arange(256) << 8  # a green LUT, stored the way ImageJ writes it
tifffile.imwrite(io_dir / "pal.tif", np.full((64, 80), 200, np.uint8),
                 photometric="palette", colormap=cmap)
spal = stack_io.load_stack(io_dir / "pal.tif")
assert tuple(spal.luts[0, 200]) == (0, 200, 0)
assert tuple(spal.render(0, 0, [0])[3, 3]) == (0, 200, 0)
ok("RGB TIFFs composite in R/G/B, palette LUTs honored")
# Fiji's single-channel display range (min=/max=) is read and written back.
single = io_dir / "minmax.tif"
tifffile.imwrite(single, (np.arange(64 * 80) % 2000).astype(np.uint16).reshape(64, 80),
                 imagej=True, metadata={"min": 100.0, "max": 1000.0})
smm = stack_io.load_stack(single)
assert smm.ranges.tolist() == [[100.0, 1000.0]]
smm.ranges[0] = (150.0, 900.0)
smm.save(io_dir / "minmax_out.tif")
with tifffile.TiffFile(io_dir / "minmax_out.tif") as tf:
    assert (tf.imagej_metadata["min"], tf.imagej_metadata["max"]) == (150.0, 900.0)
smm_back = stack_io.load_stack(io_dir / "minmax_out.tif")  # a lone LUT reopens too
assert smm_back.ranges.tolist() == [[150.0, 900.0]] and np.array_equal(smm_back.luts, smm.luts)
# An ImageJ "color" hyperstack stays "color" (not "grayscale") through a
# save and a projection; composite still wins while it is on.
colored = io_dir / "color.tif"
tifffile.imwrite(colored, hyper, imagej=True, metadata={"axes": "TZCYX", "mode": "color"})
sc = stack_io.load_stack(colored)
assert not sc.composite and sc.plain_mode == "color"
sc.save(io_dir / "color_out.tif")
with tifffile.TiffFile(io_dir / "color_out.tif") as tf:
    assert tf.imagej_metadata["mode"] == "color"
assert stack_io.project(sc, "Z", "Max", 0, 2).plain_mode == "color"
sc.composite = True
sc.save(io_dir / "color_comp.tif")
with tifffile.TiffFile(io_dir / "color_comp.tif") as tf:
    assert tf.imagej_metadata["mode"] == "composite"
# Multi-channel with Composite off saves as "color" — one channel at a time in
# its LUT color, as the app shows it — even from a "grayscale" file.
grayed = io_dir / "gray.tif"
tifffile.imwrite(grayed, hyper, imagej=True, metadata={"axes": "TZCYX", "mode": "grayscale"})
sg = stack_io.load_stack(grayed)
assert not sg.composite
sg.save(io_dir / "gray_out.tif")
with tifffile.TiffFile(io_dir / "gray_out.tif") as tf:
    assert tf.imagej_metadata["mode"] == "color"
ok("single-channel min/max and ImageJ color mode round-trip")


# ---- bug-fix pass: session + roi ----
import tifffile  # noqa: E402

session.close_all()
app.processEvents()
# Restored contrast reaches every cached plane: the pane renders (0, 0) with
# the load-time ranges before the saved ones are applied.
pa = workspace.show_stack(stack_io.load_stack(STACKS[0]))
pa.stack.ranges[0] = (0.0, 50.0)
pa.stack.version += 1
pa.bars["t"].set_value(5)
pa.bars["z"].set_value(4)
single = os.path.join(TMP, "single.tif")
tifffile.imwrite(single, (np.arange(64 * 48, dtype=np.uint16) % 1000).reshape(48, 64), imagej=True)
pb = workspace.show_stack(stack_io.load_stack(single))
pb.stack.ranges[0] = (100.0, 300.0)
pb.stack.version += 1
pb.refresh()
data = session.capture()
assert all(Path(e["path"]).is_absolute() for e in data["stacks"])
for e in data["stacks"]:  # a session from before paths were resolved
    e["path"] = os.path.relpath(e["path"])
session.close_all()
app.processEvents()
session.restore(data)
app.processEvents()
ra = next(q for q in viewer._all_panes if q.stack.name == Path(STACKS[0]).name)
rb = next(q for q in viewer._all_panes if q.stack.name == "single.tif")
assert list(ra.stack.ranges[0]) == [0.0, 50.0] and list(rb.stack.ranges[0]) == [100.0, 300.0]


def shows_current_ranges(q):
    t, z, _c = q.position()
    rgb = q.stack.render(t, z, q._display_channels(), q._last_stride, q._mip_on(), q.proj_method)
    return np.array_equal(q.image_item.image, rgb)


assert shows_current_ranges(rb)  # single plane: the only render predates the ranges
assert ra.position()[:2] == (5, 4) and shows_current_ranges(ra)
ra.bars["t"].set_value(0)
ra.bars["z"].set_value(0)
assert shows_current_ranges(ra)  # scrubbing back to the plane cached at load
ok("session restore: saved contrast on every plane, relative paths from old sessions open")

# Ellipses: measured, described and saved as the true ellipse that is drawn
# (pg's shape() is a 24-point polygon that missed ~1.2% of the area).
sel = ra.selection
sel.set_rect(100, 100, 400, 400, ellipse=True)
(ys, xs), mask = sel.mask()
yy, xx = np.mgrid[ys, xs]
true_ellipse = ((xx + 0.5 - 300) / 200) ** 2 + ((yy + 0.5 - 300) / 200) ** 2 < 1
assert (mask != true_ellipse).sum() < 50, (mask != true_ellipse).sum()
assert sel.describe() == "ellipse 100,100 400×400"
saved = sel.to_json()
for _ in range(5):
    sel.from_json(saved)
    saved = sel.to_json()
assert saved == {"kind": "ellipse", "rect": [100.0, 100.0, 400.0, 400.0]}
sel.set_rect(10, 20, 30, 40)
assert sel.mask()[1].sum() == 30 * 40  # rectangles unchanged
ok("ellipse selection: true-ellipse mask, exact describe and session round trip")
# Replaced selections are freed at once: left to the cycle collector, an ROI
# and its handles were freed in an unsafe order and segfaulted later.
import gc  # noqa: E402

gc_threshold = gc.get_threshold()
gc.set_threshold(1)
try:
    for _ in range(60):
        sel.set_rect(100, 100, 400, 400, ellipse=True)
        sel.from_json(sel.to_json())
        sel.set_rect(10, 20, 30, 40)
finally:
    gc.set_threshold(*gc_threshold)
sel.roi.removeClicked()  # right-click > Remove ROI clears after the ROI's own signal
assert wait_until(lambda: sel.roi is None)
ok("replacing and removing selections under constant garbage collection")

# NaN pixels drop out of the measurement (and its area), like Fiji.
nan_img = np.full((40, 50), 7.0, dtype=np.float32)
nan_img[0, 0] = np.nan
nan_img[5, 5] = 1.0
nan_path = os.path.join(TMP, "nan.tif")
tifffile.imwrite(nan_path, nan_img, imagej=True)
pn = workspace.show_stack(stack_io.load_stack(nan_path))
m = pn.measurement()
assert (m.min, m.max) == (1.0, 7.0) and abs(m.mean - np.nanmean(nan_img)) < 1e-9
assert m.area == 40 * 50 - 1
pn.selection.set_rect(0, 0, 1, 1)  # only the NaN pixel: stats read NaN, no error
assert np.isnan(pn.measurement().mean)
ok("measurements ignore NaN pixels")
session.close_all()
app.processEvents()

# Per-tile projections survive a restore under MIP all, which forces its own
# method on every tile that joins the grid.
q1 = workspace.show_stack(stack_io.load_stack(STACKS[0]))
q2 = workspace.show_stack(stack_io.load_stack(STACKS[1]))
workspace.combine_all()
app.processEvents()
ws = workspace.get_workspace()
ws.mip_checkbox.setChecked(True)
q1.mip_box.setChecked(False)
q2.set_proj_method("Median")
data = session.capture()
session.close_all()
app.processEvents()
session.restore(data)
app.processEvents()
r1 = next(q for q in ws.panes if q.stack.name == Path(STACKS[0]).name)
r2 = next(q for q in ws.panes if q.stack.name == Path(STACKS[1]).name)
assert ws.mip_checkbox.isChecked()
assert not r1.mip_box.isChecked()
assert r2.mip_box.isChecked() and r2.proj_method == "Median"
ws.mip_checkbox.setChecked(False)
ok("session restore keeps per-tile projections under MIP all")
session.close_all()
app.processEvents()

# ---- bug-fix pass: viewer core ----
import tifffile  # noqa: E402
from PySide6.QtWidgets import QFileDialog  # noqa: E402

from tiff_visualizer import render_pool  # noqa: E402

core_src = stack_io.load_stack("example_stacks/XY05.tif")
crop = np.ascontiguousarray(core_src.data[:, :5, :, :180, :240])  # 8t × 5z, small
short_path = os.path.join(TMP, "Z5.tif")
tifffile.imwrite(short_path, crop, imagej=True, metadata={"axes": "TZCYX"})
tless_path = os.path.join(TMP, "T1.tif")
tifffile.imwrite(tless_path, np.ascontiguousarray(crop[:1]), imagej=True, metadata={"axes": "TZCYX"})
va = workspace.show_stack(core_src)
vb = workspace.show_stack(stack_io.load_stack(short_path))
workspace.combine_all()
app.processEvents()
vws = workspace.get_workspace()
vws.minimal_checkbox.setChecked(False)
workspace.set_shared_axes(True)
app.processEvents()

# A projected tile has an image at every shared z, even past its slice count.
vb.mip_box.setChecked(True)
vws.shared_bars["z"].set_value(7)
app.processEvents()
vb.refresh()
assert not vb._blank and "no image" not in vb.header_label.text()
vb.mip_box.setChecked(False)
assert vb._blank  # a single slice past the end is still black
vb.mip_box.setChecked(True)
vws.shared_bars["z"].set_value(0)
ok("projected tile stays drawn when the shared z passes its slice count")

# Locks: only offered with shared axes, end with them without moving the
# tile, and floating a locked tile keeps its own position.
vws.shared_bars["t"].set_value(1)
va.lock_button.setChecked(True)
va.bars["t"].set_value(5)
assert va.lock_button.isVisibleTo(va)
workspace.set_shared_axes(False)
app.processEvents()
assert not va.shared_locked and not va.lock_button.isChecked() and va.position()[0] == 5
assert not va.lock_button.isVisibleTo(va)  # meaningless without shared axes
workspace.set_shared_axes(True)
app.processEvents()
QTest.qWait(100)
assert va.lock_button.isVisibleTo(va) and va.position()[0] == vws.shared_position()[0]
va.lock_button.setChecked(True)
va.bars["t"].set_value(6)
vws.shared_bars["t"].set_value(2)
workspace.float_pane(va)
app.processEvents()
assert va.position()[0] == 6 and not va.shared_locked and va.shared_controller is None
workspace.combine_all()
app.processEvents()
ok("locks: shown only with shared axes, cleared without moving, floating keeps position")

# Shared playback renders in the pool, never on the UI thread, and every
# tile ends on the frame it should show.
submitted, sync_renders = [], []
real_submit, real_cached = render_pool.submit, viewer.StackPane._cached_render
render_pool.submit = lambda *a, **k: (submitted.append(a[3]), real_submit(*a, **k))
viewer.StackPane._cached_render = lambda self, *a: (sync_renders.append(a), real_cached(self, *a))[1]
try:
    play = vws.shared_bars["t"]
    play.play_button.setChecked(True)
    QTest.qWait(700)
    play.play_button.setChecked(False)
finally:
    render_pool.submit, viewer.StackPane._cached_render = real_submit, real_cached
assert submitted and not sync_renders, (len(submitted), len(sync_renders))


def shows_its_frame(q):
    t_, z_, _c = q.position()
    rgb = q._plane_cache.get(q._render_key(t_, z_, q._last_stride))
    return q._blank or (rgb is not None and np.array_equal(q.image_item.image, rgb))


assert wait_until(lambda: all(shows_its_frame(q) for q in vws.panes))
workspace.set_shared_axes(False)
workspace.split_all()
app.processEvents()
# A prefetched frame still in flight is awaited, not rendered twice, and
# shown when it lands.
held = []
render_pool.submit = lambda pane, stack, args, key_, rid: held.append((stack, args, key_, rid))
try:
    va.bars["t"].set_value(0)
    va._plane_cache.clear()
    va._inflight.clear()
    va._prefetch_neighbors()
    nxt = next(h for h in held if h[1][0] == 1)  # the t+1 neighbor
    count = len(held)
    va.bars["t"].set_value_silent(1)
    va.refresh(async_render=True)
    assert len(held) == count and va._awaited_key == nxt[2]
    landed = nxt[0].render(*nxt[1])
    viewer._on_async_render_done(va, nxt[2], landed, nxt[3])
    assert np.array_equal(va.image_item.image, landed) and nxt[2] not in va._inflight
finally:
    render_pool.submit = real_submit
va._inflight.clear()
ok("playback: pool renders only, next frame prefetched once and shown on landing")

# The pixel readout follows plane changes under a still cursor, until the
# cursor leaves the view.
va.mip_box.setChecked(False)
va.bars["z"].set_value(0)
va._on_mouse_moved(va.viewbox.mapViewToScene(QPointF(228.5, 14.5)))


def expected_probe(q):
    t_, z_, c_ = q.position()
    vals = q.stack.data[t_, z_, :, 14, 228]
    shown = " ".join(str(v) for v in vals) if q._composite_on() else str(vals[c_])
    return f"x=228 y=14  value: {shown}"


assert va.probe_label.text() == expected_probe(va)
for _ in range(4):
    va.handle_key(key(Qt.Key_Right))
assert va.position()[1] == 4 and va.probe_label.text() == expected_probe(va)
QApplication.sendEvent(va.view.viewport(), QEvent(QEvent.Type.Leave))
stale = va.probe_label.text()
va.handle_key(key(Qt.Key_Right))
assert va.probe_label.text() == stale  # cursor gone: no re-read
ok("pixel readout re-reads when the plane changes under a still cursor")

# Brightness sort ranks what the tile shows: the projection when it's on.
m_slice = va.mean_intensity()
va.mip_box.setChecked(True)
assert va.mean_intensity() > m_slice
ok("brightness metric uses the projected plane")

# With z collapsed, z steps (arrows, wheel) do nothing, the header drops the
# per-slice label, z playback stops, and Space on a t-less stack plays nothing.
z_before = va.position()[1]
va.handle_key(key(Qt.Key_Right))
va._step("z", 3)
assert va.position()[1] == z_before
t_, z_, c_ = va.position()
assert core_src.label(t_, z_, c_) not in va.header_label.text()
va.mip_box.setChecked(False)
assert core_src.label(t_, z_, c_) in va.header_label.text()
tl = workspace.show_stack(stack_io.load_stack(tless_path))
assert "t" not in tl.bars
tl.bars["z"].play_button.setChecked(True)
tl.mip_box.setChecked(True)
assert not tl.bars["z"].play_button.isChecked()
tl.handle_key(key(Qt.Key_Space))
assert not tl.bars["z"].play_button.isChecked()
tl.mip_box.setChecked(False)
tl.handle_key(key(Qt.Key_Space))
assert tl.bars["z"].play_button.isChecked()
tl.bars["z"].stop_playback()
ok("collapsed z: no z steps, no slice label, no z playback")

# Save As and Projection honor the Composite checkbox; the projection shows
# the source's visible channels.
real_save_dialog = QFileDialog.getSaveFileName
real_proj_exec = viewer.ProjectionDialog.exec
try:
    for composite_on in (True, False):
        out = os.path.join(TMP, f"composite_{composite_on}.tif")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, o=out, **k: (o, ""))
        vb.composite_box.setChecked(composite_on)
        vb.save_as()
        assert stack_io.load_stack(out).composite is composite_on
    vb.composite_box.setChecked(True)
    vb.set_channel_visible(1, False)
    viewer.ProjectionDialog.exec = lambda self: QDialog.Accepted
    vb.project()
    proj_pane = viewer._all_panes[-1]
    assert proj_pane is not vb and proj_pane._composite_on()
    assert proj_pane.visible_channels == [True, False, True, True]
finally:
    QFileDialog.getSaveFileName = real_save_dialog
    viewer.ProjectionDialog.exec = real_proj_exec
ok("Save As and Projection follow the Composite checkbox and visible channels")
for q in (va, vb, tl, proj_pane):
    q.window().close()
app.processEvents()

# ---- bug-fix pass: viewer exports + windows ----
import gc  # noqa: E402
import weakref  # noqa: E402

import tifffile  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QProgressDialog  # noqa: E402


def _flush_deletes():
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        gc.collect()


def _stub_save(path):
    return staticmethod(lambda *_a, **_k: (str(path), ""))


def _close_panes(panes):
    for q in panes:
        if q in workspace.get_workspace().panes:
            workspace.get_workspace().close_panes([q])
        elif isinstance(q.window(), viewer.StackWindow):
            q.window().close()
    _flush_deletes()


# Folders list their TIFFs in natural order, without hidden or ._ sidecars.
folder = Path(TMP) / "natural"
folder.mkdir()
for name in ("XY10.tif", "XY2.tif", "._XY3.tif", ".hidden.tif", "notes.txt"):
    if name.endswith(".tif") and not name.startswith("."):
        tifffile.imwrite(folder / name, np.zeros((4, 4), np.uint8))
    else:
        (folder / name).write_bytes(b"\0\0")
(folder / "dir.tif").mkdir()
assert [p.name for p in viewer.list_tiffs(folder)] == ["XY2.tif", "XY10.tif"]
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(folder / n)) for n in ("XY2.tif", "._XY3.tif")])
assert [Path(p).name for p in viewer.FileDropMixin._dropped_tiff_paths(mime)] == ["XY2.tif"]
before = list(viewer._all_panes)
viewer.open_folder(directory=folder)
opened = [q for q in viewer._all_panes if q not in before]
assert [q.stack.name for q in opened] == ["XY2.tif", "XY10.tif"]
_close_panes(opened)
ok("folders: natural order, hidden and ._ sidecar files skipped")

# Closing a floating window frees it, its pane and its stack.
fp = workspace.show_stack(stack_io.load_stack(str(STACKS[0])))
app.processEvents()
alive = [weakref.ref(fp.window()), weakref.ref(fp), weakref.ref(fp.stack)]
destroyed = []
fp.destroyed.connect(lambda *_: destroyed.append(True))
fp.window().close()
del fp
_flush_deletes()
assert not any(r() for r in alive) and destroyed
roi.set_tool("rect")  # the dead window's tool menu no longer listens
roi.set_tool("hand")
ok("closing a floating window frees window, pane and stack")

# Exports of a tile that is blank on screen never pass off a stale frame.
long_stack = stack_io.load_stack(str(STACKS[0]))
short_stack = stack_io.TiffStack(
    None, "short.tif", np.asarray(long_stack.data[:3]), long_stack.luts.copy(),
    long_stack.ranges.copy(), long_stack.composite, None,
)
long_p = workspace.show_stack(long_stack)
short_p = workspace.show_stack(short_stack)
workspace.combine_selected([long_p, short_p])
workspace.set_shared_axes(True)
app.processEvents()
wsx = workspace.get_workspace()
wsx.shared_set("t", 1)
wsx.shared_set("z", 6)
wsx.shared_set("t", 5)  # short.tif (3 t) goes blank, its bars keep t 2 z 7
wsx.shared_set("z", 2)
app.processEvents()
assert short_p._blank and short_p.position()[:2] == (1, 6)
assert short_p._displayed_position()[:2] == (5, 2)
QApplication.clipboard().setText("sentinel")
short_p.copy_view()
assert QApplication.clipboard().text() == "sentinel"
assert "no image at this position" in wsx.statusBar().currentMessage()
real_save_dialog = QFileDialog.getSaveFileName
real_critical = QMessageBox.critical
failures_shown = []
try:
    png = Path(TMP) / "blank.png"
    QFileDialog.getSaveFileName = _stub_save(png)
    short_p.export_png()
    assert not png.exists()
    # A movie over t on the blank tile holds z at the shared z 3, not its stale z 7.
    viewer.ExportMovieDialog.exec = lambda self: (
        self.axis_combo.setCurrentText("T"), self.ranges["T"].restore_text("full"),
        QDialog.Accepted,
    )[-1]
    names = []
    movie = Path(TMP) / "blank_t.gif"
    QFileDialog.getSaveFileName = staticmethod(
        lambda _p, _t, start, _f: (names.append(Path(start).name), (str(movie), ""))[1]
    )
    short_p.export_movie()
    assert names == ["short_t1-3_z3.gif"], names
    gif = Image.open(movie)
    assert gif.n_frames == 3
    first_frame = np.asarray(gif.convert("RGB"), dtype=float)
    chans = short_p._display_channels()
    at_shared = short_stack.render(0, 2, chans).astype(float)
    at_stale = short_stack.render(0, 6, chans).astype(float)
    assert np.abs(first_frame - at_shared).mean() < np.abs(first_frame - at_stale).mean()
    # A movie over z needs the tile's t, which this stack doesn't have.
    viewer.ExportMovieDialog.exec = lambda self: (
        self.axis_combo.setCurrentText("Z"), QDialog.Accepted
    )[-1]
    movie.unlink()
    short_p.export_movie()
    assert not movie.exists() and "no image at this position" in wsx.statusBar().currentMessage()
    # Export results are reported where the user works, failures in a dialog.
    QMessageBox.critical = staticmethod(lambda _p, title, text: failures_shown.append(text))
    QFileDialog.getSaveFileName = _stub_save(Path(TMP) / "ok.png")
    long_p.export_png()
    assert (Path(TMP) / "ok.png").exists()
    assert wsx.statusBar().currentMessage().startswith("Exported view to")
    long_p.copy_view()
    assert QApplication.clipboard().mimeData().hasImage()
    assert wsx.statusBar().currentMessage() == "View copied to clipboard"
    missing = Path(TMP) / "no_such_dir"
    QFileDialog.getSaveFileName = _stub_save(missing / "x.png")
    long_p.export_png()
    viewer.ExportMovieDialog.exec = lambda self: QDialog.Accepted
    QFileDialog.getSaveFileName = _stub_save(missing / "x.gif")
    long_p.export_movie()
    viewer.StackMontageDialog.exec = lambda self: QDialog.Accepted
    QFileDialog.getSaveFileName = _stub_save(missing / "x_montage.png")
    long_p.export_stack_montage()
    assert len(failures_shown) == 3 and all("no_such_dir" in f for f in failures_shown)
    # Cancel stops a movie before anything is written.
    real_canceled = QProgressDialog.wasCanceled
    QProgressDialog.wasCanceled = lambda self: True
    QFileDialog.getSaveFileName = _stub_save(Path(TMP) / "canceled.gif")
    long_p.export_movie()
    QProgressDialog.wasCanceled = real_canceled
    assert not (Path(TMP) / "canceled.gif").exists()
finally:
    QFileDialog.getSaveFileName = real_save_dialog
    QMessageBox.critical = real_critical
    for cls in (viewer.ExportMovieDialog, viewer.StackMontageDialog):
        cls.exec = QDialog.exec
workspace.set_shared_axes(False)
_close_panes([long_p, short_p])
ok("exports: blank tiles refused or held at the shared position, results and failures reported")

# Stack montage: "collapse z" survives a different projection method, and a
# t-less stack isn't offered a collapse that would leave one tile.
mp = workspace.show_stack(stack_io.load_stack(str(STACKS[0])))
d = viewer.StackMontageDialog(mp)
d.z_combo.setCurrentIndex(1)
d.accept()
mp.set_proj_method("Mean", enable=False)
d = viewer.StackMontageDialog(mp)
assert d.z_combo.currentData() is True and "AVG" in d.z_combo.currentText()
d.reject()
app_settings.settings().setValue("dialogs/stackMontage/z", "All slices as rows")  # older label format
assert viewer.StackMontageDialog(mp).z_combo.currentData() is False
tless = workspace.show_stack(stack_io.project(mp.stack, "T", "Max", 0, mp.stack.n_frames - 1))
tless.mip_box.setChecked(True)
d = viewer.StackMontageDialog(tless)
assert d.z_combo.count() == 1 and d.z_combo.currentIndex() == 0 and d._counts() == (1, 9)
d.reject()
app_settings.settings().remove("dialogs/stackMontage/z")
_close_panes([mp, tless])
ok("stack montage: collapse-z remembered across methods, none offered without t")

# ---- bug-fix pass: workspace ----
ws = workspace.get_workspace()
prior_grid = list(ws.panes)
mode_boxes = (ws.shared_checkbox, ws.shared_channels_checkbox, ws.minimal_checkbox)
prior_modes = [box.isChecked() for box in mode_boxes]
workspace.split_all()  # start from an empty grid; restored at the end
ws.minimal_checkbox.setChecked(False)
ws.shared_checkbox.setChecked(True)
ws.shared_channels_checkbox.setChecked(True)
fx = []
for fx_path in STACKS[:3]:
    fx_pane = viewer.StackPane(stack_io.load_stack(str(fx_path)))
    viewer.StackWindow(fx_pane).show()
    fx.append(fx_pane)
fa, fb, fc = fx
fa.composite_box.setChecked(True)
fa.set_channel_visible(1, False)
fb.composite_box.setChecked(False)
viewer._active_pane = fa  # the stack last worked on
workspace.combine_selected(fx)
app.processEvents()
assert all(q.channel_state() == (True, [True, False, True, True]) for q in fx)
assert [box.isChecked() for box in ws.shared_channel_boxes] == [True, False, True, True]
ok("shared channels: a grid combined from empty adopts the last-focused stack's channels")

# A locked tile keeps its own bars (and position) when tiles join or leave.
fb.lock_button.setChecked(True)
fb.bars["t"].set_value(5)
fx_extra = viewer.StackPane(stack_io.load_stack(SPARE_STACK))
ws.add_pane(fx_extra)
assert fb.shared_locked and fb.bars["t"].isVisibleTo(fb) and fb.position()[0] == 5
assert not fa.bars["t"].isVisibleTo(fa)
ws.close_pane(fx_extra)
assert fb.bars["t"].isVisibleTo(fb) and fb.position()[0] == 5
fb.lock_button.setChecked(False)
ok("locked tile keeps its bars when the grid changes")

# The ★ filter clears itself once the last flagged tile closes or floats.
fx_extra = viewer.StackPane(stack_io.load_stack(SPARE_STACK))
ws.add_pane(fx_extra)
fx_extra.toggle_flag()
ws.flag_checkbox.setChecked(True)
assert ws._displayed_panes() == [fx_extra]
ws.close_pane(fx_extra)
assert not ws.flag_checkbox.isChecked() and not ws.flag_checkbox.isVisibleTo(ws)
fc.toggle_flag()
ws.flag_checkbox.setChecked(True)
workspace.float_pane(fc)
assert not ws.flag_checkbox.isChecked() and not ws.flag_checkbox.isVisibleTo(ws)
fa.toggle_flag()  # flagging again must not collapse the grid to one tile
assert ws._displayed_panes() == ws.panes == [fa, fb]
fa.toggle_flag()
fc.toggle_flag()
ok("★ only clears when the last flagged tile closes or floats")

# Playback and its speed survive tiles joining and leaving.
ws.toggle_time_playback()
ws.shared_bars["t"]._set_fps(5)
fx_extra = viewer.StackPane(stack_io.load_stack(SPARE_STACK))
ws.add_pane(fx_extra)
t_bar = ws.shared_bars["t"]
assert t_bar.play_button.isChecked() and t_bar._timer.isActive() and t_bar._fps == 5
ws.close_pane(fx_extra)
t_bar = ws.shared_bars["t"]
assert t_bar.play_button.isChecked() and t_bar._timer.isActive() and t_bar._fps == 5
ws.toggle_time_playback()
assert not ws.shared_bars["t"]._timer.isActive()
ok("grid playback keeps running (same fps) when tiles join or leave")

# Minimalist holds shared axes: switching them off leaves Minimalist too.
ws.minimal_checkbox.setChecked(True)
assert ws.shared_checkbox.isChecked() and not fa.float_button.isVisibleTo(fa)
ws.shared_checkbox.setChecked(False)
assert not ws.minimal_checkbox.isChecked()
assert fa.float_button.isVisibleTo(fa) and fa.bars["t"].isVisibleTo(fa)
ok("turning shared axes off leaves Minimalist")

# Any manual drag returns the sort to Manual, Brightness included.
ws.sort_combo.setCurrentIndex(2)
ws._apply_sort()
ws.move_pane(ws.panes[-1], ws.panes[0], after=False)
assert ws.sort_combo.currentData() == "manual"
ok("dragging a tile after a Brightness sort returns to Manual")

ws.close_panes([q for q in fx if q in ws.panes])
for fx_window in [w for w in viewer._open_windows if w.pane is fc]:
    fx_window.close()
app.processEvents()
for box, was in zip(mode_boxes, prior_modes):
    box.setChecked(was)
if prior_grid:
    workspace.combine_selected(prior_grid)

# ---- bug-fix pass: B&C ----
import gc  # noqa: E402
import weakref  # noqa: E402

import tifffile  # noqa: E402


def bc_close(pane):
    ws_ = workspace._workspace
    if ws_ is not None and pane in ws_.panes:
        ws_.close_pane(pane)
    else:
        pane.window().close()
    app.processEvents()


# Undo history holds stacks weakly and skips steps whose stacks have closed.
loose = stack_io.load_stack(SPARE_STACK)
loose_ref = weakref.ref(loose)
bc_panel.push_range_undo([(loose, 0, 0.0, 1.0)])
del loose
gc.collect()
assert loose_ref() is None  # undo alone doesn't keep a stack's data alive
bc_a = workspace.show_stack(stack_io.load_stack(str(STACKS[0])))
bc_b = workspace.show_stack(stack_io.load_stack(str(STACKS[1])))
app.processEvents()
b_before = tuple(bc_b.stack.ranges[0])
bc_panel.push_range_undo([(bc_b.stack, 0, 1.0, 2.0)])
bc_panel.push_range_undo([(stack_io.load_stack(SPARE_STACK), 0, 5.0, 6.0)])  # dies at once
bc_panel.undo_last_range_change()  # skips the dead step, undoes the open one
assert tuple(bc_b.stack.ranges[0]) == (1.0, 2.0)
bc_b.stack.ranges[0] = b_before
bc_b.stack.version += 1
# The floating panel lets go of a closed stack — pinned or not — and never
# applies its range to the others.
panel = show_bc_panel(bc_a)
panel.pin_button.setChecked(True)
bc_a.set_channel(0)
app.processEvents()
panel.controls.max_spin.setValue(99)
assert tuple(bc_a.stack.ranges[0])[1] == 99.0
bc_a.window().close()
app.processEvents()
b_range = tuple(bc_b.stack.ranges[0])
panel.controls._on_apply_all()
assert tuple(bc_b.stack.ranges[0]) == b_range
assert panel.controls.target is not bc_a and not panel.pin_button.isChecked()
assert panel.target_label.text() != STACKS[0].name
ok("B&C: undo holds stacks weakly, a closed stack is dropped, not applied to all")

# With the z projection on, histogram / Auto / Reset use the projected plane;
# Sum in per-slice units (render scales its display window by the slice count).
panel.set_target(bc_b)
bc_b.set_channel(3)
bc_b.set_proj_method("Max")
app.processEvents()
t_, z_, c_ = bc_b.position()
block = np.asarray(bc_b.stack.data[t_, :, c_])
panel.controls._on_auto()
assert tuple(bc_b.stack.ranges[c_]) == stack_io.auto_range(block.max(axis=0))
x_edges = panel.controls.hist_curve.xData
assert (x_edges[0], x_edges[-1]) == (block.max(axis=0).min(), block.max(axis=0).max())
bc_b.set_proj_method("Sum")
app.processEvents()
panel.controls._on_auto()
expect = stack_io.auto_range(block.astype(np.float64).mean(axis=0))
assert np.allclose(bc_b.stack.ranges[c_], expect, atol=1e-4), (bc_b.stack.ranges[c_], expect)
bc_b.mip_box.setChecked(False)
app.processEvents()
panel.controls._on_auto()
assert tuple(bc_b.stack.ranges[c_]) == stack_io.auto_range(block[z_])
ok("B&C: histogram, Auto and Reset follow the z projection (Sum per slice)")

# Float images: the boxes show small ranges exactly, editing one bound
# leaves the other untouched, and max <= min opens a gap scaled to the data.
bc_rng = np.random.default_rng(0)
bc_arr = bc_rng.uniform(0.001, 0.005, size=(3, 64, 80)).astype(np.float32)
bc_arr[0, 0, 0] = np.nan
bc_float_path = os.path.join(TMP, "bc_float.tif")
tifffile.imwrite(bc_float_path, bc_arr, imagej=True, metadata={"axes": "ZYX"})
bc_f = workspace.show_stack(stack_io.load_stack(bc_float_path))
bc_f.stack.ranges[0] = (0.0012, 0.0047)
bc_f.stack.version += 1
panel.set_target(bc_f)
app.processEvents()
cs = panel.controls
assert (cs.min_spin.text(), cs.max_spin.text()) == ("0.001200", "0.004700")
cs.max_spin.stepUp()
assert tuple(bc_f.stack.ranges[0]) == (0.0012, 0.00471)
cs.min_spin.setValue(0.01)  # past max -> a data-scaled gap, not +1
lo_, hi_ = bc_f.stack.ranges[0]
assert lo_ == 0.01 and 0 < hi_ - lo_ < 1e-3
bc_f.mip_box.setChecked(True)  # a NaN pixel in the projection must not break the bins
app.processEvents()
assert len(cs.hist_curve.xData) == 257
bc_f.stack.ranges[0] = (np.nan, np.nan)  # shown as the data span, never applied
bc_f.stack.version += 1
cs.refresh()
cs.max_spin.setValue(0.004)
assert np.isfinite(bc_f.stack.ranges[0]).all() and bc_f.stack.ranges[0][1] == 0.004
for pane_ in (bc_b, bc_f):
    bc_close(pane_)
ok("B&C: float precision, one-sided edits, scaled min/max gap, NaN-safe histogram")

# ---- bug-fix pass: control window + startup + updater ----
import threading  # noqa: E402

import tifffile  # noqa: E402

import tiff_visualizer.__main__ as app_main  # noqa: E402

# A manual check asked while the launch check is still out gets its answer
# when that check lands — failure and offer alike; a lone launch check
# stays quiet.
gate = threading.Event()
answers = []
updater._say_check_failed = lambda error: answers.append(("failed", str(error)))
updater._offer = lambda release, manual: answers.append(("offer", manual))


def slow_fail(*a, **k):
    gate.wait(5)
    raise OSError("slow network")


def slow_newer(*a, **k):
    gate.wait(5)
    return updater.Release("v99.0", "u", "notes", ())


for fetch, expected in ((slow_fail, ("failed", "slow network")), (slow_newer, ("offer", True))):
    gate.clear()
    answers.clear()
    updater.fetch_latest = fetch
    updater._check(manual=False)
    assert updater._in_flight
    updater._check(manual=True)  # rides on the launch check
    gate.set()
    assert wait_until(lambda: bool(answers)) and answers == [expected], answers
answers.clear()
updater.fetch_latest = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
updater._check(manual=False)
assert wait_until(lambda: not updater._in_flight) and not answers  # the owed answer was paid
ok("manual update check during the launch check still answers")

# Platform names are whole words: "darwin" is a Mac asset, not a Windows one.
named = (("TIFF-Visualizer-1.6.0-darwin-arm64.zip", "m"), ("TIFF-Visualizer-1.6.0-win64.zip", "w"))
assert updater.Release("v1.6.0", "u", "", named).asset_for_platform("darwin")[1] == "m"
assert updater.Release("v1.6.0", "u", "", named).asset_for_platform("win32")[1] == "w"
assert updater.Release("v1", "u", "", named[:1]).asset_for_platform("win32") is None
windows = (("TIFF-Visualizer-1.6.0-Windows.zip", "w"),)
assert updater.Release("v1", "u", "", windows).asset_for_platform("darwin") is None
ok("updater: darwin assets count as macOS, Windows ones still refused")

# Folder lists: natural order, no macOS ._ sidecars, and a stack opened through
# a relative or symlinked path still ticks (and Close all closes) its entry.
fix_folder = Path(TMP) / "fixfolder"
fix_folder.mkdir()
for name in ("XY10.tif", "XY2.tif"):
    tifffile.imwrite(fix_folder / name, np.zeros((2, 16, 16), np.uint16), imagej=True)
(fix_folder / "._XY1.tif").write_bytes(b"\x00\x05\x16\x07AppleDouble")
(fix_folder / "notes.txt").write_text("not a stack")
fix_link = Path(TMP) / "fixlink"
os.symlink(fix_folder, fix_link)
cw = control_panel.get_control_window()
cw.add_folder_section(fix_link)
section = cw.folder_sections[str(fix_link.resolve())]
assert [f.name for f in section.checks] == ["XY2.tif", "XY10.tif"]
viewer.open_path(os.path.relpath(fix_folder / "XY2.tif"))
viewer.open_path(fix_link / "XY10.tif")
app.processEvents()
cw.refresh_state()
assert all(box.isChecked() for box in section.checks.values())
assert not section.open_all_button.isEnabled() and section.close_all_button.isEnabled()
section.close_all()
app.processEvents()
assert not [q for q in viewer._all_panes if q.stack.name in ("XY2.tif", "XY10.tif")]
assert not any(box.isChecked() for box in section.checks.values())
cw.remove_folder_section(section)
app.processEvents()
ok("folder list: natural order, ._ files skipped, relative/symlinked paths match")

# Finder/Dock opens: a burst of FileOpen events becomes one batched open of
# absolute paths — symlinks kept, so a link keeps its name (argv goes
# through the same open_batch).
batched = []
real_open_paths = app_main.open_paths
app_main.open_paths = lambda paths: batched.append(list(paths))
try:
    batcher = app_main.OpenBatcher(app_main.open_batch, 30)
    for name in ("XY2.tif", "XY10.tif"):
        batcher.add(os.path.relpath(fix_link / name))
    assert wait_until(lambda: bool(batched))
    QTest.qWait(100)
    assert batched == [[Path(os.path.abspath(fix_link / n)) for n in ("XY2.tif", "XY10.tif")]]
finally:
    app_main.open_paths = real_open_paths
ok("file-open events batch into one open_paths call")

# ---- bug-fix pass: integration ----
# Float Reset keeps a small data range; a flat float plane gets a gap scaled
# to its values, integers one gray level.
tiny = np.array([[0.0012, 0.0047]], np.float32)
assert stack_io.full_range(tiny.dtype, tiny) == (float(tiny.min()), float(tiny.max()))
lo, hi = stack_io.auto_range(np.full((4, 4), 0.5, np.float32))
assert lo == 0.5 and 0.5 < hi < 0.51
assert stack_io.auto_range(np.full((4, 4), 7, np.uint16)) == (7.0, 8.0)
ok("float ranges: Reset spans the data, flat planes get a scaled gap")
# Save As waits for in-flight preloads: a copy still reading a memory-mapped
# file that gets overwritten would crash or bring back stale data.
from tiff_visualizer import preload  # noqa: E402

pre_path = os.path.join(TMP, "preload_wait.tif")
stack_io.load_stack(str(STACKS[0])).save(pre_path)
pre_stack = stack_io.load_stack(pre_path)
assert not pre_stack.in_memory
app_settings.settings().setValue("preload/enabled", True)
try:
    preload.maybe_preload(pre_stack)
    assert preload._threads
    preload.wait_for_loads()
    assert not any(t.isRunning() for t in preload._threads)
finally:
    app_settings.settings().setValue("preload/enabled", False)
assert wait_until(lambda: pre_stack.in_memory)
ok("preload: wait_for_loads blocks until copies finish")
# The grid montage reports its result in the grid window and a failed write
# in a dialog, like the other exports.
gp = workspace.show_stack(stack_io.load_stack(str(STACKS[0])))
workspace.combine_all()
app.processEvents()
gws = workspace.get_workspace()
real_save_dialog = QFileDialog.getSaveFileName
real_critical = QMessageBox.critical
grid_failures = []
try:
    workspace.MontageDialog.exec = lambda self: (self.mode_combo.setCurrentIndex(0), QDialog.Accepted)[-1]
    QMessageBox.critical = staticmethod(lambda _p, _title, text: grid_failures.append(text))
    QFileDialog.getSaveFileName = _stub_save(Path(TMP) / "grid_ok.png")
    gws._export_montage()
    assert (Path(TMP) / "grid_ok.png").exists()
    assert gws.statusBar().currentMessage().startswith("Exported montage of")
    QFileDialog.getSaveFileName = _stub_save(Path(TMP) / "no_such_dir" / "grid.png")
    gws._export_montage()
    assert len(grid_failures) == 1 and "no_such_dir" in grid_failures[0]
finally:
    QFileDialog.getSaveFileName = real_save_dialog
    QMessageBox.critical = real_critical
    workspace.MontageDialog.exec = QDialog.exec
workspace.split_all()
_close_panes([gp])
ok("grid montage: result in the grid window, failed writes reported")

# A symlinked position keeps its own name (headers, labels, export names),
# while its folder-list box still matches it through the resolved path.
link_dir = Path(TMP) / "linked_positions"
raw_dir = Path(TMP) / "raw_scans"
link_dir.mkdir()
raw_dir.mkdir()
target = raw_dir / "scan_0012.tif"
stack_io.load_stack(str(STACKS[0])).save(target)
(link_dir / "XY2.tif").symlink_to(target)
linked = stack_io.load_stack(link_dir / "XY2.tif")
assert linked.name == "XY2.tif" and linked.path == link_dir / "XY2.tif"
cw = control_panel.get_control_window()
cw.add_folder_section(link_dir)
section = cw.folder_sections[str(link_dir.resolve())]
box = next(iter(section.checks.values()))
assert box.text() == "XY2.tif"
box.setChecked(True)
app.processEvents()
opened = [p for p in viewer._all_panes if p.stack.name == "XY2.tif"]
assert len(opened) == 1 and box.isChecked()
box.setChecked(False)
app.processEvents()
assert not [p for p in viewer._all_panes if p.stack.name == "XY2.tif"]
cw.remove_folder_section(section)
ok("symlinked stacks keep their own name and still match their folder box")
# Update assets whose names run platform words together are still refused.
names = lambda *ns: updater.Release("v9.0", "u", "", tuple((n, "https://x/" + n) for n in ns))
assert names("TIFFVisualizerWin64.zip").asset_for_platform("darwin") is None
assert names("TiffVisualizerMac.zip").asset_for_platform("win32") is None
assert names("TIFF-Visualizer-9.0-darwin-arm64.zip").asset_for_platform("win32") is None
assert names("TIFF-Visualizer-9.0-darwin-arm64.zip").asset_for_platform("darwin")[0].endswith("arm64.zip")
ok("updater: run-together platform names refused, darwin still macOS")
# A 16-bit palette maps pixel values to their own palette entries.
import tifffile  # noqa: E402

pal = np.zeros((3, 65536), np.uint16)
pal[0, :30000] = 65535
pal[1, 30000:] = 65535
tifffile.imwrite(Path(TMP) / "pal16.tif", np.linspace(100, 200, 64, dtype=np.uint16).reshape(8, 8),
                 photometric="palette", colormap=pal)
pal_stack = stack_io.load_stack(Path(TMP) / "pal16.tif")
assert (pal_stack.render(0, 0, [0]).reshape(-1, 3) == (255, 0, 0)).all()
ok("16-bit palette colors follow pixel values")
# A projected tile following the shared axes still steps the shared z, which
# drives the slice-showing tiles; alone, a projected tile ignores z.
za = workspace.show_stack(stack_io.load_stack(str(STACKS[0])))
zb = workspace.show_stack(stack_io.load_stack(str(STACKS[1])))
workspace.combine_all()
workspace.set_shared_axes(True)
app.processEvents()
zws = workspace.get_workspace()
za.mip_box.setChecked(True)
z_before = zws.shared_position()[1]
za._step("z", 1)
app.processEvents()
assert zws.shared_position()[1] == z_before + 1 and zb.position()[1] == z_before + 1
# Sum is ranked like the mean it is displayed as, not by slice count.
za.set_proj_method("Sum")
summed = za.mean_intensity()
za.set_proj_method("Mean")
assert abs(summed - za.mean_intensity()) < 1e-6
workspace.set_shared_axes(False)
workspace.split_all()
za._step("z", 1)  # floating with the projection on: z stays put
assert za.position()[1] == z_before + 1
za.mip_box.setChecked(False)
_close_panes([za, zb])
ok("projected tiles still drive the shared z; Sum ranks like the mean")
# Save As waits for queued renders, which may read the file's memory map.
from tiff_visualizer import render_pool  # noqa: E402

rp = stack_io.load_stack(str(STACKS[0]))
render_pool.set_handler(lambda *_a: None)
for i in range(6):
    render_pool.submit(None, rp, (i % rp.n_frames, 0, [0], 1, True, "Median"), ("k", i), i)
render_pool.wait_idle()
assert not render_pool._pending
render_pool.set_handler(viewer._on_async_render_done)
ok("render pool: wait_idle drains queued renders")
# Clearing a selection mid-drag (Esc, Select None) lets go of the dragged
# handle first, so the scene doesn't keep sending the drag to a freed item.
dp = workspace.show_stack(stack_io.load_stack(str(STACKS[0])))
app.processEvents()
dp.selection.set_rect(10, 10, 100, 80)
drag_scene = dp.selection.roi.scene()
drag_scene.dragItem = dp.selection.roi.getHandles()[0]
dp.selection.clear()
assert drag_scene.dragItem is None and dp.selection.roi is None
_close_panes([dp])
ok("selection cleared mid-drag releases the scene's drag item")

print(f"\nALL {PASSED} CHECKS PASSED")
os._exit(0)
