"""Report parsing and building, checked against reports captured from real devices."""

import pytest

from aquasuitelinux.core import control, devices, status
from aquasuitelinux.core.crc import crc16_usb, is_sealed, seal
from aquasuitelinux.core.transport import parse_report_descriptor


def test_crc16_usb_known_vector():
    # CRC-16/USB check value for "123456789"
    assert crc16_usb(b"123456789") == 0xB4C8


def test_captured_quadro_control_report_checksum(quadro_control):
    assert len(quadro_control) == devices.QUADRO.ctrl_length
    assert is_sealed(quadro_control)
    buf = bytearray(quadro_control)
    buf[-1] ^= 0xFF
    assert not is_sealed(buf)
    seal(buf)
    assert bytes(buf) == quadro_control


def test_parse_captured_quadro_status(quadro_status):
    spec = devices.QUADRO
    vals = {k: v for k, _l, _kind, v, _g in status.parse_status(spec, quadro_status)}
    assert vals["temp1"] == pytest.approx(32.61)
    assert vals["temp2"] == pytest.approx(27.19)
    assert vals["flow"] == pytest.approx(60.1)
    assert vals["fan2.rpm"] == 1759
    assert vals["fan1.voltage"] == pytest.approx(12.13)
    assert vals["virt1"] is None                     # 0x7FFF = not set
    assert status.device_serial(spec, quadro_status) == "06140-01384"
    assert status.firmware_version(spec, quadro_status) == 1028


def test_decode_captured_quadro_settings(quadro_control):
    rep = control.ControlReport(devices.QUADRO, quadro_control)
    assert rep.valid
    assert rep.flow_pulses() == 169
    assert rep.temp_offsets() == [0.0, 0.0, 0.0, 0.0]
    fc = rep.fan(0)
    assert fc.mode == control.MODE_MANUAL and fc.pwm == 10000 and fc.source == control.SOURCE_NONE
    assert fc.curve_temps[0] == 2700 and fc.curve_temps[-1] == 4000
    assert fc.curve_powers[-1] == 10000
    st = rep.setup(0)
    assert (st.min_power, st.max_power, st.fallback) == (500, 10000, 10000)


def test_modify_settings_roundtrip(quadro_control):
    rep = control.ControlReport(devices.QUADRO, quadro_control)
    fc = rep.fan(2)
    fc.mode, fc.source = control.MODE_CURVE, 4
    fc.curve_temps = [200 + 50 * i for i in range(16)]
    rep.set_fan(2, fc)
    rep.set_setup(2, control.FanSetup(hold_min=False, start_boost=True, min_power=2500, max_power=9000,
                                      fallback=8000))
    rep.set_temp_offset(1, -0.8)
    rep.set_flow_pulses(420)
    data = rep.seal()
    assert is_sealed(data)
    again = control.ControlReport(devices.QUADRO, data)
    assert again.fan(2).mode == control.MODE_CURVE and again.fan(2).source == 4
    assert again.fan(2).curve_temps[5] == 450
    s = again.setup(2)
    assert (s.hold_min, s.start_boost, s.min_power, s.max_power, s.fallback) == (False, True, 2500, 9000, 8000)
    assert again.temp_offsets()[1] == pytest.approx(-0.8)
    assert again.flow_pulses() == 420
    # untouched outputs are unchanged
    assert again.fan(0) == control.ControlReport(devices.QUADRO, quadro_control).fan(0)


def test_software_sensor_report_matches_aquasuite_capture(quadro_soft):
    spec = devices.QUADRO.soft_sensors
    values = control.parse_soft_sensor_report(spec, quadro_soft)
    assert values[0] == (31.0, devices.SOFT_TEMPERATURE)
    assert all(v is None for v in values[1:])
    # building the same values gives the exact bytes aquasuite sent
    assert control.soft_sensor_report(spec, values) == quadro_soft


def test_source_index_mapping():
    spec = devices.QUADRO
    assert control.source_index(spec, "temp1") == 0
    assert control.source_index(spec, "temp4") == 3
    assert control.source_index(spec, "virt1") == 4
    assert control.source_index(spec, "virt16") == 19
    assert control.source_index(spec, "flow") is None
    assert control.source_key(spec, 5) == "virt2"
    assert control.source_key(spec, control.SOURCE_NONE) is None


def test_resample_curve_keeps_shape():
    from aquasuitelinux.core.controllers import interpolate
    pts = [(2, 20), (5, 40), (10, 100)]
    out = control.resample_curve(pts)
    assert len(out) == 16
    assert out[0] == (2.0, 20.0) and out[-1] == (10.0, 100.0)
    for x in (2, 3.3, 5, 7.7, 10):
        assert interpolate(out, x) == pytest.approx(interpolate(pts, x), abs=0.01)
    many = [(20 + i, i * 3) for i in range(30)]
    assert len(control.resample_curve(many)) == 16


def test_status_encode_decode_roundtrip_all_devices():
    for spec in devices.ALL_SPECS:
        values = {s.key: 21.5 for s in spec.temps}
        for f in spec.flows:
            values[f.key] = 88.8
        for fan in spec.fans:
            values[f"{fan.key}.rpm"] = 1234
        data = status.encode_status(spec, values, (111, 222))
        parsed = {k: v for k, _l, _kind, v, _g in status.parse_status(spec, data)}
        for s in spec.temps:
            assert parsed[s.key] == pytest.approx(21.5), (spec.kind, s.key)
        for f in spec.flows:
            assert parsed[f.key] == pytest.approx(88.8, abs=0.1), (spec.kind, f.key)
        for fan in spec.fans:
            if fan.status is not None or spec.family == "aquastreamxt":
                assert parsed[f"{fan.key}.rpm"] == pytest.approx(1234, rel=0.01), (spec.kind, fan.key)


def test_leakshield_feed_report():
    rep = control.leakshield_feed_report(2800, 145.5)
    assert len(rep) == 51 and rep[0] == 0x04
    assert int.from_bytes(rep[1:3], "big") == 2800 and rep[33] == 0x03
    assert int.from_bytes(rep[3:5], "big") == 1455 and rep[34] == 0x0C
    assert crc16_usb(rep[:49]) == int.from_bytes(rep[49:51], "big")


def test_aquaero_manual_write_layout():
    spec = devices.AQUAERO
    buf = bytearray(spec.ctrl_length)
    control.aquaero_set_manual(spec, buf, 1, 42.0)
    assert int.from_bytes(buf[0x55C + 2:0x55C + 4], "big") == 4200
    assert int.from_bytes(buf[0x220 + 0x10:0x220 + 0x12], "big") == 0x5C + 1
    assert control.aquaero_manual_power(spec, buf, 1) == 42.0


def test_parse_report_descriptor():
    desc = bytes([
        0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,
        0x85, 0x01, 0x75, 0x08, 0x95, 0xDB, 0x09, 0x02, 0x81, 0x02,     # input 1: 219 bytes
        0x85, 0x03, 0x96, 0xC0, 0x03, 0x09, 0x03, 0xB1, 0x02,           # feature 3: 960 bytes
        0x85, 0x04, 0x95, 0x42, 0x09, 0x04, 0x91, 0x02,                 # output 4: 66 bytes
        0xC0,
    ])
    reps = {(r.report_id, r.kind): r.length for r in parse_report_descriptor(desc)}
    assert reps[(1, "input")] == 0xDC
    assert reps[(3, "feature")] == 0x3C1
    assert reps[(4, "output")] == 0x43
