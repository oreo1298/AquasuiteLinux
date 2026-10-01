"""Dialogs: virtual sensors, feeds, device settings, aquasuite import, profiles, pins, settings, about."""

from __future__ import annotations

import base64
import json
import platform
import shutil
import subprocess
from pathlib import Path

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..core import aquasuite
from ..core.config import (
    VIRTUAL_KINDS,
    FeedConfig,
    LeakshieldFeedConfig,
    ProfileConfig,
    VirtualSensorConfig,
    new_id,
)
from ..core.devices import BY_KIND
from ..core.model import Reading
from ..core.virtual import VARIABLES, VirtualSensors, compile_expression, derived_unit
from .widgets import (
    KeyValueGrid,
    SegmentedControl,
    SensorCombo,
    format_value,
    scaled_font,
)


def _muted(text: str, wrap: bool = True) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("Muted")
    lb.setWordWrap(wrap)
    return lb


def _title(text: str) -> QLabel:
    lb = QLabel(text.upper())
    lb.setObjectName("CardTitle")
    lb.setFont(scaled_font(lb, 0.8, bold=True))
    return lb


def _buttons(dialog: QDialog, ok_text: str = "Save") -> QDialogButtonBox:
    box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    ok = box.button(QDialogButtonBox.Ok)
    ok.setText(ok_text)
    ok.setProperty("variant", "primary")
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


# ---------------------------------------------------------------------- virtual sensors
class VirtualSensorDialog(QDialog):
    """Create or edit a virtual sensor, with a live preview of its value."""

    def __init__(self, bridge, parent=None, sensor: VirtualSensorConfig | None = None, kind: str = "difference",
                 inputs: list[str] | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.sensor = sensor or VirtualSensorConfig(id=new_id(), kind=kind, inputs=list(inputs or []))
        self.is_new = sensor is None
        self.setWindowTitle("New virtual sensor" if self.is_new else f"Edit {self.sensor.name}")
        self.setMinimumWidth(560)
        self.result_sensor: VirtualSensorConfig | None = None
        self._calc = VirtualSensors()

        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.kind = QComboBox()
        for k, label in VIRTUAL_KINDS.items():
            self.kind.addItem(label, k)
        self.kind.setCurrentIndex(max(0, self.kind.findData(self.sensor.kind)))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("Type", self.kind)
        self.name = QLineEdit(self.sensor.name if not self.is_new else "")
        form.addRow("Name", self.name)
        lay.addLayout(form)
        self.help = _muted("")
        lay.addWidget(self.help)

        self.inputs_box = QWidget()
        self.inputs_form = QFormLayout(self.inputs_box)
        self.inputs_form.setContentsMargins(0, 0, 0, 0)
        self.inputs_form.setHorizontalSpacing(14)
        lay.addWidget(self.inputs_box)
        self.combos: list[SensorCombo] = []
        self.multi = QListWidget()
        self.multi.setMinimumHeight(160)
        lay.addWidget(self.multi)

        extra = QFormLayout()
        extra.setHorizontalSpacing(14)
        self.factor = QDoubleSpinBox()
        self.factor.setRange(-1000, 1000)
        self.factor.setDecimals(3)
        self.factor.setValue(self.sensor.factor)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-10000, 10000)
        self.offset.setDecimals(2)
        self.offset.setValue(self.sensor.offset)
        self.value = QDoubleSpinBox()
        self.value.setRange(-100000, 100000)
        self.value.setDecimals(2)
        self.value.setValue(self.sensor.value)
        self.expression = QLineEdit(self.sensor.expression)
        self.expression.setPlaceholderText("e.g. max(a, b) - c")
        self.coolant = QDoubleSpinBox()
        self.coolant.setRange(0.5, 1.2)
        self.coolant.setDecimals(2)
        self.coolant.setSingleStep(0.01)
        self.coolant.setValue(self.sensor.coolant_factor)
        self.coolant.setToolTip("1.00 for water; about 0.93 with 20 % glycol, 0.88 with 30 %")
        self.unit = QLineEdit(self.sensor.unit)
        self.unit.setPlaceholderText("automatic")
        self.unit.setMaximumWidth(120)
        self.smoothing = QDoubleSpinBox()
        self.smoothing.setRange(0, 600)
        self.smoothing.setDecimals(0)
        self.smoothing.setSuffix(" s")
        self.smoothing.setSpecialValueText("off")
        self.smoothing.setValue(self.sensor.smoothing)
        self.smoothing.setToolTip("Averages out quick changes (exponential moving average)")
        self.rows = {}
        for key, label, w in (("factor", "Factor", self.factor), ("offset", "Offset", self.offset),
                              ("value", "Value", self.value), ("expression", "Formula", self.expression),
                              ("coolant", "Coolant factor", self.coolant), ("unit", "Unit", self.unit),
                              ("smoothing", "Smoothing", self.smoothing)):
            lb = QLabel(label)
            extra.addRow(lb, w)
            self.rows[key] = (lb, w)
            if hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._preview)
            else:
                w.textChanged.connect(self._preview)
        lay.addLayout(extra)

        prev = QFrame()
        prev.setObjectName("Inset")
        pl = QHBoxLayout(prev)
        pl.addWidget(_title("Now"))
        self.preview = QLabel("—")
        self.preview.setObjectName("BigValue")
        self.preview.setFont(scaled_font(self.preview, 1.5, bold=True))
        pl.addWidget(self.preview, 1)
        lay.addWidget(prev)
        lay.addWidget(_buttons(self, "Create" if self.is_new else "Save"))
        self._kind_changed()

    # layout for each kind
    def _clear_inputs(self) -> None:
        while self.inputs_form.rowCount():
            self.inputs_form.removeRow(0)
        self.combos = []

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        self._clear_inputs()
        temps = {"temperature", "delta"}
        labels = []
        help_text = ""
        if kind == "difference":
            labels = [("Warmer sensor (e.g. coolant)", temps), ("Reference (e.g. ambient air)", temps)]
            help_text = ("Delta T = coolant temperature − air temperature. Use a sensor in the water and one in "
                         "the air going into the radiators.")
        elif kind == "scale":
            labels = [("Sensor", None)]
            help_text = "Value = sensor × factor + offset (e.g. °C to °F: factor 1.8, offset 32)."
        elif kind == "heat_load":
            labels = [("Flow", {"flow"}), ("Warm side (before the radiator)", temps),
                      ("Cold side (after the radiator)", temps)]
            help_text = "Heat carried away by the coolant: flow × temperature difference × water's heat capacity."
        elif kind == "expression":
            labels = [(f"{VARIABLES[i]}", None) for i in range(4)]
            help_text = ("A formula using a, b, c, d; numbers; + − × ÷ ** %; comparisons; "
                         "“x if condition else y”; min, max, abs, round, sqrt, clamp.")
        elif kind == "constant":
            help_text = "A fixed value — handy as a stand-in while testing curves."
        else:
            help_text = f"{VIRTUAL_KINDS[kind]} of the selected sensors."
        self.help.setText(help_text)
        readings = self.bridge.sensor_list(include_na=True)
        for i, (label, kinds) in enumerate(labels):
            combo = SensorCombo(kinds=kinds, allow_none="—" if kind == "expression" else "Choose a sensor…")
            combo.set_readings([r for r in readings if not kinds or r.get("kind") in kinds]
                               if kinds else readings, exclude={f"virtual/{self.sensor.id}"})
            if i < len(self.sensor.inputs):
                combo.set_current_id(self.sensor.inputs[i])
            elif kind == "difference" and i == 1 and self.sensor.inputs:
                combo.set_current_id(self._guess_ambient(self.sensor.inputs[0]))
            combo.currentIndexChanged.connect(self._preview)
            self.inputs_form.addRow(label, combo)
            self.combos.append(combo)
        multi = kind in ("average", "min", "max", "sum")
        self.multi.setVisible(multi)
        if multi:
            self.multi.clear()
            for r in readings:
                if r["id"] == f"virtual/{self.sensor.id}":
                    continue
                it = QListWidgetItem(f"{r['_display']}  ·  {format_value(r.get('value'), r.get('unit', ''))}")
                it.setData(Qt.UserRole, r["id"])
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if r["id"] in self.sensor.inputs else Qt.Unchecked)
                self.multi.addItem(it)
            self.multi.itemChanged.connect(self._preview)
        show = {"factor": kind == "scale", "offset": kind == "scale", "value": kind == "constant",
                "expression": kind == "expression", "coolant": kind == "heat_load", "unit": True,
                "smoothing": kind != "constant"}
        for key, (lb, w) in self.rows.items():
            lb.setVisible(show[key])
            w.setVisible(show[key])
        if self.is_new and not self.name.text():
            self.name.setPlaceholderText({"difference": "Coolant ΔT", "heat_load": "Heat load"}.get(kind,
                                                                                                  VIRTUAL_KINDS[kind]))
        if kind == "difference" and self.is_new and not self.sensor.smoothing:
            self.smoothing.setValue(5)
        self._preview()

    def _guess_ambient(self, coolant: str) -> str:
        words = ("ambient", "air", "room", "intake", "raum", "luft")
        for r in self.bridge.sensor_list({"temperature"}):
            if r["id"] != coolant and any(w in r["label"].lower() for w in words):
                return r["id"]
        return ""

    def _inputs(self) -> list[str]:
        if self.kind.currentData() in ("average", "min", "max", "sum"):
            return [self.multi.item(i).data(Qt.UserRole) for i in range(self.multi.count())
                    if self.multi.item(i).checkState() == Qt.Checked]
        ids = [c.current_id() for c in self.combos]
        if self.kind.currentData() == "expression":
            while ids and not ids[-1]:
                ids.pop()
        return ids

    def _collect(self) -> VirtualSensorConfig:
        kind = self.kind.currentData()
        name = self.name.text().strip() or self.name.placeholderText() or VIRTUAL_KINDS[kind]
        return VirtualSensorConfig(id=self.sensor.id, name=name, kind=kind, inputs=self._inputs(),
                                   unit=self.unit.text().strip(), factor=self.factor.value(),
                                   offset=self.offset.value(), value=self.value.value(),
                                   expression=self.expression.text().strip(), smoothing=self.smoothing.value(),
                                   coolant_factor=self.coolant.value())

    def _preview(self, *_a) -> None:
        cfg = self._collect()
        inputs = [Reading.from_dict(self.bridge.readings[i]) if i in self.bridge.readings else None
                  for i in cfg.inputs]
        try:
            if cfg.kind == "expression":
                compile_expression(cfg.expression)
            value = self._calc.compute(cfg, inputs)
            self.preview.setText(format_value(value, derived_unit(cfg, inputs)))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.preview.setText(str(exc))

    def accept(self) -> None:
        cfg = self._collect()
        if cfg.kind == "difference" and (len(cfg.inputs) < 2 or not all(cfg.inputs[:2])):
            QMessageBox.warning(self, "Choose two sensors", "A difference needs two sensors.")
            return
        if cfg.kind == "heat_load" and (len(cfg.inputs) < 3 or not all(cfg.inputs[:3])):
            QMessageBox.warning(self, "Choose three sensors", "Heat load needs a flow and two temperatures.")
            return
        if cfg.kind == "expression":
            try:
                compile_expression(cfg.expression)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Formula", str(exc))
                return
        self.result_sensor = cfg
        super().accept()


# ---------------------------------------------------------------------- feeds
class FeedDialog(QDialog):
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.result_change = None
        self.setWindowTitle("Send a sensor to a device")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(_muted("The value is sent every second into one of the device's software sensors, as "
                             "aquasuite does. Curves stored on the device (and the device's own display, if it has "
                             "one) can then use it."))
        if not bridge.config.settings.device_feeds:
            lay.addWidget(_muted("Sending sensor values to devices is turned off in Settings (it is experimental). "
                                 "The feed is saved and starts once you turn it on."))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.device = QComboBox()
        for d in bridge.snap.get("devices", []):
            if "soft_sensors" in d.get("capabilities", []) or "leakshield_feed" in d.get("capabilities", []):
                self.device.addItem(f"{d['name']} ({d['key']})", d["key"])
        self.device.currentIndexChanged.connect(self._device_changed)
        form.addRow("Device", self.device)
        self.slot = QSpinBox()
        self.slot.setRange(1, 16)
        self.slot_label = QLabel("Software sensor")
        form.addRow(self.slot_label, self.slot)
        self.source = SensorCombo(allow_none="Choose a sensor…")
        self.source.set_readings(bridge.sensor_list())
        self.source_label = QLabel("Value")
        form.addRow(self.source_label, self.source)
        self.flow = SensorCombo(kinds={"flow"}, allow_none="None")
        self.flow.set_readings(bridge.sensor_list({"flow"}))
        self.flow_label = QLabel("Flow")
        form.addRow(self.flow_label, self.flow)
        lay.addLayout(form)
        self.used = _muted("")
        lay.addWidget(self.used)
        lay.addWidget(_buttons(self, "Add"))
        self._device_changed()
        if not self.device.count():
            self.used.setText("None of the connected devices has software sensors.")

    def _is_leakshield(self) -> bool:
        key = self.device.currentData() or ""
        return key.startswith("leakshield")

    def _device_changed(self) -> None:
        ls = self._is_leakshield()
        self.slot.setVisible(not ls)
        self.slot_label.setVisible(not ls)
        self.flow.setVisible(ls)
        self.flow_label.setVisible(ls)
        self.source_label.setText("Pump speed" if ls else "Value")
        key = self.device.currentData()
        used = sorted({f["slot"] for f in self.bridge.snap.get("feeds", []) if f["device"] == key} |
                      {f.slot for f in self.bridge.config.feeds if f.device == key})
        if used and not ls:
            self.used.setText("In use: software sensor " + ", ".join(map(str, used)))
            free = next((s for s in range(1, 17) if s not in used), 16)
            self.slot.setValue(free)
        else:
            self.used.setText("")

    def accept(self) -> None:
        key = self.device.currentData()
        if not key:
            return super().reject()
        if self._is_leakshield():
            pump, flow = self.source.current_id(), self.flow.current_id()

            def change(cfg):
                cfg.leakshield = [x for x in cfg.leakshield if x.device != key]
                cfg.leakshield.append(LeakshieldFeedConfig(key, pump, flow))
        else:
            src, slot = self.source.current_id(), self.slot.value()
            if not src:
                QMessageBox.warning(self, "Choose a sensor", "Choose the value to send.")
                return

            def change(cfg):
                cfg.feeds = [f for f in cfg.feeds if not (f.device == key and f.slot == slot)]
                cfg.feeds.append(FeedConfig(key, slot, src))
        self.result_change = change
        super().accept()


# ---------------------------------------------------------------------- device settings
class DeviceDialog(QDialog):
    def __init__(self, bridge, key: str, parent=None, on_import=None):
        super().__init__(parent)
        self.bridge = bridge
        self.key = key
        self.on_import = on_import
        info = next((d for d in bridge.snap.get("devices", []) if d["key"] == key), None) or {}
        self.info = info
        self.setWindowTitle(f"{info.get('name', key)} settings")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.name = QLineEdit(bridge.config.devices.get(key).name if key in bridge.config.devices else "")
        self.name.setPlaceholderText(info.get("model", key))
        form.addRow("Name", self.name)
        lay.addLayout(form)
        grid = KeyValueGrid(2)
        backend = {"hidraw": "USB (hidraw)", "hwmon": "kernel driver (hwmon)", "simulated": "simulated"}
        grid.add_row("Model", info.get("model", "—"))
        grid.add_row("Serial", info.get("serial") or "—")
        grid.add_row("Firmware", str(info.get("firmware") or "—"))
        grid.add_row("Connection", backend.get(info.get("backend"), info.get("backend", "—")))
        grid.add_row("Power cycles", str(info.get("power_cycles") if info.get("power_cycles") is not None else "—"))
        grid.add_row("Settings writes", f"{info.get('writes', 0)} this session")
        if info.get("feed_path"):
            state = {"ok": "working", "broken": "not working", "pending": "checking…",
                     "off": "turned off in Settings"}.get(info.get("feed", ""), "")
            grid.add_row("Software sensor data", info["feed_path"] + (f" — {state}" if state else ""))
        lay.addWidget(grid)
        if info.get("notes"):
            lay.addWidget(_muted(info["notes"]))
        caps = set(info.get("capabilities", []))
        self.offsets: list[QDoubleSpinBox] = []
        self.pulses: QSpinBox | None = None
        self.settings: dict | None = None
        self.sensor_box = QWidget()
        sl = QFormLayout(self.sensor_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setHorizontalSpacing(14)
        self.sensor_title = _title("Sensor calibration (stored on the device)")
        lay.addWidget(self.sensor_title)
        lay.addWidget(self.sensor_box)
        self.status = _muted("")
        lay.addWidget(self.status)
        spec = BY_KIND.get(info.get("kind", ""))
        if "temp_offsets" in caps and spec:
            for i in range(spec.num_temp_offsets):
                sp = QDoubleSpinBox()
                sp.setRange(-15, 15)
                sp.setDecimals(2)
                sp.setSingleStep(0.1)
                sp.setSuffix(" K")
                sp.setEnabled(False)
                label = spec.temps[i].label if i < len(spec.temps) else f"Sensor {i + 1}"
                r = bridge.reading(f"{key}/temp{i + 1}")
                sl.addRow(f"{r['label'] if r else label} offset", sp)
                self.offsets.append(sp)
        if "flow_pulses" in caps:
            self.pulses = QSpinBox()
            self.pulses.setRange(10, 1000)
            self.pulses.setSuffix(" pulses/L")
            self.pulses.setEnabled(False)
            self.pulses.setToolTip("aquacomputer high flow: 169; mps flow sensors differ — see the sensor's manual")
            sl.addRow("Flow sensor calibration", self.pulses)
        has_sensor_settings = bool(self.offsets or self.pulses)
        self.sensor_title.setVisible(has_sensor_settings)
        self.sensor_box.setVisible(has_sensor_settings)

        row = QHBoxLayout()
        self.btn_backup = QPushButton("Back up settings…")
        self.btn_backup.clicked.connect(self._backup)
        self.btn_restore = QPushButton("Restore…")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_import = QPushButton("Import aquasuite settings…")
        self.btn_import.clicked.connect(self._import)
        for b in (self.btn_backup, self.btn_restore, self.btn_import):
            b.setEnabled("backup" in caps)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        if "backup" not in caps and info.get("backend") == "hwmon":
            lay.addWidget(_muted("Settings are only reachable over USB: install the udev rule (see the README) or use "
                                 "the background service."))
        lay.addWidget(_buttons(self))
        if has_sensor_settings:
            self.status.setText("Reading the device settings…")
            bridge.call("device_settings", key, done=self._got_settings,
                        error=lambda e: self.status.setText(f"Could not read the settings: {e}"))

    def _got_settings(self, data: dict) -> None:
        self.settings = data
        for sp, v in zip(self.offsets, data.get("temp_offsets", [])):
            sp.setValue(v)
            sp.setEnabled(True)
        if self.pulses is not None and data.get("flow_pulses") is not None:
            self.pulses.setValue(int(data["flow_pulses"]))
            self.pulses.setEnabled(True)
        self.status.setText("")

    def _backup(self) -> None:
        def got(b):
            default = str(Path.home() / f"{self.info.get('kind', 'device')}-{self.info.get('serial', '')}-settings.json")
            path, _ = QFileDialog.getSaveFileName(self, "Save settings backup", default, "Settings backup (*.json)")
            if path:
                Path(path).write_text(json.dumps(b, indent=2), encoding="utf-8")
                self.bridge.message.emit(f"Settings saved to {path}", "success")
        self.bridge.call("backup", self.key, done=got)

    def _restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restore settings", str(Path.home()), "Settings backup (*.json)")
        if not path:
            return
        try:
            backup = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Restore", f"Could not read the file: {exc}")
            return
        if QMessageBox.question(self, "Restore settings", "Replace all settings stored on the device with this "
                                "backup?") != QMessageBox.Yes:
            return
        self.bridge.call("restore", self.key, backup, success="Settings restored")

    def _import(self) -> None:
        if self.on_import:
            self.reject()
            self.on_import(self.key)

    def accept(self) -> None:
        name = self.name.text().strip()

        def change(cfg):
            cfg.device(self.key).name = name
        if name != (self.bridge.config.devices.get(self.key).name if self.key in self.bridge.config.devices else ""):
            self.bridge.edit(change, "Device renamed")
        if self.settings is not None:
            changes = {}
            offsets = [sp.value() for sp in self.offsets]
            if [round(v, 2) for v in offsets] != [round(v, 2) for v in self.settings.get("temp_offsets", [])]:
                changes["temp_offsets"] = offsets
            if self.pulses is not None and self.pulses.value() != self.settings.get("flow_pulses"):
                changes["flow_pulses"] = self.pulses.value()
            if changes:
                self.bridge.call("apply_device_settings", self.key, changes)
        super().accept()


# ---------------------------------------------------------------------- aquasuite import
class ImportDialog(QDialog):
    """Bring fan curves over from aquasuite: read them from the device, or find them in a file."""

    def __init__(self, bridge, parent=None, device_key: str | None = None, new_virtual=None):
        super().__init__(parent)
        self.bridge = bridge
        self.new_virtual = new_virtual
        self.plan: aquasuite.ImportPlan | None = None
        self.found: list[aquasuite.FoundSettings] = []
        self.slot_combos: dict[int, SensorCombo] = {}
        self.setWindowTitle("Import aquasuite settings")
        self.resize(760, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(_muted("aquasuite stores your fan curves, their inputs and power limits on the device itself, so "
                             "the easiest way to bring them over is to read them back from the device. You can also "
                             "pick any aquasuite file or backup; AquasuiteLinux finds device settings inside it and "
                             "only accepts ones whose checksum is correct."))
        self.source = SegmentedControl()
        self.source.add("From a connected device", "device")
        self.source.add("From a file", "file")
        self.source.changed.connect(self._source_changed)
        lay.addWidget(self.source)

        self.dev_row = QWidget()
        dr = QHBoxLayout(self.dev_row)
        dr.setContentsMargins(0, 0, 0, 0)
        self.device = QComboBox()
        for d in bridge.snap.get("devices", []):
            if "backup" in d.get("capabilities", []):
                self.device.addItem(f"{d['name']} ({d['key']})", d["key"])
        if device_key:
            self.device.setCurrentIndex(max(0, self.device.findData(device_key)))
        read = QPushButton("Read settings")
        read.setProperty("variant", "primary")
        read.clicked.connect(self._read_device)
        dr.addWidget(self.device, 1)
        dr.addWidget(read)
        lay.addWidget(self.dev_row)

        self.file_row = QWidget()
        fr = QGridLayout(self.file_row)
        fr.setContentsMargins(0, 0, 0, 0)
        choose = QPushButton("Choose file…")
        choose.clicked.connect(self._choose_file)
        self.file_label = _muted("aquasuite keeps its data in C:\\ProgramData\\aquasuite-data and "
                                 "Documents\\aquasuite on Windows.")
        self.found_combo = QComboBox()
        self.found_combo.currentIndexChanged.connect(self._found_selected)
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(self._found_selected)
        fr.addWidget(choose, 0, 0)
        fr.addWidget(self.file_label, 0, 1)
        fr.addWidget(QLabel("Settings found"), 1, 0)
        fr.addWidget(self.found_combo, 1, 1)
        fr.addWidget(QLabel("Apply to"), 2, 0)
        fr.addWidget(self.target, 2, 1)
        fr.setColumnStretch(1, 1)
        lay.addWidget(self.file_row)

        lay.addWidget(_title("Outputs"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Output", "aquasuite setting"])
        self.tree.setColumnWidth(0, 200)
        self.tree.setRootIsDecorated(False)
        lay.addWidget(self.tree, 1)
        self.slots_title = _title("Software sensors")
        lay.addWidget(self.slots_title)
        self.slots_box = QWidget()
        self.slots_form = QFormLayout(self.slots_box)
        self.slots_form.setContentsMargins(0, 0, 0, 0)
        self.slots_form.setHorizontalSpacing(14)
        lay.addWidget(self.slots_box)
        self.notes = _muted("")
        lay.addWidget(self.notes)
        self.write_device = QCheckBox("Also write these settings to the device (replaces what is stored on it)")
        lay.addWidget(self.write_device)
        self.box = _buttons(self, "Import")
        lay.addWidget(self.box)
        self.source.set_value("device" if self.device.count() else "file")
        self._source_changed(self.source.value())
        self._set_ready(False)
        if device_key and self.device.count():
            self._read_device()

    def _set_ready(self, ready: bool) -> None:
        self.box.button(QDialogButtonBox.Ok).setEnabled(ready)
        self.slots_title.setVisible(ready and bool(self.plan and self.plan.soft_slots))
        self.slots_box.setVisible(ready and bool(self.plan and self.plan.soft_slots))

    def _source_changed(self, value) -> None:
        self.dev_row.setVisible(value == "device")
        self.file_row.setVisible(value == "file")
        self.write_device.setVisible(value == "file")
        self.write_device.setChecked(False)

    def _read_device(self) -> None:
        key = self.device.currentData()
        if not key:
            return
        self.notes.setText("Reading the settings from the device…")

        def got(data):
            spec = BY_KIND[data["kind"]]
            raw = base64.b64decode(data["raw"])
            self._show_plan(aquasuite.plan_import(spec, raw, key, data.get("names")))

        self.bridge.call("device_settings", key, done=got,
                         error=lambda e: self.notes.setText(f"Could not read the device: {e}"))

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "aquasuite file or backup", str(Path.home()), "All files (*)")
        if not path:
            return
        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Import", f"Could not read the file: {exc}")
            return
        self.found = aquasuite.find_settings(blob, Path(path).name)
        self.found_combo.blockSignals(True)
        self.found_combo.clear()
        for f in self.found:
            self.found_combo.addItem(f"{f.spec.name} — {f.origin}")
        self.found_combo.blockSignals(False)
        if not self.found:
            self.file_label.setText(f"No device settings found in {Path(path).name}. If this is an aquasuite file, "
                                    "please open an issue and attach it so its format can be supported; meanwhile "
                                    "use “From a connected device”.")
            self.tree.clear()
            self._set_ready(False)
            return
        self.file_label.setText(f"{Path(path).name}: found {len(self.found)} settings block(s)")
        self._found_selected()

    def _found_selected(self) -> None:
        i = self.found_combo.currentIndex()
        if i < 0 or i >= len(self.found):
            return
        f = self.found[i]
        sender = self.sender()
        if sender is not self.target:
            self.target.blockSignals(True)
            self.target.clear()
            for d in self.bridge.snap.get("devices", []):
                if d.get("kind") == f.spec.kind:
                    self.target.addItem(f"{d['name']} ({d['key']})", d["key"])
            key_from_file = f"{f.spec.kind}-{f.serial}" if f.serial else f.spec.kind
            if not self.target.count():
                self.target.addItem(f"{f.spec.name} (not connected — saved for later)", key_from_file)
            self.target.blockSignals(False)
        key = self.target.currentData()
        connected = any(d["key"] == key for d in self.bridge.snap.get("devices", []))
        self.write_device.setEnabled(connected)
        self._show_plan(aquasuite.plan_import(f.spec, f.data, key, f.names))

    def _show_plan(self, plan: aquasuite.ImportPlan) -> None:
        self.plan = plan
        self.tree.clear()
        for item in plan.items:
            names = self.bridge.outputs.get(item.output_id, {})
            tw = QTreeWidgetItem([names.get("name") or item.label, item.description])
            tw.setFlags(tw.flags() | Qt.ItemIsUserCheckable)
            tw.setCheckState(0, Qt.Checked if item.selected else Qt.Unchecked)
            tw.setData(0, Qt.UserRole, item.output_id)
            tw.setToolTip(1, item.description)
            self.tree.addTopLevelItem(tw)
        while self.slots_form.rowCount():
            self.slots_form.removeRow(0)
        self.slot_combos.clear()
        suggested = aquasuite.suggest_sources(plan, self.bridge.config, self.bridge.readings)
        for slot in plan.soft_slots:
            row = QHBoxLayout()
            combo = SensorCombo(kinds={"temperature", "delta", "percent", "power", "flow"},
                                allow_none="Leave unassigned (runs at fallback power)")
            combo.set_readings(self.bridge.sensor_list({"temperature", "delta", "percent", "power", "flow"}))
            want = suggested.get(slot)
            if plan.delta_t:
                text = plan.describe_source(aquasuite.NEW_DELTA_T)
                combo.insertItem(1, text[:1].upper() + text[1:], aquasuite.NEW_DELTA_T)
            dts = [v for v in self.bridge.config.virtual_sensors if v.kind == "difference"]
            if want:
                combo.setCurrentIndex(max(0, combo.findData(want)))
            elif dts:
                combo.set_current_id(f"virtual/{dts[0].id}")
            new = QPushButton("New Delta T…")
            new.clicked.connect(lambda _c=False, cb=combo: self._new_delta(cb))
            row.addWidget(combo, 1)
            row.addWidget(new)
            w = QWidget()
            w.setLayout(row)
            name = plan.slot_name(slot)
            self.slots_form.addRow(f"Software sensor {slot}{f' “{name}”' if name else ''} ←", w)
            self.slot_combos[slot] = combo
        notes = list(plan.notes)
        if plan.sensor_names():
            notes.append("Sensor names from aquasuite: " + ", ".join(
                f"{k} “{v}”" for k, v in plan.sensor_names().items()))
        if plan.temp_offsets and any(plan.temp_offsets):
            notes.append("Sensor offsets on the device: " + ", ".join(f"{v:+.2f} K" for v in plan.temp_offsets))
        if plan.flow_pulses:
            notes.append(f"Flow sensor calibration: {plan.flow_pulses} pulses per litre.")
        self.notes.setText("\n".join(notes))
        self._set_ready(True)

    def _new_delta(self, combo: SensorCombo) -> None:
        dlg = VirtualSensorDialog(self.bridge, self, kind="difference")
        if dlg.exec() and dlg.result_sensor:
            vs = dlg.result_sensor

            def done(ok):
                if ok:
                    combo.set_readings(self.bridge.sensor_list({"temperature", "delta", "percent", "power", "flow"}))
                    combo.set_current_id(f"virtual/{vs.id}")
            self.bridge.edit(lambda cfg: cfg.virtual_sensors.append(vs), f"“{vs.name}” created", done=done)

    def accept(self) -> None:
        if not self.plan:
            return
        for i in range(self.tree.topLevelItemCount()):
            tw = self.tree.topLevelItem(i)
            self.plan.items[i].selected = tw.checkState(0) == Qt.Checked
        slot_map = {slot: combo.current_id() for slot, combo in self.slot_combos.items()}
        cfg = aquasuite.apply_import(self.bridge.config, self.plan, slot_map)
        n = sum(1 for i in self.plan.items if i.selected)
        if self.write_device.isChecked() and self.write_device.isEnabled() and self.found:
            f = self.found[self.found_combo.currentIndex()]
            backup = aquasuite.make_backup(f.spec, f.serial, 0, f.data)
            self.bridge.call("restore", self.plan.device_key, backup, success="Settings written to the device")
        self.bridge.apply(cfg, f"Imported {n} output{'s' if n != 1 else ''} from aquasuite")
        super().accept()


# ---------------------------------------------------------------------- profiles
class ProfilesDialog(QDialog):
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.cfg = bridge.config.copy()
        self.setWindowTitle("Profiles")
        self.resize(720, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(_muted("A profile swaps the controller of chosen outputs — for example quiet curves at night. "
                             "Outputs a profile doesn't list keep the controller set on the Fans page."))
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = QPushButton("New")
        add.clicked.connect(self._new)
        ren = QPushButton("Rename")
        ren.clicked.connect(self._rename)
        rem = QPushButton("Delete")
        rem.setProperty("variant", "danger")
        rem.clicked.connect(self._delete)
        for b in (add, ren, rem):
            row.addWidget(b)
        left.addLayout(row)
        body.addLayout(left, 1)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Output", "Controller in this profile"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 200)
        body.addWidget(self.table, 2)
        lay.addLayout(body, 1)
        lay.addWidget(_buttons(self))
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for p in self.cfg.profiles:
            self.list.addItem(p.name)
        if self.cfg.profiles:
            self.list.setCurrentRow(0)
        else:
            self.table.setRowCount(0)

    def _store(self) -> None:
        row = getattr(self, "_row", -1)
        if 0 <= row < len(self.cfg.profiles):
            prof = self.cfg.profiles[row]
            prof.assignments = {}
            for i in range(self.table.rowCount()):
                oid = self.table.item(i, 0).data(Qt.UserRole)
                cid = self.table.cellWidget(i, 1).currentData()
                if cid:
                    prof.assignments[oid] = cid

    def _load(self, row: int) -> None:
        self._store()
        self._row = row
        if row < 0 or row >= len(self.cfg.profiles):
            return
        prof = self.cfg.profiles[row]
        outs = self.bridge.snap.get("outputs", [])
        self.table.setRowCount(len(outs))
        for i, o in enumerate(outs):
            it = QTableWidgetItem(f"{o['name']} ({self.bridge.device_name(o['device'])})")
            it.setData(Qt.UserRole, o["id"])
            it.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(i, 0, it)
            combo = QComboBox()
            combo.addItem("Same as default", "")
            for c in self.cfg.controllers:
                combo.addItem(c.name, c.id)
            combo.setCurrentIndex(max(0, combo.findData(prof.assignments.get(o["id"], ""))))
            self.table.setCellWidget(i, 1, combo)

    def _new(self) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Name:", text="Quiet")
        if ok and name.strip():
            self._store()
            self.cfg.profiles.append(ProfileConfig(id=new_id(), name=name.strip()))
            self._row = -1
            self._fill()
            self.list.setCurrentRow(len(self.cfg.profiles) - 1)

    def _rename(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        name, ok = QInputDialog.getText(self, "Rename profile", "Name:", text=self.cfg.profiles[row].name)
        if ok and name.strip():
            self.cfg.profiles[row].name = name.strip()
            self.list.item(row).setText(name.strip())

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        prof = self.cfg.profiles.pop(row)
        if self.cfg.active_profile == prof.id:
            self.cfg.active_profile = ""
        self._row = -1
        self._fill()

    def accept(self) -> None:
        self._store()
        self.bridge.apply(self.cfg, "Profiles saved")
        super().accept()


# ---------------------------------------------------------------------- pins
class PinsDialog(QDialog):
    def __init__(self, bridge, gui_settings, parent=None):
        super().__init__(parent)
        from .overview import default_pins
        self.bridge = bridge
        self.gs = gui_settings
        self.setWindowTitle("Overview readings")
        self.resize(560, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(_muted("Tick the readings shown as tiles on the overview (and in its chart)."))
        self.list = QListWidget()
        pins = gui_settings.pinned or default_pins(bridge)
        chart = set(gui_settings.chart)
        for r in bridge.sensor_list(include_na=False):
            it = QListWidgetItem(f"{r['_display']}  ·  {format_value(r.get('value'), r.get('unit', ''))}")
            it.setData(Qt.UserRole, r["id"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if r["id"] in pins else Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        self.chart_same = QCheckBox("Chart the pinned temperatures (otherwise keep my chart selection)")
        self.chart_same.setChecked(not chart)
        lay.addWidget(self.chart_same)
        row = QHBoxLayout()
        reset = QPushButton("Automatic choice")
        reset.clicked.connect(self._reset)
        row.addWidget(reset)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_buttons(self))
        self._auto = False

    def _reset(self) -> None:
        self._auto = True
        self.accept()

    def accept(self) -> None:
        if self._auto:
            self.gs.pinned = []
        else:
            pins = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                    if self.list.item(i).checkState() == Qt.Checked]
            self.gs.pinned = pins
        if self.chart_same.isChecked():
            self.gs.chart = []
        self.gs.save()
        super().accept()


# ---------------------------------------------------------------------- settings
SERVICE_UNIT = "aquasuited.service"


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def service_unit_installed() -> bool:
    if not systemctl_available():
        return False
    try:
        out = subprocess.run(["systemctl", "list-unit-files", SERVICE_UNIT], capture_output=True, text=True,
                             timeout=5)
        return SERVICE_UNIT in out.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def enable_service() -> tuple[bool, str]:
    """Enable and start the service through polkit (pkexec)."""
    if not shutil.which("pkexec"):
        return False, "pkexec (polkit) is not installed. Run: sudo systemctl enable --now aquasuited"
    try:
        out = subprocess.run(["pkexec", "systemctl", "enable", "--now", SERVICE_UNIT], capture_output=True,
                             text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if out.returncode != 0:
        return False, (out.stderr or out.stdout or "cancelled").strip()
    return True, "The background service is running."


class SettingsDialog(QDialog):
    def __init__(self, bridge, gui_settings, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.gs = gui_settings
        self.switch_to_service = False
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        s = bridge.config.settings
        lay = QVBoxLayout(self)

        lay.addWidget(_title("Appearance"))
        f1 = QFormLayout()
        f1.setHorizontalSpacing(14)
        self.theme = SegmentedControl()
        for label, v in (("System", "system"), ("Dark", "dark"), ("Light", "light")):
            self.theme.add(label, v)
        self.theme.set_value(gui_settings.theme)
        f1.addRow("Theme", self.theme)
        self.tray = QCheckBox("Show an icon in the system tray")
        self.tray.setChecked(gui_settings.tray)
        self.close_tray = QCheckBox("Closing the window keeps the app running in the tray")
        self.close_tray.setChecked(gui_settings.close_to_tray)
        f1.addRow("", self.tray)
        f1.addRow("", self.close_tray)
        lay.addLayout(f1)

        lay.addWidget(_title("Control"))
        f2 = QFormLayout()
        f2.setHorizontalSpacing(14)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 10)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" s")
        self.interval.setValue(s.interval)
        f2.addRow("Update every", self.interval)
        self.history = QSpinBox()
        self.history.setRange(5, 24 * 60)
        self.history.setSuffix(" min")
        self.history.setValue(s.history_minutes)
        f2.addRow("Keep history for", self.history)
        self.exit = QComboBox()
        self.exit.addItem("Set software-controlled fans to their fallback power", "fallback")
        self.exit.addItem("Leave them as they are", "keep")
        self.exit.setCurrentIndex(max(0, self.exit.findData(s.exit_action)))
        f2.addRow("When control stops", self.exit)
        self.system = QCheckBox("Read CPU, GPU and drive temperatures of this PC")
        self.system.setChecked(s.system_sensors)
        self.nvidia = QCheckBox("Include NVIDIA GPUs (nvidia-smi)")
        self.nvidia.setChecked(s.nvidia)
        f2.addRow("", self.system)
        f2.addRow("", self.nvidia)
        self.feeds = QCheckBox("Send sensor values to devices (experimental)")
        self.feeds.setChecked(s.device_feeds)
        self.feeds.setToolTip("Lets a QUADRO, OCTO or D5 NEXT run curves on a Delta T or a PC sensor by itself, and "
                              "gives a LEAKSHIELD the pump speed and flow.\nUses the device's USB bulk endpoint, which "
                              "isn't confirmed on real hardware yet. When off, those curves run in software.")
        f2.addRow("", self.feeds)
        lay.addLayout(f2)

        lay.addWidget(_title("Background service"))
        mode = bridge.mode
        if mode == "service":
            text = "Connected to the background service: fan control runs at boot and without this window."
        elif mode == "demo":
            text = "Demo mode: simulated devices."
        elif service_unit_installed():
            text = ("The service is installed but not running. Enable it to keep fan control running at boot and "
                    "when this window is closed.")
        else:
            text = ("The service unit isn't installed (install the Arch package, or run packaging/install.sh "
                    "--service).")
        lay.addWidget(_muted(text))
        srow = QHBoxLayout()
        self.btn_service = QPushButton("Enable background service")
        self.btn_service.setProperty("variant", "primary")
        self.btn_service.setEnabled(mode == "standalone" and service_unit_installed())
        self.btn_service.clicked.connect(self._enable_service)
        srow.addWidget(self.btn_service)
        srow.addStretch(1)
        lay.addLayout(srow)
        self.prefer = QCheckBox("Use the background service when it is running")
        self.prefer.setChecked(gui_settings.prefer_service)
        lay.addWidget(self.prefer)
        lay.addWidget(_buttons(self))

    def _enable_service(self) -> None:
        ok, msg = enable_service()
        if ok:
            self.switch_to_service = True
            QMessageBox.information(self, "Background service", msg + "\n\nYour settings will be moved to it.")
            self.accept()
        else:
            QMessageBox.warning(self, "Background service", msg)

    def accept(self) -> None:
        self.gs.theme = self.theme.value() or "system"
        self.gs.tray = self.tray.isChecked()
        self.gs.close_to_tray = self.close_tray.isChecked()
        self.gs.prefer_service = self.prefer.isChecked()
        self.gs.save()
        s = self.bridge.config.settings
        if (self.interval.value(), self.history.value(), self.exit.currentData(), self.system.isChecked(),
                self.nvidia.isChecked(), self.feeds.isChecked()) != (s.interval, s.history_minutes, s.exit_action,
                                                                     s.system_sensors, s.nvidia, s.device_feeds):
            def change(cfg):
                cfg.settings.interval = self.interval.value()
                cfg.settings.history_minutes = self.history.value()
                cfg.settings.exit_action = self.exit.currentData()
                cfg.settings.system_sensors = self.system.isChecked()
                cfg.settings.nvidia = self.nvidia.isChecked()
                cfg.settings.device_feeds = self.feeds.isChecked()
            self.bridge.edit(change, "Settings saved")
        super().accept()


# ---------------------------------------------------------------------- about
class AboutDialog(QDialog):
    def __init__(self, parent=None, icon=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {__app_name__}")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        head = QHBoxLayout()
        if icon is not None and not icon.isNull():
            logo = QLabel()
            logo.setPixmap(icon.pixmap(64, 64))
            head.addWidget(logo)
        titles = QVBoxLayout()
        name = QLabel(__app_name__)
        name.setFont(scaled_font(name, 1.6, bold=True))
        titles.addWidget(name)
        titles.addWidget(_muted(f"Version {__version__} · Aquacomputer control for Linux", False))
        head.addLayout(titles, 1)
        lay.addLayout(head)
        lay.addWidget(_muted("Monitoring, fan curves, Delta T and other virtual sensors, alarms and profiles for "
                             "Aquacomputer QUADRO, OCTO, D5 NEXT, aquaero, farbwerk, high flow, LEAKSHIELD and "
                             "aquastream devices. Built with Qt."))
        line = QFrame()
        line.setObjectName("Divider")
        lay.addWidget(line)
        for d in (f"Qt {qVersion()} · PySide6 {pyside_version}",
                  f"Python {platform.python_version()} on {platform.system()} {platform.release()}"):
            lay.addWidget(_muted(d, False))
        lay.addWidget(_muted("Protocol knowledge comes from the Linux aquacomputer_d5next driver and liquidctl; see "
                             "NOTICE. AquasuiteLinux is an independent project and is not affiliated with or endorsed "
                             "by Aqua Computer GmbH & Co. KG. aquasuite, QUADRO, OCTO and the other product names are "
                             "their trademarks."))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        home = buttons.addButton("Project page", QDialogButtonBox.ActionRole)
        home.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/oreo1298/AquasuiteLinux")))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)
