"""Read and modify control (settings) reports, and build the volatile output reports.

Standard-family control substructure of one fan (Quadro, Octo, D5 Next)::

    +0x00  u8     mode          0 manual, 1 PID, 2 curve, 3+n follow fan n+1
    +0x01  be16   power         manual power, 0..10000 (hundredths of a percent)
    +0x03  be16   sensor        PID/curve input, 0..3 physical, 4.. software sensors, 0xFFFF none
    +0x05  6×be16 PID           target temp, P, I, D1, D2, hysteresis
    +0x11  2 bytes padding
    +0x13  be16   curve start
    +0x15  16×be16 curve temperatures (hundredths of °C)
    +0x35  16×be16 curve powers (hundredths of a percent)

Setup record of one fan (all modes)::

    flags (bit 1 hold minimum power, bit 2 start boost), be16 min, be16 max, be16 fallback

Checksum: CRC-16/USB over bytes 1..n-3, stored big-endian in the last two bytes.
"""

from __future__ import annotations

from .crc import crc16_usb, is_sealed, seal
from .devices import SENSOR_NA, DeviceSpec, SoftSensorSpec
from .errors import NotSupported
from .model import FanControl, FanSetup

MODE_MANUAL, MODE_PID, MODE_CURVE, MODE_FOLLOW = 0, 1, 2, 3
SOURCE_NONE = 0xFFFF
CURVE_POINTS = 16
HOLD_MIN_BIT = 1 << 1
START_BOOST_BIT = 1 << 2

_PWM = 0x01
_SOURCE = 0x03
_PID = 0x05
_CURVE_START = 0x13
_CURVE_TEMPS = 0x15
_CURVE_POWERS = 0x35


def _get16(buf: bytes | bytearray, off: int, signed: bool = False) -> int:
    return int.from_bytes(buf[off:off + 2], "big", signed=signed)


def _set16(buf: bytearray, off: int, value: int) -> None:
    buf[off:off + 2] = (int(value) & 0xFFFF).to_bytes(2, "big")


class ControlReport:
    """A mutable copy of a standard-family control report."""

    def __init__(self, spec: DeviceSpec, data: bytes | bytearray):
        if spec.ctrl_id is None or spec.family != "standard":
            raise NotSupported(f"{spec.name} has no standard control report")
        if len(data) < spec.ctrl_length:
            raise ValueError(f"control report too short ({len(data)} < {spec.ctrl_length} bytes)")
        self.spec = spec
        self.buf = bytearray(data[:spec.ctrl_length])

    # ------------------------------------------------------------------ integrity
    @property
    def valid(self) -> bool:
        return self.buf[0] == self.spec.ctrl_id and is_sealed(self.buf)

    def seal(self) -> bytes:
        seal(self.buf)
        return bytes(self.buf)

    def checksum(self) -> int:
        return crc16_usb(memoryview(self.buf)[1:len(self.buf) - 2])

    # ------------------------------------------------------------------ fans
    def _fan_ctrl(self, index: int) -> int:
        off = self.spec.fans[index].ctrl
        if off is None:
            raise NotSupported(f"{self.spec.fans[index].label} cannot be configured")
        return off

    def fan(self, index: int) -> FanControl:
        o = self._fan_ctrl(index)
        b = self.buf
        return FanControl(
            mode=b[o],
            pwm=_get16(b, o + _PWM),
            source=_get16(b, o + _SOURCE),
            pid=[_get16(b, o + _PID + 2 * k) for k in range(6)],
            curve_start=_get16(b, o + _CURVE_START),
            curve_temps=[_get16(b, o + _CURVE_TEMPS + 2 * k, signed=True) for k in range(CURVE_POINTS)],
            curve_powers=[_get16(b, o + _CURVE_POWERS + 2 * k) for k in range(CURVE_POINTS)],
        )

    def set_fan(self, index: int, fc: FanControl) -> None:
        o = self._fan_ctrl(index)
        b = self.buf
        b[o] = fc.mode & 0xFF
        _set16(b, o + _PWM, max(0, min(10000, fc.pwm)))
        _set16(b, o + _SOURCE, fc.source)
        for k in range(6):
            _set16(b, o + _PID + 2 * k, fc.pid[k])
        _set16(b, o + _CURVE_START, fc.curve_start)
        temps = list(fc.curve_temps)[:CURVE_POINTS]
        powers = list(fc.curve_powers)[:CURVE_POINTS]
        for k in range(CURVE_POINTS):
            _set16(b, o + _CURVE_TEMPS + 2 * k, temps[k] if k < len(temps) else temps[-1])
            _set16(b, o + _CURVE_POWERS + 2 * k, max(0, min(10000, powers[k] if k < len(powers) else powers[-1])))

    def set_manual(self, index: int, power_percent: float) -> None:
        o = self._fan_ctrl(index)
        self.buf[o] = MODE_MANUAL
        _set16(self.buf, o + _PWM, round(max(0.0, min(100.0, power_percent)) * 100))

    def setup(self, index: int) -> FanSetup | None:
        f = self.spec.fans[index]
        if f.setup_min is None:
            return None
        b = self.buf
        flags = b[f.setup_flags] if f.setup_flags is not None else 0
        return FanSetup(
            hold_min=bool(flags & HOLD_MIN_BIT) if f.setup_flags is not None else False,
            start_boost=bool(flags & START_BOOST_BIT) if f.setup_flags is not None else False,
            flags_raw=flags,
            min_power=_get16(b, f.setup_min),
            max_power=_get16(b, f.setup_min + 2),
            fallback=_get16(b, f.setup_min + 4),
        )

    def set_setup(self, index: int, s: FanSetup) -> None:
        f = self.spec.fans[index]
        if f.setup_min is None:
            return
        b = self.buf
        if f.setup_flags is not None:
            flags = b[f.setup_flags] & ~(HOLD_MIN_BIT | START_BOOST_BIT)
            flags |= HOLD_MIN_BIT if s.hold_min else 0
            flags |= START_BOOST_BIT if s.start_boost else 0
            b[f.setup_flags] = flags
        _set16(b, f.setup_min, max(0, min(10000, s.min_power)))
        _set16(b, f.setup_min + 2, max(0, min(10000, s.max_power)))
        _set16(b, f.setup_min + 4, max(0, min(10000, s.fallback)))

    # ------------------------------------------------------------------ sensors
    def temp_offsets(self) -> list[float]:
        o = self.spec.temp_offsets
        if o is None:
            return []
        return [_get16(self.buf, o + 2 * i, signed=True) / 100 for i in range(self.spec.num_temp_offsets)]

    def set_temp_offset(self, index: int, kelvin: float) -> None:
        o = self.spec.temp_offsets
        if o is None or not 0 <= index < self.spec.num_temp_offsets:
            raise NotSupported("this device has no offset for that sensor")
        _set16(self.buf, o + 2 * index, round(max(-15.0, min(15.0, kelvin)) * 100))

    def flow_pulses(self) -> int | None:
        o = self.spec.flow_pulses
        return None if o is None else _get16(self.buf, o)

    def set_flow_pulses(self, pulses: int) -> None:
        o = self.spec.flow_pulses
        if o is None:
            raise NotSupported("this device has no flow sensor calibration")
        _set16(self.buf, o, max(10, min(1000, int(pulses))))

    def to_dict(self) -> dict:
        """Everything we understand, for import previews and the device settings dialog."""
        fans = []
        for i, f in enumerate(self.spec.fans):
            if f.ctrl is None:
                continue
            fc = self.fan(i)
            st = self.setup(i)
            fans.append({
                "key": f.key, "label": f.label, "mode": fc.mode, "pwm": fc.pwm / 100,
                "source": None if fc.source == SOURCE_NONE else fc.source,
                "pid": fc.pid, "curve_start": fc.curve_start / 100,
                "curve": [[t / 100, p / 100] for t, p in zip(fc.curve_temps, fc.curve_powers)],
                "setup": None if st is None else {
                    "hold_min": st.hold_min, "start_boost": st.start_boost, "min": st.min_power / 100,
                    "max": st.max_power / 100, "fallback": st.fallback / 100},
            })
        return {"kind": self.spec.kind, "fans": fans, "temp_offsets": self.temp_offsets(),
                "flow_pulses": self.flow_pulses(), "valid": self.valid}


def source_index(spec: DeviceSpec, sensor_key: str) -> int | None:
    """The fan-controller input index of a sensor on the same device, or None."""
    for i, t in enumerate(spec.temps):
        if t.key == sensor_key:
            return i
    if spec.virtual and sensor_key.startswith("virt"):
        try:
            slot = int(sensor_key[4:])
        except ValueError:
            return None
        if 1 <= slot <= spec.virtual[1]:
            return len(spec.temps) + slot - 1
    return None


def source_key(spec: DeviceSpec, index: int) -> str | None:
    """The sensor key a fan-controller input index refers to."""
    if index == SOURCE_NONE:
        return None
    if index < len(spec.temps):
        return spec.temps[index].key
    slot = index - len(spec.temps) + 1
    if spec.virtual and 1 <= slot <= spec.virtual[1]:
        return f"virt{slot}"
    return None


def resample_curve(points: list[tuple[float, float]], count: int = CURVE_POINTS) -> list[tuple[float, float]]:
    """Fit a curve with any number of points into exactly ``count`` points (same shape)."""
    pts = sorted((float(x), float(y)) for x, y in points)
    if not pts:
        return [(20.0 + i, 100.0) for i in range(count)]
    if len(pts) == 1:
        x, y = pts[0]
        return [(x + i * 0.1, y) for i in range(count)]
    if len(pts) == count:
        return pts
    from .controllers import interpolate

    xs = [p[0] for p in pts]
    lo, hi = xs[0], xs[-1]
    if len(pts) < count:
        # keep the original corners and fill the gaps evenly so the shape is exact
        result = list(pts)
        while len(result) < count:
            gaps = [(result[i + 1][0] - result[i][0], i) for i in range(len(result) - 1)]
            width, i = max(gaps)
            mid = result[i][0] + width / 2
            result.insert(i + 1, (mid, interpolate(pts, mid)))
        return result
    return [(lo + (hi - lo) * i / (count - 1), interpolate(pts, lo + (hi - lo) * i / (count - 1)))
            for i in range(count)]


# ---------------------------------------------------------------------- software sensors
def soft_sensor_report(spec: SoftSensorSpec, values: list[tuple[float, int] | None]) -> bytes:
    """Build the report that sets the device's software sensors.

    ``values[i]`` is ``(value, type)`` for slot i+1, or None to mark the slot unavailable.
    Values are hundredths (°C, % or W).
    """
    buf = bytearray(spec.length)
    buf[0] = spec.report_id
    for i in range(spec.slots):
        item = values[i] if i < len(values) else None
        off = spec.values + 2 * i
        if item is None or item[0] is None:
            _set16(buf, off, SENSOR_NA)
            buf[spec.types + i] = 0
        else:
            raw = int(round(max(-327.0, min(327.0, item[0])) * 100))
            _set16(buf, off, raw)
            buf[spec.types + i] = item[1]
        buf[spec.extra + i] = 0x64
    seal(buf)
    return bytes(buf)


def parse_soft_sensor_report(spec: SoftSensorSpec, data: bytes) -> list[tuple[float, int] | None]:
    out: list[tuple[float, int] | None] = []
    for i in range(spec.slots):
        raw = _get16(data, spec.values + 2 * i)
        typ = data[spec.types + i]
        out.append(None if raw == SENSOR_NA or typ == 0 else
                   ((raw - 0x10000 if raw >= 0x8000 else raw) / 100, typ))
    return out


# ---------------------------------------------------------------------- Leakshield
LEAKSHIELD_FEED_LENGTH = 49
_LS_PUMP, _LS_FLOW, _LS_PUMP_UNIT, _LS_FLOW_UNIT = 1, 3, 33, 34
_LS_UNIT_RPM, _LS_UNIT_DLH = 0x03, 0x0C


def leakshield_feed_report(pump_rpm: float | None, flow_lph: float | None) -> bytes:
    """Pump speed and flow for the Leakshield's pressure model (checksum over bytes 0..48)."""
    buf = bytearray(LEAKSHIELD_FEED_LENGTH + 2)
    buf[0] = 0x04
    for i in range(16):
        _set16(buf, 1 + 2 * i, SENSOR_NA)
    if pump_rpm is not None:
        _set16(buf, _LS_PUMP, int(round(max(0, min(20000, pump_rpm)))))
        buf[_LS_PUMP_UNIT] = _LS_UNIT_RPM
    if flow_lph is not None:
        _set16(buf, _LS_FLOW, int(round(max(0, min(6000, flow_lph * 10)))))
        buf[_LS_FLOW_UNIT] = _LS_UNIT_DLH
    crc = crc16_usb(buf[:LEAKSHIELD_FEED_LENGTH])
    buf[LEAKSHIELD_FEED_LENGTH] = crc >> 8
    buf[LEAKSHIELD_FEED_LENGTH + 1] = crc & 0xFF
    return bytes(buf)


# ---------------------------------------------------------------------- Aquaero / Aquastream XT
def aquaero_set_manual(spec: DeviceSpec, buf: bytearray, index: int, power_percent: float) -> None:
    """Point an Aquaero fan at its own power preset and set the preset (as the kernel driver does)."""
    x = spec.extra_info
    fan = spec.fans[index]
    pct = round(max(0.0, min(100.0, power_percent)) * 100)
    _set16(buf, x["preset_start"] + 2 * index, pct)
    _set16(buf, fan.ctrl + x["src"], x["preset_id_pwm"] + index)
    _set16(buf, fan.ctrl + x["min_power"], 0)
    _set16(buf, fan.ctrl + x["max_power"], 10000)


def aquaero_manual_power(spec: DeviceSpec, buf: bytes, index: int) -> float:
    return _get16(buf, spec.extra_info["preset_start"] + 2 * index) / 100


AQUASTREAMXT_PUMP_MIN, AQUASTREAMXT_PUMP_MAX = 3000, 6000


def aquastreamxt_set_manual(buf: bytearray, index: int, power_percent: float) -> None:
    pct = max(0.0, min(100.0, power_percent))
    if index == 0:
        rpm = round(pct / 100 * 50) * 60 + AQUASTREAMXT_PUMP_MIN
        raw = round(45_000_000 / rpm)
        buf[0x08:0x0A] = raw.to_bytes(2, "little")
        buf[0x03] = 0x14                          # manual pump speed
    else:
        buf[0x1B] = round(pct * 255 / 100)
        buf[0x1A] = 0x01                          # manual fan power
