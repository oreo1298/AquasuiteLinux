"""Headless GUI smoke tests (skipped without PySide6)."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from aquasuitelinux.gui.theme import theme
    theme.apply(app, "dark")
    yield app


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def window(qapp):
    from aquasuitelinux.core.session import local_engine
    from aquasuitelinux.gui.bridge import Bridge
    from aquasuitelinux.gui.main_window import MainWindow
    from aquasuitelinux.gui.settings import GuiSettings
    api = local_engine(demo=True)
    bridge = Bridge(api)
    win = MainWindow(bridge, GuiSettings(tray=False), None)
    bridge.start()
    win.show()
    pump(qapp, 2.5)
    yield win
    win.quit()
    pump(qapp, 0.3)


def test_pages_build_and_update(qapp, window):
    for i in range(5):
        window.show_page(i)
        pump(qapp, 1.2)
    assert window.overview._outputs
    assert window.sensors.items
    assert window.controllers.list.count() == 5
    assert window.outputs.list.count() >= 6
    assert window.alarms.list.count() == 2
    assert "QUADRO" in window.pill.text()


def test_edit_curve_and_apply(qapp, window):
    window.open_controller("radiator")
    pump(qapp, 0.8)
    cp = window.controllers
    assert cp.current.id == "radiator"
    cp.curve.points[0][1] = 25.0
    cp.curve._emit()
    assert cp.dirty
    cp.apply()
    pump(qapp, 1.5)
    assert not cp.dirty
    assert window.bridge.config.controller("radiator").points[0][1] == 25.0


def test_output_settings_apply(qapp, window):
    from aquasuitelinux.core.demo import QUADRO
    window.open_output(f"{QUADRO}/fan4")
    pump(qapp, 0.8)
    op = window.outputs
    op.min_power.set_value(35)
    op._touch()
    op.apply()
    pump(qapp, 1.5)
    assert window.bridge.config.outputs[f"{QUADRO}/fan4"].min_power == 35


def test_fans_page_explains_the_power_range(qapp, window):
    from aquasuitelinux.core.demo import QUADRO
    window.open_output(f"{QUADRO}/fan1")
    pump(qapp, 1.5)
    o = window.bridge.outputs[f"{QUADRO}/fan1"]
    # the demo's radiator fans have a 20 % minimum: the curve's value is spread over 20–100 %
    assert o["target"] == round(20 + o["curve"] * 0.8, 1)
    assert "spread over 20–100 %" in window.outputs.live.text()


def test_dialogs_build(qapp, window):
    from aquasuitelinux.core.demo import QUADRO
    from aquasuitelinux.gui import dialogs
    from aquasuitelinux.gui.settings import GuiSettings
    b = window.bridge
    for make in (lambda: dialogs.VirtualSensorDialog(b, window, kind="difference", inputs=[f"{QUADRO}/temp1"]),
                 lambda: dialogs.VirtualSensorDialog(b, window, kind="expression"),
                 lambda: dialogs.VirtualSensorDialog(b, window, kind="heat_load"),
                 lambda: dialogs.FeedDialog(b, window),
                 lambda: dialogs.DeviceDialog(b, QUADRO, window),
                 lambda: dialogs.ProfilesDialog(b, window),
                 lambda: dialogs.PinsDialog(b, GuiSettings(), window),
                 lambda: dialogs.SettingsDialog(b, GuiSettings(), window),
                 lambda: dialogs.AboutDialog(window)):
        d = make()
        d.show()
        pump(qapp, 0.3)
        d.close()


def test_settings_turn_device_feeds_off(qapp, window):
    from aquasuitelinux.core.demo import QUADRO
    from aquasuitelinux.gui import dialogs
    from aquasuitelinux.gui.settings import GuiSettings
    b = window.bridge
    assert b.config.settings.device_feeds                       # the demo turns them on
    d = dialogs.SettingsDialog(b, GuiSettings(tray=False), window)
    d.feeds.setChecked(False)
    d.accept()
    pump(qapp, 2.5)
    assert not b.config.settings.device_feeds
    outs = {o["id"]: o for o in b.snap["outputs"]}
    assert outs[f"{QUADRO}/fan1"]["placement"] == "software"
    assert "turned off in Settings" in outs[f"{QUADRO}/fan1"]["reason"]


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_offers_to_move_settings_to_an_empty_service(qapp, monkeypatch, answer):
    from PySide6.QtWidgets import QMessageBox

    from aquasuitelinux.core import config as config_mod
    from aquasuitelinux.core.api import LocalAPI
    from aquasuitelinux.core.config import Config
    from aquasuitelinux.core.demo import demo_config, demo_provider
    from aquasuitelinux.core.engine import Engine
    from aquasuitelinux.gui.bridge import Bridge
    from aquasuitelinux.gui.main_window import MainWindow
    from aquasuitelinux.gui.settings import GuiSettings
    config_mod.save(demo_config(), config_mod.user_config_path())
    asked = []

    def question(*args, **kwargs):
        asked.append(args[2])
        return QMessageBox.Yes if answer == "yes" else QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", question)
    eng = Engine(Config(), demo_provider(), mode="service")          # a service started with no setup
    eng.tick()
    eng.start()
    bridge = Bridge(LocalAPI(eng))
    gs = GuiSettings(tray=False)
    win = MainWindow(bridge, gs, None)
    bridge.start()
    pump(qapp, 2.0)
    try:
        assert len(asked) == 1 and "5 controllers" in asked[0]
        if answer == "yes":
            assert len(eng.config.controllers) == 5 and eng.config.outputs
        else:
            assert not eng.config.controllers and gs.declined_move
            win._move_checked = False
            win.offer_settings_move()                                   # declined: not asked again
            assert len(asked) == 1
    finally:
        win.quit()
        pump(qapp, 0.3)


def test_delta_t_dialog_computes_preview(qapp, window):
    from aquasuitelinux.core.demo import QUADRO
    from aquasuitelinux.gui.dialogs import VirtualSensorDialog
    d = VirtualSensorDialog(window.bridge, window, kind="difference", inputs=[f"{QUADRO}/temp1", f"{QUADRO}/temp2"])
    d._preview()
    assert d.preview.text().endswith("K")
    d.accept()
    assert d.result_sensor.inputs == [f"{QUADRO}/temp1", f"{QUADRO}/temp2"]


def test_import_dialog_reads_device(qapp, window):
    from aquasuitelinux.core.demo import QUADRO
    from aquasuitelinux.gui.dialogs import ImportDialog
    d = ImportDialog(window.bridge, window, QUADRO)
    d.show()
    pump(qapp, 1.5)
    assert d.tree.topLevelItemCount() == 4
    assert list(d.slot_combos) == [1]
    # the simulated QUADRO calls software sensor 1 "Delta T": the demo's Delta T is picked for it
    assert d.slot_combos[1].current_id() == "virtual/deltat"
    from PySide6.QtWidgets import QFormLayout
    label = d.slots_form.itemAt(0, QFormLayout.ItemRole.LabelRole).widget()
    assert label.text() == "Software sensor 1 “Delta T” ←"
    d.close()


def test_curve_editor_interaction(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QMouseEvent

    from aquasuitelinux.gui.curve_editor import CurveEditor
    ed = CurveEditor()
    ed.resize(600, 400)
    ed.set_curve([[2, 20], [10, 100]], "K")
    got = []
    ed.changed.connect(got.append)
    pt = ed._to_px(6, 50)
    ev = QMouseEvent(QMouseEvent.MouseButtonDblClick, pt, pt, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    ed.mouseDoubleClickEvent(ev)
    assert len(ed.points) == 3 and got
    assert abs(ed.points[1][0] - 6) < 0.2
    ed.selected = 1
    from PySide6.QtGui import QKeyEvent
    ed.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    assert len(ed.points) == 2


def test_curve_axis_ends_at_the_last_point(qapp):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent

    from aquasuitelinux.core.config import DELTA_T_CURVE
    from aquasuitelinux.gui.curve_editor import CurveEditor, curve_range
    assert curve_range("K", DELTA_T_CURVE) == (0.0, 10.0)                      # a Delta T curve up to 10 K
    assert curve_range("°C", [[25, 20], [40, 100]]) == (20.0, 40.0)
    assert curve_range("°C", [[10, 20], [15, 100]]) == (9.0, 15.0)              # below the usual start
    ed = CurveEditor()
    ed.resize(600, 400)
    ed.set_curve(DELTA_T_CURVE, "K")
    assert ed.x_range == (0.0, 10.0)

    def mouse(kind, pos):
        return QMouseEvent(kind, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    # drag the last point past the right edge: the axis stretches with it, then ends at the new last point
    start = ed._to_px(10, 100)
    ed.mousePressEvent(mouse(QMouseEvent.MouseButtonPress, start))
    beyond = QPointF(start.x() + ed._plot().width() * 0.2, start.y())
    ed.mouseMoveEvent(mouse(QMouseEvent.MouseMove, beyond))
    ed.mouseMoveEvent(mouse(QMouseEvent.MouseMove, beyond))                    # same spot: no runaway
    assert abs(ed.points[-1][0] - 12.0) < 0.2 and ed.x_range[1] == ed.points[-1][0]
    ed.mouseReleaseEvent(mouse(QMouseEvent.MouseButtonRelease, beyond))
    assert ed.x_range == (0.0, ed.points[-1][0])
    # back to the left: the axis shrinks to it on release
    ed.mousePressEvent(mouse(QMouseEvent.MouseButtonPress, ed._to_px(*ed.points[-1])))
    ed.mouseMoveEvent(mouse(QMouseEvent.MouseMove, ed._to_px(9, 100)))
    ed.mouseReleaseEvent(mouse(QMouseEvent.MouseButtonRelease, ed._to_px(9, 100)))
    assert abs(ed.x_range[1] - 9) < 0.2
    # the arrow keys can move the last point past the end too
    ed.select(len(ed.points) - 1)
    before = ed.points[-1][0]
    for _ in range(5):
        ed.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    assert ed.points[-1][0] > before and ed.x_range[1] == ed.points[-1][0]


def test_curve_points_can_be_added_without_changing_the_shape(qapp):
    from aquasuitelinux.core.config import DELTA_T_CURVE
    from aquasuitelinux.core.controllers import interpolate
    from aquasuitelinux.gui.curve_editor import CurveEditor
    ed = CurveEditor()
    original = [list(p) for p in DELTA_T_CURVE]                 # 5 points
    ed.set_curve(original, "K")
    ed.set_point_count(10)
    assert len(ed.points) == 10
    assert all(p in ed.points for p in original)                # the points you set stay where they are
    for x in (2, 3.3, 5, 7.9, 10):
        assert abs(interpolate(ed.points, x) - interpolate(original, x)) <= 0.1
    assert [p[0] for p in ed.points] == sorted(p[0] for p in ed.points)
    ed.set_point_count(40)
    assert len(ed.points) == CurveEditor.MAX_POINTS == 16
    ed.set_point_count(1)
    assert len(ed.points) == CurveEditor.MIN_POINTS == 2
    # with a point selected, a new one goes right after it, and removing takes the selected one
    ed.set_curve(original, "K")
    ed.select(1)
    assert ed.add_point() and ed.points[2] == [5.0, 37.5] and ed.selected == 2
    ed.select(0)
    assert ed.remove_point() and ed.points[0] == [4.0, 30.0]


def test_controllers_page_sets_ten_points(qapp, window):
    window.open_controller("radiator")
    pump(qapp, 0.8)
    cp = window.controllers
    assert cp.point_count.value() == 5 and cp.table.rowCount() == 5
    cp.point_count.setValue(10)
    assert len(cp.curve.points) == 10 and cp.table.rowCount() == 10 and cp.dirty
    cp.table.setCurrentCell(3, 0)
    assert cp.curve.selected == 3                                # table row ↔ point on the graph
    cp.btn_add_point.click()
    assert cp.point_count.value() == 11 and cp.curve.selected == 4
    cp.btn_remove_point.click()
    assert cp.point_count.value() == 10
    cp.apply()
    pump(qapp, 1.5)
    assert len(window.bridge.config.controller("radiator").points) == 10


def test_themes_and_icons(qapp):
    from aquasuitelinux.gui import icons
    from aquasuitelinux.gui.theme import theme
    for mode in ("light", "dark"):
        theme.apply(qapp, mode)
        assert theme.palette.name == mode
    for name in icons._PATHS:
        assert not icons.icon(name, "#ffffff").isNull()
