"""Overview: devices, key readings, fans and pumps, history and status at a glance."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .charts import LineChart
from .theme import mono_font, theme
from .widgets import (
    Card,
    SegmentedControl,
    StatusDot,
    ValueTile,
    badge,
    bind_icon,
    flat_button,
    format_value,
    scaled_font,
    set_placement_badge,
    set_prop,
)

KIND_ICONS = {"temperature": "thermometer", "delta": "delta", "flow": "flow", "power": "heat", "rpm": "fan",
              "percent": "gauge", "pressure": "gauge", "voltage": "plug", "current": "plug", "volume": "droplet"}


def default_pins(bridge) -> list[str]:
    """Sensible tiles when the user hasn't chosen: Delta T, coolant, ambient, flow, heat load, CPU."""
    out: list[str] = []
    readings = bridge.readings
    for r in readings.values():
        if r["source"] == "virtual" and r.get("value") is not None and len(out) < 2:
            out.append(r["id"])
    for d in bridge.snap.get("devices", []):
        temps = [r for r in readings.values() if r["source"] == d["key"] and r["kind"] == "temperature"
                 and r["id"].split("/", 1)[1].startswith("temp") and r.get("value") is not None]
        out += [r["id"] for r in temps[:2]]
        flows = [r for r in readings.values() if r["source"] == d["key"] and r["kind"] == "flow"
                 and r["id"].endswith("/flow") and r.get("value") is not None]
        out += [r["id"] for r in flows[:1]]
    for sid in ("system/k10temp/tctl", "system/coretemp/package_id_0"):
        if sid in readings and readings[sid].get("value") is not None:
            out.append(sid)
            break
    seen: list[str] = []
    for sid in out:
        if sid not in seen:
            seen.append(sid)
    return seen[:6]


class DeviceRow(QWidget):
    def __init__(self, info: dict):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(8)
        self.dot = StatusDot(8)
        lay.addWidget(self.dot, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name = QLabel()
        self.name.setObjectName("Value")
        self.detail = QLabel()
        self.detail.setObjectName("Muted")
        self.detail.setFont(scaled_font(self.detail, 0.84))
        col.addWidget(self.name)
        col.addWidget(self.detail)
        lay.addLayout(col, 1)
        self.update_info(info)

    def update_info(self, info: dict) -> None:
        pal = theme.palette
        name = info.get("name") or info.get("model")
        self.name.setText(name)
        backend = {"hidraw": "USB", "hwmon": "kernel driver", "simulated": "demo"}.get(info["backend"], info["backend"])
        parts = [info["model"]] if info["model"] != name else []
        if info.get("serial"):
            parts.append(info["serial"])
        parts.append(backend)
        self.detail.setText(" · ".join(parts))
        color = pal.success if info.get("online") else pal.danger
        if info.get("online") and info.get("backend") == "hwmon":
            color = pal.warning
        self.dot.set_color(color, True)
        tip = [f"{info['model']} — firmware {info.get('firmware') or '?'}"]
        if info.get("error"):
            tip.append(info["error"])
        if info.get("backend") == "hwmon":
            tip.append("Read through the kernel driver: monitoring works; for full control install the udev rule "
                       "or run the background service.")
        self.setToolTip("\n".join(tip))


class OutputTile(QFrame):
    clicked = Signal(str)

    def __init__(self, oid: str):
        super().__init__()
        self.oid = oid
        self.setObjectName("Tile")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.icon = QLabel()
        self._icon = bind_icon(self.icon, "fan", "text_muted", 16)
        self.name = QLabel()
        self.name.setObjectName("Value")
        self.badge = badge()
        top.addWidget(self.icon)
        top.addWidget(self.name, 1)
        top.addWidget(self.badge)
        lay.addLayout(top)
        row = QHBoxLayout()
        self.rpm = QLabel("—")
        self.rpm.setObjectName("BigValue")
        self.rpm.setFont(scaled_font(self.rpm, 1.55, bold=True))
        self.rpm_unit = QLabel("rpm")
        self.rpm_unit.setObjectName("Muted")
        self.pct = QLabel("")
        self.pct.setObjectName("Muted")
        row.addWidget(self.rpm)
        row.addWidget(self.rpm_unit, 0, Qt.AlignBottom)
        row.addStretch(1)
        row.addWidget(self.pct, 0, Qt.AlignBottom)
        lay.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.ctrl = QLabel("")
        self.ctrl.setObjectName("Faint")
        self.ctrl.setFont(scaled_font(self.ctrl, 0.84))
        lay.addWidget(self.ctrl)

    def update_output(self, o: dict) -> None:
        self._icon.set_name("pump" if o.get("pump") else "fan")
        self.name.setText(o["name"])
        rpm = o.get("rpm")
        self.rpm.setText("—" if rpm is None else f"{rpm:,.0f}".replace(",", " "))
        power = o.get("reported") if o.get("reported") is not None else o.get("target")
        self.pct.setText(format_value(power, "%"))
        self.bar.setValue(int((power or 0) * 10))
        state = {"device": "device", "unmanaged": "idle"}.get(o["placement"], "")
        if self.bar.property("state") != state:
            set_prop(self.bar, "state", state)
        set_placement_badge(self.badge, o["placement"], o.get("override"))
        if o["placement"] == "unmanaged":
            self.ctrl.setText("Device keeps its own settings")
        else:
            self.ctrl.setText(o.get("controller_name") or "")
        tip = o.get("reason") or ""
        if o["placement"] == "device":
            tip = "The device runs this controller itself" + (f", reading software sensor {o['slot']}"
                                                                 if o.get("slot") else "")
        self.setToolTip(tip)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.oid)
        super().mouseReleaseEvent(event)


class OverviewPage(QWidget):
    open_output = Signal(str)
    open_device = Signal(str)
    import_device = Signal(str)
    customize = Signal()
    sensor_clicked = Signal(str)

    def __init__(self, bridge, gui_settings):
        super().__init__()
        self.setObjectName("Page")
        self.bridge = bridge
        self.gs = gui_settings
        self._tiles: dict[str, ValueTile] = {}
        self._outputs: dict[str, OutputTile] = {}
        self._device_rows: dict[str, tuple[QListWidgetItem, DeviceRow]] = {}
        self._last_history = 0.0
        self._last_events = 0.0

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)
        self.split = split

        # devices
        self.devices_card = Card("Devices", "chip")
        self.devices_card.setMinimumWidth(220)
        self.devices_card.setMaximumWidth(360)
        rescan = flat_button("refresh", "Look for devices again")
        rescan.clicked.connect(lambda: bridge.call("rescan", success="Looking for devices…"))
        self.devices_card.add_header_widget(rescan)
        self.device_list = QListWidget()
        self.device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.device_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.device_list.customContextMenuRequested.connect(self._device_menu)
        self.device_list.itemDoubleClicked.connect(lambda it: self.open_device.emit(it.data(Qt.UserRole)))
        self.devices_card.add(self.device_list, 1)
        self.no_devices = QLabel("No Aquacomputer devices found.\n\nPlug one in, or start with --demo to try the "
                                 "app with simulated devices.")
        self.no_devices.setObjectName("Empty")
        self.no_devices.setWordWrap(True)
        self.no_devices.setAlignment(Qt.AlignCenter)
        self.devices_card.add(self.no_devices)
        self.problems = QLabel("")
        self.problems.setObjectName("BannerWarn")
        self.problems.setWordWrap(True)
        self.problems.hide()
        self.devices_card.add(self.problems)
        split.addWidget(self.devices_card)

        # center
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(12)
        self.key_card = Card("Key readings", "gauge")
        cust = flat_button("pin", "Choose the readings shown here")
        cust.clicked.connect(self.customize.emit)
        self.key_card.add_header_widget(cust)
        self.tile_grid = QGridLayout()
        self.tile_grid.setSpacing(10)
        self.key_card.add_layout(self.tile_grid)
        cl.addWidget(self.key_card)
        self.fan_card = Card("Fans and pumps", "fan")
        self.fan_grid = QGridLayout()
        self.fan_grid.setSpacing(10)
        self.fan_card.add_layout(self.fan_grid)
        self.no_fans = QLabel("No controllable fan or pump outputs.")
        self.no_fans.setObjectName("Empty")
        self.fan_card.add(self.no_fans)
        cl.addWidget(self.fan_card)
        self.chart_card = Card("History", "chart")
        self.span = SegmentedControl()
        for label, secs in (("5 min", 300), ("15 min", 900), ("1 h", 3600)):
            self.span.add(label, secs)
        self.span.set_value(self.gs.chart_span if self.gs.chart_span in (300, 900, 3600) else 900)
        self.span.changed.connect(self._span_changed)
        self.chart_card.add_header_widget(self.span)
        self.chart = LineChart()
        self.chart.setMinimumHeight(230)
        self.chart_card.add(self.chart, 1)
        cl.addWidget(self.chart_card, 1)
        scroll.setWidget(center)
        split.addWidget(scroll)

        # status
        self.status_card = Card("Status", "activity")
        self.status_card.setMinimumWidth(260)
        self.status_card.setMaximumWidth(380)
        mode_row = QHBoxLayout()
        self.mode_dot = StatusDot(8)
        self.mode_text = QLabel()
        self.mode_text.setWordWrap(True)
        mode_row.addWidget(self.mode_dot, 0, Qt.AlignTop)
        mode_row.addWidget(self.mode_text, 1)
        self.status_card.add_layout(mode_row)
        self.profile_label = QLabel("PROFILE")
        self.profile_label.setObjectName("CardTitle")
        self.profile_label.setFont(scaled_font(self.profile_label, 0.78, bold=True))
        self.status_card.add(self.profile_label)
        self.profile_seg_holder = QVBoxLayout()
        self.status_card.add_layout(self.profile_seg_holder)
        self.profile_seg: SegmentedControl | None = None
        self._profile_names: tuple = ()
        al = QLabel("ALARMS")
        al.setObjectName("CardTitle")
        al.setFont(scaled_font(al, 0.78, bold=True))
        self.status_card.add(al)
        self.alarm_box = QVBoxLayout()
        self.alarm_box.setSpacing(4)
        self.status_card.add_layout(self.alarm_box)
        ev = QLabel("ACTIVITY")
        ev.setObjectName("CardTitle")
        ev.setFont(scaled_font(ev, 0.78, bold=True))
        self.status_card.add(ev)
        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setFont(mono_font(8.5))
        self.log.setMaximumBlockCount(300)
        self.status_card.add(self.log, 1)
        split.addWidget(self.status_card)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([250, 900, 300])

        bridge.snapshot.connect(self.update_snapshot)
        bridge.config_changed.connect(lambda _c: self._rebuild_profiles(force=True))

    # ------------------------------------------------------------------ helpers
    def pins(self) -> list[str]:
        return self.gs.pinned or default_pins(self.bridge)

    def _span_changed(self, secs) -> None:
        self.gs.chart_span = int(secs)
        self.gs.save()
        self._last_history = 0.0
        self._refresh_history()

    def _device_menu(self, pos) -> None:
        item = self.device_list.itemAt(pos)
        if not item:
            return
        key = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Device settings…", lambda: self.open_device.emit(key))
        menu.addAction("Import aquasuite settings from this device…", lambda: self.import_device.emit(key))
        menu.exec(self.device_list.mapToGlobal(pos))

    # ------------------------------------------------------------------ updates
    def update_snapshot(self, snap: dict) -> None:
        if not self.isVisible():
            return
        self._update_devices(snap)
        self._update_tiles()
        self._update_outputs(snap)
        self._update_status(snap)
        self._refresh_history()

    def _update_devices(self, snap: dict) -> None:
        devices = snap.get("devices", [])
        keys = [d["key"] for d in devices]
        if keys != list(self._device_rows):
            self.device_list.clear()
            self._device_rows.clear()
            for d in devices:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, d["key"])
                row = DeviceRow(d)
                item.setSizeHint(row.sizeHint())
                self.device_list.addItem(item)
                self.device_list.setItemWidget(item, row)
                self._device_rows[d["key"]] = (item, row)
        else:
            for d in devices:
                self._device_rows[d["key"]][1].update_info(d)
        self.no_devices.setVisible(not devices)
        self.device_list.setVisible(bool(devices))
        probs = snap.get("problems") or []
        self.problems.setText("\n".join(probs[:3]))
        self.problems.setVisible(bool(probs))

    def _update_tiles(self) -> None:
        pins = self.pins()
        if list(self._tiles) != pins:
            while self.tile_grid.count():
                w = self.tile_grid.takeAt(0).widget()
                if w:
                    w.deleteLater()
            self._tiles.clear()
            cols = 3 if len(pins) > 4 or len(pins) == 3 else max(1, len(pins))
            for i, sid in enumerate(pins):
                tile = ValueTile(self.bridge.label(sid))
                tile.clicked.connect(lambda s=sid: self.sensor_clicked.emit(s))
                self.tile_grid.addWidget(tile, i // cols, i % cols)
                self._tiles[sid] = tile
        alarms = {a["sensor"] for a in self.bridge.snap.get("alarms", []) if a.get("active")}
        for sid, tile in self._tiles.items():
            r = self.bridge.reading(sid)
            if r is None:
                tile.set_reading(None, "", self.bridge.label(sid))
                continue
            tile.set_icon(KIND_ICONS.get(r["kind"], "sensor"))
            tile.set_reading(r.get("value"), r.get("unit", ""), self.bridge.label(sid), alert=sid in alarms)

    def _update_outputs(self, snap: dict) -> None:
        outs = snap.get("outputs", [])
        ids = [o["id"] for o in outs]
        if ids != list(self._outputs):
            while self.fan_grid.count():
                w = self.fan_grid.takeAt(0).widget()
                if w:
                    w.deleteLater()
            self._outputs.clear()
            for i, o in enumerate(outs):
                tile = OutputTile(o["id"])
                tile.clicked.connect(self.open_output.emit)
                self.fan_grid.addWidget(tile, i // 3, i % 3)
                self._outputs[o["id"]] = tile
        for o in outs:
            self._outputs[o["id"]].update_output(o)
        self.no_fans.setVisible(not outs)

    def _rebuild_profiles(self, force: bool = False) -> None:
        cfg = self.bridge.config
        names = tuple(p.name for p in cfg.profiles)
        if names == self._profile_names and self.profile_seg is not None and not force:
            return
        self._profile_names = names
        if self.profile_seg is not None:
            self.profile_seg.deleteLater()
            self.profile_seg = None
        visible = bool(names)
        self.profile_label.setVisible(visible)
        if not visible:
            return
        seg = SegmentedControl()
        seg.add("Default", "")
        for n in names:
            seg.add(n, n)
        prof = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        seg.set_value(prof.name if prof else "")
        seg.changed.connect(lambda name: self.bridge.call("set_profile", name,
                                                          success=f"Profile: {name or 'Default'}"))
        self.profile_seg_holder.addWidget(seg)
        self.profile_seg = seg

    def _update_status(self, snap: dict) -> None:
        pal = theme.palette
        mode = snap.get("mode", "")
        texts = {
            "service": ("Background service — fan control keeps running when you close this window.", pal.success),
            "standalone": ("Running inside this app — fan control stops when you quit. Enable the background "
                           "service in Settings to keep it running.", pal.warning),
            "demo": ("Demo mode — simulated devices; nothing touches your hardware.", pal.info),
        }
        text, color = texts.get(mode, (mode, pal.text_faint))
        self.mode_text.setText(text)
        self.mode_dot.set_color(color, True)
        self._rebuild_profiles()
        if self.profile_seg is not None:
            self.profile_seg.set_value(snap.get("profile") or "")
        while self.alarm_box.count():
            w = self.alarm_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        alarms = snap.get("alarms", [])
        if not alarms:
            lb = QLabel("No alarms set up")
            lb.setObjectName("Faint")
            self.alarm_box.addWidget(lb)
        for a in alarms:
            row = QLabel(("●  " if a["active"] else "○  ") + a["name"] +
                         (f"  ·  {a['seconds']} s" if a["active"] else ""))
            row.setObjectName("Value" if a["active"] else ("Muted" if a["enabled"] else "Faint"))
            if a["active"]:
                row.setStyleSheet(f"color: {pal.danger};")
            self.alarm_box.addWidget(row)
        new = [e for e in snap.get("events", []) if e[0] > self._last_events]
        for t, level, text in new:
            mark = {"error": "!", "warning": "!", "success": "✓"}.get(level, "•")
            self.log.appendPlainText(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {mark}  {text}")
            self._last_events = t

    def _refresh_history(self) -> None:
        now = time.monotonic()
        if now - self._last_history < 2.0:
            return
        self._last_history = now
        ids = self.gs.chart or [s for s in self.pins() if (self.bridge.reading(s) or {}).get("kind")
                                in ("temperature", "delta")][:5]
        pins = list(self._tiles)
        span = float(self.span.value() or 900)

        def done(h):
            for sid, tile in self._tiles.items():
                tile.spark.set_values(h.get("series", {}).get(sid, [])[-180:])
            series = []
            colors = theme.palette.series
            for i, sid in enumerate(ids):
                r = self.bridge.reading(sid) or {}
                series.append({"id": sid, "label": self.bridge.label(sid), "unit": r.get("unit", ""),
                               "values": h.get("series", {}).get(sid, []), "color": colors[i % len(colors)]})
            self.chart.set_data(h.get("times", []), series, span)

        wanted = list(dict.fromkeys([*ids, *pins]))
        if wanted:
            self.bridge.history(wanted, max(span, 180.0), done)
        else:
            self.chart.set_data([], [])

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.bridge.snap:
            self._last_history = 0.0
            self.update_snapshot(self.bridge.snap)
