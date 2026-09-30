"""Demo mode: simulated devices plus a configuration that shows off the features."""

from __future__ import annotations

from .config import (
    DELTA_T_CURVE,
    AlarmConfig,
    Config,
    ControllerConfig,
    OutputConfig,
    ProfileConfig,
    VirtualSensorConfig,
)
from .engine import StaticProvider
from .simulator import SimSystemSensors, World, demo_devices

QUADRO = "quadro-10234-55001"
D5 = "d5next-21077-14342"
HFN = "highflownext-30412-09813"


def demo_provider(speed: float = 4.0) -> StaticProvider:
    world = World(speed=speed)
    _world, devices = demo_devices(world)
    return StaticProvider(devices, SimSystemSensors(world))


def demo_config() -> Config:
    cfg = Config()
    cfg.devices[QUADRO] = cfg.device(QUADRO)
    cfg.devices[QUADRO].sensor_names = {"temp1": "Coolant (GPU out)", "temp2": "Ambient air", "temp3": "Radiator out"}
    cfg.devices[D5] = cfg.device(D5)
    dt = VirtualSensorConfig(id="deltat", name="Coolant ΔT", kind="difference",
                             inputs=[f"{QUADRO}/temp1", f"{QUADRO}/temp2"], smoothing=5.0)
    heat = VirtualSensorConfig(id="heatload", name="Heat load", kind="heat_load",
                               inputs=[f"{QUADRO}/flow", f"{QUADRO}/temp1", f"{QUADRO}/temp3"], smoothing=5.0)
    cfg.virtual_sensors = [dt, heat]
    rad = ControllerConfig(id="radiator", name="Radiator ΔT curve", kind="curve", input="virtual/deltat",
                           points=[list(p) for p in DELTA_T_CURVE], hysteresis=0.2)
    quiet = ControllerConfig(id="quiet", name="Quiet ΔT curve", kind="curve", input="virtual/deltat",
                             points=[[3.0, 10.0], [6.0, 25.0], [9.0, 50.0], [12.0, 100.0]], hysteresis=0.2)
    pump = ControllerConfig(id="pump", name="Pump curve", kind="curve", input=f"{D5}/temp1",
                            points=[[28.0, 45.0], [34.0, 60.0], [40.0, 100.0]])
    cpu = ControllerConfig(id="cpu", name="CPU temperature curve", kind="curve", input="system/k10temp/tctl",
                           points=[[50.0, 20.0], [70.0, 45.0], [85.0, 100.0]], hysteresis=2.0)
    case = ControllerConfig(id="case", name="Case fans (max of ΔT and CPU)", kind="mix", sources=["radiator", "cpu"])
    cfg.controllers = [rad, quiet, pump, cpu, case]
    for i, name in enumerate(("Radiator top 1", "Radiator top 2", "Radiator front"), start=1):
        cfg.outputs[f"{QUADRO}/fan{i}"] = OutputConfig(name=name, controller="radiator", min_power=20)
    cfg.outputs[f"{QUADRO}/fan4"] = OutputConfig(name="Case exhaust", controller="case", min_power=25,
                                                 ramp_down=5.0)
    cfg.outputs[f"{D5}/pump"] = OutputConfig(name="D5 pump", controller="pump", min_power=30, hold_min=True)
    cfg.alarms = [
        AlarmConfig(id="hot", name="Coolant too warm", sensor=f"{QUADRO}/temp1", condition="above", threshold=45.0,
                    delay=5.0, actions=["notify", "fans_max"]),
        AlarmConfig(id="flow", name="Low flow", sensor=f"{QUADRO}/flow", condition="below", threshold=40.0,
                    delay=10.0, actions=["notify"]),
    ]
    cfg.profiles = [
        ProfileConfig(id="quietprofile", name="Quiet", assignments={f"{QUADRO}/fan{i}": "quiet" for i in (1, 2, 3)}),
        ProfileConfig(id="perf", name="Performance", assignments={f"{QUADRO}/fan{i}": "cpu" for i in (1, 2, 3)}),
    ]
    return cfg
