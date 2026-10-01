"""The control configuration: virtual sensors, controllers, outputs, alarms and profiles.

Stored as JSON. The background service uses ``/etc/aquasuitelinux/config.json``; when the
app runs without the service it uses ``~/.config/aquasuitelinux/config.json``. GUI-only
preferences (theme, window layout, pinned sensors) live separately in ``gui.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
SYSTEM_CONFIG = Path("/etc/aquasuitelinux/config.json")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "aquasuitelinux"


def user_config_path() -> Path:
    return user_config_dir() / "config.json"


def user_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "aquasuitelinux"


def _build(cls, data: Any):
    """Create dataclass ``cls`` from a dict, ignoring unknown keys and keeping defaults."""
    if isinstance(data, cls):
        return data
    if not isinstance(data, dict):
        return cls()
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


DEFAULT_CURVE = [[25.0, 20.0], [30.0, 35.0], [35.0, 60.0], [40.0, 100.0]]
DELTA_T_CURVE = [[2.0, 20.0], [4.0, 30.0], [6.0, 45.0], [8.0, 70.0], [10.0, 100.0]]

VIRTUAL_KINDS = {
    "difference": "Difference (A − B), e.g. Delta T",
    "average": "Average",
    "min": "Minimum",
    "max": "Maximum",
    "sum": "Sum",
    "scale": "Scale and offset (A × factor + offset)",
    "constant": "Constant value",
    "expression": "Formula",
    "heat_load": "Heat load from flow and Delta T",
}

CONTROLLER_KINDS = {
    "curve": "Curve controller",
    "target": "Target value controller (PID)",
    "two_point": "Two-point controller",
    "fixed": "Fixed power",
    "follow": "Follow another output",
    "mix": "Combine controllers",
}


@dataclass
class VirtualSensorConfig:
    id: str = field(default_factory=new_id)
    name: str = "Virtual sensor"
    kind: str = "difference"
    inputs: list[str] = field(default_factory=list)
    unit: str = ""                 # empty: derived from the inputs
    factor: float = 1.0
    offset: float = 0.0
    value: float = 0.0
    expression: str = ""
    smoothing: float = 0.0         # seconds; exponential moving average
    coolant_factor: float = 1.0    # heat load: 1.0 water, ~0.93 for 20 % glycol


@dataclass
class ControllerConfig:
    id: str = field(default_factory=new_id)
    name: str = "Controller"
    kind: str = "curve"
    input: str = ""
    points: list[list[float]] = field(default_factory=lambda: [list(p) for p in DEFAULT_CURVE])
    hysteresis: float = 0.0
    target: float = 30.0
    kp: float = 8.0
    ki: float = 0.08
    kd: float = 0.0
    on_above: float = 40.0
    off_below: float = 35.0
    on_power: float = 100.0
    off_power: float = 0.0
    power: float = 50.0
    follow: str = ""
    sources: list[str] = field(default_factory=list)
    mix: str = "max"


@dataclass
class OutputConfig:
    name: str = ""
    controller: str = ""           # "" = not managed; the device keeps its own settings
    placement: str = "auto"        # auto | device | software
    min_power: float = 5.0
    max_power: float = 100.0
    hold_min: bool = True
    start_boost: bool = False
    fallback: float = 100.0
    ramp_up: float = 0.0           # %/s in software control, 0 = immediate
    ramp_down: float = 0.0


@dataclass
class FeedConfig:
    """Send a sensor's value to a software sensor slot on a device (1-based slot)."""

    device: str = ""
    slot: int = 1
    source: str = ""


@dataclass
class LeakshieldFeedConfig:
    device: str = ""
    pump: str = ""
    flow: str = ""


@dataclass
class AlarmConfig:
    id: str = field(default_factory=new_id)
    name: str = "Alarm"
    sensor: str = ""
    condition: str = "above"       # above | below | missing
    threshold: float = 45.0
    delay: float = 5.0
    actions: list[str] = field(default_factory=lambda: ["notify"])   # notify | fans_max | command | shutdown
    command: str = ""
    enabled: bool = True


@dataclass
class ProfileConfig:
    id: str = field(default_factory=new_id)
    name: str = "Profile"
    assignments: dict[str, str] = field(default_factory=dict)       # output id -> controller id


@dataclass
class DeviceConfig:
    name: str = ""
    sensor_names: dict[str, str] = field(default_factory=dict)
    hidden: list[str] = field(default_factory=list)
    write_interval: float = 2.0    # minimum seconds between settings writes in software control


@dataclass
class EngineSettings:
    interval: float = 1.0
    history_minutes: int = 60
    exit_action: str = "fallback"  # fallback | keep
    allowed_groups: list[str] = field(default_factory=lambda: ["wheel", "sudo", "admin", "aquasuite"])
    log_enabled: bool = False
    log_interval: float = 10.0
    log_dir: str = ""
    system_sensors: bool = True
    nvidia: bool = True
    # Send sensor values (a Delta T, the CPU temperature, …) to the devices' software sensors so a
    # QUADRO/OCTO/D5 NEXT runs those curves itself, and pump speed/flow to a LEAKSHIELD. Off by
    # default: it uses the devices' USB bulk endpoint, which isn't confirmed on real hardware yet.
    # When off, curves on such inputs run in software.
    device_feeds: bool = False


@dataclass
class Config:
    version: int = CONFIG_VERSION
    settings: EngineSettings = field(default_factory=EngineSettings)
    devices: dict[str, DeviceConfig] = field(default_factory=dict)
    virtual_sensors: list[VirtualSensorConfig] = field(default_factory=list)
    controllers: list[ControllerConfig] = field(default_factory=list)
    outputs: dict[str, OutputConfig] = field(default_factory=dict)
    feeds: list[FeedConfig] = field(default_factory=list)
    leakshield: list[LeakshieldFeedConfig] = field(default_factory=list)
    alarms: list[AlarmConfig] = field(default_factory=list)
    profiles: list[ProfileConfig] = field(default_factory=list)
    active_profile: str = ""

    # ------------------------------------------------------------------ (de)serialisation
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        data = migrate(dict(data or {}))
        cfg = cls()
        cfg.version = CONFIG_VERSION
        cfg.settings = _build(EngineSettings, data.get("settings"))
        cfg.devices = {k: _build(DeviceConfig, v) for k, v in (data.get("devices") or {}).items()}
        cfg.virtual_sensors = [_build(VirtualSensorConfig, v) for v in data.get("virtual_sensors") or []]
        cfg.controllers = [_build(ControllerConfig, v) for v in data.get("controllers") or []]
        cfg.outputs = {k: _build(OutputConfig, v) for k, v in (data.get("outputs") or {}).items()}
        cfg.feeds = [_build(FeedConfig, v) for v in data.get("feeds") or []]
        cfg.leakshield = [_build(LeakshieldFeedConfig, v) for v in data.get("leakshield") or []]
        cfg.alarms = [_build(AlarmConfig, v) for v in data.get("alarms") or []]
        cfg.profiles = [_build(ProfileConfig, v) for v in data.get("profiles") or []]
        cfg.active_profile = str(data.get("active_profile") or "")
        return cfg

    def copy(self) -> Config:
        return Config.from_dict(json.loads(json.dumps(self.to_dict())))

    def has_setup(self) -> bool:
        """Whether this configuration makes the engine do anything (a fresh one doesn't)."""
        return bool(self.virtual_sensors or self.controllers or self.alarms or self.feeds or self.leakshield
                    or self.profiles or any(o.controller for o in self.outputs.values()))

    def setup_summary(self) -> str:
        """E.g. "1 virtual sensor, 2 controllers, 3 fans assigned"."""
        def n(count: int, one: str, many: str) -> str:
            return f"{count} {one if count == 1 else many}"
        assigned = sum(1 for o in self.outputs.values() if o.controller)
        parts = [n(len(self.virtual_sensors), "virtual sensor", "virtual sensors"),
                 n(len(self.controllers), "controller", "controllers"),
                 n(assigned, "fan assigned", "fans assigned")]
        if self.alarms:
            parts.append(n(len(self.alarms), "alarm", "alarms"))
        return ", ".join(parts)

    # ------------------------------------------------------------------ lookups
    def controller(self, cid: str) -> ControllerConfig | None:
        return next((c for c in self.controllers if c.id == cid), None)

    def virtual(self, vid: str) -> VirtualSensorConfig | None:
        return next((v for v in self.virtual_sensors if v.id == vid), None)

    def profile(self, pid_or_name: str) -> ProfileConfig | None:
        return next((p for p in self.profiles if p.id == pid_or_name or p.name.lower() == pid_or_name.lower()),
                    None)

    def output(self, oid: str) -> OutputConfig:
        return self.outputs.setdefault(oid, OutputConfig())

    def device(self, key: str) -> DeviceConfig:
        return self.devices.setdefault(key, DeviceConfig())

    def assignment(self, output_id: str) -> str:
        """The controller that drives an output, taking the active profile into account."""
        prof = self.profile(self.active_profile) if self.active_profile else None
        if prof and output_id in prof.assignments:
            return prof.assignments[output_id]
        oc = self.outputs.get(output_id)
        return oc.controller if oc else ""

    def remove_controller(self, cid: str) -> None:
        self.controllers = [c for c in self.controllers if c.id != cid]
        for oc in self.outputs.values():
            if oc.controller == cid:
                oc.controller = ""
        for prof in self.profiles:
            prof.assignments = {k: v for k, v in prof.assignments.items() if v != cid}
        for c in self.controllers:
            c.sources = [s for s in c.sources if s != cid]

    def remove_virtual(self, vid: str) -> None:
        self.virtual_sensors = [v for v in self.virtual_sensors if v.id != vid]


def migrate(data: dict) -> dict:
    """Upgrade older configuration layouts in place (none yet; version 1 is the first)."""
    data.setdefault("version", CONFIG_VERSION)
    return data


def load(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    try:
        return Config.from_dict(json.loads(text))
    except (ValueError, TypeError) as exc:
        from .errors import ConfigError
        raise ConfigError(f"{path} is not a valid configuration: {exc}") from exc


def save(cfg: Config, path: Path, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
