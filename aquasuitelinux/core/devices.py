"""What every supported Aquacomputer device reports and where.

The offsets come from the Linux ``aquacomputer_d5next`` hwmon driver (mainline and the
extended out-of-tree version), liquidctl, and captured reports; see docs/PROTOCOL.md.
Offsets are relative to the start of the HID report, whose first byte is the report ID.

Modern devices (Quadro, Octo, D5 Next, Farbwerk 360, High Flow Next, Leakshield,
Aquastream Ultimate, Farbwerk) send a big-endian status report (ID 0x01) every second
and keep their settings in a feature report (ID 0x03) sealed with CRC-16/USB. The
Aquaero uses its own control report (ID 0x0B, no checksum). "Legacy" devices (Aquastream
XT, Poweradjust 3, High Flow USB / MPS) must be polled with GET_FEATURE and use
little-endian values.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VENDOR_ID = 0x0C70

# Sensor kinds and their display units.
UNITS = {
    "temperature": "°C",
    "delta": "K",
    "flow": "L/h",
    "rpm": "rpm",
    "percent": "%",
    "power": "W",
    "voltage": "V",
    "current": "A",
    "pressure": "mbar",
    "conductivity": "µS/cm",
    "volume": "ml",
    "number": "",
}

SENSOR_NA = 0x7FFF

# The "save" report the official software sends after each control report.
SAVE_REPORT = bytes([0x02, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x34, 0xC6])
AQUAERO_SAVE_REPORT = bytes([0x06, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00])
AQUASTREAMXT_SAVE_REPORT = bytes([0x02, 0x05, 0x00, 0x00])


@dataclass(frozen=True)
class SensorSpec:
    """One value in the status report."""

    key: str
    label: str
    kind: str
    offset: int
    scale: float = 0.01          # raw * scale = value in the kind's unit
    signed: bool = True
    width: int = 2               # bytes
    group: str = ""


@dataclass(frozen=True)
class FanFields:
    """Relative offsets inside a fan/pump substructure of the status report."""

    percent: int | None = 0x00
    voltage: int | None = 0x02
    current: int | None = 0x04
    power: int | None = 0x06
    speed: int | None = 0x08


GENERAL_FAN = FanFields()
AQUAERO_FAN = FanFields(percent=None, voltage=0x04, current=0x06, power=0x08, speed=0x00)
AQUASTREAMULT_FAN = FanFields(percent=None, voltage=0x02, current=0x00, power=0x04, speed=0x06)


@dataclass(frozen=True)
class FanSpec:
    """A fan or pump output: where its readings are and where it is configured."""

    key: str
    label: str
    status: int | None                  # fan substructure in the status report
    ctrl: int | None = None             # control substructure (mode byte) in the control report
    setup_flags: int | None = None      # "hold min power" / "start boost" flag byte
    setup_min: int | None = None        # min power; max power is +2 and fallback power +4
    pump: bool = False


@dataclass(frozen=True)
class SoftSensorSpec:
    """The output report that feeds values into the device's software (virtual) sensors."""

    report_id: int = 0x04
    slots: int = 16
    length: int = 0x43
    values: int = 0x01
    types: int = 0x21
    extra: int = 0x31              # 16 bytes the official software sets to 0x64
    verified: bool = False         # layout confirmed from a capture of this device


# Software-sensor value types understood by the devices.
SOFT_DISABLED, SOFT_TEMPERATURE, SOFT_PERCENT, SOFT_POWER = 0, 3, 5, 7


@dataclass(frozen=True)
class DeviceSpec:
    kind: str
    name: str
    product_id: int
    family: str = "standard"                 # standard | aquaero | aquastreamxt | legacy
    status_id: int = 0x01
    status_via_feature: bool = False         # legacy devices are polled with GET_FEATURE
    status_length: int = 0                   # length to request for polled status reports
    little_endian: bool = False
    serial_offset: int | None = 0x03
    serial_parts: int = 2
    firmware_offset: int | None = 0x0D
    power_cycles_offset: int | None = None
    temps: tuple[SensorSpec, ...] = ()
    virtual: tuple[int, int] | None = None   # (start, count) of software sensors
    virtual_types: int | None = None         # offset of the per-slot type bytes
    calc_virtual: tuple[int, int] | None = None
    aquabus_temps: tuple[int, int] | None = None
    flows: tuple[SensorSpec, ...] = ()
    aquabus_flows: tuple[int, int] | None = None
    fans: tuple[FanSpec, ...] = ()
    fan_fields: FanFields = GENERAL_FAN
    extras: tuple[SensorSpec, ...] = ()
    ctrl_id: int | None = None
    ctrl_length: int = 0
    ctrl_crc: bool = True
    save_report: bytes = SAVE_REPORT
    temp_offsets: int | None = None          # control report offset of the temperature offsets
    num_temp_offsets: int = 0
    flow_pulses: int | None = None           # control report offset of flow sensor pulses per litre
    device_curves: bool = False              # fans can run curve/PID/follow modes on the device
    follow: bool = False                     # "follow fan N" modes (Octo, Quadro)
    soft_sensors: SoftSensorSpec | None = None
    leakshield_feed: bool = False
    multi_interface: bool = False            # several HID interfaces share the product ID
    notes: str = ""
    extra_info: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def controllable(self) -> bool:
        return any(f.ctrl is not None for f in self.fans)

    def fan(self, key: str) -> FanSpec | None:
        for f in self.fans:
            if f.key == key:
                return f
        return None

    def fan_index(self, key: str) -> int:
        for i, f in enumerate(self.fans):
            if f.key == key:
                return i
        return -1


def _temps(start: int, count: int, labels: list[str] | None = None, step: int = 2) -> tuple[SensorSpec, ...]:
    return tuple(
        SensorSpec(f"temp{i + 1}", (labels[i] if labels else f"Sensor {i + 1}"), "temperature",
                   start + i * step, group="Temperatures")
        for i in range(count)
    )


def _fans(status: list[int], ctrl: list[int] | None, flags: list[int] | None, mins: list[int] | None,
          prefix: str = "fan", label: str = "Fan") -> tuple[FanSpec, ...]:
    out = []
    for i, st in enumerate(status):
        out.append(FanSpec(
            f"{prefix}{i + 1}", f"{label} {i + 1}", st,
            ctrl[i] if ctrl else None,
            flags[i] if flags else None,
            mins[i] if mins else None,
        ))
    return tuple(out)


QUADRO = DeviceSpec(
    kind="quadro", name="QUADRO", product_id=0xF00D,
    power_cycles_offset=0x18,
    temps=_temps(0x34, 4),
    virtual=(0x3C, 16), virtual_types=0x5C,
    flows=(SensorSpec("flow", "Flow sensor", "flow", 0x6E, 0.1, signed=False, group="Flow"),),
    fans=_fans([0x70, 0x7D, 0x8A, 0x97], [0x36, 0x8B, 0xE0, 0x135],
               [0x12, 0x1B, 0x24, 0x2D], [0x13, 0x1C, 0x25, 0x2E]),
    extras=(SensorSpec("vcc", "Supply voltage", "voltage", 0x6C, signed=False, group="Power"),),
    ctrl_id=0x03, ctrl_length=0x3C1,
    temp_offsets=0x0A, num_temp_offsets=4, flow_pulses=0x06,
    device_curves=True, follow=True,
    soft_sensors=SoftSensorSpec(verified=True),
    notes="4 PWM fan outputs, 4 temperature sensors, flow sensor, 16 software sensors.",
)

OCTO = DeviceSpec(
    kind="octo", name="OCTO", product_id=0xF011,
    power_cycles_offset=0x18,
    temps=_temps(0x3D, 4),
    virtual=(0x45, 16),
    flows=(SensorSpec("flow", "Flow sensor", "flow", 0x7B, 0.1, signed=False, group="Flow"),),
    fans=_fans([0x7D, 0x8A, 0x97, 0xA4, 0xB1, 0xBE, 0xCB, 0xD8],
               [0x5A, 0xAF, 0x104, 0x159, 0x1AE, 0x203, 0x258, 0x2AD],
               [0x12, 0x1B, 0x24, 0x2D, 0x36, 0x3F, 0x48, 0x51],
               [0x13, 0x1C, 0x25, 0x2E, 0x37, 0x40, 0x49, 0x52]),
    ctrl_id=0x03, ctrl_length=0x65F,
    temp_offsets=0x0A, num_temp_offsets=4, flow_pulses=0x06,
    device_curves=True, follow=True,
    soft_sensors=SoftSensorSpec(verified=False),
    notes="8 PWM fan outputs, 4 temperature sensors, flow sensor, 16 software sensors.",
)

D5NEXT = DeviceSpec(
    kind="d5next", name="D5 NEXT", product_id=0xF00E,
    power_cycles_offset=0x18,
    temps=(SensorSpec("temp1", "Coolant temperature", "temperature", 0x57, group="Temperatures"),),
    virtual=(0x3F, 8),
    flows=(SensorSpec("flow", "Flow sensor", "flow", 0x59, 0.1, signed=False, group="Flow"),),
    fans=(
        FanSpec("pump", "Pump", 0x6C, 0x96, None, 0x39, pump=True),
        FanSpec("fan", "Fan", 0x5F, 0x41, 0x2F, 0x30),
    ),
    extras=(
        SensorSpec("vcc12", "+12 V", "voltage", 0x37, signed=False, group="Power"),
        SensorSpec("vcc5", "+5 V", "voltage", 0x39, signed=False, group="Power"),
    ),
    ctrl_id=0x03, ctrl_length=0x329,
    temp_offsets=0x2D, num_temp_offsets=1,
    device_curves=True,
    notes="D5 pump with a fan output, coolant temperature, 8 software sensors.",
)

FARBWERK360 = DeviceSpec(
    kind="farbwerk360", name="farbwerk 360", product_id=0xF010,
    temps=_temps(0x32, 4),
    virtual=(0x3A, 16),
    ctrl_id=0x03, ctrl_length=0x682,
    temp_offsets=0x08, num_temp_offsets=4,
    soft_sensors=SoftSensorSpec(verified=False),
    notes="RGB controller with 4 temperature sensors and 16 software sensors.",
)

FARBWERK = DeviceSpec(
    kind="farbwerk", name="farbwerk", product_id=0xF00A,
    temps=_temps(0x2F, 4),
    notes="RGB controller with 4 temperature sensors.",
)

HIGHFLOWNEXT = DeviceSpec(
    kind="highflownext", name="high flow NEXT", product_id=0xF012,
    power_cycles_offset=0x18,
    temps=(
        SensorSpec("temp1", "Coolant temperature", "temperature", 85, group="Temperatures"),
        SensorSpec("temp2", "External sensor", "temperature", 87, group="Temperatures"),
    ),
    flows=(SensorSpec("flow", "Flow", "flow", 81, 0.1, signed=False, group="Flow"),),
    extras=(
        SensorSpec("quality", "Water quality", "percent", 89, 1.0, signed=False, group="Coolant"),
        SensorSpec("heat", "Dissipated power", "power", 91, 1.0, group="Coolant"),
        SensorSpec("conductivity", "Conductivity", "conductivity", 95, 0.001, signed=False, group="Coolant"),
        SensorSpec("vcc5", "+5 V", "voltage", 97, signed=False, group="Power"),
        SensorSpec("vcc5usb", "+5 V (USB)", "voltage", 99, signed=False, group="Power"),
    ),
    notes="Flow sensor with coolant temperature, water quality and conductivity.",
)

LEAKSHIELD = DeviceSpec(
    kind="leakshield", name="LEAKSHIELD", product_id=0xF014,
    temps=(
        SensorSpec("temp1", "Temperature 1", "temperature", 265, group="Temperatures"),
        SensorSpec("temp2", "Temperature 2", "temperature", 287, group="Temperatures"),
    ),
    extras=(
        SensorSpec("pressure", "Pressure", "pressure", 285, 0.1, group="Pressure"),
        SensorSpec("pressure_min", "Pressure min", "pressure", 291, 0.1, signed=False, group="Pressure"),
        SensorSpec("pressure_target", "Pressure target", "pressure", 293, 0.1, signed=False, group="Pressure"),
        SensorSpec("pressure_max", "Pressure max", "pressure", 295, 0.1, signed=False, group="Pressure"),
        SensorSpec("pump_in", "Pump speed (provided)", "rpm", 101, 1.0, signed=False, group="Provided data"),
        SensorSpec("flow_in", "Flow (provided)", "flow", 111, 0.1, signed=False, group="Provided data"),
        SensorSpec("reservoir", "Reservoir volume", "volume", 313, 1.0, signed=False, group="Coolant"),
        SensorSpec("filled", "Reservoir filled", "volume", 311, 1.0, signed=False, group="Coolant"),
    ),
    leakshield_feed=True, multi_interface=True,
    notes="Leak prevention: loop pressure and reservoir level. Can be fed pump speed and flow.",
)

AQUASTREAMULT = DeviceSpec(
    kind="aquastreamult", name="aquastream ULTIMATE", product_id=0xF00B,
    temps=(
        SensorSpec("temp1", "Coolant temperature", "temperature", 0x2D, group="Temperatures"),
        SensorSpec("temp2", "External sensor", "temperature", 0x2F, group="Temperatures"),
    ),
    flows=(SensorSpec("flow", "Flow", "flow", 0x37, 0.1, signed=False, group="Flow"),),
    fans=(FanSpec("fan", "Fan", 0x41),),
    fan_fields=AQUASTREAMULT_FAN,
    extras=(
        SensorSpec("pump.rpm", "Pump speed", "rpm", 0x51, 1.0, signed=False, group="Pump"),
        SensorSpec("pump.voltage", "Pump voltage", "voltage", 0x3D, signed=False, group="Pump"),
        SensorSpec("pump.current", "Pump current", "current", 0x53, 0.001, signed=False, group="Pump"),
        SensorSpec("pump.power", "Pump power", "power", 0x55, signed=False, group="Pump"),
        SensorSpec("pressure", "Pressure", "pressure", 0x57, 1.0, signed=False, group="Pump"),
    ),
    notes="Pump with coolant temperature, pressure and flow readings.",
)

AQUAERO = DeviceSpec(
    kind="aquaero", name="aquaero 5/6", product_id=0xF001, family="aquaero",
    serial_offset=0x07, firmware_offset=0x0B,
    temps=_temps(0x65, 8),
    virtual=(0x85, 8),
    calc_virtual=(0x95, 4),
    aquabus_temps=(0x9D, 20),
    flows=(
        SensorSpec("flow", "Flow sensor 1", "flow", 0xF9, 0.1, signed=False, group="Flow"),
        SensorSpec("flow2", "Flow sensor 2", "flow", 0xFB, 0.1, signed=False, group="Flow"),
    ),
    aquabus_flows=(0xFD, 12),
    fans=_fans([0x167, 0x173, 0x17F, 0x18B], [0x20C, 0x220, 0x234, 0x248], None, None),
    fan_fields=AQUAERO_FAN,
    ctrl_id=0x0B, ctrl_length=0xA93, ctrl_crc=False, save_report=AQUAERO_SAVE_REPORT,
    temp_offsets=0xDB, num_temp_offsets=8,
    multi_interface=True,
    notes="Fan controller with 4 outputs, 8 sensors, 2 flow sensors and aquabus devices.",
    extra_info={"preset_start": 0x55C, "preset_id_pwm": 0x5C, "src": 0x10, "min_power": 0x04,
                "max_power": 0x06, "mode": 0x0F},
)

AQUASTREAMXT = DeviceSpec(
    kind="aquastreamxt", name="aquastream XT", product_id=0xF0B6, family="aquastreamxt",
    status_id=0x04, status_via_feature=True, status_length=0x42, little_endian=True,
    serial_offset=0x3A, serial_parts=1, firmware_offset=0x32,
    temps=(
        SensorSpec("temp1", "Fan IC temperature", "temperature", 0x0D, group="Temperatures"),
        SensorSpec("temp2", "External sensor", "temperature", 0x0F, group="Temperatures"),
        SensorSpec("temp3", "Coolant temperature", "temperature", 0x11, group="Temperatures"),
    ),
    fans=(
        FanSpec("pump", "Pump", None, 0x08, pump=True),
        FanSpec("fan", "Fan", None, 0x1B),
    ),
    ctrl_id=0x06, ctrl_length=0x34, ctrl_crc=False, save_report=AQUASTREAMXT_SAVE_REPORT,
    notes="Pump with fan output (USB). Pump speed is set as 3000-6000 rpm.",
)

POWERADJUST3 = DeviceSpec(
    kind="poweradjust3", name="poweradjust 3", product_id=0xF0BD, family="legacy",
    status_id=0x03, status_via_feature=True, status_length=0x32, little_endian=True,
    serial_offset=0x27, serial_parts=1, firmware_offset=0x21,
    temps=(
        SensorSpec("temp1", "Fan IC temperature", "temperature", 0x01, group="Temperatures"),
        SensorSpec("temp2", "External sensor", "temperature", 0x03, group="Temperatures"),
    ),
    flows=(SensorSpec("flow", "Flow meter", "flow", 0x09, 0.01, signed=False, group="Flow"),),
    extras=(
        SensorSpec("fan.rpm", "Fan speed", "rpm", 0x0B, 1.0, signed=False, group="Fans"),
        SensorSpec("fan.voltage", "Fan voltage", "voltage", 0x07, signed=False, group="Fans"),
        SensorSpec("fan.current", "Fan current", "current", 0x05, 0.001, signed=False, group="Fans"),
    ),
    notes="Single-channel fan controller (monitoring only).",
)

HIGHFLOW = DeviceSpec(
    kind="highflow", name="high flow USB / mps flow", product_id=0xF003, family="legacy",
    status_id=0x02, status_via_feature=True, status_length=0x76, little_endian=True,
    serial_offset=0x09, serial_parts=1, firmware_offset=0x03,
    temps=(
        SensorSpec("temp1", "External sensor", "temperature", 0x2B, group="Temperatures"),
        SensorSpec("temp2", "Internal sensor", "temperature", 0x2D, group="Temperatures"),
    ),
    flows=(SensorSpec("flow", "Flow", "flow", 0x23, 0.1, signed=False, group="Flow"),),
    notes="USB flow meter with two temperature sensors.",
)

ALL_SPECS: tuple[DeviceSpec, ...] = (
    QUADRO, OCTO, D5NEXT, FARBWERK360, FARBWERK, HIGHFLOWNEXT, LEAKSHIELD, AQUASTREAMULT,
    AQUAERO, AQUASTREAMXT, POWERADJUST3, HIGHFLOW,
)
BY_PRODUCT = {s.product_id: s for s in ALL_SPECS}
BY_KIND = {s.kind: s for s in ALL_SPECS}

# hwmon "name" attribute of the aquacomputer_d5next driver for each device kind
HWMON_NAMES = {
    "d5next": "d5next", "farbwerk": "farbwerk", "farbwerk360": "farbwerk360", "octo": "octo",
    "quadro": "quadro", "highflownext": "highflownext", "leakshield": "leakshield",
    "aquastreamxt": "aquastreamxt", "aquaero": "aquaero", "aquastreamultimate": "aquastreamult",
    "poweradjust3": "poweradjust3", "highflow": "highflow",
}


def status_span(spec: DeviceSpec) -> int:
    """The minimum status report length that holds every value we parse."""
    ends = [0]
    for s in (*spec.temps, *spec.flows, *spec.extras):
        ends.append(s.offset + s.width)
    for rng in (spec.virtual, spec.calc_virtual, spec.aquabus_temps, spec.aquabus_flows):
        if rng:
            ends.append(rng[0] + rng[1] * 2)
    for f in spec.fans:
        if f.status is not None:
            ends.append(f.status + 0x0A)
    if spec.firmware_offset:
        ends.append(spec.firmware_offset + 2)
    return max(ends)
