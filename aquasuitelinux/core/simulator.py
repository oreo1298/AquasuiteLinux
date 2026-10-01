"""Simulated devices for demo mode and the tests.

A small water-loop model (CPU + GPU heat, radiators cooled by the simulated fans, a pump
that sets the flow) drives simulated Quadro / Octo / D5 Next / high flow NEXT devices. Each
one sits behind a ``SimTransport`` with the same interface as ``HidrawTransport``, speaking
the real binary protocol: it sends status reports, answers and checks control reports
(CRC included) and accepts software-sensor reports. Fans set to curve mode are controlled
by the simulated device itself, like the real firmware does.
"""

from __future__ import annotations

import math
import random
import threading
import time

from . import control
from .controllers import clamp, interpolate, scale_power
from .crc import is_sealed, seal
from .devices import BY_KIND, DeviceSpec
from .errors import DeviceError
from .model import Reading
from .status import encode_status

SERIALS = {"quadro": (10234, 55001), "octo": (18810, 40404), "d5next": (21077, 14342),
           "highflownext": (30412, 9813), "farbwerk360": (40077, 12001), "leakshield": (51234, 777)}
SOFT_TIMEOUT = 10.0     # software sensor values expire like on the device


class World:
    """The shared thermal model. Time runs ``speed`` times faster than real time."""

    def __init__(self, speed: float = 4.0, seed: int = 7):
        self.speed = speed
        self.rng = random.Random(seed)
        self.real_last = time.monotonic()
        self.t = 0.0
        self.coolant = 29.0
        self.ambient = 23.0
        self.devices: list[SimDevice] = []
        self.lock = threading.RLock()
        self.cpu_w = 45.0
        self.gpu_w = 25.0

    def session(self, t: float) -> float:
        """0..1: how hard the PC works (a gaming session every 5 minutes of model time)."""
        phase = t % 300.0
        if phase < 60:
            return 0.0
        if phase < 80:
            return (phase - 60) / 20
        if phase < 220:
            return 1.0
        if phase < 250:
            return 1.0 - (phase - 220) / 30
        return 0.0

    def advance(self) -> None:
        with self.lock:
            now = time.monotonic()
            real_dt = min(5.0, now - self.real_last)
            self.real_last = now
            dt_total = real_dt * self.speed
            while dt_total > 1e-6:
                dt = min(0.5, dt_total)
                dt_total -= dt
                self._step(dt)

    def _step(self, dt: float) -> None:
        self.t += dt
        g = self.session(self.t)
        noise = self.rng.uniform(-4, 4)
        self.cpu_w = 45 + 105 * g + noise
        self.gpu_w = 25 + 255 * g + noise * 2
        self.ambient = 23.0 + 0.6 * math.sin(2 * math.pi * self.t / 1800.0)
        for d in self.devices:
            d.step(dt / self.speed)
        fans = [f for d in self.devices if d.radiator for f in d.fans if not f.pump]
        frac = sum(f.rpm / f.max_rpm for f in fans) / len(fans) if fans else 0.5
        flow_factor = clamp(self.flow() / 80.0, 0.3, 1.0) ** 0.25
        conductance = (10.0 + 55.0 * frac ** 0.8) * flow_factor
        heat = self.cpu_w + self.gpu_w
        self.coolant += (heat - conductance * (self.coolant - self.ambient)) / 9000.0 * dt

    def pump_rpm(self) -> float:
        for d in self.devices:
            for f in d.fans:
                if f.pump:
                    return f.rpm
        return 3200.0

    def flow(self) -> float:
        return 0.035 * self.pump_rpm()

    @property
    def heat(self) -> float:
        return self.cpu_w + self.gpu_w

    def cpu_temp(self) -> float:
        return self.coolant + 0.26 * self.cpu_w + 8

    def gpu_temp(self) -> float:
        return self.coolant + 0.06 * self.gpu_w + 4


class SimFan:
    def __init__(self, key: str, max_rpm: float, pump: bool = False):
        self.key = key
        self.max_rpm = max_rpm
        self.pump = pump
        self.out = 0.0
        self.rpm = 0.0
        self.boost_until = 0.0

    def target_rpm(self) -> float:
        if self.out < 1.0:
            return 0.0
        if self.pump:
            return 1200 + 3600 * self.out / 100
        return self.max_rpm * (0.15 + 0.85 * self.out / 100)


def factory_control_report(spec: DeviceSpec) -> bytearray:
    """A plausible settings report, as a new device (or one set up with aquasuite) would have."""
    buf = bytearray(spec.ctrl_length)
    buf[0] = spec.ctrl_id
    if spec.flow_pulses is not None:
        buf[spec.flow_pulses:spec.flow_pulses + 2] = (169).to_bytes(2, "big")
    rep = control.ControlReport(spec, buf)
    delta_curve = ([round(200 + i * 800 / 15) for i in range(16)],
                   [round(2000 + 8000 * (i / 15) ** 1.4) for i in range(16)])
    factory = ([2700, 2810, 2890, 2980, 3060, 3150, 3230, 3320, 3400, 3490, 3570, 3660, 3740, 3830, 3910, 4000],
               [0, 140, 280, 500, 800, 1200, 1680, 2260, 2920, 3660, 4500, 5420, 6440, 7540, 8720, 10000])
    for i, f in enumerate(spec.fans):
        if f.ctrl is None:
            continue
        fc = control.FanControl()
        if spec.kind == "quadro" and i < 3:
            # curves on software sensor 1, as aquasuite users feed a Delta T into it
            fc.mode, fc.source = control.MODE_CURVE, len(spec.temps)
            fc.curve_temps, fc.curve_powers = delta_curve
        elif f.pump:
            fc.mode, fc.source = control.MODE_CURVE, 0
            fc.curve_temps = factory[0]
            fc.curve_powers = [4000 + round(p * 0.6) for p in factory[1]]
        else:
            fc.mode, fc.source = control.MODE_CURVE, 0
            fc.curve_temps, fc.curve_powers = factory
        rep.set_fan(i, fc)
        rep.set_setup(i, control.FanSetup(hold_min=True, min_power=2000 if not f.pump else 3000,
                                          max_power=10000, fallback=10000))
    return bytearray(rep.seal()) if spec.ctrl_crc else rep.buf


class SimDevice:
    def __init__(self, world: World, kind: str, radiator: bool = True):
        self.world = world
        self.spec = BY_KIND[kind]
        self.serial = SERIALS.get(kind, (11111, 22222))
        self.radiator = radiator
        self.fans = [SimFan(f.key, 4800 if f.pump else 1800, f.pump) for f in self.spec.fans]
        self.ctrl = factory_control_report(self.spec) if self.spec.family == "standard" and self.spec.ctrl_id else None
        self.soft: list[tuple[float, int, float] | None] = [None] * 16
        self.settings_writes = 0
        self.save_reports = 0
        self.leakshield_feed: tuple[float | None, float | None] = (None, None)
        world.devices.append(self)

    # --------------------------------------------------------------- values
    def temps(self) -> dict[str, float | None]:
        w = self.world
        k = self.spec.kind
        if k == "quadro":
            return {"temp1": w.coolant + w.heat * 0.004, "temp2": w.ambient + 0.3, "temp3": w.coolant - 0.4,
                    "temp4": None}
        if k == "octo":
            return {"temp1": w.coolant + w.heat * 0.004, "temp2": w.ambient + 0.4, "temp3": None, "temp4": None}
        if k == "d5next":
            return {"temp1": w.coolant - 0.2}
        if k == "highflownext":
            return {"temp1": w.coolant, "temp2": w.coolant - 1.1}
        if k == "farbwerk360":
            return {"temp1": w.ambient + 1.5, "temp2": None, "temp3": None, "temp4": None}
        if k == "leakshield":
            return {"temp1": w.ambient + 2.0, "temp2": w.coolant}
        return {}

    def soft_value(self, slot: int) -> float | None:
        item = self.soft[slot] if 0 <= slot < len(self.soft) else None
        if item is None or time.monotonic() - item[2] > SOFT_TIMEOUT:
            return None
        return item[0]

    def source_value(self, index: int) -> float | None:
        temps = self.temps()
        keys = [t.key for t in self.spec.temps]
        if index < len(keys):
            return temps.get(keys[index])
        return self.soft_value(index - len(keys))

    # --------------------------------------------------------------- control (device firmware)
    def step(self, real_dt: float) -> None:
        now = time.monotonic()
        if self.ctrl is not None:
            rep = control.ControlReport(self.spec, self.ctrl)
            outs = [f.out for f in self.fans]
            for i, f in enumerate(self.spec.fans):
                if f.ctrl is None:
                    continue
                fc = rep.fan(i)
                st = rep.setup(i) or control.FanSetup(min_power=0, max_power=10000, fallback=10000)
                if fc.mode == control.MODE_MANUAL:
                    out = fc.pwm / 100
                elif fc.mode in (control.MODE_CURVE, control.MODE_PID):
                    x = self.source_value(fc.source) if fc.source != control.SOURCE_NONE else None
                    if x is None:
                        c = None
                    elif fc.mode == control.MODE_CURVE:
                        pts = [(t / 100, p / 100) for t, p in zip(fc.curve_temps, fc.curve_powers)]
                        c = interpolate(pts, x)
                    else:
                        c = clamp((x - fc.pid[0] / 100) * 12 + 30)
                    out = scale_power(c, st.min_power / 100, st.max_power / 100, st.hold_min, st.fallback / 100)
                else:
                    other = fc.mode - control.MODE_FOLLOW
                    out = outs[other] if 0 <= other < len(outs) else 0.0
                fan = self.fans[i]
                if st.start_boost and out > 0 and fan.out <= 0:
                    fan.boost_until = now + 2.0
                fan.out = 100.0 if now < fan.boost_until and out > 0 else out
        for fan in self.fans:
            tau = 1.2
            fan.rpm += (fan.target_rpm() - fan.rpm) * (1 - math.exp(-real_dt / tau))

    # --------------------------------------------------------------- reports
    def status_values(self) -> dict[str, float | None]:
        w = self.world
        v: dict[str, float | None] = dict(self.temps())
        for i in range(16):
            v[f"virt{i + 1}"] = self.soft_value(i)
        if self.spec.kind in ("quadro", "octo"):
            v["flow"] = w.flow()
            v["vcc"] = 12.13
        elif self.spec.kind == "d5next":
            v["flow"] = None
            v["vcc12"], v["vcc5"] = 12.05, 5.03
        elif self.spec.kind == "highflownext":
            v["flow"] = w.flow()
            v["quality"], v["conductivity"], v["vcc5"], v["vcc5usb"] = 100.0, 12.5, 5.02, 5.01
            t1, t2 = v["temp1"], v["temp2"]
            v["heat"] = round(w.flow() / 3600 * 4186 * (t1 - t2)) if t1 is not None and t2 is not None else None
        elif self.spec.kind == "leakshield":
            pump, flow = self.leakshield_feed
            v.update({"pressure": 12.0 + (pump or 0) / 1000, "pressure_min": 5.0, "pressure_target": 12.0,
                      "pressure_max": 25.0, "pump_in": pump, "flow_in": flow, "reservoir": 300.0, "filled": 262.0})
        for f in self.fans:
            v[f"{f.key}.percent"] = round(f.out, 2)
            v[f"{f.key}.rpm"] = round(f.rpm)
            v[f"{f.key}.voltage"] = 12.1 if f.out > 0 else 12.1
            cur = (0.1 + 0.9 * (f.rpm / f.max_rpm) ** 2) if f.pump else (0.02 + 0.16 * (f.rpm / f.max_rpm) ** 2)
            v[f"{f.key}.current"] = round(cur if f.rpm > 1 else 0.0, 3)
            v[f"{f.key}.power"] = round(v[f"{f.key}.current"] * 12.1, 2)
        return v

    def status_report(self) -> bytes:
        types = [0 if s is None else s[1] for s in self.soft]
        return encode_status(self.spec, self.status_values(), self.serial, 1030, 318, virt_types=types)


class SimTransport:
    """Looks like HidrawTransport to HidDevice."""

    def __init__(self, device: SimDevice):
        self.device = device
        self._last = 0.0
        self.closed = False
        self.fail_writes = False

    def close(self) -> None:
        self.closed = True

    def read_input(self, timeout: float = 0.0) -> list[bytes]:
        if self.closed:
            raise DeviceError("simulated device closed")
        self.device.world.advance()
        now = time.monotonic()
        if now - self._last < 0.2:
            if timeout:
                time.sleep(min(timeout, 0.2))
            return []
        self._last = now
        return [self.device.status_report()]

    def get_feature(self, report_id: int, length: int) -> bytes:
        d = self.device
        if d.ctrl is not None and report_id == d.spec.ctrl_id:
            return bytes(d.ctrl[:length])
        if d.spec.status_via_feature and report_id == d.spec.status_id:
            return d.status_report()[:length]
        raise DeviceError(f"simulated {d.spec.name} has no feature report {report_id:#04x}")

    def set_feature(self, data: bytes) -> None:
        d = self.device
        if self.fail_writes:
            raise DeviceError("simulated write failure")
        if d.ctrl is not None and data[0] == d.spec.ctrl_id:
            if len(data) != len(d.ctrl):
                raise DeviceError("settings report has the wrong length")
            if d.spec.ctrl_crc and not is_sealed(data):
                raise DeviceError("settings report checksum is wrong")
            d.ctrl = bytearray(data)
            d.settings_writes += 1
            return
        if data == d.spec.save_report:
            d.save_reports += 1
            return
        raise DeviceError(f"unexpected feature report {data[0]:#04x}")

    def write_output(self, data: bytes) -> None:
        d = self.device
        ss = d.spec.soft_sensors
        if ss and data[0] == ss.report_id and len(data) == ss.length:
            if not is_sealed(data):
                raise DeviceError("software sensor report checksum is wrong")
            now = time.monotonic()
            for i, item in enumerate(control.parse_soft_sensor_report(ss, data)):
                d.soft[i] = None if item is None else (item[0], item[1], now)
            return
        if d.spec.leakshield_feed and data[0] == 0x04 and len(data) == control.LEAKSHIELD_FEED_LENGTH + 2:
            pump = int.from_bytes(data[1:3], "big")
            flow = int.from_bytes(data[3:5], "big")
            d.leakshield_feed = (None if data[33] == 0 else float(pump), None if data[34] == 0 else flow / 10)
            return
        raise DeviceError(f"unexpected output report {data[0]:#04x}")


class SimSystemSensors:
    """CPU/GPU temperatures and load that follow the simulated loop."""

    def __init__(self, world: World):
        self.world = world

    def poll(self) -> list[Reading]:
        w = self.world
        return [
            Reading("system/k10temp/tctl", "CPU Tctl", "temperature", round(w.cpu_temp(), 1), "System", "system"),
            Reading("system/amdgpu/edge", "GPU edge", "temperature", round(w.gpu_temp(), 1), "System", "system"),
            Reading("system/cpu_load", "CPU load", "percent", round(5 + 80 * w.session(w.t), 1), "System", "system"),
        ]

    def close(self) -> None:
        pass


def resealed(buf: bytearray) -> bytes:
    seal(buf)
    return bytes(buf)


DEMO_KINDS = ("quadro", "d5next", "highflownext")


def demo_devices(world: World | None = None, kinds: tuple[str, ...] = DEMO_KINDS):
    """HidDevices on simulated transports, ready for the engine."""
    from .device import HidDevice

    world = world or World()
    out = []
    for kind in kinds:
        sim = SimDevice(world, kind, radiator=kind in ("quadro", "octo"))
        out.append(HidDevice(sim.spec, SimTransport(sim), f"sim:{kind}", "simulated"))
    return world, out
