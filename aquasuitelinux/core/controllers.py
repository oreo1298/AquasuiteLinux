"""Controllers turn a sensor value into a power (0-100 %); the output stage maps that onto a fan.

The output stage follows the Aquacomputer devices: controller output 0..100 % is scaled
between the output's minimum and maximum power; at 0 % the fan stops unless "hold minimum
power" is set; when the input is unavailable the output runs at its fallback power; "start
boost" briefly runs a stopped fan at full power so it starts reliably.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import ControllerConfig, OutputConfig


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return lo if v < lo else hi if v > hi else v


def interpolate(points, x: float) -> float:
    """Piecewise-linear curve through ``points`` [(x, y), ...], flat outside its ends."""
    pts = sorted((float(p[0]), float(p[1])) for p in points)
    if not pts:
        return 100.0
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def scale_power(c: float | None, min_power: float, max_power: float, hold_min: bool, fallback: float) -> float:
    """Map a controller output onto an output's power range (see module docstring)."""
    if c is None or (isinstance(c, float) and math.isnan(c)):
        return clamp(fallback)
    c = clamp(c)
    lo, hi = clamp(min_power), clamp(max_power)
    if hi < lo:
        lo, hi = hi, lo
    if c <= 0.0:
        return lo if hold_min else 0.0
    return lo + c / 100.0 * (hi - lo)


class ControllerState:
    """Per-controller memory: curve hysteresis, PID integral, two-point switch state."""

    def __init__(self) -> None:
        self.x_eff: float | None = None
        self.integral = 0.0
        self.last_x: float | None = None
        self.on = False
        self.value: float | None = None
        self.input_value: float | None = None

    def reset(self) -> None:
        self.__init__()


def evaluate(cfg: ControllerConfig, st: ControllerState, x: float | None, dt: float,
             controller_values: dict[str, float | None], output_values: dict[str, float | None]) -> float | None:
    """One step of a controller. Returns None when its input is unavailable."""
    kind = cfg.kind
    st.input_value = x
    if kind == "fixed":
        st.value = clamp(cfg.power)
        return st.value
    if kind == "follow":
        st.value = output_values.get(cfg.follow)
        return st.value
    if kind == "mix":
        vals = [controller_values.get(s) for s in cfg.sources]
        vals = [v for v in vals if v is not None]
        if not vals:
            st.value = None
        elif cfg.mix == "min":
            st.value = min(vals)
        elif cfg.mix == "average":
            st.value = sum(vals) / len(vals)
        else:
            st.value = max(vals)
        return st.value
    if x is None:
        st.value = None
        st.last_x = None
        return None

    if kind == "curve":
        h = max(0.0, cfg.hysteresis)
        if st.x_eff is None or x >= st.x_eff or x <= st.x_eff - h:
            st.x_eff = x
        st.value = clamp(interpolate(cfg.points, st.x_eff))
    elif kind == "target":
        err = x - cfg.target
        deriv = 0.0 if st.last_x is None or dt <= 0 else (x - st.last_x) / dt
        p = cfg.kp * err
        d = cfg.kd * deriv
        candidate = st.integral + cfg.ki * err * dt
        out = p + candidate + d
        # integrate only while that doesn't push further into saturation (anti-windup)
        if not ((out > 100.0 and err > 0) or (out < 0.0 and err < 0)):
            st.integral = clamp(candidate, -100.0, 100.0)
        st.value = clamp(p + st.integral + d)
    elif kind == "two_point":
        if x >= cfg.on_above:
            st.on = True
        elif x <= cfg.off_below:
            st.on = False
        st.value = clamp(cfg.on_power if st.on else cfg.off_power)
    else:
        st.value = None
    st.last_x = x
    return st.value


def order(controllers: list[ControllerConfig]) -> list[ControllerConfig]:
    """Controllers ordered so that "mix" controllers come after their sources (cycles dropped)."""
    by_id = {c.id: c for c in controllers}
    done: list[ControllerConfig] = []
    state: dict[str, int] = {}

    def visit(c: ControllerConfig) -> None:
        mark = state.get(c.id, 0)
        if mark:
            return
        state[c.id] = 1
        if c.kind == "mix":
            for s in c.sources:
                if s in by_id and state.get(s, 0) != 1:
                    visit(by_id[s])
        state[c.id] = 2
        done.append(c)

    for c in controllers:
        visit(c)
    return done


@dataclass
class StageState:
    applied: float | None = None
    boost_until: float = 0.0


BOOST_SECONDS = 2.0


def output_stage(c: float | None, oc: OutputConfig, st: StageState, dt: float, now: float) -> float:
    """Software-side output stage: scaling, fallback, start boost and ramp limits."""
    target = scale_power(c, oc.min_power, oc.max_power, oc.hold_min, oc.fallback)
    prev = st.applied
    if oc.start_boost and target > 0.0 and (prev is None or prev <= 0.0):
        st.boost_until = now + BOOST_SECONDS
    if now < st.boost_until and target > 0.0:
        st.applied = 100.0
        return 100.0
    if prev is not None and dt > 0:
        if oc.ramp_up > 0 and target > prev:
            target = min(target, prev + oc.ramp_up * dt)
        if oc.ramp_down > 0 and target < prev:
            target = max(target, prev - oc.ramp_down * dt)
    st.applied = clamp(target)
    return st.applied
