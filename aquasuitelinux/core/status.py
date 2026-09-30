"""Decode status (sensor) reports into readings, and encode them for the simulator."""

from __future__ import annotations

from .devices import SENSOR_NA, DeviceSpec, SensorSpec

_AQUASTREAMXT_PUMP_CONST = 45_000_000
_AQUASTREAMXT_FAN_CONST = 5_646_000


def _u16(data: bytes, off: int, le: bool) -> int:
    return int.from_bytes(data[off:off + 2], "little" if le else "big")


def _s16(data: bytes, off: int, le: bool) -> int:
    return int.from_bytes(data[off:off + 2], "little" if le else "big", signed=True)


def _value(spec: SensorSpec, data: bytes, le: bool) -> float | None:
    if spec.offset + spec.width > len(data):
        return None
    raw = int.from_bytes(data[spec.offset:spec.offset + spec.width], "little" if le else "big", signed=False)
    if spec.width == 2 and raw == SENSOR_NA:
        return None
    if spec.signed and spec.width == 2 and raw >= 0x8000:
        raw -= 0x10000
    return round(raw * spec.scale, 4)


def device_serial(spec: DeviceSpec, data: bytes) -> str:
    if spec.serial_offset is None or len(data) < spec.serial_offset + 2 * spec.serial_parts:
        return ""
    le = spec.little_endian
    parts = [_u16(data, spec.serial_offset + 2 * i, le) for i in range(spec.serial_parts)]
    return "-".join(f"{p:05d}" for p in parts)


def firmware_version(spec: DeviceSpec, data: bytes) -> int:
    if spec.firmware_offset is None or len(data) < spec.firmware_offset + 2:
        return 0
    return _u16(data, spec.firmware_offset, spec.little_endian)


def power_cycles(spec: DeviceSpec, data: bytes) -> int | None:
    off = spec.power_cycles_offset
    if off is None or len(data) < off + 4:
        return None
    return int.from_bytes(data[off:off + 4], "big")


def parse_status(spec: DeviceSpec, data: bytes) -> list[tuple[str, str, str, float | None, str]]:
    """Return ``(key, label, kind, value, group)`` for everything the report carries."""
    le = spec.little_endian
    out: list[tuple[str, str, str, float | None, str]] = []

    for s in spec.temps:
        out.append((s.key, s.label, s.kind, _value(s, data, le), s.group or "Temperatures"))

    if spec.virtual:
        start, count = spec.virtual
        types = None
        if spec.virtual_types is not None and len(data) >= spec.virtual_types + count:
            types = data[spec.virtual_types:spec.virtual_types + count]
        for i in range(count):
            sub = SensorSpec(f"virt{i + 1}", f"Software sensor {i + 1}", "temperature", start + 2 * i)
            kind = "temperature"
            if types is not None:
                kind = {5: "percent", 7: "power"}.get(types[i], "temperature")
            out.append((sub.key, sub.label, kind, _value(sub, data, le), "Software sensors"))

    if spec.calc_virtual:
        start, count = spec.calc_virtual
        for i in range(count):
            sub = SensorSpec(f"calc{i + 1}", f"Calculated sensor {i + 1}", "temperature", start + 2 * i)
            out.append((sub.key, sub.label, "temperature", _value(sub, data, le), "Software sensors"))

    if spec.aquabus_temps:
        start, count = spec.aquabus_temps
        for i in range(count):
            sub = SensorSpec(f"bus{i + 1}", f"aquabus sensor {i + 1}", "temperature", start + 2 * i)
            out.append((sub.key, sub.label, "temperature", _value(sub, data, le), "aquabus"))

    for s in spec.flows:
        out.append((s.key, s.label, s.kind, _value(s, data, le), s.group or "Flow"))

    if spec.aquabus_flows:
        start, count = spec.aquabus_flows
        for i in range(count):
            sub = SensorSpec(f"busflow{i + 1}", f"aquabus flow {i + 1}", "flow", start + 2 * i, 0.1, signed=False)
            out.append((sub.key, sub.label, "flow", _value(sub, data, le), "aquabus"))

    ff = spec.fan_fields
    for fan in spec.fans:
        if fan.status is None:
            continue
        base = fan.status
        fields = (("rpm", "speed", ff.speed, 1.0), ("percent", "output", ff.percent, 0.01),
                  ("voltage", "voltage", ff.voltage, 0.01), ("current", "current", ff.current, 0.001),
                  ("power", "power", ff.power, 0.01))
        for name, label, rel, scale in fields:
            if rel is None or base + rel + 2 > len(data):
                continue
            kind = "percent" if name == "percent" else name
            value = round(_u16(data, base + rel, le) * scale, 4)
            out.append((f"{fan.key}.{name}", f"{fan.label} {label}", kind, value, "Fans"))

    if spec.family == "aquastreamxt" and len(data) >= 0x20:
        pump_raw = _u16(data, 0x13, True)
        out.append(("pump.rpm", "Pump speed", "rpm",
                    float(round(_AQUASTREAMXT_PUMP_CONST / pump_raw)) if pump_raw else 0.0, "Fans"))
        fan_status = _u16(data, 0x1D, True)
        fan_raw = _u16(data, 0x1B, True)
        fan_rpm = 0.0 if fan_status == 0x4 or not fan_raw else float(round(_AQUASTREAMXT_FAN_CONST / fan_raw))
        out.append(("fan.rpm", "Fan speed", "rpm", fan_rpm, "Fans"))
        out.append(("pump.current", "Pump current", "current",
                    round((round(_u16(data, 0x0B, True) * 176 / 100) - 52) / 1000, 3), "Fans"))
        out.append(("pump.voltage", "Pump voltage", "voltage", round(_u16(data, 0x09, True) / 61, 2), "Fans"))
        out.append(("fan.voltage", "Fan voltage", "voltage", round(_u16(data, 0x07, True) / 63, 2), "Fans"))

    for s in spec.extras:
        out.append((s.key, s.label, s.kind, _value(s, data, le), s.group or "Other"))

    if spec.kind == "highflownext":
        # The power estimate is meaningless without the external temperature sensor.
        temps = {k: v for k, _l, _kd, v, _g in out}
        if temps.get("temp2") is None:
            out = [(k, lb, kd, (None if k == "heat" else v), g) for k, lb, kd, v, g in out]
    return out


# ---------------------------------------------------------------------- encoding (simulator)
def _put(buf: bytearray, off: int, value: int, le: bool, width: int = 2) -> None:
    if off + width > len(buf):
        return
    value &= (1 << (8 * width)) - 1
    buf[off:off + width] = value.to_bytes(width, "little" if le else "big")


def encode_value(spec: SensorSpec, value: float | None) -> int:
    if value is None:
        return SENSOR_NA
    return int(round(value / spec.scale))


def encode_status(spec: DeviceSpec, values: dict[str, float | None], serial: tuple[int, ...] = (12345, 54321),
                  firmware: int = 1030, cycles: int = 42, length: int = 0,
                  virt_types: list[int] | None = None) -> bytes:
    """Build a status report for ``spec`` from ``values`` (keys as produced by parse_status)."""
    from .devices import status_span

    le = spec.little_endian
    buf = bytearray(max(length, status_span(spec) + 4))
    buf[0] = spec.status_id
    if spec.serial_offset is not None:
        for i in range(spec.serial_parts):
            _put(buf, spec.serial_offset + 2 * i, serial[i] if i < len(serial) else 0, le)
    if spec.firmware_offset is not None:
        _put(buf, spec.firmware_offset, firmware, le)
    if spec.power_cycles_offset is not None:
        _put(buf, spec.power_cycles_offset, cycles, False, 4)
    for s in (*spec.temps, *spec.flows, *spec.extras):
        _put(buf, s.offset, encode_value(s, values.get(s.key)), le, s.width)
    ranges = ((spec.virtual, "virt", 0.01), (spec.calc_virtual, "calc", 0.01), (spec.aquabus_temps, "bus", 0.01),
              (spec.aquabus_flows, "busflow", 0.1))
    for rng, prefix, scale in ranges:
        if rng:
            start, count = rng
            for i in range(count):
                v = values.get(f"{prefix}{i + 1}")
                _put(buf, start + 2 * i, SENSOR_NA if v is None else int(round(v / scale)), le)
    if spec.virtual_types is not None and virt_types:
        for i, t in enumerate(virt_types[:spec.virtual[1] if spec.virtual else 0]):
            buf[spec.virtual_types + i] = t
    ff = spec.fan_fields
    for fan in spec.fans:
        if fan.status is None:
            continue
        for name, rel, scale in (("rpm", ff.speed, 1.0), ("percent", ff.percent, 0.01), ("voltage", ff.voltage, 0.01),
                                 ("current", ff.current, 0.001), ("power", ff.power, 0.01)):
            if rel is None:
                continue
            v = values.get(f"{fan.key}.{name}") or 0.0
            _put(buf, fan.status + rel, int(round(v / scale)), le)
    if spec.family == "aquastreamxt":
        rpm = values.get("pump.rpm") or 0
        _put(buf, 0x13, int(round(_AQUASTREAMXT_PUMP_CONST / rpm)) if rpm else 0, True)
        frpm = values.get("fan.rpm") or 0
        _put(buf, 0x1D, 0 if frpm else 0x4, True)
        _put(buf, 0x1B, int(round(_AQUASTREAMXT_FAN_CONST / frpm)) if frpm else 0, True)
    return bytes(buf)
