"""The main window: toolbar, the five pages, status bar, toasts and the tray icon."""

from __future__ import annotations

import base64

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..core.config import DELTA_T_CURVE, ControllerConfig, new_id
from .alarms_page import AlarmsPage
from .controllers_page import ControllersPage
from .dialogs import (
    AboutDialog,
    DeviceDialog,
    ImportDialog,
    PinsDialog,
    ProfilesDialog,
    SettingsDialog,
    VirtualSensorDialog,
)
from .outputs_page import OutputsPage
from .overview import OverviewPage
from .sensors_page import SensorsPage
from .theme import theme
from .widgets import (
    StatusDot,
    Toast,
    bind_icon,
    flat_button,
    format_value,
    scaled_font,
    tool_button,
)

PAGES = (("Overview", "overview", "Everything at a glance (Ctrl+1)"),
         ("Sensors", "sensor", "All sensors, virtual sensors like Delta T, feeds (Ctrl+2)"),
         ("Controllers", "curve", "Fan curves and other controllers (Ctrl+3)"),
         ("Fans", "fan", "Fans and pumps: controllers and power limits (Ctrl+4)"),
         ("Alarms", "bell", "Alarms and the data log (Ctrl+5)"))


class MainWindow(QMainWindow):
    def __init__(self, bridge, gui_settings, app_icon=None):
        super().__init__()
        self.bridge = bridge
        self.gs = gui_settings
        self.app_icon = app_icon
        self._quitting = False
        self.setWindowTitle(__app_name__)
        self.resize(1480, 920)
        self.setMinimumSize(QSize(1000, 640))

        self._build_toolbar()
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.overview = OverviewPage(bridge, gui_settings)
        self.sensors = SensorsPage(bridge, gui_settings)
        self.controllers = ControllersPage(bridge)
        self.outputs = OutputsPage(bridge)
        self.alarms = AlarmsPage(bridge)
        for page in (self.overview, self.sensors, self.controllers, self.outputs, self.alarms):
            self.stack.addWidget(page)
        self._build_statusbar()
        self.toast = Toast(self)
        self.tray: QSystemTrayIcon | None = None

        self.overview.open_output.connect(self.open_output)
        self.overview.open_device.connect(self.open_device)
        self.overview.import_device.connect(self.open_import)
        self.overview.customize.connect(self.open_pins)
        self.overview.sensor_clicked.connect(self.open_sensor)
        self.sensors.edit_virtual.connect(self.edit_virtual)
        self.sensors.new_virtual.connect(self.new_virtual)
        self.sensors.add_alarm.connect(self.add_alarm)
        self.sensors.pins_changed.connect(lambda: self.overview.update_snapshot(self.bridge.snap))
        self.outputs.edit_controller.connect(self.open_controller)
        self.outputs.new_delta_t_curve.connect(self.new_delta_t_curve)
        bridge.message.connect(self.show_message)
        bridge.snapshot.connect(self._update_status)
        bridge.lost.connect(self._lost)

        self._shortcuts()
        self._restore()
        self._setup_tray()

    # ------------------------------------------------------------------ toolbar
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(22, 22))
        bar.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(Qt.TopToolBarArea, bar)
        self.toolbar = bar

        ident = QWidget()
        il = QHBoxLayout(ident)
        il.setContentsMargins(4, 0, 14, 0)
        il.setSpacing(10)
        logo = QLabel()
        if self.app_icon is not None and not self.app_icon.isNull():
            logo.setPixmap(self.app_icon.pixmap(34, 34))
        else:
            self._logo = bind_icon(logo, "fan", "accent", 30)
        il.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        app = QLabel(__app_name__)
        app.setObjectName("AppName")
        app.setFont(scaled_font(app, 1.28, bold=True))
        sub = QLabel("Aquacomputer control for Linux")
        sub.setObjectName("Faint")
        sub.setFont(scaled_font(sub, 0.8))
        names.addWidget(app)
        names.addWidget(sub)
        il.addLayout(names)
        bar.addWidget(ident)
        bar.addSeparator()

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons = []
        for i, (label, icon, tip) in enumerate(PAGES):
            b = tool_button(label, icon, tip, checkable=True)
            self.nav.addButton(b, i)
            bar.addWidget(b)
            self.nav_buttons.append(b)
        self.nav_buttons[0].setChecked(True)
        self.nav.idClicked.connect(self.show_page)
        bar.addSeparator()

        self.btn_import = tool_button("Import", "import", "Import fan curves from aquasuite (Ctrl+I)")
        self.btn_import.clicked.connect(lambda: self.open_import(None))
        bar.addWidget(self.btn_import)
        self.btn_profile = tool_button("Profile", "layers", "Switch or manage profiles")
        self.btn_profile.setPopupMode(self.btn_profile.ToolButtonPopupMode.InstantPopup)
        self.profile_menu = QMenu(self.btn_profile)
        self.profile_menu.aboutToShow.connect(self._fill_profile_menu)
        self.btn_profile.setMenu(self.profile_menu)
        bar.addWidget(self.btn_profile)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        pill = QWidget()
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(0, 0, 6, 0)
        pl.setSpacing(4)
        self.pill_dot = StatusDot(8)
        self.pill = QLabel("Connecting…")
        pl.addWidget(self.pill_dot)
        pl.addWidget(self.pill)
        bar.addWidget(pill)
        self.btn_theme = flat_button("sun", "Switch between dark and light", size=20)
        self.btn_theme.clicked.connect(self.toggle_theme)
        bar.addWidget(self.btn_theme)
        self.btn_settings = flat_button("settings", "Settings (Ctrl+,)", size=20)
        self.btn_settings.clicked.connect(self.open_settings)
        bar.addWidget(self.btn_settings)
        self.btn_about = flat_button("info", f"About {__app_name__}", size=20)
        self.btn_about.clicked.connect(lambda: AboutDialog(self, self.app_icon).exec())
        bar.addWidget(self.btn_about)
        theme.changed.connect(lambda p: self.btn_theme._binder.set_name("sun" if p.dark else "moon", "text_muted"))
        self.btn_theme._binder.set_name("sun" if theme.palette.dark else "moon", "text_muted")

    def _fill_profile_menu(self) -> None:
        menu = self.profile_menu
        menu.clear()
        cfg = self.bridge.config
        active = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        for name in ["", *[p.name for p in cfg.profiles]]:
            act = QAction(name or "Default", menu, checkable=True)
            act.setChecked((active.name if active else "") == name)
            act.triggered.connect(lambda _c=False, n=name: self.bridge.call(
                "set_profile", n, success=f"Profile: {n or 'Default'}"))
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction("Manage profiles…", lambda: ProfilesDialog(self.bridge, self).exec())

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.status_text = QLabel("Ready")
        sb.addWidget(self.status_text, 1)
        self.status_right = QLabel(f"{__app_name__} {__version__}")
        self.status_right.setObjectName("Muted")
        sb.addPermanentWidget(self.status_right)

    def _shortcuts(self) -> None:
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self)
            s.activated.connect(fn)
            return s

        for i in range(len(PAGES)):
            sc(f"Ctrl+{i + 1}", lambda i=i: self.show_page(i))
        sc("Ctrl+I", lambda: self.open_import(None))
        sc("Ctrl+,", self.open_settings)
        sc("Ctrl+Q", self.quit)

    # ------------------------------------------------------------------ navigation
    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)

    def open_output(self, oid: str) -> None:
        self.show_page(3)
        self.outputs.select(oid)

    def open_controller(self, cid: str) -> None:
        self.show_page(2)
        self.controllers.select(cid)

    def open_sensor(self, sid: str) -> None:
        self.show_page(1)
        self.sensors.select_sensor(sid)

    def open_device(self, key: str) -> None:
        DeviceDialog(self.bridge, key, self, on_import=self.open_import).exec()

    def open_import(self, key: str | None) -> None:
        ImportDialog(self.bridge, self, key).exec()

    def open_pins(self) -> None:
        if PinsDialog(self.bridge, self.gs, self).exec():
            self.overview.update_snapshot(self.bridge.snap)

    # ------------------------------------------------------------------ creating things
    def new_virtual(self, kind: str, inputs: list) -> None:
        dlg = VirtualSensorDialog(self.bridge, self, kind=kind, inputs=inputs)
        if not dlg.exec() or not dlg.result_sensor:
            return
        vs = dlg.result_sensor

        def done(ok):
            if ok and vs.kind == "difference" and not any(c.input == f"virtual/{vs.id}"
                                                          for c in self.bridge.config.controllers):
                if QMessageBox.question(self, "Fan curve", f"Create a fan curve that uses “{vs.name}”?\n\n"
                                        "You can then assign it to your radiator fans on the Fans page.") \
                        == QMessageBox.Yes:
                    self._create_curve(vs.id, vs.name)
        self.bridge.edit(lambda cfg: cfg.virtual_sensors.append(vs), f"“{vs.name}” created", done=done)

    def _create_curve(self, vid: str, vname: str, assign_to: str | None = None) -> None:
        c = ControllerConfig(id=new_id(), name=f"{vname} curve", kind="curve", input=f"virtual/{vid}",
                             points=[list(p) for p in DELTA_T_CURVE], hysteresis=0.2)

        def change(cfg):
            cfg.controllers.append(c)
            if assign_to:
                cfg.output(assign_to).controller = c.id

        def done(ok):
            if ok:
                if assign_to:
                    self.outputs.select(assign_to)
                else:
                    self.open_controller(c.id)
        self.bridge.edit(change, f"“{c.name}” created", done=done)

    def new_delta_t_curve(self, oid: str) -> None:
        dts = [v for v in self.bridge.config.virtual_sensors if v.kind == "difference"]
        if dts:
            self._create_curve(dts[0].id, dts[0].name, assign_to=oid)
            return
        dlg = VirtualSensorDialog(self.bridge, self, kind="difference")
        if not dlg.exec() or not dlg.result_sensor:
            return
        vs = dlg.result_sensor
        self.bridge.edit(lambda cfg: cfg.virtual_sensors.append(vs), f"“{vs.name}” created",
                         done=lambda ok: ok and self._create_curve(vs.id, vs.name, assign_to=oid))

    def edit_virtual(self, vid: str) -> None:
        vs = self.bridge.config.virtual(vid)
        if vs is None:
            return
        dlg = VirtualSensorDialog(self.bridge, self, sensor=vs)
        if dlg.exec() and dlg.result_sensor:
            new = dlg.result_sensor
            self.bridge.edit(lambda cfg: setattr(cfg, "virtual_sensors",
                                                 [new if v.id == vid else v for v in cfg.virtual_sensors]),
                             f"“{new.name}” saved")

    def add_alarm(self, sid: str) -> None:
        self.show_page(4)
        self.alarms.add(sid)

    # ------------------------------------------------------------------ status
    def show_message(self, text: str, kind: str = "info") -> None:
        self.status_text.setText(text)
        self.toast.show_message(text, kind)
        if kind == "error" and self.tray is not None and not self.isVisible():
            self.tray.showMessage(__app_name__, text, QSystemTrayIcon.Warning, 8000)

    def _update_status(self, snap: dict) -> None:
        pal = theme.palette
        devices = snap.get("devices", [])
        active_alarms = [a for a in snap.get("alarms", []) if a.get("active")]
        if not devices:
            self.pill.setText("No devices")
            self.pill_dot.set_color(pal.text_faint, False)
        else:
            names = ", ".join(d["name"] for d in devices[:3]) + ("…" if len(devices) > 3 else "")
            self.pill.setText(names + (f" · {len(active_alarms)} alarm" + ("s" if len(active_alarms) > 1 else "")
                                       if active_alarms else ""))
            online = all(d.get("online") for d in devices)
            color = pal.danger if active_alarms or not online else (
                pal.warning if any(d["backend"] == "hwmon" for d in devices) or snap.get("problems") else pal.success)
            self.pill_dot.set_color(color, True)
        mode = {"service": "background service", "standalone": "running in this app", "demo": "demo"}.get(
            snap.get("mode"), snap.get("mode", ""))
        profile = f" · profile {snap['profile']}" if snap.get("profile") else ""
        self.status_right.setText(f"{mode}{profile} · {__app_name__} {__version__}")
        self.btn_import.setEnabled(True)
        if self.tray is not None:
            pins = self.overview.pins()[:4]
            lines = [__app_name__]
            for sid in pins:
                r = self.bridge.reading(sid)
                if r:
                    lines.append(f"{r['label']}: {format_value(r.get('value'), r.get('unit', ''))}")
            self.tray.setToolTip("\n".join(lines))

    def _lost(self, text: str) -> None:
        self.show_message(f"Lost contact with the engine: {text}", "error")

    # ------------------------------------------------------------------ settings / theme
    def toggle_theme(self) -> None:
        mode = theme.toggle(QApplication.instance())
        self.gs.theme = mode
        self.gs.save()

    def open_settings(self) -> None:
        old_theme = self.gs.theme
        dlg = SettingsDialog(self.bridge, self.gs, self)
        if not dlg.exec():
            return
        if self.gs.theme != old_theme:
            theme.apply(QApplication.instance(), self.gs.theme)
        self._setup_tray()
        if dlg.switch_to_service:
            QTimer.singleShot(200, self.switch_to_service)

    def switch_to_service(self) -> None:
        """Hand fan control over from the in-app engine to the freshly started service."""
        from ..core.api import ServiceAPI
        from ..core.ipc import Client
        client = Client()
        for _ in range(20):
            if client.available():
                break
            QApplication.processEvents()
            import time
            time.sleep(0.25)
        else:
            self.show_message("The service did not answer; staying in this app", "error")
            return
        local_cfg = self.bridge.config.copy()
        old = self.bridge.api
        self.bridge.stop()
        try:
            old.close()
        except Exception:  # noqa: BLE001 - the old engine may already be gone
            pass
        api = ServiceAPI(client)
        try:
            remote = api.get_config()
            if not remote.get("controllers") and not remote.get("outputs") and \
                    (local_cfg.controllers or local_cfg.outputs):
                api.set_config(local_cfg.to_dict())
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.show_message(f"Could not move the settings to the service: {exc}", "error")
        self.bridge.api = api
        self.bridge.start()
        self.show_message("Now using the background service", "success")

    # ------------------------------------------------------------------ tray
    def _setup_tray(self) -> None:
        want = self.gs.tray and QSystemTrayIcon.isSystemTrayAvailable()
        if not want:
            if self.tray is not None:
                self.tray.hide()
                self.tray = None
            return
        if self.tray is not None:
            return
        tray = QSystemTrayIcon(self.app_icon if self.app_icon is not None else theme.icon("fan", "accent"), self)
        menu = QMenu()
        menu.addAction("Show AquasuiteLinux", self._show_from_tray)
        prof = menu.addMenu("Profile")
        prof.aboutToShow.connect(lambda: self._fill_tray_profiles(prof))
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self._show_from_tray()
                               if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        self.tray = tray
        self._tray_menu = menu

    def _fill_tray_profiles(self, menu: QMenu) -> None:
        menu.clear()
        cfg = self.bridge.config
        active = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        for name in ["", *[p.name for p in cfg.profiles]]:
            act = QAction(name or "Default", menu, checkable=True)
            act.setChecked((active.name if active else "") == name)
            act.triggered.connect(lambda _c=False, n=name: self.bridge.call("set_profile", n))
            menu.addAction(act)

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> None:
        w = self.gs.window
        try:
            if w.get("geometry"):
                self.restoreGeometry(QByteArray(base64.b64decode(w["geometry"])))
        except (ValueError, TypeError):
            pass

    def quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.gs.close_to_tray and self.tray is not None and not self._quitting:
            self.hide()
            event.ignore()
            if self.bridge.mode == "standalone":
                self.tray.showMessage(__app_name__, "Still controlling your fans in the background.",
                                      QSystemTrayIcon.Information, 4000)
            return
        if self.bridge.mode == "standalone" and not self._quitting:
            managed = [o for o in self.bridge.snap.get("outputs", []) if o["placement"] == "software"]
            if managed:
                answer = QMessageBox.question(
                    self, "Quit AquasuiteLinux",
                    f"{len(managed)} fan{'s are' if len(managed) != 1 else ' is'} controlled by this app. When it "
                    "quits they go to their fallback power (outputs controlled on the device keep running).\n\n"
                    "Enable the background service in Settings to keep control running.\n\nQuit anyway?")
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
        self.gs.window = {"geometry": base64.b64encode(bytes(self.saveGeometry())).decode()}
        self.gs.save()
        if self.tray is not None:
            self.tray.hide()
        self.bridge.close()
        super().closeEvent(event)
        QApplication.instance().quit()
