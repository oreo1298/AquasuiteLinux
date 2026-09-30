"""The engine against simulated devices: placement, feeds, write limiting, alarms, profiles."""

import pytest
from conftest import run_ticks

from aquasuitelinux.core import control
from aquasuitelinux.core.config import (
    AlarmConfig,
    Config,
    ControllerConfig,
    FeedConfig,
    OutputConfig,
    VirtualSensorConfig,
)
from aquasuitelinux.core.demo import D5, QUADRO
from aquasuitelinux.core.engine import Engine, StaticProvider
from aquasuitelinux.core.simulator import SimSystemSensors, World, demo_devices


def sim_engine(cfg: Config, kinds=("quadro",), speed=4.0):
    world = World(speed=speed)
    _w, devs = demo_devices(world, kinds)
    eng = Engine(cfg, StaticProvider(devs, SimSystemSensors(world)), mode="demo")
    return eng, world, devs


def sim(dev):
    return dev.transport.device


def test_demo_places_curves_on_device_and_feeds_delta_t(demo_engine):
    snap = run_ticks(demo_engine, 10)
    outs = {o["id"]: o for o in snap["outputs"]}
    for i in (1, 2, 3):
        o = outs[f"{QUADRO}/fan{i}"]
        assert o["placement"] == "device" and o["slot"] == 1
        # the simulated firmware follows the curve we stored, fed by our Delta T
        assert o["reported"] == pytest.approx(o["target"], abs=3)
    assert outs[f"{QUADRO}/fan4"]["placement"] == "software"      # "mix" runs in software
    assert outs[f"{D5}/pump"]["placement"] == "device"            # curve on the pump's own sensor
    assert outs[f"{D5}/fan"]["placement"] == "unmanaged"
    readings = {r["id"]: r for r in snap["readings"]}
    assert readings[f"{QUADRO}/virt1"]["value"] == pytest.approx(readings["virtual/deltat"]["value"], abs=0.1)
    dev = {d["key"]: d for d in snap["devices"]}[QUADRO]
    assert dev["feed"] == "ok"


def test_device_mode_writes_settings_once():
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input=f"{QUADRO}/temp1",
                                        points=[[20, 20], [40, 100]])]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c", min_power=10, fallback=90)
    eng, _world, devs = sim_engine(cfg)
    try:
        run_ticks(eng, 8)
        q = sim(devs[0])
        assert q.settings_writes == 1 and q.save_reports == 1
        rep = control.ControlReport(devs[0].spec, q.ctrl)
        fc = rep.fan(0)
        assert fc.mode == control.MODE_CURVE and fc.source == 0
        assert fc.curve_temps[0] == 2000 and fc.curve_temps[-1] == 4000
        assert rep.setup(0).fallback == 9000 and rep.setup(0).min_power == 1000
        # other outputs untouched
        assert rep.fan(3).mode == control.MODE_CURVE
    finally:
        eng.stop()


def test_software_mode_limits_writes():
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="c", kind="target", input=f"{QUADRO}/temp1", target=25, kp=4, ki=0.2)]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c", placement="software")
    cfg.device(QUADRO).write_interval = 3.0
    eng, _world, devs = sim_engine(cfg, speed=20.0)
    try:
        run_ticks(eng, 16, pause=0.25)       # ~4 s
        writes = sim(devs[0]).settings_writes
        assert 1 <= writes <= 3
        rep = control.ControlReport(devs[0].spec, sim(devs[0]).ctrl)
        assert rep.fan(0).mode == control.MODE_MANUAL
        st = rep.setup(0)
        assert (st.min_power, st.max_power) == (0, 10000)   # neutral limits: scaling is done in software
    finally:
        eng.stop()


def test_explicit_feed_and_unmanaged_outputs_untouched():
    cfg = Config()
    cfg.virtual_sensors = [VirtualSensorConfig(id="cpu", kind="scale", inputs=["system/k10temp/tctl"])]
    cfg.feeds = [FeedConfig(QUADRO, 5, "virtual/cpu")]
    eng, _world, devs = sim_engine(cfg)
    try:
        before = bytes(sim(devs[0]).ctrl)
        snap = run_ticks(eng, 6)
        readings = {r["id"]: r for r in snap["readings"]}
        # the device echoes the value sent on the previous tick
        assert readings[f"{QUADRO}/virt5"]["value"] == pytest.approx(readings["virtual/cpu"]["value"], abs=3)
        assert bytes(sim(devs[0]).ctrl) == before                 # nothing written to the settings
    finally:
        eng.stop()


def test_feed_that_is_not_echoed_falls_back_to_software():
    cfg = Config()
    cfg.virtual_sensors = [VirtualSensorConfig(id="dt", kind="difference",
                                               inputs=[f"{QUADRO}/temp1", f"{QUADRO}/temp2"])]
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input="virtual/dt", points=[[0, 20], [10, 100]])]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c")
    eng, _world, devs = sim_engine(cfg)
    q = sim(devs[0])
    q.world  # noqa: B018
    original = devs[0].transport.write_output
    devs[0].transport.write_output = lambda data: None          # device ignores software sensor data
    try:
        import aquasuitelinux.core.engine as engine_mod
        engine_mod.FEED_VERIFY_SECONDS = 1.0
        snap = run_ticks(eng, 12)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software"
        assert "not accepting" in o["reason"]
        assert any("does not report" in e[2] for e in snap["events"])
    finally:
        devs[0].transport.write_output = original
        import aquasuitelinux.core.engine as engine_mod
        engine_mod.FEED_VERIFY_SECONDS = 6.0
        eng.stop()


def test_alarm_forces_full_power(demo_engine):
    cfg = demo_engine.config.copy()
    cfg.alarms = [AlarmConfig(id="x", name="Test", sensor=f"{QUADRO}/temp2", condition="above", threshold=0,
                              delay=0, actions=["fans_max", "notify"])]
    demo_engine.set_config(cfg, save=False)
    snap = run_ticks(demo_engine, 4)
    managed = [o for o in snap["outputs"] if o["placement"] != "unmanaged"]
    assert managed and all(o["target"] == 100 for o in managed)
    assert snap["alarms"][0]["active"]


def test_profiles_and_overrides(demo_engine):
    run_ticks(demo_engine, 2)
    demo_engine.set_profile("Quiet")
    snap = run_ticks(demo_engine, 2)
    assert snap["profile"] == "Quiet"
    assert {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]["controller"] == "quiet"
    demo_engine.override(f"{QUADRO}/fan2", 77, 30)
    snap = run_ticks(demo_engine, 3)
    o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan2"]
    assert o["override"] and o["target"] == 77 and o["reported"] == pytest.approx(77, abs=0.5)


def test_exit_sets_fallback_and_invalidates_feeds():
    cfg = Config()
    cfg.virtual_sensors = [VirtualSensorConfig(id="dt", kind="difference",
                                               inputs=[f"{QUADRO}/temp1", f"{QUADRO}/temp2"])]
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input="virtual/dt"),
                       ControllerConfig(id="f", kind="fixed", power=30)]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c")
    cfg.outputs[f"{QUADRO}/fan2"] = OutputConfig(controller="f", fallback=85)
    eng, _world, devs = sim_engine(cfg)
    run_ticks(eng, 5)
    q = sim(devs[0])
    eng.stop()
    rep = control.ControlReport(devs[0].spec, q.ctrl)
    assert rep.fan(1).mode == control.MODE_MANUAL and rep.fan(1).pwm == 8500
    assert all(s is None for s in q.soft)


def test_device_key_adoption():
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="c", kind="fixed", power=40)]
    cfg.outputs["quadro/fan1"] = OutputConfig(controller="c", name="Top")
    cfg.virtual_sensors = [VirtualSensorConfig(id="v", kind="scale", inputs=["quadro/temp1"])]
    eng, _world, _devs = sim_engine(cfg)
    try:
        run_ticks(eng, 2)
        assert f"{QUADRO}/fan1" in eng.config.outputs and "quadro/fan1" not in eng.config.outputs
        assert eng.config.virtual_sensors[0].inputs == [f"{QUADRO}/temp1"]
    finally:
        eng.stop()


def test_history_and_snapshot_shape(demo_engine):
    snap = run_ticks(demo_engine, 5)
    for key in ("devices", "readings", "outputs", "controllers", "alarms", "feeds", "events", "profile"):
        assert key in snap
    h = demo_engine.history_for(["virtual/deltat", f"output/{QUADRO}/fan1"], 600)
    assert len(h["times"]) == 5
    assert len(h["series"]["virtual/deltat"]) == 5


def test_backup_restore_roundtrip(demo_engine):
    run_ticks(demo_engine, 2)
    b = demo_engine.backup(QUADRO)
    assert b["kind"] == "quadro"
    demo_engine.apply_device_settings(QUADRO, {"temp_offsets": [0.5, None, None, None], "flow_pulses": 300})
    assert demo_engine.device_settings(QUADRO)["flow_pulses"] == 300
    demo_engine.restore(QUADRO, b)
    s = demo_engine.device_settings(QUADRO)
    assert s["flow_pulses"] == 169 and s["temp_offsets"][0] == 0
