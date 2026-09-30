"""Alarms (limits with actions) and the CSV data log."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.alarms import ACTIONS
from ..core.config import AlarmConfig, new_id
from .widgets import Card, SegmentedControl, SensorCombo, flat_button, format_value


class AlarmsPage(QWidget):
    def __init__(self, bridge):
        super().__init__()
        self.setObjectName("Page")
        self.bridge = bridge
        self.current: AlarmConfig | None = None
        self.dirty = False
        self._loading = False

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        left = Card("Alarms", "bell")
        left.setMinimumWidth(240)
        left.setMaximumWidth(380)
        add = flat_button("plus", "Add an alarm")
        add.clicked.connect(lambda: self.add(""))
        left.add_header_widget(add)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._selected)
        left.add(self.list, 1)
        self.empty = QLabel("No alarms yet.\nA coolant temperature limit and a low-flow warning are a good start.")
        self.empty.setObjectName("Empty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        left.add(self.empty)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(12)
        self.editor = Card("Alarm", "warning")
        body = QWidget()
        f = QFormLayout(body)
        f.setHorizontalSpacing(14)
        self.enabled = QCheckBox("Enabled")
        self.enabled.toggled.connect(self._touch)
        f.addRow("", self.enabled)
        self.name = QLineEdit()
        self.name.textEdited.connect(self._touch)
        f.addRow("Name", self.name)
        self.sensor = SensorCombo(allow_none="Choose a sensor…")
        self.sensor.currentIndexChanged.connect(self._touch)
        f.addRow("Sensor", self.sensor)
        self.condition = SegmentedControl()
        for label, v in (("Above", "above"), ("Below", "below"), ("Unavailable", "missing")):
            self.condition.add(label, v)
        self.condition.changed.connect(self._condition_changed)
        f.addRow("Alarm when", self.condition)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-1000, 100000)
        self.threshold.setDecimals(1)
        self.threshold.valueChanged.connect(self._touch)
        f.addRow("Limit", self.threshold)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 3600)
        self.delay.setDecimals(0)
        self.delay.setSuffix(" s")
        self.delay.valueChanged.connect(self._touch)
        f.addRow("For at least", self.delay)
        self.actions: dict[str, QCheckBox] = {}
        box = QVBoxLayout()
        box.setSpacing(7)
        for key, label in ACTIONS.items():
            cb = QCheckBox(label)
            cb.toggled.connect(self._touch)
            self.actions[key] = cb
            box.addWidget(cb)
        self.command = QLineEdit()
        self.command.setPlaceholderText("e.g. notify-send \"Coolant warm\"  (AQUA_ALARM and AQUA_VALUE are set)")
        self.command.textEdited.connect(self._touch)
        box.addWidget(self.command)
        f.addRow("Then", box)
        self.state = QLabel()
        self.state.setObjectName("Muted")
        f.addRow("", self.state)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setProperty("variant", "danger")
        self.btn_delete.clicked.connect(self._delete)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setProperty("variant", "primary")
        self.btn_apply.clicked.connect(self.apply)
        row.addWidget(self.btn_delete)
        row.addWidget(self.btn_apply)
        f.addRow(row)
        self.body = body
        self.editor.add(body)
        self.placeholder = QLabel("Select an alarm, or add one with +")
        self.placeholder.setObjectName("Empty")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.editor.add(self.placeholder, 1)
        self.editor.body().addStretch(1)
        rl.addWidget(self.editor, 1)

        log = Card("Data log", "log")
        lf = QFormLayout()
        lf.setHorizontalSpacing(14)
        self.log_on = QCheckBox("Write every reading to a CSV file")
        self.log_on.toggled.connect(self._log_touch)
        lf.addRow("", self.log_on)
        self.log_interval = QDoubleSpinBox()
        self.log_interval.setRange(1, 3600)
        self.log_interval.setDecimals(0)
        self.log_interval.setSuffix(" s")
        self.log_interval.valueChanged.connect(self._log_touch)
        lf.addRow("Every", self.log_interval)
        drow = QHBoxLayout()
        self.log_dir = QLineEdit()
        self.log_dir.setPlaceholderText("default folder")
        self.log_dir.textEdited.connect(self._log_touch)
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._browse)
        drow.addWidget(self.log_dir, 1)
        drow.addWidget(browse)
        lf.addRow("Folder", drow)
        lrow = QHBoxLayout()
        lrow.addStretch(1)
        self.btn_log = QPushButton("Apply")
        self.btn_log.clicked.connect(self._apply_log)
        lrow.addWidget(self.btn_log)
        lf.addRow(lrow)
        note = QLabel("One file per day. The background service writes to /var/log/aquasuitelinux; the app on its "
                      "own writes to ~/.local/share/aquasuitelinux/log.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        lf.addRow(note)
        log.add_layout(lf)
        rl.addWidget(log)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([280, 1000])

        bridge.config_changed.connect(self._config_changed)
        bridge.snapshot.connect(self._snapshot)
        self.body.setVisible(False)
        self._set_dirty(False)

    # ------------------------------------------------------------------ list
    def _config_changed(self, cfg) -> None:
        keep = self.current.id if self.current else None
        self._fill(select=keep, reload=not self.dirty)
        s = cfg.settings
        self.log_on.blockSignals(True)
        self.log_on.setChecked(s.log_enabled)
        self.log_on.blockSignals(False)
        self.log_interval.blockSignals(True)
        self.log_interval.setValue(s.log_interval)
        self.log_interval.blockSignals(False)
        self.log_dir.setText(s.log_dir)
        self.btn_log.setEnabled(False)

    def _fill(self, select: str | None = None, reload: bool = True) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        row = -1
        states = {a["id"]: a for a in self.bridge.snap.get("alarms", [])}
        for i, a in enumerate(self.bridge.config.alarms):
            st = states.get(a.id, {})
            mark = "● " if st.get("active") else ""
            it = QListWidgetItem(f"{mark}{a.name}\n{self._summary(a)}")
            it.setData(Qt.UserRole, a.id)
            self.list.addItem(it)
            if a.id == select:
                row = i
        self.list.blockSignals(False)
        self.empty.setVisible(not self.bridge.config.alarms)
        if row < 0 and self.list.count():
            row = 0
        if row >= 0:
            self.list.setCurrentRow(row)
            if reload:
                self._load(self.bridge.config.alarms[row])
        else:
            self.current = None
            self.body.setVisible(False)
            self.placeholder.setVisible(True)

    def _summary(self, a: AlarmConfig) -> str:
        r = self.bridge.reading(a.sensor) or {}
        name = self.bridge.label(a.sensor) if a.sensor else "no sensor"
        if a.condition == "missing":
            return f"{name} unavailable"
        return f"{name} {'>' if a.condition == 'above' else '<'} {format_value(a.threshold, r.get('unit', ''))}"

    def _selected(self, item, _prev=None) -> None:
        if item is None:
            return
        a = next((x for x in self.bridge.config.alarms if x.id == item.data(Qt.UserRole)), None)
        if a and (self.current is None or a.id != self.current.id):
            self._load(a)

    def add(self, sensor: str) -> None:
        r = self.bridge.reading(sensor) or {}
        a = AlarmConfig(id=new_id(), name=f"{r.get('label', 'Sensor')} limit" if sensor else "New alarm",
                        sensor=sensor)
        if r.get("kind") == "flow":
            a.condition, a.threshold, a.name = "below", 40.0, f"Low {r.get('label', 'flow').lower()}"
        elif r.get("value") is not None and r.get("kind") in ("temperature", "delta"):
            a.threshold = round(r["value"] + 10)

        def done(ok):
            if ok:
                self._fill(select=a.id)
        self.bridge.edit(lambda cfg: cfg.alarms.append(a), "Alarm added", done=done)

    def _load(self, a: AlarmConfig) -> None:
        self._loading = True
        self.current = copy.deepcopy(a)
        self.body.setVisible(True)
        self.placeholder.setVisible(False)
        self.editor.title_label.setText(a.name.upper())
        self.enabled.setChecked(a.enabled)
        self.name.setText(a.name)
        self.sensor.set_readings(self.bridge.sensor_list(include_na=True))
        self.sensor.set_current_id(a.sensor)
        self.condition.set_value(a.condition)
        self.threshold.setValue(a.threshold)
        self.delay.setValue(a.delay)
        for k, cb in self.actions.items():
            cb.setChecked(k in a.actions)
        self.command.setText(a.command)
        self._condition_changed(a.condition, touch=False)
        self._loading = False
        self._set_dirty(False)

    def _condition_changed(self, cond, touch: bool = True) -> None:
        self.threshold.setEnabled(cond != "missing")
        if touch:
            self._touch()

    def _touch(self, *_a) -> None:
        if not self._loading and self.current:
            self._set_dirty(True)
        self.command.setEnabled(self.actions["command"].isChecked())

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.btn_apply.setEnabled(dirty)

    def apply(self) -> None:
        if not self.current:
            return
        a = copy.deepcopy(self.current)
        a.enabled = self.enabled.isChecked()
        a.name = self.name.text().strip() or "Alarm"
        a.sensor = self.sensor.current_id()
        a.condition = self.condition.value() or "above"
        a.threshold = self.threshold.value()
        a.delay = self.delay.value()
        a.actions = [k for k, cb in self.actions.items() if cb.isChecked()]
        a.command = self.command.text().strip()
        if not a.sensor:
            QMessageBox.warning(self, "No sensor", "Choose the sensor to watch.")
            return

        def change(cfg):
            cfg.alarms = [a if x.id == a.id else x for x in cfg.alarms]

        def done(ok):
            if ok:
                self.current = a
                self._set_dirty(False)
                self._fill(select=a.id, reload=False)
        self.bridge.edit(change, f"“{a.name}” saved", done=done)

    def _delete(self) -> None:
        if not self.current:
            return
        aid = self.current.id
        if QMessageBox.question(self, "Delete alarm", f"Delete “{self.current.name}”?") != QMessageBox.Yes:
            return
        self.current = None
        self.dirty = False
        self.bridge.edit(lambda cfg: setattr(cfg, "alarms", [x for x in cfg.alarms if x.id != aid]), "Alarm deleted")

    # ------------------------------------------------------------------ log
    def _log_touch(self, *_a) -> None:
        self.btn_log.setEnabled(True)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Data log folder", self.log_dir.text())
        if d:
            self.log_dir.setText(d)
            self._log_touch()

    def _apply_log(self) -> None:
        def change(cfg):
            cfg.settings.log_enabled = self.log_on.isChecked()
            cfg.settings.log_interval = self.log_interval.value()
            cfg.settings.log_dir = self.log_dir.text().strip()
        self.bridge.edit(change, "Data log settings saved")

    def _snapshot(self, snap: dict) -> None:
        if not self.isVisible() or not self.current:
            return
        self.sensor.refresh_values(self.bridge.readings, self.bridge.display)
        st = {a["id"]: a for a in snap.get("alarms", [])}.get(self.current.id)
        r = self.bridge.reading(self.current.sensor) or {}
        if st is None:
            self.state.setText("")
        elif st["active"]:
            self.state.setText(f"ACTIVE for {st['seconds']} s — now {format_value(st['value'], r.get('unit', ''))}")
        else:
            self.state.setText(f"Not triggered — now {format_value(st['value'], r.get('unit', ''))}")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self.dirty:
            self._fill(select=self.current.id if self.current else None)
