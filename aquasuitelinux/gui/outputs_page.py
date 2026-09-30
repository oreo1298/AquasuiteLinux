"""Fans and pumps: assign controllers, set power limits and choose where control runs."""

from __future__ import annotations

import copy
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.config import OutputConfig
from .charts import LineChart
from .theme import theme
from .widgets import (
    Card,
    SegmentedControl,
    badge,
    format_value,
    scaled_font,
    set_placement_badge,
)

PLACEMENT_HELP = {
    "device": "The device runs this controller itself: its curve and limits are stored on it once, and inputs "
              "from elsewhere (a Delta T, the CPU temperature) are streamed into a software sensor every "
              "second. Nothing is written to the device's memory while it runs, and it falls back to its "
              "fallback power if the data stops.",
    "software": "AquasuiteLinux computes the power every second and sets it on the device, writing only when it "
                "changes by at least 1 % and at most every couple of seconds.",
    "unmanaged": "Not managed: the device keeps whatever it is set to (for example the settings aquasuite "
                 "stored on it).",
}


class PowerRow(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(0, 100)
        self.spin.setDecimals(0)
        self.spin.setSuffix(" %")
        self.spin.setMinimumWidth(84)
        self.slider.valueChanged.connect(lambda v: self._sync(v))
        self.spin.valueChanged.connect(lambda v: self.slider.setValue(int(v)))
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)

    def _sync(self, v: int) -> None:
        if int(self.spin.value()) != v:
            self.spin.setValue(v)
        self.changed.emit()

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, v: float) -> None:
        self.slider.blockSignals(True)
        self.spin.blockSignals(True)
        self.slider.setValue(int(round(v)))
        self.spin.setValue(round(v))
        self.slider.blockSignals(False)
        self.spin.blockSignals(False)


class OutputsPage(QWidget):
    edit_controller = Signal(str)
    new_delta_t_curve = Signal(str)          # output id to assign it to

    def __init__(self, bridge):
        super().__init__()
        self.setObjectName("Page")
        self.bridge = bridge
        self.current: str | None = None
        self.dirty = False
        self._loading = False
        self._last_chart = 0.0
        self._ids: list[str] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        left = Card("Fans and pumps", "fan")
        left.setMinimumWidth(240)
        left.setMaximumWidth(380)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._selected)
        left.add(self.list, 1)
        self.empty = QLabel("No controllable outputs found.")
        self.empty.setObjectName("Empty")
        self.empty.setAlignment(Qt.AlignCenter)
        left.add(self.empty)
        split.addWidget(left)

        self.editor = Card("Output", "sliders")
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.name = QLineEdit()
        self.name.textEdited.connect(self._touch)
        form.addRow("Name", self.name)
        crow = QHBoxLayout()
        self.controller = QComboBox()
        self.controller.currentIndexChanged.connect(self._touch)
        self.btn_edit_ctrl = QPushButton("Edit…")
        self.btn_edit_ctrl.clicked.connect(self._edit_controller)
        self.btn_new_curve = QPushButton("New Delta T curve")
        self.btn_new_curve.clicked.connect(lambda: self.current and self.new_delta_t_curve.emit(self.current))
        crow.addWidget(self.controller, 1)
        crow.addWidget(self.btn_edit_ctrl)
        crow.addWidget(self.btn_new_curve)
        form.addRow("Controller", crow)
        self.placement = SegmentedControl()
        self.placement.add("Automatic", "auto", "On the device when it can run the controller, otherwise in software")
        self.placement.add("On the device", "device", "Always on the device (falls back to software if impossible)")
        self.placement.add("In software", "software", "Always computed by AquasuiteLinux")
        self.placement.changed.connect(self._touch)
        form.addRow("Control runs", self.placement)
        prow = QHBoxLayout()
        self.badge = badge()
        self.place_text = QLabel()
        self.place_text.setObjectName("Muted")
        self.place_text.setWordWrap(True)
        prow.addWidget(self.badge, 0, Qt.AlignTop)
        prow.addWidget(self.place_text, 1)
        form.addRow("", prow)
        self.profile_note = QLabel()
        self.profile_note.setObjectName("Banner")
        self.profile_note.setWordWrap(True)
        form.addRow("", self.profile_note)
        bl.addLayout(form)

        limits = Card("Power limits", "gauge")
        lf = QFormLayout()
        lf.setHorizontalSpacing(14)
        self.min_power = PowerRow()
        self.max_power = PowerRow()
        self.fallback = PowerRow()
        for w in (self.min_power, self.max_power, self.fallback):
            w.changed.connect(self._touch)
        lf.addRow("Minimum power", self.min_power)
        lf.addRow("Maximum power", self.max_power)
        lf.addRow("Fallback power", self.fallback)
        checks = QHBoxLayout()
        checks.setSpacing(22)
        self.hold_min = QCheckBox("Hold minimum power")
        self.hold_min.setToolTip("When the controller asks for 0 %, keep the fan at its minimum power instead of "
                                 "stopping it.")
        self.start_boost = QCheckBox("Start boost")
        self.start_boost.setToolTip("Run a stopped fan at full power for a moment so it starts reliably.")
        for c in (self.hold_min, self.start_boost):
            c.toggled.connect(self._touch)
            checks.addWidget(c)
        checks.addStretch(1)
        lf.addRow("", checks)
        ramps = QHBoxLayout()
        self.ramp_up = QDoubleSpinBox()
        self.ramp_down = QDoubleSpinBox()
        for s in (self.ramp_up, self.ramp_down):
            s.setRange(0, 100)
            s.setDecimals(1)
            s.setSuffix(" %/s")
            s.setSpecialValueText("instant")
            s.setMinimumWidth(100)
            s.valueChanged.connect(self._touch)
        ramps.addWidget(QLabel("up"))
        ramps.addWidget(self.ramp_up)
        ramps.addSpacing(10)
        ramps.addWidget(QLabel("down"))
        ramps.addWidget(self.ramp_down)
        ramps.addStretch(1)
        self.ramp_label = QLabel("Speed changes")
        lf.addRow(self.ramp_label, ramps)
        note = QLabel("The controller's 0-100 % is spread between the minimum and maximum power. The fallback power "
                      "is used when the input sensor is unavailable. Speed changes only apply in software control.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        lf.addRow(note)
        limits.add_layout(lf)
        bl.addWidget(limits)

        test = QHBoxLayout()
        self.override = PowerRow()
        self.override.set_value(100)
        self.btn_hold = QPushButton("Set for 30 s")
        self.btn_hold.setToolTip("Temporarily run this output at the chosen power")
        self.btn_hold.clicked.connect(lambda: self._override(self.override.value(), 30))
        self.btn_release = QPushButton("Release")
        self.btn_release.clicked.connect(lambda: self._override(None, 0))
        test.addWidget(QLabel("Test"))
        test.addWidget(self.override, 1)
        test.addWidget(self.btn_hold)
        test.addWidget(self.btn_release)
        bl.addLayout(test)

        self.chart = LineChart()
        self.chart.setMinimumHeight(150)
        bl.addWidget(self.chart, 1)

        foot = QHBoxLayout()
        self.live = QLabel()
        self.live.setObjectName("Value")
        foot.addWidget(self.live, 1)
        self.btn_revert = QPushButton("Revert")
        self.btn_revert.clicked.connect(lambda: self.current and self._load(self.current))
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setProperty("variant", "primary")
        self.btn_apply.clicked.connect(self.apply)
        foot.addWidget(self.btn_revert)
        foot.addWidget(self.btn_apply)
        bl.addLayout(foot)
        self.body = body
        self.editor.add(body, 1)
        self.placeholder = QLabel("Select a fan or pump")
        self.placeholder.setObjectName("Empty")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.editor.add(self.placeholder, 1)
        split.addWidget(self.editor)
        split.setStretchFactor(1, 1)
        split.setSizes([280, 1000])

        bridge.snapshot.connect(self._snapshot)
        bridge.config_changed.connect(self._config_changed)
        self._set_dirty(False)
        self.body.setVisible(False)

    # ------------------------------------------------------------------ list
    def _fill_list(self) -> None:
        outs = self.bridge.snap.get("outputs", [])
        ids = [o["id"] for o in outs]
        if ids == self._ids:
            for i in range(self.list.count()):
                it = self.list.item(i)
                oid = it.data(Qt.UserRole)
                o = self.bridge.outputs.get(oid) if oid else None
                if o:
                    it.setText(self._item_text(o))
            return
        self._ids = ids
        self.list.blockSignals(True)
        self.list.clear()
        last_dev = None
        select_row = -1
        for o in outs:
            if o["device"] != last_dev:
                hdr = QListWidgetItem(self.bridge.device_name(o["device"]).upper())
                hdr.setFlags(Qt.NoItemFlags)
                hdr.setFont(scaled_font(self.list, 0.8, bold=True))
                self.list.addItem(hdr)
                last_dev = o["device"]
            it = QListWidgetItem(self._item_text(o))
            it.setData(Qt.UserRole, o["id"])
            it.setIcon(theme.icon("pump" if o.get("pump") else "fan", "text_muted"))
            self.list.addItem(it)
            if o["id"] == self.current:
                select_row = self.list.count() - 1
        self.list.blockSignals(False)
        self.empty.setVisible(not outs)
        if select_row >= 0:
            self.list.setCurrentRow(select_row)
        elif outs and self.current is None:
            self.select(outs[0]["id"])

    def _item_text(self, o: dict) -> str:
        rpm = format_value(o.get("rpm"), "rpm")
        power = o.get("reported") if o.get("reported") is not None else o.get("target")
        return f"{o['name']}\n{rpm}  ·  {format_value(power, '%')}"

    def select(self, oid: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == oid:
                self.list.setCurrentRow(i)
                return
        self.current = oid
        self._load(oid)

    def _selected(self, item, _prev=None) -> None:
        oid = item.data(Qt.UserRole) if item else None
        if not oid or oid == self.current and not self.dirty:
            if oid:
                self._load(oid)
            return
        self.current = oid
        self._load(oid)

    # ------------------------------------------------------------------ editor
    def _config_changed(self, _cfg) -> None:
        if self.current and not self.dirty:
            self._load(self.current)

    def _fill_controllers(self, current: str) -> None:
        self.controller.blockSignals(True)
        self.controller.clear()
        self.controller.addItem("Not managed — the device keeps its own settings", "")
        for c in self.bridge.config.controllers:
            self.controller.addItem(c.name, c.id)
        idx = self.controller.findData(current)
        self.controller.setCurrentIndex(max(0, idx))
        self.controller.blockSignals(False)

    def _load(self, oid: str) -> None:
        o = self.bridge.outputs.get(oid)
        if o is None:
            return
        self._loading = True
        oc = self.bridge.config.outputs.get(oid) or OutputConfig()
        self.body.setVisible(True)
        self.placeholder.setVisible(False)
        self.editor.title_label.setText(f"{o['name']} · {self.bridge.device_name(o['device'])}".upper())
        self.name.setText(oc.name or o["label"])
        self._fill_controllers(oc.controller)
        self.placement.set_value(oc.placement)
        self.min_power.set_value(oc.min_power)
        self.max_power.set_value(oc.max_power)
        self.fallback.set_value(oc.fallback)
        self.hold_min.setChecked(oc.hold_min)
        self.start_boost.setChecked(oc.start_boost)
        self.ramp_up.setValue(oc.ramp_up)
        self.ramp_down.setValue(oc.ramp_down)
        self._loading = False
        self._set_dirty(False)
        self._update_status(o)
        self._last_chart = 0.0

    def _touch(self, *_a) -> None:
        if not self._loading and self.current:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.btn_apply.setEnabled(dirty)
        self.btn_revert.setEnabled(dirty)

    def apply(self) -> None:
        if not self.current:
            return
        oid = self.current
        lo, hi = self.min_power.value(), self.max_power.value()
        if hi < lo:
            lo, hi = hi, lo
        oc = copy.deepcopy(self.bridge.config.outputs.get(oid) or OutputConfig())
        o = self.bridge.outputs.get(oid, {})
        name = self.name.text().strip()
        oc.name = "" if name == o.get("label") else name
        oc.controller = self.controller.currentData() or ""
        oc.placement = self.placement.value() or "auto"
        oc.min_power, oc.max_power, oc.fallback = lo, hi, self.fallback.value()
        oc.hold_min, oc.start_boost = self.hold_min.isChecked(), self.start_boost.isChecked()
        oc.ramp_up, oc.ramp_down = self.ramp_up.value(), self.ramp_down.value()

        def change(cfg):
            cfg.outputs[oid] = oc

        def done(ok):
            if ok:
                self._set_dirty(False)
        self.bridge.edit(change, f"{name or o.get('label', 'Output')} saved", done=done)

    def _edit_controller(self) -> None:
        cid = self.controller.currentData()
        if cid:
            self.edit_controller.emit(cid)

    def _override(self, power, seconds) -> None:
        if self.current:
            self.bridge.call("override", self.current, power, seconds,
                             success="Test running for 30 s" if power is not None else "Back to normal control")

    # ------------------------------------------------------------------ live
    def _update_status(self, o: dict) -> None:
        set_placement_badge(self.badge, o["placement"], o.get("override"))
        placement, reason = o["placement"], o.get("reason") or ""
        special = {"manual override": "A test power is set; normal control resumes when it ends.",
                   "alarm: full power": "An alarm is running every controlled fan at full power."}
        if placement == "software" and reason in special:
            text = special[reason]
        elif placement == "software" and reason:
            text = f"Runs in software because {reason}. " + PLACEMENT_HELP["software"]
        else:
            text = PLACEMENT_HELP.get(placement, "")
            if placement == "device" and o.get("slot"):
                text += f" Input: software sensor {o['slot']}."
        self.place_text.setText(text)
        prof_name = self.bridge.snap.get("profile")
        cfg = self.bridge.config
        prof = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        if prof and self.current in prof.assignments:
            ctrl = cfg.controller(prof.assignments[self.current])
            self.profile_note.setText(f"The active profile “{prof_name}” uses “{ctrl.name if ctrl else '?'}” for "
                                      "this output. The controller chosen here applies to the Default profile.")
            self.profile_note.show()
        else:
            self.profile_note.hide()
        soft = o["placement"] == "software"
        self.ramp_up.setEnabled(soft)
        self.ramp_down.setEnabled(soft)
        self.ramp_label.setEnabled(soft)
        self.btn_edit_ctrl.setEnabled(bool(self.controller.currentData()))
        rpm = format_value(o.get("rpm"), "rpm")
        power = o.get("reported") if o.get("reported") is not None else o.get("target")
        target = f"  (target {format_value(o.get('target'), '%')})" if o.get("target") is not None else ""
        self.live.setText(f"Now {rpm}  ·  {format_value(power, '%')}{target}")

    def _snapshot(self, snap: dict) -> None:
        if not self.isVisible():
            return
        self._fill_list()
        o = self.bridge.outputs.get(self.current or "")
        if o:
            self._update_status(o)
            self._refresh_chart(o)

    def _refresh_chart(self, o: dict) -> None:
        now = time.monotonic()
        if now - self._last_chart < 2.0:
            return
        self._last_chart = now
        oid = o["id"]
        ids = [f"{oid}.rpm", f"{oid}.percent", f"output/{oid}"]

        def done(h):
            pal = theme.palette
            s = h.get("series", {})
            series = [{"id": ids[0], "label": "Speed", "unit": "rpm", "values": s.get(ids[0], []), "color": pal.c1},
                      {"id": ids[1], "label": "Power", "unit": "%", "values": s.get(ids[1], []), "color": pal.c2}]
            if any(v is not None for v in s.get(ids[2], [])):
                series.append({"id": ids[2], "label": "Target", "unit": "%", "values": s.get(ids[2], []),
                               "color": pal.c3, "dashed": True})
            self.chart.set_data(h.get("times", []), series, 300)
        self.bridge.history(ids, 300, done)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ids = []
        self._fill_list()
        if self.current and not self.dirty:
            self._load(self.current)
