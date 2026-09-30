"""The control engine: read every sensor, compute virtual sensors and controllers, drive outputs.

Each tick (1 s by default):

1. poll the devices and the PC's own sensors;
2. compute virtual sensors (Delta T, averages, formulas, …);
3. run the controllers;
4. drive every managed output, in one of two places:

   * **on the device** (Quadro, Octo, D5 Next curve controllers): the curve, limits and input
     are written into the device's settings once, and inputs that live elsewhere (a Delta T,
     a CPU temperature, a sensor on another device) are streamed every second into one of
     the device's software sensors. This is how aquasuite does it: nothing is written to the
     device's memory during normal operation, and the device keeps controlling its fans by
     itself (falling back to its fallback power if the data stops);
   * **in software**: the engine computes the power and sets it as a manual power, writing
     only when it changes by at least 1 % and at most every few seconds.

5. feed software sensors and the Leakshield, check alarms, record history.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import alarms as alarms_mod
from . import control, controllers, virtual
from .config import Config, OutputConfig
from .device import BaseDevice
from .devices import SOFT_PERCENT, SOFT_POWER, SOFT_TEMPERATURE
from .errors import AquaError, ConfigError, DeviceError, NotSupported
from .model import FanControl, FanSetup, Reading

log = logging.getLogger(__name__)

RESCAN_SECONDS = 10.0
FEED_VERIFY_SECONDS = 6.0
MIN_CHANGE = 1.0            # % — smaller software power changes are not written
MAX_REWRITES = 3            # the same settings are written at most this often if the device doesn't keep them
CURVE_TOLERANCE = 20.0      # % — reported output may differ this much from the expected one
CURVE_CHECK_SECONDS = 90.0  # … for this long before the device's curves are no longer trusted


# ---------------------------------------------------------------------- providers
class DeviceProvider:
    """Finds devices. ``scan`` returns newly found devices and human-readable problems."""

    def scan(self, current: dict[str, BaseDevice]) -> tuple[list[BaseDevice], list[str]]:
        return [], []

    def system_sensors(self):
        return None


class StaticProvider(DeviceProvider):
    """A fixed list of devices (the simulator, tests)."""

    def __init__(self, devices: list[BaseDevice], system=None):
        self._devices = list(devices)
        self._system = system
        self._given = False

    def scan(self, current):
        if self._given:
            return [], []
        self._given = True
        return list(self._devices), []

    def system_sensors(self):
        return self._system


# ---------------------------------------------------------------------- history
class Series:
    """A fixed-size ring of float32 values (NaN = no value)."""

    __slots__ = ("data", "pos", "count")

    def __init__(self, size: int):
        self.data = array("f", [float("nan")]) * size
        self.pos = 0
        self.count = 0

    def append(self, value: float | None) -> None:
        self.data[self.pos] = float("nan") if value is None else value
        self.pos = (self.pos + 1) % len(self.data)
        self.count = min(self.count + 1, len(self.data))

    def last(self, n: int) -> list[float | None]:
        n = min(n, self.count)
        size = len(self.data)
        out = []
        for i in range(n):
            v = self.data[(self.pos - n + i) % size]
            out.append(None if v != v else round(v, 3))
        return out


class History:
    def __init__(self, size: int):
        self.size = max(10, size)
        self.times: deque[float] = deque(maxlen=self.size)
        self.series: dict[str, Series] = {}

    def record(self, t: float, values: dict[str, float | None]) -> None:
        self.times.append(t)
        for sid, v in values.items():
            s = self.series.get(sid)
            if s is None:
                s = self.series[sid] = Series(self.size)
                for _ in range(len(self.times) - 1):
                    s.append(None)
            s.append(v)
        for sid, s in self.series.items():
            if sid not in values:
                s.append(None)

    def get(self, ids: list[str], seconds: float) -> dict:
        if not self.times:
            return {"times": [], "series": {}}
        cutoff = self.times[-1] - seconds
        n = sum(1 for t in self.times if t >= cutoff)
        times = list(self.times)[-n:]
        return {"times": [round(t, 1) for t in times],
                "series": {sid: self.series[sid].last(n) for sid in ids if sid in self.series}}


# ---------------------------------------------------------------------- plans
@dataclass
class OutputPlan:
    output_id: str
    device_key: str
    key: str
    index: int
    controller_id: str
    placement: str                   # device | software | unmanaged
    reason: str = ""
    source_index: int | None = None
    slot: int | None = None
    follow_index: int | None = None


@dataclass
class OutputRuntime:
    stage: controllers.StageState = field(default_factory=controllers.StageState)
    target: float | None = None
    last_written: float | None = None
    placement_written: str = ""
    mismatch_since: float | None = None


def soft_type(unit: str) -> int:
    return {"%": SOFT_PERCENT, "W": SOFT_POWER}.get(unit, SOFT_TEMPERATURE)


class Engine:
    def __init__(self, config: Config, provider: DeviceProvider, mode: str = "standalone",
                 save_config=None, allow_shutdown: bool = False, system_sensors=None, control: bool = True):
        self.control = control           # False: read only — never touch outputs or software sensors
        self.config = config
        self.provider = provider
        self.mode = mode
        self.save_config = save_config
        self.devices: dict[str, BaseDevice] = {}
        self.problems: list[str] = []
        self.readings: dict[str, Reading] = {}
        self.plans: dict[str, OutputPlan] = {}
        self.runtime: dict[str, OutputRuntime] = {}
        self.ctrl_states: dict[str, controllers.ControllerState] = {}
        self.virtuals = virtual.VirtualSensors()
        self.alarms = alarms_mod.Alarms(allow_shutdown=allow_shutdown)
        self.system = system_sensors
        self.history = History(int(config.settings.history_minutes * 60 / max(0.2, config.settings.interval)))
        self.events: deque[tuple[float, str, str]] = deque(maxlen=200)
        self.overrides: dict[str, tuple[float, float]] = {}
        self.feed_slots: dict[str, dict[int, str]] = {}          # device -> slot -> source reading id
        self.feed_pushed: dict[str, dict[int, deque]] = {}
        self.feed_first_push: dict[str, float] = {}
        self.feed_ok: dict[str, bool] = {}
        self.feed_broken: set[str] = set()
        self.fed_devices: set[str] = set()
        self.last_write: dict[str, float] = {}
        self.write_failures: dict[str, int] = {}
        self.rewrites: dict[str, tuple[str, int]] = {}           # device -> (desired settings, times written)
        self.settings_stuck: set[str] = set()                    # devices that keep rejecting our settings
        self.curve_suspect: set[str] = set()                     # devices not following their stored curves
        self._plan_dirty = True
        self._last_tick = 0.0
        self._last_scan = 0.0
        self._last_log = 0.0
        self._log_file: tuple[str, list[str]] | None = None
        self._snapshot: dict = {}
        self.lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started = time.time()
        self.ticks = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aquasuite-engine", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("engine tick failed")
            interval = max(0.2, float(self.config.settings.interval))
            self._stop.wait(max(0.05, interval - (time.monotonic() - began)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self.lock:
            if self.control:
                self._shutdown_outputs()
            for dev in self.devices.values():
                dev.close()
            if self.system is not None and hasattr(self.system, "close"):
                self.system.close()

    def _shutdown_outputs(self) -> None:
        """Leave the hardware in a safe state: software outputs at fallback, feeds marked unavailable."""
        if self.config.settings.exit_action == "fallback":
            per_dev: dict[str, dict[str, float]] = {}
            for oid, plan in self.plans.items():
                if plan.placement == "software":
                    oc = self.config.outputs.get(oid) or OutputConfig()
                    per_dev.setdefault(plan.device_key, {})[plan.key] = oc.fallback
            for key, powers in per_dev.items():
                dev = self.devices.get(key)
                if dev:
                    try:
                        dev.set_manual(powers)
                    except AquaError as exc:
                        log.warning("%s: could not set fallback power on exit: %s", key, exc)
        for key in list(self.fed_devices):
            dev = self.devices.get(key)
            if dev:
                try:
                    dev.push_soft_sensors([None] * 16)
                except AquaError:
                    pass

    # ------------------------------------------------------------------ events
    def event(self, level: str, text: str) -> None:
        self.events.append((time.time(), level, text))
        getattr(log, {"error": "error", "warning": "warning"}.get(level, "info"))(text)

    # ------------------------------------------------------------------ configuration
    def set_config(self, cfg: Config, save: bool = True) -> None:
        virtual.validate(cfg.virtual_sensors)
        for c in cfg.controllers:
            if c.kind == "curve" and len(c.points) < 1:
                raise ConfigError(f"{c.name}: a curve needs at least one point")
        with self.lock:
            old = self.config
            self.config = cfg
            for cid in list(self.ctrl_states):
                if cfg.controller(cid) != old.controller(cid):
                    self.ctrl_states.pop(cid, None)
            self.settings_stuck.clear()
            self.rewrites.clear()
            if cfg.settings.history_minutes != old.settings.history_minutes or \
                    cfg.settings.interval != old.settings.interval:
                self.history = History(int(cfg.settings.history_minutes * 60 / max(0.2, cfg.settings.interval)))
            self._plan_dirty = True
        if save and self.save_config:
            self.save_config(cfg)

    def set_profile(self, name: str) -> None:
        cfg = self.config.copy()
        if name and not cfg.profile(name):
            raise ConfigError(f"no profile called {name!r}")
        cfg.active_profile = cfg.profile(name).id if name else ""
        self.set_config(cfg)
        self.event("info", f"Profile: {cfg.profile(name).name if name else 'default'}")

    def override(self, output_id: str, power: float | None, seconds: float = 10.0) -> None:
        with self.lock:
            if power is None:
                self.overrides.pop(output_id, None)
            else:
                self.overrides[output_id] = (controllers.clamp(power), time.monotonic() + max(1.0, seconds))
            self._plan_dirty = True

    # ------------------------------------------------------------------ devices
    def rescan(self) -> None:
        self._last_scan = 0.0

    def _scan(self, now: float) -> None:
        if now - self._last_scan < RESCAN_SECONDS:
            return
        self._last_scan = now
        try:
            found, problems = self.provider.scan(self.devices)
        except Exception as exc:  # noqa: BLE001 - discovery must never kill the engine
            log.exception("device scan failed")
            found, problems = [], [str(exc)]
        self.problems = problems
        if self.system is None and self.config.settings.system_sensors:
            self.system = self.provider.system_sensors()
        for dev in found:
            if dev.key in self.devices:
                dev.close()
                continue
            for other_key, other in list(self.devices.items()):
                if other.backend == "hwmon" and dev.hid_id and other.hid_id == dev.hid_id:
                    self._drop(other_key, "now reachable directly")
            self.devices[dev.key] = dev
            self.event("success", f"{dev.spec.name} connected ({dev.key}, {dev.backend})")
            self._adopt(dev)
            self._plan_dirty = True

    def _adopt(self, dev: BaseDevice) -> None:
        """Move settings saved for the same kind of device under another key (e.g. hwmon → hidraw)."""
        cfg = self.config
        prefix = dev.spec.kind
        same_kind = [k for k in self.devices if self.devices[k].spec.kind == prefix]
        if len(same_kind) != 1:
            return
        referenced = {oid.split("/", 1)[0] for oid in cfg.outputs} | set(cfg.devices)
        stale = [k for k in referenced if k != dev.key and (k == prefix or k.startswith(prefix + "-"))
                 and k not in self.devices]
        if len(stale) != 1:
            return
        old = stale[0]
        text = self._rekey(cfg, old, dev.key)
        if text:
            self.event("info", f"Settings for {old} now apply to {dev.key}")
            if self.save_config:
                self.save_config(self.config)

    @staticmethod
    def _rekey(cfg: Config, old: str, new: str) -> bool:
        changed = False

        def swap(sid: str) -> str:
            nonlocal changed
            if sid.startswith(old + "/"):
                changed = True
                return new + sid[len(old):]
            return sid

        cfg.outputs = {swap(k): v for k, v in cfg.outputs.items()}
        if old in cfg.devices:
            cfg.devices[new] = cfg.devices.pop(old)
            changed = True
        for v in cfg.virtual_sensors:
            v.inputs = [swap(i) for i in v.inputs]
        for c in cfg.controllers:
            c.input, c.follow = swap(c.input), swap(c.follow)
        for f in cfg.feeds:
            if f.device == old:
                f.device, changed = new, True
            f.source = swap(f.source)
        for ls in cfg.leakshield:
            if ls.device == old:
                ls.device, changed = new, True
            ls.pump, ls.flow = swap(ls.pump), swap(ls.flow)
        for a in cfg.alarms:
            a.sensor = swap(a.sensor)
        for p in cfg.profiles:
            p.assignments = {swap(k): v for k, v in p.assignments.items()}
        return changed

    def _drop(self, key: str, reason: str) -> None:
        dev = self.devices.pop(key, None)
        if dev:
            dev.close()
            self.event("warning", f"{dev.spec.name} ({key}) disconnected: {reason}")
            self._plan_dirty = True

    # ------------------------------------------------------------------ the tick
    def tick(self) -> dict:
        with self.lock:
            now = time.monotonic()
            dt = now - self._last_tick if self._last_tick else float(self.config.settings.interval)
            self._last_tick = now
            self.ticks += 1
            self._scan(now)
            cfg = self.config

            readings: dict[str, Reading] = {}
            for key, dev in list(self.devices.items()):
                try:
                    items = dev.poll()
                except DeviceError as exc:
                    self._drop(key, str(exc))
                    continue
                names = cfg.devices.get(key)
                for r in items:
                    if names and r.id.split("/", 1)[1] in names.sensor_names:
                        r.label = names.sensor_names[r.id.split("/", 1)[1]]
                    readings[r.id] = r
            if self.system is not None and cfg.settings.system_sensors:
                try:
                    for r in self.system.poll():
                        readings[r.id] = r
                except Exception:  # noqa: BLE001 - never let a PC sensor stop fan control
                    log.exception("system sensors failed")
            self.virtuals.evaluate(cfg.virtual_sensors, readings, dt)
            for key, slots in self.feed_slots.items():
                for slot, sid in slots.items():
                    fed, src = readings.get(f"{key}/virt{slot}"), readings.get(sid)
                    if fed is not None and src is not None:
                        fed.label = f"Software sensor {slot} ← {src.label}"
                        fed.unit, fed.kind = src.unit, src.kind
            self.readings = readings

            self.alarms.evaluate(cfg.alarms, readings, now)
            for level, text in self.alarms.events:
                self.event(level, text)
            fans_max = self.alarms.fans_max(cfg.alarms)
            for oid, (_p, until) in list(self.overrides.items()):
                if now > until:
                    del self.overrides[oid]
                    self._plan_dirty = True

            ctrl_values = self._run_controllers(dt)
            if self.control:
                self._verify_feeds(now)
                if self._plan_dirty or fans_max != getattr(self, "_fans_max", False):
                    self._fans_max = fans_max
                    self._plan(fans_max)
                    self._plan_dirty = False
                self._drive_outputs(ctrl_values, dt, now, fans_max)
                self._verify_device_outputs(now)
                self._push_feeds(now)
                self._push_leakshield()
            self._record(now)
            self._snapshot = self._build_snapshot(ctrl_values)
            return self._snapshot

    # ------------------------------------------------------------------ planning
    def _plan(self, fans_max: bool) -> None:
        cfg = self.config
        plans: dict[str, OutputPlan] = {}
        slots: dict[str, dict[int, str]] = {}
        for f in cfg.feeds:
            dev = self.devices.get(f.device)
            if dev and dev.spec.soft_sensors and 1 <= f.slot <= dev.spec.soft_sensors.slots and f.source:
                slots.setdefault(f.device, {})[f.slot] = f.source

        output_ids = set(cfg.outputs)
        prof = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        if prof:
            output_ids |= set(prof.assignments)
        output_ids |= set(self.overrides)
        for oid in sorted(output_ids):
            dev_key, _, key = oid.partition("/")
            dev = self.devices.get(dev_key)
            cid = cfg.assignment(oid)
            if dev is None or not dev.can_control(key):
                continue
            idx = dev.spec.fan_index(key)
            plan = OutputPlan(oid, dev_key, key, idx, cid, "unmanaged")
            ctrl = cfg.controller(cid) if cid else None
            oc = cfg.outputs.get(oid) or OutputConfig()
            if oid in self.overrides:
                plan.placement, plan.reason = "software", "manual override"
            elif fans_max and ctrl is not None:
                plan.placement, plan.reason = "software", "alarm: full power"
            elif ctrl is not None:
                plan.placement = "software"
                if oc.placement != "software":
                    ok, why = self._device_placement(plan, ctrl, dev, slots)
                    if ok:
                        plan.placement = "device"
                    else:
                        plan.reason = why
            plans[oid] = plan

        # a fan can only follow one that runs on the device and doesn't follow another fan itself
        for plan in plans.values():
            if plan.placement == "device" and plan.follow_index is not None:
                target = next((p for p in plans.values() if p.device_key == plan.device_key
                               and p.index == plan.follow_index), None)
                if target is None or target.placement != "device" or target.follow_index is not None:
                    plan.placement, plan.follow_index = "software", None
                    plan.reason = "the followed output is not controlled by the device"
        self.plans = plans
        self.feed_slots = slots
        for oid in list(self.runtime):
            if oid not in plans:
                del self.runtime[oid]

    def _device_placement(self, plan: OutputPlan, ctrl, dev: BaseDevice, slots) -> tuple[bool, str]:
        caps = dev.capabilities()
        spec = dev.spec
        if "device_curves" not in caps or dev.backend == "hwmon":
            return False, f"the {spec.name} can't run controllers itself" + \
                (" (hwmon access only)" if dev.backend == "hwmon" else "")
        if ctrl.kind == "follow":
            fdev, _, fkey = ctrl.follow.partition("/")
            if "follow" not in caps or fdev != plan.device_key:
                return False, "follows an output on another device"
            fi = spec.fan_index(fkey)
            if fi < 0 or fi == plan.index:
                return False, "invalid output to follow"
            plan.follow_index = fi
            return True, ""
        if plan.device_key in self.curve_suspect:
            return False, f"the {spec.name} didn't follow the curve stored on it as expected"
        if plan.device_key in self.settings_stuck:
            return False, f"the {spec.name} didn't keep the settings written to it"
        if ctrl.kind != "curve":
            return False, f"the {spec.name} can only run curve controllers itself"
        if not ctrl.input:
            return False, "the curve has no input sensor"
        src_dev, _, src_key = ctrl.input.partition("/")
        if src_dev == plan.device_key:
            idx = control.source_index(spec, src_key)
            if idx is not None:
                plan.source_index = idx
                return True, ""
        if "soft_sensors" not in caps:
            return False, f"the {spec.name} has no software sensors to receive this input"
        if plan.device_key in self.feed_broken:
            return False, f"the {spec.name} is not accepting software sensor data"
        dev_slots = slots.setdefault(plan.device_key, {})
        slot = next((s for s, src in dev_slots.items() if src == ctrl.input), None)
        if slot is None:
            free = [s for s in range(1, spec.soft_sensors.slots + 1) if s not in dev_slots]
            if not free:
                return False, "all software sensor slots are in use"
            slot = free[0]
            dev_slots[slot] = ctrl.input
        plan.slot = slot
        plan.source_index = len(spec.temps) + slot - 1
        return True, ""

    # ------------------------------------------------------------------ controllers
    def _run_controllers(self, dt: float) -> dict[str, float | None]:
        values: dict[str, float | None] = {}
        last_out = {oid: rt.target for oid, rt in self.runtime.items()}
        for c in controllers.order(self.config.controllers):
            st = self.ctrl_states.setdefault(c.id, controllers.ControllerState())
            r = self.readings.get(c.input) if c.input else None
            x = None if r is None else r.value
            values[c.id] = controllers.evaluate(c, st, x, dt, values, last_out)
        return values

    # ------------------------------------------------------------------ outputs
    def _drive_outputs(self, ctrl_values: dict[str, float | None], dt: float, now: float, fans_max: bool) -> None:
        cfg = self.config
        manual: dict[str, dict[str, float]] = {}
        device_changes: dict[str, list[OutputPlan]] = {}
        for oid, plan in self.plans.items():
            rt = self.runtime.setdefault(oid, OutputRuntime())
            oc = cfg.outputs.get(oid) or OutputConfig()
            if plan.placement == "unmanaged":
                rt.target = None
                continue
            if self.devices[plan.device_key].spec.fans[plan.index].pump and not oc.hold_min:
                oc = OutputConfig(**{**oc.__dict__, "hold_min": True})   # a controller never stops a pump
            if plan.placement == "device":
                c = ctrl_values.get(plan.controller_id)
                if plan.follow_index is not None:
                    target_plan = next((p for p in self.plans.values() if p.device_key == plan.device_key
                                        and p.index == plan.follow_index), None)
                    rt.target = self.runtime[target_plan.output_id].target if target_plan and \
                        target_plan.output_id in self.runtime else None
                else:
                    rt.target = round(controllers.scale_power(c, oc.min_power, oc.max_power, oc.hold_min,
                                                              oc.fallback), 1)
                device_changes.setdefault(plan.device_key, []).append(plan)
                continue
            # software
            if oid in self.overrides:
                target = self.overrides[oid][0]
                rt.stage.applied = target
            elif fans_max:
                target = 100.0
                rt.stage.applied = target
            else:
                target = controllers.output_stage(ctrl_values.get(plan.controller_id), oc, rt.stage, dt, now)
            rt.target = round(target, 1)
            last = rt.last_written
            changed = (last is None or abs(target - last) >= MIN_CHANGE or (target == 0 and last != 0)
                       or (target >= 100 and last < 100) or rt.placement_written != "software")
            if changed:
                manual.setdefault(plan.device_key, {})[plan.key] = rt.target

        for key, plans in device_changes.items():
            self._ensure_device_settings(key, plans, now)
        for key, powers in manual.items():
            dev = self.devices.get(key)
            if dev is None:
                continue
            interval = max(0.5, (cfg.devices.get(key).write_interval if key in cfg.devices else 2.0))
            if now - self.last_write.get(key, 0.0) < interval and not self._placement_changed(key, powers):
                continue
            try:
                self._write_software(dev, powers)
                self.last_write[key] = now
                for fan_key, pct in powers.items():
                    rt = self.runtime[f"{key}/{fan_key}"]
                    rt.last_written = pct
                    rt.placement_written = "software"
            except AquaError as exc:
                self._write_failed(key, exc)

    def _placement_changed(self, key: str, powers: dict[str, float]) -> bool:
        return any(self.runtime[f"{key}/{k}"].placement_written != "software" for k in powers)

    def _write_failed(self, key: str, exc: Exception) -> None:
        n = self.write_failures.get(key, 0) + 1
        self.write_failures[key] = n
        if n in (1, 5) or n % 60 == 0:
            self.event("error", f"{key}: could not write settings: {exc}")
        self.last_write[key] = time.monotonic()

    def _write_software(self, dev: BaseDevice, powers: dict[str, float]) -> None:
        spec = dev.spec
        if spec.family != "standard" or dev.backend == "hwmon":
            dev.set_manual(powers)
            return
        rep = control.ControlReport(spec, dev.read_control(fresh=True))
        before = rep.checksum()
        for fan_key, pct in powers.items():
            i = spec.fan_index(fan_key)
            rep.set_manual(i, pct)
            oc = self.config.outputs.get(f"{dev.key}/{fan_key}") or OutputConfig()
            # neutral limits: the engine already applied min/max/fallback in software
            rep.set_setup(i, FanSetup(hold_min=True, start_boost=False, min_power=0, max_power=10000,
                                      fallback=round(oc.fallback * 100)))
        if rep.checksum() != before:
            dev.write_control(rep.seal())

    def _ensure_device_settings(self, key: str, plans: list[OutputPlan], now: float) -> None:
        dev = self.devices.get(key)
        if dev is None:
            return
        try:
            rep = control.ControlReport(dev.spec, dev.read_control())
        except AquaError as exc:
            self._write_failed(key, exc)
            return
        before = rep.checksum()
        signature = []
        for plan in plans:
            ctrl = self.config.controller(plan.controller_id)
            oc = self.config.outputs.get(plan.output_id) or OutputConfig()
            fc = rep.fan(plan.index)
            want = FanControl(mode=fc.mode, pwm=fc.pwm, source=fc.source, pid=fc.pid, curve_start=fc.curve_start,
                              curve_temps=fc.curve_temps, curve_powers=fc.curve_powers)
            if plan.follow_index is not None:
                want.mode = control.MODE_FOLLOW + plan.follow_index
            elif ctrl is not None:
                pts = control.resample_curve([(p[0], p[1]) for p in ctrl.points])
                want.mode = control.MODE_CURVE
                want.source = plan.source_index if plan.source_index is not None else control.SOURCE_NONE
                want.curve_temps = [round(x * 100) for x, _ in pts]
                want.curve_powers = [round(controllers.clamp(y) * 100) for _, y in pts]
            rep.set_fan(plan.index, want)
            pump = dev.spec.fans[plan.index].pump
            setup = FanSetup(hold_min=oc.hold_min or pump, start_boost=oc.start_boost,
                             min_power=round(oc.min_power * 100), max_power=round(oc.max_power * 100),
                             fallback=round(oc.fallback * 100))
            rep.set_setup(plan.index, setup)
            signature.append((plan.index, want.mode, want.source, tuple(want.curve_temps), tuple(want.curve_powers),
                              setup.hold_min, setup.start_boost, setup.min_power, setup.max_power, setup.fallback))
        placement_new = [p for p in plans if self.runtime[p.output_id].placement_written != "device"]
        if rep.checksum() == before:
            for p in placement_new:
                self.runtime[p.output_id].placement_written = "device"
            self.rewrites.pop(key, None)
            return
        if now - self.last_write.get(key, 0.0) < 1.0:
            return
        # guard the device's memory: if it keeps changing what we write, stop rewriting it
        wanted = repr(signature)
        last, count = self.rewrites.get(key, ("", 0))
        count = count + 1 if last == wanted else 1
        self.rewrites[key] = (wanted, count)
        if count > MAX_REWRITES:
            self.settings_stuck.add(key)
            self._plan_dirty = True
            self.event("warning", f"{dev.spec.name} did not keep the controller settings after {MAX_REWRITES} "
                                  "writes; its outputs are now controlled in software")
            return
        try:
            dev.write_control(rep.seal())
            self.last_write[key] = now
            for p in plans:
                self.runtime[p.output_id].placement_written = "device"
                self.runtime[p.output_id].last_written = None
            names = ", ".join(self._output_name(p.output_id) for p in plans)
            self.event("info", f"{dev.spec.name}: controller settings stored on the device ({names})")
        except AquaError as exc:
            self._write_failed(key, exc)

    def _output_name(self, oid: str) -> str:
        oc = self.config.outputs.get(oid)
        if oc and oc.name:
            return oc.name
        dev_key, _, key = oid.partition("/")
        dev = self.devices.get(dev_key)
        f = dev.spec.fan(key) if dev else None
        return f.label if f else oid

    # ------------------------------------------------------------------ software sensors
    def _push_feeds(self, now: float) -> None:
        for key, dev in self.devices.items():
            slots = self.feed_slots.get(key) or {}
            spec = dev.spec.soft_sensors
            if spec is None or not dev.capabilities() & {"soft_sensors"}:
                continue
            if not slots and key not in self.fed_devices:
                continue
            values: list[tuple[float, int] | None] = [None] * spec.slots
            pushed = self.feed_pushed.setdefault(key, {})
            for slot, sid in slots.items():
                r = self.readings.get(sid)
                if r is not None and r.value is not None:
                    values[slot - 1] = (r.value, soft_type(r.unit))
                    pushed.setdefault(slot, deque(maxlen=3)).append(round(r.value, 2))
            try:
                dev.push_soft_sensors(values)
            except AquaError as exc:
                self._write_failed(key, exc)
                continue
            if slots:
                self.fed_devices.add(key)
                self.feed_first_push.setdefault(key, now)
            else:
                self.fed_devices.discard(key)
                self.feed_first_push.pop(key, None)
                self.feed_pushed.pop(key, None)

    def _verify_feeds(self, now: float) -> None:
        """Check the device reports back the values we sent; otherwise stop relying on them."""
        for key in list(self.fed_devices):
            first = self.feed_first_push.get(key)
            pushed = self.feed_pushed.get(key) or {}
            if first is None or not pushed:
                continue
            ok = True
            for slot, recent in pushed.items():
                r = self.readings.get(f"{key}/virt{slot}")
                if r is None or r.value is None or not any(abs(r.value - v) <= 0.06 for v in recent):
                    ok = False
                    break
            if ok:
                if not self.feed_ok.get(key):
                    self.feed_ok[key] = True
                continue
            if not self.feed_ok.get(key) and now - first > FEED_VERIFY_SECONDS and key not in self.feed_broken:
                self.feed_broken.add(key)
                self._plan_dirty = True
                name = self.devices[key].spec.name if key in self.devices else key
                self.event("warning", f"{name} does not report the software sensor values back; its outputs "
                                      "are now controlled in software instead")

    def _verify_device_outputs(self, now: float) -> None:
        """Outputs run by the device should report roughly the power we expect from their curve."""
        for oid, plan in self.plans.items():
            rt = self.runtime.get(oid)
            if rt is None:
                continue
            reported = self.readings.get(f"{oid}.percent")
            if (plan.placement != "device" or plan.follow_index is not None or rt.target is None
                    or rt.placement_written != "device" or reported is None or reported.value is None):
                rt.mismatch_since = None
                continue
            if abs(reported.value - rt.target) <= CURVE_TOLERANCE:
                rt.mismatch_since = None
                continue
            if rt.mismatch_since is None:
                rt.mismatch_since = now
            elif now - rt.mismatch_since > CURVE_CHECK_SECONDS and plan.device_key not in self.curve_suspect:
                self.curve_suspect.add(plan.device_key)
                self._plan_dirty = True
                dev = self.devices.get(plan.device_key)
                self.event("warning", f"{dev.spec.name if dev else plan.device_key}: {self._output_name(oid)} "
                                      f"reports {reported.value:.0f} % where its curve gives {rt.target:.0f} %; "
                                      "its outputs are now controlled in software. Please report this.")

    def _push_leakshield(self) -> None:
        for ls in self.config.leakshield:
            dev = self.devices.get(ls.device)
            if dev is None or not dev.spec.leakshield_feed:
                continue
            pump = self.readings.get(ls.pump)
            flow = self.readings.get(ls.flow)
            try:
                dev.push_leakshield(pump.value if pump else None, flow.value if flow else None)
            except AquaError as exc:
                self._write_failed(ls.device, exc)

    # ------------------------------------------------------------------ history / log
    def _record(self, now: float) -> None:
        t = time.time()
        values = {sid: r.value for sid, r in self.readings.items()}
        for oid, rt in self.runtime.items():
            values[f"output/{oid}"] = rt.target
        self.history.record(t, values)
        s = self.config.settings
        if s.log_enabled and now - self._last_log >= max(1.0, s.log_interval):
            self._last_log = now
            try:
                self._write_log(t, values)
            except OSError as exc:
                self.event("warning", f"data log: {exc}")
                s.log_enabled = False

    def _log_dir(self) -> Path:
        if self.config.settings.log_dir:
            return Path(self.config.settings.log_dir)
        if os.geteuid() == 0 and self.mode == "service":
            return Path("/var/log/aquasuitelinux")
        from .config import user_data_dir
        return user_data_dir() / "log"

    def _write_log(self, t: float, values: dict[str, float | None]) -> None:
        ids = sorted(values)
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        directory = self._log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"aquasuitelinux-{day}.csv"
        if self._log_file is None or self._log_file[0] != str(path) or self._log_file[1] != ids:
            if path.exists() and self._log_file is not None and self._log_file[0] == str(path):
                path = directory / f"aquasuitelinux-{day}-{time.strftime('%H%M%S', time.localtime(t))}.csv"
            new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                if new:
                    csv.writer(fh).writerow(["time", *ids])
            self._log_file = (str(path), ids)
        with open(self._log_file[0], "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                                     *("" if values[i] is None else values[i] for i in ids)])

    # ------------------------------------------------------------------ snapshot / API
    def _build_snapshot(self, ctrl_values: dict[str, float | None]) -> dict:
        cfg = self.config
        devices = []
        for key, dev in self.devices.items():
            info = dev.info().to_dict()
            info["name"] = (cfg.devices.get(key).name if key in cfg.devices and cfg.devices[key].name
                            else dev.spec.name)
            info["model"] = dev.spec.name
            info["writes"] = dev.writes
            info["feed"] = ("broken" if key in self.feed_broken else "ok" if self.feed_ok.get(key)
                            else "pending" if key in self.fed_devices else "")
            devices.append(info)
        outputs = []
        for key, dev in self.devices.items():
            for f in dev.spec.fans:
                if not dev.can_control(f.key):
                    continue
                oid = f"{key}/{f.key}"
                plan = self.plans.get(oid)
                rt = self.runtime.get(oid)
                oc = cfg.outputs.get(oid)
                cid = cfg.assignment(oid)
                ctrl = cfg.controller(cid) if cid else None
                rpm = self.readings.get(f"{oid}.rpm")
                reported = self.readings.get(f"{oid}.percent")
                outputs.append({
                    "id": oid, "device": key, "key": f.key, "label": f.label, "pump": f.pump,
                    "name": (oc.name if oc and oc.name else f.label),
                    "controller": cid, "controller_name": ctrl.name if ctrl else "",
                    "placement": plan.placement if plan else "unmanaged",
                    "reason": plan.reason if plan else "",
                    "slot": plan.slot if plan else None,
                    "target": rt.target if rt else None,
                    "reported": reported.value if reported else None,
                    "rpm": rpm.value if rpm else None,
                    "override": oid in self.overrides,
                })
        ctrls = []
        for c in cfg.controllers:
            st = self.ctrl_states.get(c.id)
            ctrls.append({"id": c.id, "name": c.name, "kind": c.kind, "input": c.input,
                          "input_value": st.input_value if st else None,
                          "output": ctrl_values.get(c.id)})
        feeds = []
        for key, slots in self.feed_slots.items():
            for slot, sid in sorted(slots.items()):
                r = self.readings.get(sid)
                feeds.append({"device": key, "slot": slot, "source": sid, "value": r.value if r else None,
                              "auto": not any(f.device == key and f.slot == slot for f in cfg.feeds)})
        prof = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        return {
            "time": time.time(),
            "mode": self.mode,
            "uptime": round(time.time() - self.started),
            "devices": devices,
            "problems": list(self.problems),
            "readings": [r.to_dict() for r in self.readings.values()],
            "outputs": outputs,
            "controllers": ctrls,
            "alarms": self.alarms.snapshot(cfg.alarms),
            "feeds": feeds,
            "profile": prof.name if prof else "",
            "events": [list(e) for e in list(self.events)[-50:]],
        }

    def snapshot(self) -> dict:
        return self._snapshot

    def history_for(self, ids: list[str], seconds: float) -> dict:
        with self.lock:
            return self.history.get(ids, seconds)

    def get_config(self) -> dict:
        return self.config.to_dict()

    # device settings (offsets, flow calibration, backup, import)
    def _device(self, key: str) -> BaseDevice:
        dev = self.devices.get(key)
        if dev is None:
            raise AquaError(f"device {key} is not connected")
        return dev

    def device_settings(self, key: str) -> dict:
        with self.lock:
            dev = self._device(key)
            if "settings" not in dev.capabilities():
                raise NotSupported(f"{dev.spec.name}: settings can't be read with the {dev.backend} backend")
            rep = control.ControlReport(dev.spec, dev.read_control(fresh=True))
            data = rep.to_dict()
            data["device"] = key
            return data

    def apply_device_settings(self, key: str, changes: dict) -> None:
        with self.lock:
            dev = self._device(key)
            rep = control.ControlReport(dev.spec, dev.read_control(fresh=True))
            for i, off in enumerate(changes.get("temp_offsets") or []):
                if off is not None:
                    rep.set_temp_offset(i, float(off))
            if changes.get("flow_pulses") is not None:
                rep.set_flow_pulses(int(changes["flow_pulses"]))
            dev.write_control(rep.seal())
            self.event("success", f"{dev.spec.name}: sensor settings saved on the device")

    def backup(self, key: str) -> dict:
        from .aquasuite import make_backup
        with self.lock:
            dev = self._device(key)
            return make_backup(dev.spec, dev.serial, dev.firmware, dev.read_control(fresh=True))

    def restore(self, key: str, backup: dict) -> None:
        from .aquasuite import backup_bytes
        with self.lock:
            dev = self._device(key)
            data = backup_bytes(backup, dev.spec)
            dev.write_control(data)
            self._plan_dirty = True
            self.event("success", f"{dev.spec.name}: settings restored from backup")
