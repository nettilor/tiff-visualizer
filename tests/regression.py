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

from PySide6.QtCore import QEvent, QMimeData, QPoint, QSettings, Qt, QUrl
from PySide6.QtGui import QKeyEvent
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
ws.minimal_checkbox.setChecked(False)
app.processEvents()
assert p1.close_button.isVisibleTo(p1)
ok("minimalist mode on/off")
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

print("sessions")
workspace.combine_all()
app.processEvents()
ws.minimal_checkbox.setChecked(True)
app.processEvents()
p1.bars["t"].set_value(3)
p1.set_flagged(True)
p1.set_proj_method("Median")
data = session.capture()
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
restored.mip_box.setChecked(False)
restored.set_proj_method("Max", False)
ws.minimal_checkbox.setChecked(False)
ok("session round-trip incl. minimalist, grid order and flags")
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
ok("stack montage: t×z sheet, wrapped axis, MIP option, row/column/custom grids")

# Montage dialog: the layout applies only while a single axis varies; the
# grid boxes show the effective grid and are editable in Custom, where the
# other dimension grows only when the typed one would drop tiles.
d = viewer.StackMontageDialog(p)
assert not d.layout_combo.isEnabled() and d.values()[6] is None  # t×z is fixed
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
d.deleteLater()
ok("stack montage dialog: layout modes, linked grid boxes, t×z lockout")

# Dialogs reopen with the options last accepted; Cancel forgets nothing.
d = viewer.StackMontageDialog(p)
d.z_combo.setCurrentIndex(1)  # MIP -> t only, every 2nd t = 4 tiles
d.t_spin.setValue(2)
d.layout_combo.setCurrentIndex(3)
d.cols_spin.setValue(4)
d.rows_spin.setValue(1)
d.scale_combo.setCurrentIndex(2)
d.labels_box.setChecked(False)
d.accept()
d = viewer.StackMontageDialog(p)
assert d.values() == (2, 1, True, 4, False, False, (4, 1)), d.values()
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
# Movie: axis + fps.
d = viewer.ExportMovieDialog(p)
d.axis_combo.setCurrentText("Z")
d.fps_spin.setValue(25)
d.accept()
assert viewer.ExportMovieDialog(p).values() == ("Z", 25)
p.mip_box.setChecked(True)  # MIP hides Z: a forced single-entry axis is no choice
d = viewer.ExportMovieDialog(p)
assert d.axis_combo.count() == 1
d.accept()
p.mip_box.setChecked(False)
assert viewer.ExportMovieDialog(p).values() == ("Z", 25)
# Grid montage: the remembered resolution beats the GIF half-size default.
d = workspace.MontageDialog(None, [p])
d.mode_combo.setCurrentIndex(1)  # movie over t -> scale bumps to Half
assert d.scale_combo.currentData() == 2
d.scale_combo.setCurrentIndex(0)
d.fps_spin.setValue(7)
d.labels_box.setChecked(False)
d.accept()
assert workspace.MontageDialog(None, [p]).values() == ("gif", "T", 7, 1, False)
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

print(f"\nALL {PASSED} CHECKS PASSED")
os._exit(0)
