"""Plain data passed between the devices, the engine, the service and the GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .devices import UNITS


@dataclass
class Reading:
    """One sensor value. ``id`` is globally unique: ``<device key>/<sensor key>``."""

    id: str
    label: str
    kind: str
    value: float | None
    group: str = ""
    source: str = ""            # device key, "virtual" or "system"
    unit: str = ""

    def __post_init__(self) -> None:
        if not self.unit:
            self.unit = UNITS.get(self.kind, "")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Reading:
        return cls(d["id"], d.get("label", d["id"]), d.get("kind", "number"), d.get("value"),
                   d.get("group", ""), d.get("source", ""), d.get("unit", ""))


@dataclass
class FanControl:
    """The control substructure of one fan output (standard family devices).

    Temperatures are in hundredths of a degree and powers in hundredths of a percent,
    exactly as the device stores them.
    """

    mode: int = 0                    # 0 manual, 1 PID (target temperature), 2 curve, 3+n follow fan n+1
    pwm: int = 0                     # manual power, 0..10000
    source: int = 0xFFFF             # sensor index for PID/curve; 0xFFFF = none
    pid: list[int] = field(default_factory=lambda: [3500, 1400, 1200, 0, 40, 20])
    curve_start: int = 2800
    curve_temps: list[int] = field(default_factory=lambda: [2700 + i * 87 for i in range(16)])
    curve_powers: list[int] = field(default_factory=lambda: [round(i * 10000 / 15) for i in range(16)])


@dataclass
class FanSetup:
    """Per-output limits that apply to every control mode."""

    hold_min: bool = False
    start_boost: bool = False
    flags_raw: int = 0
    min_power: int = 0               # hundredths of a percent
    max_power: int = 10000
    fallback: int = 10000


@dataclass
class DeviceInfo:
    key: str
    kind: str
    name: str
    serial: str
    firmware: int
    backend: str                     # hidraw | hwmon | simulated
    path: str = ""
    outputs: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    power_cycles: int | None = None
    notes: str = ""
    online: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
