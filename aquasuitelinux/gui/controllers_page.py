"""Controllers: curves (drawn on a graph), target value (PID), two-point, fixed, follow and combine."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import (
    CONTROLLER_KINDS,
    DEFAULT_CURVE,
    DELTA_T_CURVE,
    ControllerConfig,
    new_id,
)
from ..core.controllers import interpolate
from .bridge import CONTROL_INPUT_KINDS
from .curve_editor import PRESETS, CurveEditor
from .widgets import (
    Card,
    SegmentedControl,
    SensorCombo,
    flat_button,
    format_value,
    scaled_font,
)

KIND_LABELS = {"curve": "Curve", "target": "Target", "two_point": "Two-point", "fixed": "Fixed", "follow": "Follow",
               "mix": "Combine"}
KIND_HELP = {
    "curve": "Power follows a curve of the input. Curves can run on the QUADRO, OCTO and D5 NEXT themselves.",
    "target": "Holds the input at a target value by adjusting the power (PID). Runs in software.",
    "two_point": "Switches between two powers with hysteresis: on above one value, off below another.",
    "fixed": "A constant power.",
    "follow": "Uses the same power as another output. On the QUADRO and OCTO this runs on the device.",
    "mix": "Combines other controllers, e.g. the higher of a Delta T curve and a CPU curve.",
}


def _spin(lo: float, hi: float, decimals: int = 1, suffix: str = "", step: float = 0.5) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    if suffix:
        s.setSuffix(suffix)
    s.setMinimumWidth(110)
    return s


class ControllersPage(QWidget):
    changed = Signal()

    def __init__(self, bridge):
        super().__init__()
        self.setObjectName("Page")
        self.bridge = bridge
        self.current: ControllerConfig | None = None
        self.dirty = False
        self._loading = False

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        left = Card("Controllers", "curve")
        left.setMinimumWidth(240)
        left.setMaximumWidth(380)
        add = flat_button("plus", "Add a controller")
        add.clicked.connect(self._add_menu)
        left.add_header_widget(add)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._selected)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        left.add(self.list, 1)
        self.empty = QLabel("No controllers yet.\nAdd one with + — a curve on a Delta T is a great start.")
        self.empty.setObjectName("Empty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        left.add(self.empty)
        split.addWidget(left)

        self.editor = Card("Controller", "sliders")
        self.editor_body = QWidget()
        el = QVBoxLayout(self.editor_body)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(10)
        top = QFormLayout()
        top.setLabelAlignment(Qt.AlignLeft)
        top.setHorizontalSpacing(14)
        self.name = QLineEdit()
        self.name.textEdited.connect(self._touch)
        top.addRow("Name", self.name)
        self.kind = SegmentedControl()
        for k, label in KIND_LABELS.items():
            self.kind.add(label, k, CONTROLLER_KINDS[k])
        self.kind.changed.connect(self._kind_changed)
        top.addRow("Type", self.kind)
        self.input = SensorCombo(kinds=CONTROL_INPUT_KINDS, allow_none="Choose a sensor…")
        self.input.currentIndexChanged.connect(self._input_changed)
        self.input_label = QLabel("Input")
        top.addRow(self.input_label, self.input)
        el.addLayout(top)
        self.help = QLabel()
        self.help.setObjectName("Muted")
        self.help.setWordWrap(True)
        el.addWidget(self.help)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_curve())
        self.stack.addWidget(self._build_target())
        self.stack.addWidget(self._build_two_point())
        self.stack.addWidget(self._build_fixed())
        self.stack.addWidget(self._build_follow())
        self.stack.addWidget(self._build_mix())
        el.addWidget(self.stack, 1)

        foot = QHBoxLayout()
        self.live = QLabel("")
        self.live.setObjectName("Value")
        self.used_by = QLabel("")
        self.used_by.setObjectName("Faint")
        self.used_by.setWordWrap(True)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(self.live)
        col.addWidget(self.used_by)
        foot.addLayout(col, 1)
        self.btn_revert = QPushButton("Revert")
        self.btn_revert.clicked.connect(self._revert)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setProperty("variant", "primary")
        self.btn_apply.clicked.connect(self.apply)
        foot.addWidget(self.btn_revert)
        foot.addWidget(self.btn_apply)
        el.addLayout(foot)
        self.editor.add(self.editor_body, 1)
        self.placeholder = QLabel("Select a controller, or add one with +")
        self.placeholder.setObjectName("Empty")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.editor.add(self.placeholder, 1)
        split.addWidget(self.editor)
        split.setStretchFactor(1, 1)
        split.setSizes([280, 1000])

        bridge.config_changed.connect(self._config_changed)
        bridge.snapshot.connect(self._snapshot)
        self._set_dirty(False)
        self._show_editor(False)

    # ------------------------------------------------------------------ builders
    def _build_curve(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.curve = CurveEditor()
        self.curve.changed.connect(self._curve_changed)
        lay.addWidget(self.curve, 1)
        side = QVBoxLayout()
        side.setSpacing(8)
        pl = QLabel("PRESETS")
        pl.setObjectName("CardTitle")
        pl.setFont(scaled_font(pl, 0.78, bold=True))
        side.addWidget(pl)
        self.preset_buttons = []
        for name in ("Silent", "Balanced", "Performance"):
            b = QPushButton(name)
            b.clicked.connect(lambda _c=False, n=name: self._preset(n))
            side.addWidget(b)
            self.preset_buttons.append(b)
        hl = QLabel("HYSTERESIS")
        hl.setObjectName("CardTitle")
        hl.setFont(scaled_font(hl, 0.78, bold=True))
        side.addWidget(hl)
        self.hyst = _spin(0, 20, 1, "", 0.1)
        self.hyst.setToolTip("Power only goes down again once the input has dropped by this much, "
                             "which stops fans pulsing around a curve point.")
        self.hyst.valueChanged.connect(self._touch)
        side.addWidget(self.hyst)
        prow = QHBoxLayout()
        tl = QLabel("POINTS")
        tl.setObjectName("CardTitle")
        tl.setFont(scaled_font(tl, 0.78, bold=True))
        prow.addWidget(tl)
        prow.addStretch(1)
        self.point_count = QSpinBox()
        self.point_count.setRange(CurveEditor.MIN_POINTS, CurveEditor.MAX_POINTS)
        self.point_count.setToolTip(f"How many points the curve has ({CurveEditor.MIN_POINTS}–"
                                    f"{CurveEditor.MAX_POINTS}). New points are placed on the curve, so its shape "
                                    "stays the same until you drag them.")
        self.point_count.valueChanged.connect(self._point_count_changed)
        self.point_count.setAlignment(Qt.AlignCenter)
        self.point_count.setFixedWidth(52)
        self.btn_remove_point = flat_button("minus", "Remove the selected point (or the one that matters least)")
        self.btn_remove_point.clicked.connect(lambda: self.curve.remove_point())
        self.btn_add_point = flat_button("plus", "Add a point after the selected one (or in the widest gap)")
        self.btn_add_point.clicked.connect(lambda: self.curve.add_point())
        prow.addWidget(self.btn_remove_point)
        prow.addWidget(self.point_count)
        prow.addWidget(self.btn_add_point)
        side.addLayout(prow)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Input", "Power %"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMinimumWidth(170)
        self.table.itemChanged.connect(self._table_changed)
        self.table.currentCellChanged.connect(lambda row, _c, _pr, _pc: self.curve.select(row if row >= 0 else None))
        self.curve.selection_changed.connect(self._point_selected)
        side.addWidget(self.table, 1)
        hint = QLabel("Double-click the graph to add a point, right-click one to remove it.")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        hint.setFont(scaled_font(hint, 0.85))
        side.addWidget(hint)
        lay.addLayout(side)
        return w

    def _build_target(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setHorizontalSpacing(14)
        self.target = _spin(-20, 200, 1, "", 0.5)
        self.kp = _spin(0, 1000, 2, " %/K", 0.5)
        self.ki = _spin(0, 100, 3, " %/(K·s)", 0.01)
        self.kd = _spin(0, 1000, 2, " %·s/K", 0.5)
        for s in (self.target, self.kp, self.ki, self.kd):
            s.valueChanged.connect(self._touch)
        f.addRow("Target value", self.target)
        f.addRow("Proportional (P)", self.kp)
        f.addRow("Integral (I)", self.ki)
        f.addRow("Derivative (D)", self.kd)
        note = QLabel("P reacts to how far the input is above the target, I slowly removes the remaining "
                      "difference, D damps fast changes. Water loops react slowly: start with P 8, I 0.08, D 0.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        f.addRow(note)
        return w

    def _build_two_point(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setHorizontalSpacing(14)
        self.on_above = _spin(-50, 500, 1)
        self.off_below = _spin(-50, 500, 1)
        self.on_power = _spin(0, 100, 0, " %", 5)
        self.off_power = _spin(0, 100, 0, " %", 5)
        for s in (self.on_above, self.off_below, self.on_power, self.off_power):
            s.valueChanged.connect(self._touch)
        f.addRow("Switch on above", self.on_above)
        f.addRow("Switch off below", self.off_below)
        f.addRow("Power when on", self.on_power)
        f.addRow("Power when off", self.off_power)
        return w

    def _build_fixed(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setAlignment(Qt.AlignTop)
        self.fixed = QSlider(Qt.Horizontal)
        self.fixed.setRange(0, 100)
        self.fixed_spin = _spin(0, 100, 0, " %", 1)
        self.fixed.valueChanged.connect(lambda v: (self.fixed_spin.setValue(v), self._touch()))
        self.fixed_spin.valueChanged.connect(lambda v: self.fixed.setValue(int(v)))
        lay.addWidget(QLabel("Power"))
        lay.addWidget(self.fixed, 1)
        lay.addWidget(self.fixed_spin)
        return w

    def _build_follow(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.follow = QComboBox()
        self.follow.currentIndexChanged.connect(self._touch)
        f.addRow("Follow output", self.follow)
        return w

    def _build_mix(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Use the"))
        self.mix = SegmentedControl()
        for label, v in (("Highest", "max"), ("Lowest", "min"), ("Average", "average")):
            self.mix.add(label, v)
        self.mix.changed.connect(self._touch)
        row.addWidget(self.mix)
        row.addWidget(QLabel("power of:"))
        row.addStretch(1)
        lay.addLayout(row)
        self.mix_list = QListWidget()
        self.mix_list.itemChanged.connect(self._touch)
        lay.addWidget(self.mix_list, 1)
        return w

    # ------------------------------------------------------------------ list
    def _config_changed(self, cfg) -> None:
        keep = self.current.id if self.current else None
        if self.dirty and keep and cfg.controller(keep):
            self._fill_list(select=keep, reload=False)
            return
        self._fill_list(select=keep)

    def _fill_list(self, select: str | None = None, reload: bool = True) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        target_row = -1
        for i, c in enumerate(self.bridge.config.controllers):
            item = QListWidgetItem(f"{c.name}\n{KIND_LABELS.get(c.kind, c.kind)}")
            item.setData(Qt.UserRole, c.id)
            self.list.addItem(item)
            if c.id == select:
                target_row = i
        self.list.blockSignals(False)
        self.empty.setVisible(not self.bridge.config.controllers)
        if target_row < 0 and self.list.count():
            target_row = 0
        if target_row >= 0:
            self.list.setCurrentRow(target_row)
            if reload:
                self._load(self.bridge.config.controllers[target_row])
        else:
            self.current = None
            self._show_editor(False)

    def _selected(self, item, _prev=None) -> None:
        if item is None:
            return
        cid = item.data(Qt.UserRole)
        if self.current and cid == self.current.id:
            return
        if self.dirty and not self._confirm_discard():
            self.list.blockSignals(True)
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole) == self.current.id:
                    self.list.setCurrentRow(i)
            self.list.blockSignals(False)
            return
        c = self.bridge.config.controller(cid)
        if c:
            self._load(c)

    def select(self, cid: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == cid:
                self.list.setCurrentRow(i)

    def _confirm_discard(self) -> bool:
        return QMessageBox.question(self, "Unsaved changes",
                                    f"Discard the changes to “{self.current.name}”?") == QMessageBox.Yes

    def _add_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Delta T curve (recommended)", lambda: self.add("curve", delta=True))
        menu.addSeparator()
        for k, label in CONTROLLER_KINDS.items():
            menu.addAction(label, lambda kind=k: self.add(kind))
        menu.exec(self.cursor().pos())

    def add(self, kind: str, delta: bool = False, input_id: str = "") -> None:
        c = ControllerConfig(id=new_id(), name=CONTROLLER_KINDS[kind], kind=kind)
        if delta:
            dts = [v for v in self.bridge.config.virtual_sensors if v.kind == "difference"]
            c.name = "Delta T curve"
            c.points = [list(p) for p in DELTA_T_CURVE]
            c.hysteresis = 0.2
            if dts:
                c.input = f"virtual/{dts[0].id}"
        if input_id:
            c.input = input_id
        if kind == "target":
            c.name = "Target temperature"
        if kind == "mix":
            c.name = "Combined"

        def change(cfg):
            cfg.controllers.append(c)
        self.bridge.edit(change, f"Added “{c.name}”", done=lambda ok: ok and self._fill_list(select=c.id))

    def _list_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if not item:
            return
        cid = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Duplicate", lambda: self._duplicate(cid))
        menu.addAction("Delete", lambda: self._delete(cid))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _duplicate(self, cid: str) -> None:
        c = self.bridge.config.controller(cid)
        if not c:
            return
        dup = copy.deepcopy(c)
        dup.id = new_id()
        dup.name = f"{c.name} (copy)"
        self.bridge.edit(lambda cfg: cfg.controllers.append(dup), "Duplicated",
                         done=lambda ok: ok and self._fill_list(select=dup.id))

    def _delete(self, cid: str) -> None:
        c = self.bridge.config.controller(cid)
        if not c:
            return
        users = [self.bridge.outputs.get(oid, {}).get("name", oid) for oid, oc in self.bridge.config.outputs.items()
                 if oc.controller == cid]
        text = f"Delete “{c.name}”?"
        if users:
            text += "\n\nThese outputs will no longer be managed: " + ", ".join(users)
        if QMessageBox.question(self, "Delete controller", text) != QMessageBox.Yes:
            return
        self.dirty = False
        self.current = None
        self.bridge.edit(lambda cfg: cfg.remove_controller(cid), "Controller deleted")

    # ------------------------------------------------------------------ editor
    def _show_editor(self, on: bool) -> None:
        self.editor_body.setVisible(on)
        self.placeholder.setVisible(not on)

    def _load(self, c: ControllerConfig) -> None:
        self._loading = True
        self.current = copy.deepcopy(c)
        self._show_editor(True)
        self.editor.title_label.setText(c.name.upper())
        self.name.setText(c.name)
        self.kind.set_value(c.kind)
        self._refresh_inputs()
        self.input.set_current_id(c.input)
        self.hyst.setValue(c.hysteresis)
        self.target.setValue(c.target)
        self.kp.setValue(c.kp)
        self.ki.setValue(c.ki)
        self.kd.setValue(c.kd)
        self.on_above.setValue(c.on_above)
        self.off_below.setValue(c.off_below)
        self.on_power.setValue(c.on_power)
        self.off_power.setValue(c.off_power)
        self.fixed.setValue(int(c.power))
        self._fill_follow(c.follow)
        self.mix.set_value(c.mix)
        self._fill_mix(c)
        self._apply_kind(c.kind)
        self._load_curve(c.points)
        self._loading = False
        self._set_dirty(False)
        self._update_used_by()

    def _unit(self) -> str:
        r = self.bridge.reading(self.input.current_id())
        return r.get("unit", "") if r else ("K" if self.input.current_id().startswith("virtual/") else "°C")

    def _load_curve(self, points) -> None:
        self.curve.set_curve(points or DEFAULT_CURVE, self._unit())
        self._fill_table(self.curve.points)

    def _fill_table(self, points) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(points))
        for i, (x, y) in enumerate(points):
            for col, v in ((0, f"{x:g}"), (1, f"{y:g}")):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, it)
        if self.curve.selected is not None:
            self.table.setCurrentCell(self.curve.selected, 0)
        self.table.blockSignals(False)
        self.point_count.blockSignals(True)
        self.point_count.setValue(len(points))
        self.point_count.blockSignals(False)

    def _point_count_changed(self, n: int) -> None:
        self.curve.set_point_count(n)            # emits changed → table, dirty state
        if len(self.curve.points) != n:          # no room for more points: show what the curve really has
            self._fill_table(self.curve.points)

    def _point_selected(self, index) -> None:
        self.table.blockSignals(True)
        if index is None:
            self.table.clearSelection()
        else:
            self.table.setCurrentCell(index, 0)
        self.table.blockSignals(False)

    def _table_changed(self, _item) -> None:
        pts = []
        for i in range(self.table.rowCount()):
            try:
                x = float(self.table.item(i, 0).text().replace(",", "."))
                y = max(0.0, min(100.0, float(self.table.item(i, 1).text().replace(",", "."))))
                pts.append([x, y])
            except (ValueError, AttributeError):
                return
        pts.sort()
        self.curve.set_curve(pts, self._unit())
        self._touch()

    def _curve_changed(self, points) -> None:
        self._fill_table(points)
        self._touch()

    def _preset(self, name: str) -> None:
        unit = self._unit()
        table = PRESETS.get("K" if unit == "K" else "°C")
        self.curve.set_curve(table[name], unit)
        self._fill_table(self.curve.points)
        self._touch()

    def _kind_changed(self, kind) -> None:
        self._apply_kind(kind)
        self._touch()

    def _apply_kind(self, kind: str) -> None:
        self.stack.setCurrentIndex(list(KIND_LABELS).index(kind))
        needs_input = kind in ("curve", "target", "two_point")
        self.input.setVisible(needs_input)
        self.input_label.setVisible(needs_input)
        self.help.setText(KIND_HELP.get(kind, ""))

    def _input_changed(self) -> None:
        if self._loading:
            return
        if self.stack.currentIndex() == 0:
            self.curve.set_curve(self.curve.points, self._unit())
        self._touch()

    def _refresh_inputs(self) -> None:
        self.input.set_readings(self.bridge.sensor_list(CONTROL_INPUT_KINDS, include_na=True))

    def _fill_follow(self, current: str) -> None:
        self.follow.blockSignals(True)
        self.follow.clear()
        for o in self.bridge.snap.get("outputs", []):
            self.follow.addItem(f"{o['name']} ({self.bridge.device_name(o['device'])})", o["id"])
        idx = self.follow.findData(current)
        if idx < 0 and current:
            self.follow.addItem(current, current)
            idx = self.follow.count() - 1
        self.follow.setCurrentIndex(max(0, idx))
        self.follow.blockSignals(False)

    def _fill_mix(self, c: ControllerConfig) -> None:
        self.mix_list.blockSignals(True)
        self.mix_list.clear()
        for other in self.bridge.config.controllers:
            if other.id == c.id:
                continue
            it = QListWidgetItem(other.name)
            it.setData(Qt.UserRole, other.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if other.id in c.sources else Qt.Unchecked)
            self.mix_list.addItem(it)
        self.mix_list.blockSignals(False)

    def _touch(self, *_a) -> None:
        if not self._loading and self.current is not None:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.btn_apply.setEnabled(dirty)
        self.btn_revert.setEnabled(dirty)

    def _collect(self) -> ControllerConfig:
        c = copy.deepcopy(self.current)
        c.name = self.name.text().strip() or CONTROLLER_KINDS[self.kind.value()]
        c.kind = self.kind.value()
        c.input = self.input.current_id() if c.kind in ("curve", "target", "two_point") else ""
        c.points = [list(p) for p in self.curve.points]
        c.hysteresis = self.hyst.value()
        c.target, c.kp, c.ki, c.kd = self.target.value(), self.kp.value(), self.ki.value(), self.kd.value()
        c.on_above, c.off_below = self.on_above.value(), self.off_below.value()
        c.on_power, c.off_power = self.on_power.value(), self.off_power.value()
        c.power = float(self.fixed.value())
        c.follow = self.follow.currentData() or ""
        c.mix = self.mix.value() or "max"
        c.sources = [self.mix_list.item(i).data(Qt.UserRole) for i in range(self.mix_list.count())
                     if self.mix_list.item(i).checkState() == Qt.Checked]
        return c

    def apply(self) -> None:
        if self.current is None:
            return
        c = self._collect()
        if c.kind in ("curve", "target", "two_point") and not c.input:
            QMessageBox.warning(self, "No input", "Choose the sensor this controller reads.")
            return
        if c.kind == "two_point" and c.off_below >= c.on_above:
            QMessageBox.warning(self, "Check the values", "“Switch off below” must be lower than “Switch on above”.")
            return

        def change(cfg):
            cfg.controllers = [c if x.id == c.id else x for x in cfg.controllers]

        def done(ok):
            if ok:
                self.current = c
                self._set_dirty(False)
                self.editor.title_label.setText(c.name.upper())
                self._fill_list(select=c.id, reload=False)
        self.bridge.edit(change, f"“{c.name}” saved", done=done)

    def _revert(self) -> None:
        c = self.bridge.config.controller(self.current.id) if self.current else None
        if c:
            self._load(c)

    def _update_used_by(self) -> None:
        if not self.current:
            return
        names = [self.bridge.outputs.get(oid, {}).get("name") or oid for oid, oc in self.bridge.config.outputs.items()
                 if oc.controller == self.current.id]
        for prof in self.bridge.config.profiles:
            for oid, cid in prof.assignments.items():
                if cid == self.current.id:
                    names.append(f"{self.bridge.outputs.get(oid, {}).get('name', oid)} ({prof.name})")
        self.used_by.setText(("Drives: " + ", ".join(names)) if names else
                             "Not assigned to any fan yet — pick it on the Fans page.")

    # ------------------------------------------------------------------ live
    def _snapshot(self, snap: dict) -> None:
        if not self.isVisible() or self.current is None:
            return
        self.input.refresh_values(self.bridge.readings, self.bridge.display)
        live = {c["id"]: c for c in snap.get("controllers", [])}.get(self.current.id)
        r = self.bridge.reading(self.input.current_id())
        x = r.get("value") if r else None
        kind = self.kind.value()
        if kind == "curve":
            y = interpolate(self.curve.points, x) if x is not None else None
            self.curve.set_live(x, y)
        unit = r.get("unit", "") if r else ""
        if kind in ("curve", "target", "two_point"):
            out = live.get("output") if live and not self.dirty else (
                interpolate(self.curve.points, x) if kind == "curve" and x is not None else None)
            self.live.setText(f"Input now {format_value(x, unit)}  →  {format_value(out, '%')}"
                              if x is not None else "Input not available — outputs use their fallback power")
        else:
            out = live.get("output") if live else None
            self.live.setText(f"Output now {format_value(out, '%')}")
        self._update_used_by()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.current is not None and not self.dirty:
            c = self.bridge.config.controller(self.current.id)
            if c:
                self._load(c)
        elif self.current is None:
            self._fill_list()
