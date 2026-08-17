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
ws.mip_checkbox.setChecked(False)
ok("MIP all")
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
data = session.capture()
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
assert len(section.checks) == 48
box = section.checks[folder / "XY10.tif"]
box.setChecked(True)
app.processEvents()
assert any(p.stack.name == "XY10.tif" for p in viewer._all_panes)
box.setChecked(False)
app.processEvents()
assert not viewer._all_panes
ctrl.remove_folder_section(section)
ok("folder swap list open/close")

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
p = workspace.show_stack(stack_io.load_stack("example_stacks/XY11.tif"))
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
ok("stack montage: t×z sheet, wrapped axis, MIP option")

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
