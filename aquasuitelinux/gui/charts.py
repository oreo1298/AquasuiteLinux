"""A themed live line chart (painted directly; no QtCharts dependency)."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from .theme import theme
from .widgets import format_value, scaled_font


def nice_step(span: float, target: int = 5) -> float:
    if span <= 0:
        return 1.0
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


class LineChart(QWidget):
    """Several series over time. Series with different units get separate scales (left/right)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.times: list[float] = []
        self.series: list[dict] = []        # {"id","label","unit","values","color"}
        self.span = 600.0
        self.hover_x: float | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        theme.changed.connect(lambda _p: self.update())

    def set_data(self, times: list[float], series: list[dict], span: float | None = None) -> None:
        self.times = times
        self.series = series
        if span:
            self.span = span
        self.update()

    # ------------------------------------------------------------------ geometry
    def _units(self) -> list[str]:
        units: list[str] = []
        for s in self.series:
            if s.get("unit", "") not in units:
                units.append(s.get("unit", ""))
        return units[:2]

    def _range(self, unit: str) -> tuple[float, float]:
        vals = [v for s in self.series if s.get("unit", "") == unit for v in s["values"] if v is not None]
        if not vals:
            return 0.0, 1.0
        lo, hi = min(vals), max(vals)
        if unit in ("%",):
            return 0.0, 100.0
        pad = max((hi - lo) * 0.15, {"°C": 1.0, "K": 0.5, "rpm": 100.0, "L/h": 5.0}.get(unit, 0.5))
        lo, hi = lo - pad, hi + pad
        if unit in ("rpm", "L/h", "W") and lo < 0:
            lo = 0.0
        return lo, hi

    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        font = scaled_font(self, 0.82)
        p.setFont(font)
        fm = QFontMetrics(font)
        units = self._units()
        left_w = fm.horizontalAdvance("0000.0") + 8
        right_w = left_w if len(units) > 1 else 10
        legend_h = fm.height() + 10
        plot = QRectF(left_w, legend_h, self.width() - left_w - right_w, self.height() - legend_h - fm.height() - 8)
        if plot.width() < 20 or plot.height() < 20:
            return
        p.fillRect(plot, QColor(pal.chart_bg))

        now = self.times[-1] if self.times else time.time()
        # right after start there is little data: show what exists (at least two minutes) instead of a sliver
        span = self.span
        if self.times:
            span = max(120.0, min(self.span, now - self.times[0]))
        t0 = now - span

        def tx(t: float) -> float:
            return plot.left() + (t - t0) / span * plot.width()

        # grid + axes
        grid_pen = QPen(QColor(pal.grid), 1)
        ranges = {u: self._range(u) for u in units}
        if units:
            lo, hi = ranges[units[0]]
            step = nice_step(hi - lo)
            v = math.ceil(lo / step) * step
            while v <= hi + 1e-9:
                y = plot.bottom() - (v - lo) / (hi - lo) * plot.height()
                p.setPen(grid_pen)
                p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                p.setPen(QColor(pal.text_faint))
                p.drawText(QRectF(0, y - fm.height() / 2, left_w - 6, fm.height()), Qt.AlignRight | Qt.AlignVCenter,
                           f"{v:g}")
                v += step
            if len(units) > 1:
                lo2, hi2 = ranges[units[1]]
                step2 = nice_step(hi2 - lo2)
                v = math.ceil(lo2 / step2) * step2
                while v <= hi2 + 1e-9:
                    y = plot.bottom() - (v - lo2) / (hi2 - lo2) * plot.height()
                    p.setPen(QColor(pal.text_faint))
                    p.drawText(QRectF(plot.right() + 6, y - fm.height() / 2, right_w - 6, fm.height()),
                               Qt.AlignLeft | Qt.AlignVCenter, f"{v:g}")
                    v += step2
        # time labels
        tstep = nice_step(span / 60.0, 5) * 60 if span >= 300 else nice_step(span, 5)
        t = math.ceil(t0 / tstep) * tstep
        p.setPen(QColor(pal.text_faint))
        while t <= now:
            x = tx(t)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor(pal.text_faint))
            label = time.strftime("%H:%M", time.localtime(t)) if tstep >= 60 else time.strftime("%M:%S",
                                                                                                    time.localtime(t))
            p.drawText(QRectF(x - 30, plot.bottom() + 3, 60, fm.height()), Qt.AlignHCenter, label)
            t += tstep

        # series
        p.save()
        p.setClipRect(plot.adjusted(-1, -1, 1, 1))
        for s in self.series:
            unit = s.get("unit", "")
            if unit not in ranges:
                continue
            lo, hi = ranges[unit]
            color = QColor(s["color"])
            path = QPainterPath()
            started = False
            for tt, v in zip(self.times, s["values"]):
                if v is None or tt < t0 - 5:
                    started = False
                    continue
                pt = QPointF(tx(tt), plot.bottom() - (v - lo) / (hi - lo) * plot.height())
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
            pen = QPen(color, 2.0 if not s.get("dashed") else 1.6)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            if s.get("dashed"):
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawPath(path)
        p.restore()

        # legend
        x = left_w
        for s in self.series:
            last = next((v for v in reversed(s["values"]) if v is not None), None)
            text = f"{s['label']}  {format_value(last, s.get('unit', ''))}"
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(s["color"]))
            p.drawRoundedRect(QRectF(x, 5 + fm.height() / 2 - 4, 10, 8), 3, 3)
            p.setPen(QColor(pal.text_muted))
            p.drawText(QPointF(x + 15, 5 + fm.ascent()), text)
            x += fm.horizontalAdvance(text) + 32
            if x > self.width() - 60:
                break

        # hover line
        if self.hover_x is not None and plot.left() <= self.hover_x <= plot.right() and self.times:
            p.setPen(QPen(QColor(pal.text_faint), 1, Qt.DashLine))
            p.drawLine(QPointF(self.hover_x, plot.top()), QPointF(self.hover_x, plot.bottom()))
        self._plot = plot
        self._t0 = t0
        self._span = span

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self.hover_x = event.position().x()
        self.update()
        plot = getattr(self, "_plot", None)
        if not self.times or plot is None or plot.width() <= 0:
            return
        t = self._t0 + (self.hover_x - plot.left()) / plot.width() * getattr(self, "_span", self.span)
        idx = min(range(len(self.times)), key=lambda i: abs(self.times[i] - t))
        lines = [time.strftime("%H:%M:%S", time.localtime(self.times[idx]))]
        for s in self.series:
            v = s["values"][idx] if idx < len(s["values"]) else None
            lines.append(f"{s['label']}: {format_value(v, s.get('unit', ''))}")
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self.hover_x = None
        self.update()
