"""The interactive fan curve: drag points, double-click to add, right-click or Delete to remove.

A live marker shows where the controller's input is right now and what it outputs.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.controllers import interpolate
from .charts import nice_step
from .theme import theme
from .widgets import scaled_font

DEFAULT_RANGES = {"K": (0.0, 20.0), "°C": (20.0, 60.0), "%": (0.0, 100.0), "W": (0.0, 600.0),
                  "L/h": (0.0, 300.0), "rpm": (0.0, 3000.0)}


def default_range(unit: str, points: list[list[float]]) -> tuple[float, float]:
    lo, hi = DEFAULT_RANGES.get(unit, (0.0, 100.0))
    if points:
        xs = [p[0] for p in points]
        span = max(1.0, (max(xs) - min(xs)) * 0.15)
        lo = min(lo, math.floor(min(xs) - span))
        hi = max(hi, math.ceil(max(xs) + span))
    if unit in ("K", "%", "W", "L/h", "rpm"):
        lo = max(lo, 0.0) if min((p[0] for p in points), default=0) >= 0 else lo
    return lo, hi


class CurveEditor(QWidget):
    changed = Signal(list)

    MAX_POINTS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[list[float]] = [[25, 20], [40, 100]]
        self.unit = "°C"
        self.x_range = (20.0, 60.0)
        self.live_x: float | None = None
        self.live_y: float | None = None
        self.min_power = 0.0
        self.max_power = 100.0
        self.selected: int | None = None
        self._drag: int | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip("Drag points to shape the curve · double-click to add a point · "
                        "right-click or Delete to remove one · arrow keys nudge the selected point")
        theme.changed.connect(lambda _p: self.update())

    # ------------------------------------------------------------------ data
    def set_curve(self, points: list[list[float]], unit: str) -> None:
        self.points = sorted([list(map(float, p)) for p in points]) or [[25.0, 20.0], [40.0, 100.0]]
        self.unit = unit
        self.x_range = default_range(unit, self.points)
        self.selected = None
        self.update()

    def set_live(self, x: float | None, y: float | None) -> None:
        self.live_x, self.live_y = x, y
        self.update()

    def set_limits(self, min_power: float, max_power: float) -> None:
        self.min_power, self.max_power = min_power, max_power
        self.update()

    # ------------------------------------------------------------------ geometry
    def _plot(self) -> QRectF:
        fm = QFontMetrics(scaled_font(self, 0.85))
        left = fm.horizontalAdvance("100 %") + 12
        return QRectF(left, 12, self.width() - left - 16, self.height() - fm.height() - 30)

    def _to_px(self, x: float, y: float) -> QPointF:
        r = self._plot()
        lo, hi = self.x_range
        return QPointF(r.left() + (x - lo) / (hi - lo) * r.width(), r.bottom() - y / 100.0 * r.height())

    def _from_px(self, px: QPointF) -> tuple[float, float]:
        r = self._plot()
        lo, hi = self.x_range
        x = lo + (px.x() - r.left()) / r.width() * (hi - lo)
        y = (r.bottom() - px.y()) / r.height() * 100.0
        return x, max(0.0, min(100.0, y))

    def _hit(self, pos: QPointF) -> int | None:
        best, dist = None, 12.0
        for i, (x, y) in enumerate(self.points):
            pt = self._to_px(x, y)
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d < dist:
                best, dist = i, d
        return best

    def _step(self) -> float:
        lo, hi = self.x_range
        return 0.1 if hi - lo <= 30 else 0.5

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        font = scaled_font(self, 0.85)
        p.setFont(font)
        fm = QFontMetrics(font)
        r = self._plot()
        p.fillRect(r, QColor(pal.chart_bg))
        grid = QPen(QColor(pal.grid), 1)
        for pct in range(0, 101, 20):
            y = r.bottom() - pct / 100 * r.height()
            p.setPen(grid)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(pal.text_faint))
            p.drawText(QRectF(0, y - fm.height() / 2, r.left() - 8, fm.height()), Qt.AlignRight | Qt.AlignVCenter,
                       f"{pct} %")
        lo, hi = self.x_range
        step = nice_step(hi - lo, 8)
        v = math.ceil(lo / step) * step
        while v <= hi + 1e-9:
            x = r.left() + (v - lo) / (hi - lo) * r.width()
            p.setPen(grid)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor(pal.text_faint))
            p.drawText(QRectF(x - 40, r.bottom() + 4, 80, fm.height()), Qt.AlignHCenter, f"{v:g} {self.unit}")
            v += step

        # power limits band
        if self.min_power > 0 or self.max_power < 100:
            band = QColor(pal.text_faint)
            band.setAlpha(28)
            p.fillRect(QRectF(r.left(), r.bottom() - self.min_power / 100 * r.height(), r.width(),
                              self.min_power / 100 * r.height()), band)
            p.fillRect(QRectF(r.left(), r.top(), r.width(), (100 - self.max_power) / 100 * r.height()), band)

        pts = [self._to_px(x, y) for x, y in self.points]
        first, last = self.points[0], self.points[-1]
        line = QPainterPath(self._to_px(lo, first[1]))
        for pt in pts:
            line.lineTo(pt)
        line.lineTo(self._to_px(hi, last[1]))
        area = QPainterPath(line)
        area.lineTo(QPointF(r.right(), r.bottom()))
        area.lineTo(QPointF(r.left(), r.bottom()))
        area.closeSubpath()
        fill = QColor(pal.accent)
        fill.setAlpha(40)
        p.save()
        p.setClipRect(r)
        p.fillPath(area, fill)
        pen = QPen(QColor(pal.accent), 2.4)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(line)
        p.restore()

        for i, pt in enumerate(pts):
            sel = i == self.selected
            p.setPen(QPen(QColor(pal.accent), 2.0))
            p.setBrush(QColor(pal.accent if sel else pal.surface))
            p.drawEllipse(pt, 6.5 if sel else 5.5, 6.5 if sel else 5.5)
        if self.selected is not None and self.selected < len(self.points):
            x, y = self.points[self.selected]
            text = f"{x:.1f} {self.unit} → {y:.0f} %"
            pt = pts[self.selected]
            w = fm.horizontalAdvance(text) + 14
            box = QRectF(min(max(r.left(), pt.x() - w / 2), r.right() - w), pt.y() - fm.height() - 20, w,
                         fm.height() + 8)
            if box.top() < r.top():
                box.moveTop(pt.y() + 12)
            p.setPen(QPen(QColor(pal.border_strong), 1))
            p.setBrush(QColor(pal.raised))
            p.drawRoundedRect(box, 6, 6)
            p.setPen(QColor(pal.text))
            p.drawText(box, Qt.AlignCenter, text)

        if self.live_x is not None:
            y_out = interpolate(self.points, self.live_x)
            lx = max(lo, min(hi, self.live_x))
            px = self._to_px(lx, y_out)
            p.setPen(QPen(QColor(pal.warning), 1.4, Qt.DashLine))
            p.drawLine(QPointF(px.x(), r.top()), QPointF(px.x(), r.bottom()))
            p.setPen(QPen(QColor(pal.warning), 2))
            p.setBrush(QColor(pal.warning))
            p.drawEllipse(px, 4.5, 4.5)
            label = f"now {self.live_x:.1f} {self.unit} → {y_out:.0f} %"
            p.setPen(QColor(pal.warning))
            tw = fm.horizontalAdvance(label)
            tx = px.x() + 8 if px.x() + 8 + tw < r.right() else px.x() - 8 - tw
            p.drawText(QPointF(tx, r.top() + fm.ascent() + 4), label)

    # ------------------------------------------------------------------ interaction
    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        hit = self._hit(pos)
        if event.button() == Qt.RightButton:
            if hit is not None and len(self.points) > 2:
                del self.points[hit]
                self.selected = None
                self._emit()
            return
        self.selected = hit
        self._drag = hit
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._hit(event.position()) is not None or len(self.points) >= self.MAX_POINTS:
            return
        x, y = self._from_px(event.position())
        lo, hi = self.x_range
        if not lo <= x <= hi:
            return
        self.points.append([round(x, 1), round(y)])
        self.points.sort()
        self.selected = self.points.index([round(x, 1), round(y)])
        self._emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            self.setCursor(Qt.PointingHandCursor if self._hit(event.position()) is not None else Qt.ArrowCursor)
            return
        x, y = self._from_px(event.position())
        i = self._drag
        lo, hi = self.x_range
        left = self.points[i - 1][0] + self._step() if i > 0 else lo
        right = self.points[i + 1][0] - self._step() if i < len(self.points) - 1 else hi
        self.points[i] = [round(max(left, min(right, x)), 1), round(y)]
        self.update()

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self._emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        i = self.selected
        if i is None:
            return super().keyPressEvent(event)
        key = event.key()
        if key in (Qt.Key_Delete, Qt.Key_Backspace) and len(self.points) > 2:
            del self.points[i]
            self.selected = None
            self._emit()
            return
        dx = {Qt.Key_Left: -self._step(), Qt.Key_Right: self._step()}.get(key, 0.0)
        dy = {Qt.Key_Up: 1.0, Qt.Key_Down: -1.0}.get(key, 0.0)
        if not dx and not dy:
            return super().keyPressEvent(event)
        x, y = self.points[i]
        lo, hi = self.x_range
        left = self.points[i - 1][0] + self._step() if i > 0 else lo
        right = self.points[i + 1][0] - self._step() if i < len(self.points) - 1 else hi
        self.points[i] = [round(max(left, min(right, x + dx)), 1), max(0.0, min(100.0, y + dy))]
        self._emit()

    def _emit(self) -> None:
        self.update()
        self.changed.emit([list(p) for p in self.points])


PRESETS = {
    "°C": {
        "Silent": [[30, 0], [35, 20], [40, 40], [45, 70], [50, 100]],
        "Balanced": [[25, 20], [30, 35], [35, 60], [40, 100]],
        "Performance": [[25, 40], [30, 60], [35, 85], [38, 100]],
    },
    "K": {
        "Silent": [[3, 10], [6, 25], [9, 50], [12, 100]],
        "Balanced": [[2, 20], [4, 30], [6, 45], [8, 70], [10, 100]],
        "Performance": [[1, 30], [3, 50], [5, 75], [7, 100]],
    },
}
