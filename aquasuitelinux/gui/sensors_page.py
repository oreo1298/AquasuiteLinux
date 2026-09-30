"""Sensors: every reading (devices, virtual sensors, the PC), virtual sensors and software sensor feeds."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import VIRTUAL_KINDS
from .widgets import Card, flat_button, format_value, scaled_font

SOURCE_ORDER = {"virtual": 1, "system": 2}


class SensorsPage(QWidget):
    edit_virtual = Signal(str)             # virtual sensor id ("" = new)
    new_virtual = Signal(str, list)        # kind, preset inputs
    add_alarm = Signal(str)                # sensor id
    pins_changed = Signal()

    def __init__(self, bridge, gui_settings):
        super().__init__()
        self.setObjectName("Page")
        self.bridge = bridge
        self.gs = gui_settings
        self.items: dict[str, QTreeWidgetItem] = {}
        self.minmax: dict[str, list[float]] = {}
        self._structure: tuple = ()

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        card = Card("Sensors", "sensor")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter sensors")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(240)
        self.search.textChanged.connect(self._filter)
        card.add_header_widget(self.search)
        self.show_na = QCheckBox("Show unavailable")
        self.show_na.setChecked(self.gs.show_unavailable)
        self.show_na.toggled.connect(self._toggle_na)
        card.add_header_widget(self.show_na)
        reset = flat_button("refresh", "Reset the minimum and maximum columns")
        reset.clicked.connect(self._reset_minmax)
        card.add_header_widget(reset)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Sensor", "Value", "Min", "Max"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
            self.tree.headerItem().setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
        card.add(self.tree, 1)
        hint = QLabel("Right-click a sensor to rename it, pin it to the overview, build a Delta T from it or "
                      "set an alarm.")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        card.add(hint)
        split.addWidget(card)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(12)
        vcard = Card("Virtual sensors", "delta")
        add = flat_button("plus", "Add a virtual sensor")
        add.clicked.connect(self._add_menu)
        vcard.add_header_widget(add)
        self.vlist = QListWidget()
        self.vlist.itemDoubleClicked.connect(lambda it: self.edit_virtual.emit(it.data(Qt.UserRole)))
        vcard.add(self.vlist, 1)
        row = QHBoxLayout()
        self.btn_dt = QPushButton("New Delta T…")
        self.btn_dt.setProperty("variant", "primary")
        self.btn_dt.clicked.connect(lambda: self.new_virtual.emit("difference", []))
        self.btn_vedit = QPushButton("Edit…")
        self.btn_vedit.clicked.connect(self._edit_selected)
        self.btn_vdel = QPushButton("Delete")
        self.btn_vdel.setProperty("variant", "danger")
        self.btn_vdel.clicked.connect(self._delete_selected)
        row.addWidget(self.btn_dt)
        row.addStretch(1)
        row.addWidget(self.btn_vedit)
        row.addWidget(self.btn_vdel)
        vcard.add_layout(row)
        expl = QLabel("A Delta T is the coolant temperature minus the air temperature: how far above ambient "
                      "your loop runs. It makes fan curves independent of room temperature.")
        expl.setObjectName("Faint")
        expl.setWordWrap(True)
        vcard.add(expl)
        sl.addWidget(vcard, 3)

        fcard = Card("Software sensor feeds", "link")
        fadd = flat_button("plus", "Send a sensor to a device's software sensor")
        fadd.clicked.connect(self.add_feed)
        fcard.add_header_widget(fadd)
        self.flist = QListWidget()
        self.flist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.flist.customContextMenuRequested.connect(self._feed_menu)
        fcard.add(self.flist, 1)
        fexpl = QLabel("Values sent every second into a device's software sensors, like aquasuite does. "
                       "Curves stored on the device can then use them. Feeds marked auto are created for "
                       "curves that run on the device.")
        fexpl.setObjectName("Faint")
        fexpl.setWordWrap(True)
        fcard.add(fexpl)
        sl.addWidget(fcard, 2)
        side.setMinimumWidth(300)
        side.setMaximumWidth(440)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 360])

        bridge.snapshot.connect(self.update_snapshot)
        bridge.config_changed.connect(lambda _c: self._fill_virtual())

    # ------------------------------------------------------------------ tree
    def _groups(self) -> list[tuple[str, str, list[dict]]]:
        by_src: dict[str, list[dict]] = {}
        for r in self.bridge.readings.values():
            by_src.setdefault(r.get("source", ""), []).append(r)
        device_order = {d["key"]: i for i, d in enumerate(self.bridge.snap.get("devices", []))}
        order = sorted(by_src, key=lambda s: (SOURCE_ORDER.get(s, 0), device_order.get(s, 99), s))
        out = []
        for src in order:
            title = {"virtual": "Virtual sensors", "system": "This PC"}.get(src) or self.bridge.device_name(src)
            out.append((src, title, by_src[src]))
        return out

    def update_snapshot(self, _snap: dict) -> None:
        for sid, r in self.bridge.readings.items():
            v = r.get("value")
            if v is None:
                continue
            mm = self.minmax.setdefault(sid, [v, v])
            mm[0], mm[1] = min(mm[0], v), max(mm[1], v)
        if not self.isVisible():
            return
        show_na = self.show_na.isChecked()
        groups = self._groups()
        structure = tuple((src, tuple(r["id"] for r in rs if show_na or r.get("value") is not None))
                          for src, _t, rs in groups)
        if structure != self._structure:
            self._rebuild(groups, show_na)
            self._structure = structure
        for sid, item in self.items.items():
            r = self.bridge.readings.get(sid)
            if not r:
                continue
            unit = r.get("unit", "")
            item.setText(0, r["label"])
            item.setText(1, format_value(r.get("value"), unit))
            mm = self.minmax.get(sid)
            item.setText(2, format_value(mm[0], unit) if mm else "—")
            item.setText(3, format_value(mm[1], unit) if mm else "—")

    def _rebuild(self, groups, show_na: bool) -> None:
        expanded = {self.tree.topLevelItem(i).data(0, Qt.UserRole + 1): self.tree.topLevelItem(i).isExpanded()
                    for i in range(self.tree.topLevelItemCount())}
        self.tree.clear()
        self.items.clear()
        bold = scaled_font(self.tree, 1.0, bold=True)
        for src, title, readings in groups:
            top = QTreeWidgetItem([title])
            top.setFont(0, bold)
            top.setData(0, Qt.UserRole + 1, src)
            top.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(top)
            sub: dict[str, QTreeWidgetItem] = {}
            for r in readings:
                if not show_na and r.get("value") is None:
                    continue
                parent = top
                group = r.get("group") or ""
                if src not in ("virtual", "system") and group:
                    parent = sub.get(group)
                    if parent is None:
                        parent = QTreeWidgetItem([group])
                        parent.setForeground(0, self.tree.palette().placeholderText())
                        top.addChild(parent)
                        parent.setExpanded(group != "aquabus")
                        sub[group] = parent
                item = QTreeWidgetItem([r["label"], "", "", ""])
                item.setData(0, Qt.UserRole, r["id"])
                item.setToolTip(0, r["id"])
                for c in (1, 2, 3):
                    item.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
                parent.addChild(item)
                self.items[r["id"]] = item
            top.setExpanded(expanded.get(src, True))
        self._filter(self.search.text())

    def _filter(self, text: str) -> None:
        text = text.lower().strip()
        for sid, item in self.items.items():
            match = not text or text in item.text(0).lower() or text in sid.lower()
            item.setHidden(not match)
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                ch = top.child(j)
                if ch.childCount():
                    ch.setHidden(all(ch.child(k).isHidden() for k in range(ch.childCount())))

    def _toggle_na(self, on: bool) -> None:
        self.gs.show_unavailable = on
        self.gs.save()
        self._structure = ()
        self.update_snapshot(self.bridge.snap)

    def _reset_minmax(self) -> None:
        self.minmax.clear()
        self.update_snapshot(self.bridge.snap)

    def select_sensor(self, sid: str) -> None:
        item = self.items.get(sid)
        if item:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def _double_clicked(self, item: QTreeWidgetItem) -> None:
        sid = item.data(0, Qt.UserRole)
        if sid and sid.startswith("virtual/"):
            self.edit_virtual.emit(sid[8:])
        elif sid:
            self._rename(sid)

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        sid = item.data(0, Qt.UserRole) if item else None
        if not sid:
            return
        r = self.bridge.readings.get(sid, {})
        menu = QMenu(self)
        pinned = sid in (self.gs.pinned or [])
        menu.addAction("Unpin from overview" if pinned else "Pin to overview", lambda: self._toggle_pin(sid))
        charted = sid in (self.gs.chart or [])
        menu.addAction("Remove from overview chart" if charted else "Add to overview chart",
                       lambda: self._toggle_chart(sid))
        menu.addSeparator()
        if sid.startswith("virtual/"):
            menu.addAction("Edit virtual sensor…", lambda: self.edit_virtual.emit(sid[8:]))
        elif r.get("source") not in ("system",):
            menu.addAction("Rename…", lambda: self._rename(sid))
        if r.get("kind") in ("temperature",):
            menu.addAction("Create Delta T with this sensor…", lambda: self.new_virtual.emit("difference", [sid]))
        menu.addAction("Set an alarm…", lambda: self.add_alarm.emit(sid))
        menu.addSeparator()
        menu.addAction("Copy sensor ID", lambda: QGuiApplication.clipboard().setText(sid))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _toggle_pin(self, sid: str) -> None:
        from .overview import default_pins
        pins = list(self.gs.pinned or default_pins(self.bridge))
        if sid in pins:
            pins.remove(sid)
        else:
            pins.append(sid)
        self.gs.pinned = pins
        self.gs.save()
        self.pins_changed.emit()

    def _toggle_chart(self, sid: str) -> None:
        chart = list(self.gs.chart or [])
        if sid in chart:
            chart.remove(sid)
        else:
            chart.append(sid)
        self.gs.chart = chart
        self.gs.save()
        self.pins_changed.emit()

    def _rename(self, sid: str) -> None:
        dev, _, key = sid.partition("/")
        r = self.bridge.readings.get(sid, {})
        name, ok = QInputDialog.getText(self, "Rename sensor", f"Name for {r.get('label', key)}:",
                                        text=r.get("label", ""))
        if not ok:
            return

        def change(cfg):
            names = cfg.device(dev).sensor_names
            if name.strip():
                names[key] = name.strip()
            else:
                names.pop(key, None)
        self.bridge.edit(change, "Sensor renamed")

    # ------------------------------------------------------------------ virtual sensors
    def _fill_virtual(self) -> None:
        self.vlist.clear()
        for v in self.bridge.config.virtual_sensors:
            r = self.bridge.readings.get(f"virtual/{v.id}", {})
            item = QListWidgetItem(f"{v.name}\n{VIRTUAL_KINDS.get(v.kind, v.kind)}")
            item.setData(Qt.UserRole, v.id)
            item.setToolTip(", ".join(self.bridge.label(i) for i in v.inputs))
            if r:
                item.setText(f"{v.name}  ·  {format_value(r.get('value'), r.get('unit', ''))}\n"
                             f"{VIRTUAL_KINDS.get(v.kind, v.kind)}")
            self.vlist.addItem(item)
        has = bool(self.bridge.config.virtual_sensors)
        self.btn_vedit.setEnabled(has)
        self.btn_vdel.setEnabled(has)
        self._fill_feeds()

    def _add_menu(self) -> None:
        menu = QMenu(self)
        for kind, label in VIRTUAL_KINDS.items():
            menu.addAction(label, lambda k=kind: self.new_virtual.emit(k, []))
        menu.exec(self.cursor().pos())

    def _selected_virtual(self) -> str | None:
        it = self.vlist.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _edit_selected(self) -> None:
        vid = self._selected_virtual()
        if vid:
            self.edit_virtual.emit(vid)

    def _delete_selected(self) -> None:
        vid = self._selected_virtual()
        if not vid:
            return
        users = [c.name for c in self.bridge.config.controllers if c.input == f"virtual/{vid}"]
        from PySide6.QtWidgets import QMessageBox
        text = "Delete this virtual sensor?"
        if users:
            text += "\n\nThese controllers use it and will lose their input: " + ", ".join(users)
        if QMessageBox.question(self, "Delete virtual sensor", text) != QMessageBox.Yes:
            return
        self.bridge.edit(lambda cfg: cfg.remove_virtual(vid), "Virtual sensor deleted")

    # ------------------------------------------------------------------ feeds
    def _fill_feeds(self) -> None:
        self.flist.clear()
        snap_feeds = self.bridge.snap.get("feeds", [])
        shown = set()
        for f in snap_feeds:
            shown.add((f["device"], f["slot"]))
            text = (f"{self.bridge.device_name(f['device'])} · software sensor {f['slot']}\n"
                    f"← {self.bridge.label(f['source'])}  ·  {format_value(f.get('value'), '')}"
                    + ("  (auto)" if f.get("auto") else ""))
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, (f["device"], f["slot"], f.get("auto", False)))
            self.flist.addItem(item)
        for f in self.bridge.config.feeds:
            if (f.device, f.slot) in shown:
                continue
            item = QListWidgetItem(f"{self.bridge.device_name(f.device)} · software sensor {f.slot}\n"
                                   f"← {self.bridge.label(f.source)}  (device not connected)")
            item.setData(Qt.UserRole, (f.device, f.slot, False))
            self.flist.addItem(item)
        for ls in self.bridge.config.leakshield:
            item = QListWidgetItem(f"{self.bridge.device_name(ls.device)} · pump speed and flow\n"
                                   f"← {self.bridge.label(ls.pump)}, {self.bridge.label(ls.flow)}")
            item.setData(Qt.UserRole, (ls.device, -1, False))
            self.flist.addItem(item)
        if not self.flist.count():
            item = QListWidgetItem("No feeds")
            item.setFlags(Qt.NoItemFlags)
            self.flist.addItem(item)

    def _feed_menu(self, pos) -> None:
        item = self.flist.itemAt(pos)
        data = item.data(Qt.UserRole) if item else None
        if not data:
            return
        dev, slot, auto = data
        menu = QMenu(self)
        if auto:
            act = menu.addAction("Created automatically for a curve on the device")
            act.setEnabled(False)
        elif slot == -1:
            menu.addAction("Remove Leakshield feed", lambda: self.bridge.edit(
                lambda cfg: setattr(cfg, "leakshield", [x for x in cfg.leakshield if x.device != dev]),
                "Feed removed"))
        else:
            menu.addAction("Remove feed", lambda: self.bridge.edit(
                lambda cfg: setattr(cfg, "feeds", [f for f in cfg.feeds if not (f.device == dev and f.slot == slot)]),
                "Feed removed"))
        menu.exec(self.flist.viewport().mapToGlobal(pos))

    def add_feed(self) -> None:
        from .dialogs import FeedDialog
        dlg = FeedDialog(self.bridge, self)
        if dlg.exec() and dlg.result_change:
            self.bridge.edit(dlg.result_change, "Feed added")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fill_virtual()
        self._structure = ()
        if self.bridge.snap:
            self.update_snapshot(self.bridge.snap)

