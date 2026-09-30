"""A connected Aquacomputer device: readings, settings and outputs.

``HidDevice`` speaks the HID protocol over any transport with the hidraw interface
(``read_input``, ``get_feature``, ``set_feature``, ``write_output``): the real
``HidrawTransport`` or the simulator's transport, so demo mode runs the same code.
``HwmonDevice`` is the fallback when only the kernel driver's sysfs files are usable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from . import control
from .devices import BY_KIND, HWMON_NAMES, DeviceSpec
from .errors import DeviceError, NotSupported
from .model import DeviceInfo, Reading
from .status import device_serial, firmware_version, parse_status, power_cycles

log = logging.getLogger(__name__)

STALE_AFTER = 5.0          # seconds without a status report before readings are dropped
CTRL_CACHE_SECONDS = 30.0  # re-read the control report at most this often unless we wrote it


class BaseDevice:
    spec: DeviceSpec
    backend = ""
    path = ""

    def __init__(self) -> None:
        self.serial = ""
        self.firmware = 0
        self.cycles: int | None = None
        self.online = True
        self.error = ""
        self.writes = 0                 # control report writes since start (flash wear awareness)
        self.hid_id = ""                # sysfs HID device name, shared by the hidraw and hwmon views
        self.lock = threading.RLock()

    # identity ------------------------------------------------------------
    @property
    def key(self) -> str:
        return f"{self.spec.kind}-{self.serial}" if self.serial else self.spec.kind

    def info(self) -> DeviceInfo:
        return DeviceInfo(self.key, self.spec.kind, self.spec.name, self.serial, self.firmware, self.backend,
                          self.path, [f.key for f in self.spec.fans if self.can_control(f.key)],
                          sorted(self.capabilities()), self.cycles, self.spec.notes, self.online, self.error)

    # capabilities ----------------------------------------------------------
    def capabilities(self) -> set[str]:
        return set()

    def can_control(self, output_key: str) -> bool:
        return False

    # I/O -------------------------------------------------------------------
    def poll(self) -> list[Reading]:
        raise NotImplementedError

    def set_manual(self, powers: dict[str, float]) -> None:
        raise NotSupported(f"{self.spec.name} outputs cannot be controlled with this backend")

    def read_control(self, fresh: bool = False) -> bytes:
        raise NotSupported("settings are not accessible with this backend")

    def write_control(self, data: bytes) -> None:
        raise NotSupported("settings are not accessible with this backend")

    def push_soft_sensors(self, values: list[tuple[float, int] | None]) -> None:
        raise NotSupported("software sensors are not accessible with this backend")

    def push_leakshield(self, pump_rpm: float | None, flow: float | None) -> None:
        raise NotSupported("not a Leakshield, or not accessible with this backend")

    def raw_status(self) -> bytes | None:
        return None

    def close(self) -> None:
        pass

    def _readings(self, values) -> list[Reading]:
        return [Reading(f"{self.key}/{k}", label, kind, value, group, self.key)
                for k, label, kind, value, group in values]


class HidDevice(BaseDevice):
    """A device reached through a hidraw-style transport."""

    def __init__(self, spec: DeviceSpec, transport, path: str = "", backend: str = "hidraw"):
        super().__init__()
        self.spec = spec
        self.transport = transport
        self.path = path
        self.backend = backend
        self._status: bytes | None = None
        self._status_time = 0.0
        self._ctrl: bytes | None = None
        self._ctrl_time = 0.0
        self._open()

    def _open(self) -> None:
        deadline = time.monotonic() + 2.5
        while self._status is None and time.monotonic() < deadline:
            self._fetch_status(timeout=0.5)
        if self._status is None:
            raise DeviceError(f"{self.spec.name} at {self.path or 'unknown path'} sent no sensor data")
        self.serial = device_serial(self.spec, self._status)
        self.firmware = firmware_version(self.spec, self._status)
        self.cycles = power_cycles(self.spec, self._status)

    def _fetch_status(self, timeout: float = 0.0) -> None:
        spec = self.spec
        if spec.status_via_feature:
            data = self.transport.get_feature(spec.status_id, spec.status_length)
            if data and data[0] == spec.status_id:
                self._status, self._status_time = data, time.monotonic()
            return
        for report in self.transport.read_input(timeout):
            # parse_status copes with short reports, so any status report beats none
            if report and report[0] == spec.status_id:
                self._status, self._status_time = report, time.monotonic()

    # ----------------------------------------------------------------- capabilities
    def capabilities(self) -> set[str]:
        caps: set[str] = {"monitor"}
        s = self.spec
        if s.controllable:
            caps.add("manual")
        if s.family == "standard" and s.ctrl_id is not None:
            caps.add("settings")
            caps.add("backup")
        if s.device_curves:
            caps.add("device_curves")
        if s.follow:
            caps.add("follow")
        if s.soft_sensors:
            caps.add("soft_sensors")
        if s.temp_offsets is not None and s.family == "standard":
            caps.add("temp_offsets")
        if s.flow_pulses is not None:
            caps.add("flow_pulses")
        if s.leakshield_feed:
            caps.add("leakshield_feed")
        return caps

    def can_control(self, output_key: str) -> bool:
        f = self.spec.fan(output_key)
        return f is not None and f.ctrl is not None

    # ----------------------------------------------------------------- reading
    def poll(self) -> list[Reading]:
        with self.lock:
            try:
                self._fetch_status()
                self.error = ""
            except DeviceError as exc:
                self.online = False
                self.error = str(exc)
                raise
            fresh = self._status is not None and time.monotonic() - self._status_time < STALE_AFTER
            self.online = fresh
            if not fresh:
                self.error = "no sensor data for a few seconds"
                values = [(k, lb, kd, None, g) for k, lb, kd, _v, g in parse_status(self.spec, self._status or b"")]
                return self._readings(values)
            self.cycles = power_cycles(self.spec, self._status)
            return self._readings(parse_status(self.spec, self._status))

    def raw_status(self) -> bytes | None:
        return self._status

    # ----------------------------------------------------------------- settings
    def read_control(self, fresh: bool = False) -> bytes:
        s = self.spec
        if s.ctrl_id is None:
            raise NotSupported(f"{s.name} has no settings report")
        with self.lock:
            if fresh or self._ctrl is None or time.monotonic() - self._ctrl_time > CTRL_CACHE_SECONDS:
                data = self.transport.get_feature(s.ctrl_id, s.ctrl_length)
                if len(data) < s.ctrl_length or data[0] != s.ctrl_id:
                    raise DeviceError(f"{s.name} returned an unexpected settings report ({len(data)} bytes)")
                self._ctrl, self._ctrl_time = bytes(data[:s.ctrl_length]), time.monotonic()
            return self._ctrl

    def write_control(self, data: bytes) -> None:
        s = self.spec
        if s.ctrl_id is None:
            raise NotSupported(f"{s.name} has no settings report")
        buf = bytearray(data[:s.ctrl_length])
        if len(buf) != s.ctrl_length or buf[0] != s.ctrl_id:
            raise ValueError("settings report has the wrong size or ID for this device")
        if s.ctrl_crc:
            from .crc import seal
            seal(buf)
        with self.lock:
            self.transport.set_feature(bytes(buf))
            self.transport.set_feature(s.save_report)
            self.writes += 1
            self._ctrl, self._ctrl_time = bytes(buf), time.monotonic()
        log.info("%s: wrote settings (%d writes this session)", self.key, self.writes)

    def set_manual(self, powers: dict[str, float]) -> None:
        """Set several outputs to fixed powers with one settings write."""
        if not powers:
            return
        s = self.spec
        with self.lock:
            buf = bytearray(self.read_control(fresh=True))
            for key, pct in powers.items():
                idx = s.fan_index(key)
                if idx < 0 or s.fans[idx].ctrl is None:
                    raise NotSupported(f"{s.name} has no controllable output {key!r}")
                if s.family == "standard":
                    rep = control.ControlReport(s, buf)
                    rep.set_manual(idx, pct)
                    buf = rep.buf
                elif s.family == "aquaero":
                    control.aquaero_set_manual(s, buf, idx, pct)
                elif s.family == "aquastreamxt":
                    control.aquastreamxt_set_manual(buf, idx, pct)
                else:
                    raise NotSupported(f"{s.name} outputs cannot be controlled")
            self.write_control(bytes(buf))

    def push_soft_sensors(self, values: list[tuple[float, int] | None]) -> None:
        if not self.spec.soft_sensors:
            raise NotSupported(f"{self.spec.name} has no software sensors")
        with self.lock:
            self.transport.write_output(control.soft_sensor_report(self.spec.soft_sensors, values))

    def push_leakshield(self, pump_rpm: float | None, flow: float | None) -> None:
        if not self.spec.leakshield_feed:
            raise NotSupported(f"{self.spec.name} is not a Leakshield")
        with self.lock:
            self.transport.write_output(control.leakshield_feed_report(pump_rpm, flow))

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001 - closing a vanished device
            pass


# ---------------------------------------------------------------------- hwmon fallback
HWMON_ROOT = Path("/sys/class/hwmon")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


class HwmonDevice(BaseDevice):
    """Readings (and PWM, as root) from the kernel's aquacomputer_d5next hwmon files."""

    backend = "hwmon"

    def __init__(self, spec: DeviceSpec, hwmon_dir: Path, index: int = 0):
        super().__init__()
        self.spec = spec
        self.dir = hwmon_dir
        self.path = str(hwmon_dir)
        self.index = index
        self.serial = self._debugfs_value("serial_number") or ""
        fw = self._debugfs_value("firmware_version")
        self.firmware = int(fw) if fw and fw.isdigit() else 0

    @property
    def key(self) -> str:
        if self.serial:
            return f"{self.spec.kind}-{self.serial}"
        return self.spec.kind if self.index == 0 else f"{self.spec.kind}{self.index + 1}"

    def _debugfs_value(self, name: str) -> str | None:
        root = Path("/sys/kernel/debug")
        try:
            hid = os.path.basename(os.path.realpath(self.dir / "device"))
            for d in root.glob(f"aquacomputer_*-{hid}"):
                return _read(d / name)
        except OSError:
            return None
        return None

    def capabilities(self) -> set[str]:
        caps = {"monitor"}
        if any((self.dir / f"pwm{i + 1}").exists() for i in range(len(self.spec.fans))):
            caps.add("manual")
        return caps

    def can_control(self, output_key: str) -> bool:
        idx = self.spec.fan_index(output_key)
        return idx >= 0 and (self.dir / f"pwm{idx + 1}").exists()

    def poll(self) -> list[Reading]:
        s = self.spec
        values: list[tuple[str, str, str, float | None, str]] = []

        def num(name: str, scale: float) -> float | None:
            raw = _read(self.dir / name)
            try:
                return None if raw is None else round(int(raw) * scale, 4)
            except ValueError:
                return None

        temp_i = 1
        for t in s.temps:
            values.append((t.key, t.label, "temperature", num(f"temp{temp_i}_input", 0.001), "Temperatures"))
            temp_i += 1
        if s.virtual:
            for i in range(s.virtual[1]):
                values.append((f"virt{i + 1}", f"Software sensor {i + 1}", "temperature",
                               num(f"temp{temp_i}_input", 0.001), "Software sensors"))
                temp_i += 1
        fans = [f for f in s.fans if f.status is not None]
        for i, f in enumerate(fans):
            values.append((f"{f.key}.rpm", f"{f.label} speed", "rpm", num(f"fan{i + 1}_input", 1.0), "Fans"))
            values.append((f"{f.key}.power", f"{f.label} power", "power", num(f"power{i + 1}_input", 1e-6), "Fans"))
            values.append((f"{f.key}.voltage", f"{f.label} voltage", "voltage", num(f"in{i}_input", 0.001), "Fans"))
            values.append((f"{f.key}.current", f"{f.label} current", "current", num(f"curr{i + 1}_input", 0.001),
                           "Fans"))
            pwm = num(f"pwm{i + 1}", 100 / 255)
            if pwm is not None:
                values.append((f"{f.key}.percent", f"{f.label} output", "percent", round(pwm, 1), "Fans"))
        for j, fl in enumerate(s.flows):
            values.append((fl.key, fl.label, "flow", num(f"fan{len(fans) + 1 + j}_input", 0.1), "Flow"))
        self.online = any(v[3] is not None for v in values)
        return self._readings(values)

    def set_manual(self, powers: dict[str, float]) -> None:
        for key, pct in powers.items():
            idx = self.spec.fan_index(key)
            if idx < 0:
                raise NotSupported(f"{self.spec.name} has no output {key!r}")
            enable = self.dir / f"pwm{idx + 1}_enable"
            try:
                if enable.exists():
                    enable.write_text("1")
                    time.sleep(0.2)
                (self.dir / f"pwm{idx + 1}").write_text(str(round(max(0, min(100, pct)) * 255 / 100)))
            except OSError as exc:
                raise DeviceError(f"cannot write {self.dir}/pwm{idx + 1}: {exc.strerror}") from exc
            self.writes += 1


def hwmon_devices(root: Path = HWMON_ROOT) -> list[HwmonDevice]:
    found: list[HwmonDevice] = []
    counts: dict[str, int] = {}
    if not root.is_dir():
        return found
    for d in sorted(root.iterdir()):
        name = _read(d / "name")
        kind = HWMON_NAMES.get(name or "")
        if not kind or kind not in BY_KIND:
            continue
        n = counts.get(kind, 0)
        counts[kind] = n + 1
        found.append(HwmonDevice(BY_KIND[kind], d, n))
    return found
