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


def feeding_config() -> Config:
    """A config that sends software sensor data to devices (off by default)."""
    cfg = Config()
    cfg.settings.device_feeds = True
    return cfg


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
    cfg = feeding_config()
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
    cfg = feeding_config()
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
    cfg = feeding_config()
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


def test_rewrite_guard_stops_writing_rejected_settings(monkeypatch):
    import aquasuitelinux.core.device as device_mod
    monkeypatch.setattr(device_mod, "CTRL_CACHE_SECONDS", 0.0)       # re-read the settings every tick
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input=f"{QUADRO}/temp1", points=[[20, 20], [40, 100]])]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c")
    eng, _world, devs = sim_engine(cfg)
    q = sim(devs[0])
    original = devs[0].transport.set_feature

    def stubborn(data):
        # a device that silently resets the first curve point of fan 1 after every write
        original(data)
        if data[0] == devs[0].spec.ctrl_id:
            rep = control.ControlReport(devs[0].spec, q.ctrl)
            fc = rep.fan(0)
            fc.curve_temps = [1234, *fc.curve_temps[1:]]
            rep.set_fan(0, fc)
            q.ctrl = bytearray(rep.seal())
    devs[0].transport.set_feature = stubborn
    try:
        snap = run_ticks(eng, 22, pause=0.3)
        assert q.settings_writes <= 5                     # 3 device writes, then software control
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software" and "didn't keep" in o["reason"]
    finally:
        eng.stop()


def test_device_not_following_curve_moves_to_software(monkeypatch):
    import aquasuitelinux.core.engine as engine_mod
    monkeypatch.setattr(engine_mod, "CURVE_CHECK_SECONDS", 1.0)
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input=f"{QUADRO}/temp1", points=[[0, 20], [100, 20]])]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c", min_power=0, hold_min=False)
    eng, _world, devs = sim_engine(cfg)
    q = sim(devs[0])
    q.source_value = lambda index: None                # the firmware reads "nothing": runs at fallback (100 %)
    try:
        snap = run_ticks(eng, 14, pause=0.25)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software" and "didn't follow" in o["reason"]
        assert any("Please report" in e[2] for e in snap["events"])
    finally:
        eng.stop()


def test_pump_is_never_stopped_by_a_controller():
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="z", kind="fixed", power=0)]
    cfg.outputs[f"{D5}/pump"] = OutputConfig(controller="z", min_power=30, hold_min=False, placement="software")
    eng, _world, _devs = sim_engine(cfg, kinds=("d5next",))
    try:
        snap = run_ticks(eng, 4)
        o = {o["id"]: o for o in snap["outputs"]}[f"{D5}/pump"]
        assert o["target"] == 30
    finally:
        eng.stop()


def _delta_t_curve_config(**output):
    cfg = feeding_config()
    cfg.virtual_sensors = [VirtualSensorConfig(id="dt", kind="difference",
                                               inputs=[f"{QUADRO}/temp1", f"{QUADRO}/temp2"])]
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input="virtual/dt", points=[[2, 20], [10, 100]])]
    cfg.outputs[f"{QUADRO}/fan1"] = OutputConfig(controller="c", **output)
    return cfg


def test_feed_send_failure_stops_at_once_and_moves_to_software():
    """What a real QUADRO did: the software sensor report can't be sent (EPROTO / ETIMEDOUT)."""
    from aquasuitelinux.core.errors import DeviceError
    eng, _world, devs = sim_engine(_delta_t_curve_config())
    q = sim(devs[0])
    attempts = []

    def fail(data):
        attempts.append(data)
        raise DeviceError("/dev/hidraw8: writing report 0x04 failed: Protocol error")
    devs[0].transport.write_output = fail
    try:
        snap = run_ticks(eng, 8)
        assert len(attempts) == 1                                   # no retry storm
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software" and "not accepting" in o["reason"]
        rep = control.ControlReport(devs[0].spec, q.ctrl)
        assert rep.fan(0).mode == control.MODE_MANUAL               # never stored a curve it can't feed
        warnings = [e[2] for e in snap["events"] if "could not send software sensor values" in e[2]]
        assert len(warnings) == 1
        assert {d["key"]: d for d in snap["devices"]}[QUADRO]["feed"] == "broken"
        # "Look for devices again" gives it another chance
        devs[0].transport.write_output = lambda data: None          # accepts silently from now on
        eng.rescan()
        assert QUADRO not in eng.feed_broken
    finally:
        eng.stop()


def test_pending_output_is_not_written_until_the_feed_is_confirmed():
    eng, _world, devs = sim_engine(_delta_t_curve_config())
    q = sim(devs[0])
    try:
        snap = eng.tick()
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "pending" and q.settings_writes == 0
        snap = run_ticks(eng, 6)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "device"
        assert q.settings_writes == 1                                # only the curve, once
    finally:
        eng.stop()


def test_pending_gives_up_after_deadline(monkeypatch):
    import aquasuitelinux.core.engine as engine_mod
    monkeypatch.setattr(engine_mod, "PENDING_SECONDS", 1.0)
    cfg = _delta_t_curve_config()
    cfg.virtual_sensors[0].inputs = [f"{QUADRO}/temp4", f"{QUADRO}/temp2"]   # temp4 isn't connected: no value
    eng, _world, _devs = sim_engine(cfg)
    try:
        snap = run_ticks(eng, 8)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software" and o["target"] == 100      # input missing -> fallback power
    finally:
        eng.stop()


def test_devices_without_a_feed_path_use_software():
    eng, _world, devs = sim_engine(_delta_t_curve_config())
    devs[0].feed_mode = "none"
    try:
        snap = run_ticks(eng, 3)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software" and "no software sensors" in o["reason"]
    finally:
        eng.stop()


class FakeBulk:
    """Stands in for the usbfs channel and hands the data to the simulated device."""

    def __init__(self, transport):
        self.transport = transport
        self.sent = 0
        self.closed = 0

    def write(self, data):
        self.sent += 1
        self.transport.write_output(data)

    def close(self):
        self.closed += 1

    def describe(self):
        return "fake bulk endpoint"


def test_feeds_are_off_by_default_and_remote_curves_run_in_software():
    cfg = _delta_t_curve_config()
    cfg.settings.device_feeds = False
    eng, _world, devs = sim_engine(cfg)
    sent = []
    forward = devs[0].transport.write_output
    devs[0].transport.write_output = lambda data: (sent.append(data), forward(data))
    try:
        snap = run_ticks(eng, 4)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software" and "turned off in Settings" in o["reason"]
        assert sent == []                                              # nothing goes to the bulk endpoint
        assert {d["key"]: d for d in snap["devices"]}[QUADRO]["feed"] == "off"
        assert control.ControlReport(devs[0].spec, sim(devs[0]).ctrl).fan(0).mode == control.MODE_MANUAL
        # switched on while running: the curve moves onto the device once the feed is confirmed
        on = eng.config.copy()
        on.settings.device_feeds = True
        eng.set_config(on, save=False)
        snap = run_ticks(eng, 8)
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "device" and sent
        # and off again: sending stops, the curve goes back to software
        eng.set_config(cfg.copy(), save=False)
        n = len(sent)
        snap = run_ticks(eng, 3)
        assert len(sent) == n
        assert {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]["placement"] == "software"
    finally:
        eng.stop()


def test_device_that_goes_quiet_while_receiving_data_gets_no_more(monkeypatch):
    import aquasuitelinux.core.device as device_mod
    monkeypatch.setattr(device_mod, "STALE_AFTER", 0.5)
    eng, _world, devs = sim_engine(_delta_t_curve_config())
    bulk = devs[0].bulk = FakeBulk(devs[0].transport)
    try:
        run_ticks(eng, 6)
        assert eng.feed_ok.get(QUADRO) and bulk.sent
        devs[0].transport.read_input = lambda timeout=0.0: []      # the device stops reporting
        snap = run_ticks(eng, 5)
        assert QUADRO in eng.feed_broken and bulk.closed            # stopped, USB interface released
        assert any("stopped sending sensor data" in e[2] for e in snap["events"])
        n = bulk.sent
        run_ticks(eng, 3)
        assert bulk.sent == n
        o = {o["id"]: o for o in eng.snapshot()["outputs"]}[f"{QUADRO}/fan1"]
        assert o["placement"] == "software"
    finally:
        eng.stop()


def test_device_that_disconnects_soon_after_feeding_is_not_fed_again():
    from aquasuitelinux.core.errors import DeviceError
    eng, _world, devs = sim_engine(_delta_t_curve_config())
    try:
        run_ticks(eng, 3)
        assert QUADRO in eng.fed_devices

        def gone():
            raise DeviceError("/dev/hidraw8 was disconnected")
        devs[0].poll = gone
        snap = run_ticks(eng, 1)
        assert QUADRO not in eng.devices and QUADRO in eng.feed_broken
        assert any("disconnected soon after" in e[2] for e in snap["events"])
    finally:
        eng.stop()


def test_a_failing_step_is_reported_once_and_the_rest_keeps_running(monkeypatch):
    cfg = Config()
    cfg.controllers = [ControllerConfig(id="f", kind="fixed", power=40)]
    cfg.outputs[f"{QUADRO}/fan2"] = OutputConfig(controller="f", placement="software")
    eng, _world, devs = sim_engine(cfg, kinds=("quadro", "d5next"))

    def broken(*_a):
        raise KeyError("boom")
    monkeypatch.setattr(eng, "_verify_device_outputs", broken)
    d5 = next(d for d in devs if d.spec.kind == "d5next")
    monkeypatch.setattr(d5, "poll", lambda: 1 / 0)                     # a bug, not a disconnect
    try:
        first = eng.tick()
        snap = run_ticks(eng, 4)
        assert snap["time"] > first["time"]                            # the snapshot keeps updating
        assert any(r["id"] == f"{QUADRO}/temp1" for r in snap["readings"])
        assert d5.key in eng.devices                                   # not dropped
        errors = [e[2] for e in snap["events"] if e[1] == "error"]
        assert sum("checking device curves" in e for e in errors) == 1
        assert sum(f"reading {d5.key}" in e for e in errors) == 1
        o = {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan2"]
        assert o["placement"] == "software" and o["target"] == 43      # 40 % scaled into 5…100 %
    finally:
        eng.stop()


def test_loaded_config_is_adopted_by_the_connected_device():
    """A setup saved under another key (hwmon fallback, another QUADRO) applies to the one connected."""
    eng, _world, _devs = sim_engine(Config())
    try:
        run_ticks(eng, 1)
        cfg = Config()
        cfg.controllers = [ControllerConfig(id="f", kind="fixed", power=50)]
        cfg.outputs["quadro/fan1"] = OutputConfig(controller="f", placement="software")
        eng.set_config(cfg, save=False)
        assert f"{QUADRO}/fan1" in eng.config.outputs
        snap = run_ticks(eng, 2)
        assert {o["id"]: o for o in snap["outputs"]}[f"{QUADRO}/fan1"]["placement"] == "software"
    finally:
        eng.stop()


def test_setup_summary():
    cfg = Config()
    assert not cfg.has_setup() and cfg.setup_summary() == "0 virtual sensors, 0 controllers, 0 fans assigned"
    cfg.outputs["x/fan1"] = OutputConfig(name="only a name")
    assert not cfg.has_setup()
    cfg.controllers = [ControllerConfig(id="f", kind="fixed")]
    cfg.outputs["x/fan1"].controller = "f"
    assert cfg.has_setup() and cfg.setup_summary() == "0 virtual sensors, 1 controller, 1 fan assigned"
