"""Small reusable widgets: the suite's cards, key/value grids, tiles, toasts and theme-aware
buttons, plus value tiles, sparklines, placement badges and a sensor picker."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .theme import theme


def scaled_font(widget: QWidget, factor: float, bold: bool = False):
    font = widget.font()
    font.setPointSizeF(max(6.0, font.pointSizeF() * factor))
    if bold:
        font.setBold(True)
    return font


class Card(QFrame):
    """A rounded surface with an optional uppercase title, icon and trailing widgets."""

    def __init__(self, title: str | None = None, icon: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 14)
        self._layout.setSpacing(9)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self._icon_name = icon
        self.icon_label = QLabel()
        self.title_label = QLabel((title or "").upper())
        self.title_label.setObjectName("CardTitle")
        self.title_label.setFont(scaled_font(self.title_label, 0.82, bold=True))
        if title:
            if icon:
                self.header.addWidget(self.icon_label)
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            self._layout.addLayout(self.header)
        theme.changed.connect(self._retheme)
        self._retheme(theme.palette)

    def _retheme(self, palette) -> None:
        if self._icon_name:
            self.icon_label.setPixmap(icons.pixmap(self._icon_name, palette.text_muted, 16))

    def add_header_widget(self, widget: QWidget) -> QWidget:
        self.header.addWidget(widget)
        return widget

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)


class KeyValueGrid(QWidget):
    """Two-column label/value list."""

    def __init__(self, columns: int = 1, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(6)
        self.columns = columns
        self._count = 0

    def clear(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._count = 0

    def add_row(self, label: str, value: str = "—") -> QLabel:
        row, col = divmod(self._count, self.columns)
        k = QLabel(label)
        k.setObjectName("Muted")
        k.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        v = QLabel(value)
        v.setObjectName("Value")
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.setWordWrap(True)
        self.grid.addWidget(k, row, col * 2)
        self.grid.addWidget(v, row, col * 2 + 1)
        self.grid.setColumnStretch(col * 2 + 1, 1)
        self._count += 1
        return v


class StatTile(QFrame):
    """A compact value-over-caption tile."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Inset")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(1)
        self.value = QLabel("—")
        self.value.setObjectName("Value")
        self.value.setFont(scaled_font(self.value, 1.12, bold=True))
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.caption = QLabel(caption)
        self.caption.setObjectName("Muted")
        self.caption.setFont(scaled_font(self.caption, 0.85))
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set(self, value: str, tooltip: str = "") -> None:
        self.value.setText(value)
        self.setToolTip(tooltip)


class StatusDot(QWidget):
    """A small filled circle with an optional soft halo."""

    def __init__(self, size: int = 10, parent=None):
        super().__init__(parent)
        self._color = QColor("#888")
        self._halo = True
        self.setFixedSize(QSize(size + 8, size + 8))

    def set_color(self, color: str, halo: bool = True) -> None:
        self._color = QColor(color)
        self._halo = halo
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        if self._halo:
            halo = QColor(self._color)
            halo.setAlpha(55)
            p.setBrush(halo)
            p.setPen(Qt.NoPen)
            p.drawEllipse(r)
        inner = r.adjusted(4, 4, -4, -4)
        p.setBrush(self._color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(inner)


class Toast(QWidget):
    """A transient notification floating near the bottom right of its parent."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._icon = QLabel()
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setMaximumWidth(460)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(10)
        lay.addWidget(self._icon, 0, Qt.AlignTop)
        lay.addWidget(self._text, 1)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._fade_out)
        self._accent = QColor("#5b8cff")
        self._fading = False
        self._anim.finished.connect(self._on_anim_finished)
        self.hide()

    def _on_anim_finished(self) -> None:
        if self._fading:
            self._fading = False
            self.hide()

    def show_message(self, text: str, kind: str = "success", timeout_ms: int = 3800) -> None:
        pal = theme.palette
        color = {"success": pal.success, "warning": pal.warning, "error": pal.danger,
                 "info": pal.accent}.get(kind, pal.accent)
        name = {"success": "check", "warning": "warning", "error": "error"}.get(kind, "info")
        self._accent = QColor(color)
        self._icon.setPixmap(icons.pixmap(name, color, 18))
        self._text.setText(text)
        self.adjustSize()
        self._place()
        self.raise_()
        self.show()
        self._anim.stop()
        self._fading = False
        self._anim.setDuration(180)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(timeout_ms)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._fading = True
        self._anim.setDuration(400)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        size = self.sizeHint()
        x = parent.width() - size.width() - 22
        y = parent.height() - size.height() - 24
        self.setGeometry(QRect(x, y, size.width(), size.height()))

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self._timer.stop()
        self._fade_out()

    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QColor(pal.raised))
        p.setPen(QColor(pal.border_strong))
        p.drawPath(path)
        bar = QPainterPath()
        bar.addRoundedRect(rect.adjusted(0, 0, -(rect.width() - 4), 0), 2, 2)
        p.fillPath(bar, self._accent)


class SegmentedControl(QFrame):
    """A row of mutually exclusive toggle buttons."""

    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._values: list[object] = []
        self._group.idClicked.connect(lambda i: self.changed.emit(self._values[i]))

    def add(self, label: str, value: object, tooltip: str = "") -> None:
        btn = QToolButton()
        btn.setObjectName("Segment")
        btn.setText(label)
        btn.setToolTip(tooltip)
        btn.setCheckable(True)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setCursor(Qt.PointingHandCursor)
        self._group.addButton(btn, len(self._values))
        self._values.append(value)
        self._layout.addWidget(btn)

    def value(self) -> object:
        i = self._group.checkedId()
        return self._values[i] if i >= 0 else None

    def set_value(self, value: object) -> None:
        if value in self._values:
            self._group.button(self._values.index(value)).setChecked(True)


class _IconBinder(QObject):
    """Re-tints a widget's icon whenever the theme changes (lives as the widget's child)."""

    def __init__(self, target, name: str, token: str, size: int | None = None):
        super().__init__(target)
        self._target = target
        self.name = name
        self.token = token
        self.size = size
        theme.changed.connect(self.apply)
        self.apply(theme.palette)

    def set_name(self, name: str, token: str | None = None) -> None:
        self.name = name
        if token:
            self.token = token
        self.apply(theme.palette)

    def apply(self, _palette=None) -> None:
        if isinstance(self._target, QLabel):
            p = theme.palette
            self._target.setPixmap(icons.pixmap(self.name, getattr(p, self.token), self.size or 18))
        else:
            self._target.setIcon(theme.icon(self.name, self.token))


def bind_icon(widget, name: str, token: str = "text", size: int | None = None) -> _IconBinder:
    return _IconBinder(widget, name, token, size)


def tool_button(text: str, icon: str, tooltip: str = "", checkable: bool = False,
                under: bool = True, icon_size: int = 22) -> QToolButton:
    btn = QToolButton()
    btn.setText(text)
    btn.setToolTip(tooltip or text)
    btn.setCheckable(checkable)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setIconSize(QSize(icon_size, icon_size))
    btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon if under else
                           (Qt.ToolButtonTextBesideIcon if text else Qt.ToolButtonIconOnly))
    btn._binder = bind_icon(btn, icon)
    return btn


def flat_button(icon: str, tooltip: str, checkable: bool = False, size: int = 18) -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("Flat")
    btn.setToolTip(tooltip)
    btn.setCheckable(checkable)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setIconSize(QSize(size, size))
    btn._binder = bind_icon(btn, icon, "text_muted")
    return btn


def chip(text: str, checked: bool = False) -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("Chip")
    btn.setText(text)
    btn.setCheckable(True)
    btn.setChecked(checked)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def set_prop(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property used by the style sheet and re-polish the widget."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ---------------------------------------------------------------------- cooling widgets
def format_value(value, unit: str = "", digits: int | None = None) -> str:
    """A reading as text: 31.4 °C, 1 204 rpm, 5.9 K, 118 L/h, 62 %."""
    if value is None:
        return "—"
    if digits is None:
        digits = {"rpm": 0, "%": 0, "L/h": 0, "ml": 0, "mbar": 1, "W": 1, "V": 2, "A": 3, "µS/cm": 1}.get(unit, 1)
    text = f"{value:,.{digits}f}".replace(",", "\u2009")
    return f"{text} {unit}".strip()


def value_parts(value, unit: str = "") -> tuple[str, str]:
    text = format_value(value, unit)
    if value is None or not unit:
        return text, ""
    return text[: -len(unit)].strip(), unit


class Sparkline(QWidget):
    """A tiny line chart of recent values."""

    def __init__(self, parent=None, color_token: str = "accent"):
        super().__init__(parent)
        self.values: list[float | None] = []
        self.token = color_token
        self.setMinimumHeight(26)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theme.changed.connect(lambda _p: self.update())

    def set_values(self, values) -> None:
        self.values = list(values)[-180:]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        vals = [v for v in self.values if v is not None]
        if len(vals) < 2:
            return
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-6:
            lo, hi = lo - 0.5, hi + 0.5
        w, h = self.width(), self.height()
        n = len(self.values)
        path = QPainterPath()
        started = False
        for i, v in enumerate(self.values):
            if v is None:
                started = False
                continue
            pt = QPointF(i * (w - 2) / max(1, n - 1) + 1, h - 3 - (v - lo) / (hi - lo) * (h - 6))
            if started:
                path.lineTo(pt)
            else:
                path.moveTo(pt)
                started = True
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(getattr(theme.palette, self.token)), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(path)


class ValueTile(QFrame):
    """A big reading with its unit, a caption, an icon and a sparkline."""

    clicked = Signal()

    def __init__(self, caption: str = "", icon: str = "thermometer", parent=None):
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 8)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.icon = QLabel()
        self._icon = bind_icon(self.icon, icon, "text_muted", 15)
        self.caption = QLabel(caption)
        self.caption.setObjectName("Muted")
        self.caption.setFont(scaled_font(self.caption, 0.88))
        top.addWidget(self.icon)
        top.addWidget(self.caption, 1)
        lay.addLayout(top)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.value = QLabel("—")
        self.value.setObjectName("BigValue")
        self.value.setFont(scaled_font(self.value, 1.9, bold=True))
        self.unit = QLabel("")
        self.unit.setObjectName("Muted")
        self.unit.setFont(scaled_font(self.unit, 1.05))
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, Qt.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        self.spark = Sparkline(self)
        lay.addWidget(self.spark)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_icon(self, name: str) -> None:
        self._icon.set_name(name)

    def set_reading(self, value, unit: str, caption: str | None = None, alert: bool = False) -> None:
        text, u = value_parts(value, unit)
        self.value.setText(text)
        self.unit.setText(u)
        if caption is not None:
            self.caption.setText(caption)
        name = "TileAlert" if alert else "Tile"
        if self.objectName() != name:
            self.setObjectName(name)
            set_prop(self, "alert", alert)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


PLACEMENT_TEXT = {"device": ("ON DEVICE", "BadgeDevice"), "software": ("SOFTWARE", "BadgeSoftware"),
                  "unmanaged": ("NOT MANAGED", "BadgeIdle")}


def badge(text: str = "", kind: str = "BadgeIdle") -> QLabel:
    lb = QLabel(text)
    lb.setObjectName(kind)
    lb.setFont(scaled_font(lb, 0.72, bold=True))
    lb.setAlignment(Qt.AlignCenter)
    return lb


def set_badge(label: QLabel, text: str, kind: str) -> None:
    label.setText(text)
    if label.objectName() != kind:
        label.setObjectName(kind)
        set_prop(label, "kind", kind)


def set_placement_badge(label: QLabel, placement: str, override: bool = False) -> None:
    if override:
        set_badge(label, "OVERRIDE", "Badge")
        return
    text, kind = PLACEMENT_TEXT.get(placement, PLACEMENT_TEXT["unmanaged"])
    set_badge(label, text, kind)


class SensorCombo(QComboBox):
    """Pick a sensor by its reading id; the list shows the current value of each sensor."""

    def __init__(self, parent=None, kinds: set[str] | None = None, allow_none: str = ""):
        super().__init__(parent)
        self.kinds = kinds
        self.allow_none = allow_none
        self._wanted = ""
        self.setMinimumContentsLength(22)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

    def set_readings(self, readings: list[dict], exclude: set[str] | None = None) -> None:
        current = self.current_id() or self._wanted
        self.blockSignals(True)
        self.clear()
        if self.allow_none:
            self.addItem(self.allow_none, "")
        last_group = None
        for r in readings:
            if exclude and r["id"] in exclude:
                continue
            if self.kinds and r.get("kind") not in self.kinds:
                continue
            group = r.get("_group") or r.get("source", "")
            if group != last_group and self.count():
                self.insertSeparator(self.count())
            last_group = group
            label = r.get("_display") or r.get("label") or r["id"]
            self.addItem(f"{label}  ·  {format_value(r.get('value'), r.get('unit', ''))}", r["id"])
        if current and self.findData(current) < 0:
            self.addItem(f"{current} (not available)", current)
        idx = self.findData(current) if current else (0 if self.count() else -1)
        self.setCurrentIndex(max(0, idx))
        self.blockSignals(False)

    def refresh_values(self, readings: dict[str, dict], display) -> None:
        """Update the values shown in the list without touching the selection."""
        if self.view().isVisible():
            return
        for i in range(self.count()):
            sid = self.itemData(i)
            r = readings.get(sid) if isinstance(sid, str) and sid else None
            if r is not None:
                self.setItemText(i, f"{display(r)}  ·  {format_value(r.get('value'), r.get('unit', ''))}")

    def set_current_id(self, sid: str) -> None:
        self._wanted = sid
        idx = self.findData(sid)
        if idx < 0 and sid:
            self.addItem(f"{sid} (not available)", sid)
            idx = self.count() - 1
        self.setCurrentIndex(max(0, idx))

    def current_id(self) -> str:
        data = self.currentData()
        return data if isinstance(data, str) else ""
