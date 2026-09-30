"""Controllers, the output stage and virtual sensors."""

import pytest

from aquasuitelinux.core import controllers, virtual
from aquasuitelinux.core.config import ControllerConfig, OutputConfig, VirtualSensorConfig
from aquasuitelinux.core.errors import ConfigError
from aquasuitelinux.core.model import Reading


def test_interpolate():
    pts = [[20, 10], [30, 50], [40, 100]]
    assert controllers.interpolate(pts, 10) == 10
    assert controllers.interpolate(pts, 25) == 30
    assert controllers.interpolate(pts, 35) == 75
    assert controllers.interpolate(pts, 99) == 100


def test_scale_power_semantics():
    assert controllers.scale_power(None, 20, 100, True, 90) == 90          # input missing -> fallback
    assert controllers.scale_power(0, 20, 100, True, 100) == 20            # hold minimum power
    assert controllers.scale_power(0, 20, 100, False, 100) == 0            # fan stops
    assert controllers.scale_power(50, 20, 100, False, 100) == 60          # scaled into [min, max]
    assert controllers.scale_power(100, 20, 80, False, 100) == 80


def test_curve_hysteresis():
    c = ControllerConfig(kind="curve", points=[[0, 0], [10, 100]], hysteresis=1.0)
    st = controllers.ControllerState()
    assert controllers.evaluate(c, st, 5.0, 1, {}, {}) == 50
    assert controllers.evaluate(c, st, 4.5, 1, {}, {}) == 50               # within hysteresis: hold
    assert controllers.evaluate(c, st, 3.9, 1, {}, {}) == pytest.approx(39)
    assert controllers.evaluate(c, st, 4.2, 1, {}, {}) == pytest.approx(42)  # rising follows at once
    assert controllers.evaluate(c, st, None, 1, {}, {}) is None


def test_pid_moves_towards_target_and_does_not_wind_up():
    c = ControllerConfig(kind="target", target=30, kp=10, ki=0.5, kd=0)
    st = controllers.ControllerState()
    out_hot = [controllers.evaluate(c, st, 35, 1, {}, {}) for _ in range(200)]
    assert out_hot[-1] == 100
    assert st.integral <= 100
    out = controllers.evaluate(c, st, 29, 1, {}, {})
    assert out < 100
    for _ in range(100):
        out = controllers.evaluate(c, st, 25, 1, {}, {})
    assert out == 0


def test_two_point_and_mix_and_follow():
    two = ControllerConfig(id="t", kind="two_point", on_above=40, off_below=35, on_power=90, off_power=10)
    st = controllers.ControllerState()
    assert controllers.evaluate(two, st, 38, 1, {}, {}) == 10
    assert controllers.evaluate(two, st, 41, 1, {}, {}) == 90
    assert controllers.evaluate(two, st, 37, 1, {}, {}) == 90
    assert controllers.evaluate(two, st, 34, 1, {}, {}) == 10
    mix = ControllerConfig(kind="mix", sources=["a", "b"], mix="max")
    assert controllers.evaluate(mix, controllers.ControllerState(), None, 1, {"a": 30, "b": 55}, {}) == 55
    mix.mix = "average"
    assert controllers.evaluate(mix, controllers.ControllerState(), None, 1, {"a": 30, "b": 50}, {}) == 40
    follow = ControllerConfig(kind="follow", follow="dev/fan1")
    assert controllers.evaluate(follow, controllers.ControllerState(), None, 1, {}, {"dev/fan1": 66}) == 66


def test_controller_order_puts_mix_last_and_survives_cycles():
    a = ControllerConfig(id="a", kind="mix", sources=["b"])
    b = ControllerConfig(id="b", kind="curve")
    c = ControllerConfig(id="c", kind="mix", sources=["c"])
    ids = [x.id for x in controllers.order([a, b, c])]
    assert ids.index("b") < ids.index("a") and "c" in ids


def test_output_stage_boost_and_ramp():
    oc = OutputConfig(min_power=20, max_power=100, hold_min=False, start_boost=True, ramp_down=10)
    st = controllers.StageState(applied=0.0)
    assert controllers.output_stage(50, oc, st, 1, now=100.0) == 100          # boost from standstill
    assert controllers.output_stage(50, oc, st, 1, now=103.0) == pytest.approx(90)   # ramp down 10 %/s
    assert controllers.output_stage(50, oc, st, 1, now=104.0) == pytest.approx(80)


def _readings(**vals):
    return {k: Reading(k, k, "temperature", v) for k, v in vals.items()}


def test_delta_t_and_smoothing():
    vs = virtual.VirtualSensors()
    dt = VirtualSensorConfig(id="dt", name="Delta T", kind="difference", inputs=["w", "a"])
    r = _readings(w=33.5, a=24.0)
    out = vs.evaluate([dt], r, 1.0)
    assert out[0].value == pytest.approx(9.5) and out[0].unit == "K" and out[0].kind == "delta"
    smooth = VirtualSensorConfig(id="s", kind="difference", inputs=["w", "a"], smoothing=10)
    vs.evaluate([smooth], _readings(w=30, a=20), 1.0)
    v = vs.evaluate([smooth], _readings(w=40, a=20), 1.0)[0].value
    assert 10 < v < 12


def test_other_virtual_kinds():
    vs = virtual.VirtualSensors()
    r = _readings(a=20.0, b=30.0, c=40.0)
    r["flow"] = Reading("flow", "flow", "flow", 120.0)
    cfgs = [
        VirtualSensorConfig(id="avg", kind="average", inputs=["a", "b", "c"]),
        VirtualSensorConfig(id="mx", kind="max", inputs=["a", "b", "missing"]),
        VirtualSensorConfig(id="sc", kind="scale", inputs=["a"], factor=1.8, offset=32),
        VirtualSensorConfig(id="k", kind="constant", value=7.5),
        VirtualSensorConfig(id="ex", kind="expression", inputs=["a", "b"], expression="max(a, b) - 25 if a < b else 0"),
        VirtualSensorConfig(id="heat", kind="heat_load", inputs=["flow", "c", "b"]),
        VirtualSensorConfig(id="chain", kind="difference", inputs=["virtual/avg", "a"]),
    ]
    out = {x.id: x.value for x in vs.evaluate(cfgs, r, 1.0)}
    assert out["virtual/avg"] == 30
    assert out["virtual/mx"] == 30
    assert out["virtual/sc"] == 68
    assert out["virtual/k"] == 7.5
    assert out["virtual/ex"] == 5
    assert out["virtual/heat"] == pytest.approx(120 / 3600 * 4186 * 10, rel=1e-3)
    assert out["virtual/chain"] == 10


def test_formula_sandbox_rejects_code():
    for bad in ("__import__('os').system('x')", "a.real", "open('x')", "[a for a in b]", "lambda: 1"):
        with pytest.raises(ConfigError):
            virtual.compile_expression(bad)
    assert virtual.evaluate_expression("clamp(a * 2, 0, 50) + sqrt(16)", [30]) == 54


def test_virtual_cycle_detected():
    a = VirtualSensorConfig(id="a", kind="difference", inputs=["virtual/b", "x"])
    b = VirtualSensorConfig(id="b", kind="difference", inputs=["virtual/a", "x"])
    with pytest.raises(ConfigError):
        virtual.validate([a, b])
