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


def test_themes_and_icons(qapp):
    from aquasuitelinux.gui import icons
    from aquasuitelinux.gui.theme import theme
    for mode in ("light", "dark"):
        theme.apply(qapp, mode)
        assert theme.palette.name == mode
    for name in icons._PATHS:
        assert not icons.icon(name, "#ffffff").isNull()
