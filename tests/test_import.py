"""Importing aquasuite settings from the device and from files."""

import base64
import gzip
import io
import json
import zipfile

from aquasuitelinux.core import aquasuite, control, devices
from aquasuitelinux.core.config import Config
from aquasuitelinux.core.simulator import factory_control_report


def aquasuite_configured_quadro() -> bytes:
    return bytes(factory_control_report(devices.QUADRO))


def test_plan_import_from_device_settings():
    data = aquasuite_configured_quadro()
    plan = aquasuite.plan_import(devices.QUADRO, data, "quadro-1-2")
    assert len(plan.items) == 4
    # fans 1-3 share one curve on software sensor 1, fan 4 a curve on sensor 1
    ctrl_ids = {i.controller.id for i in plan.items[:3]}
    assert len(ctrl_ids) == 1
    assert plan.items[0].soft_slot == 1
    assert plan.items[0].controller.input == "quadro-1-2/virt1"
    assert plan.items[3].controller.input == "quadro-1-2/temp1"
    assert plan.items[0].output.min_power == 20 and plan.items[0].output.hold_min
    assert plan.soft_slots == [1]
    assert plan.flow_pulses == 169


def test_apply_import_maps_software_sensor_to_delta_t():
    data = aquasuite_configured_quadro()
    plan = aquasuite.plan_import(devices.QUADRO, data, "quadro-1-2")
    cfg = aquasuite.apply_import(Config(), plan, {1: "virtual/dt"})
    curve = next(c for c in cfg.controllers if c.input == "virtual/dt")
    assert cfg.outputs["quadro-1-2/fan1"].controller == curve.id
    assert cfg.outputs["quadro-1-2/fan3"].controller == curve.id
    assert [(f.device, f.slot, f.source) for f in cfg.feeds] == [("quadro-1-2", 1, "virtual/dt")]
    assert len(cfg.controllers) == 2


def test_imported_curve_points_are_compact_and_exact():
    data = aquasuite_configured_quadro()
    plan = aquasuite.plan_import(devices.QUADRO, data, "q")
    pts = plan.items[3].controller.points
    rep = control.ControlReport(devices.QUADRO, data).fan(3)
    from aquasuitelinux.core.controllers import interpolate
    full = [(t / 100, p / 100) for t, p in zip(rep.curve_temps, rep.curve_powers)]
    for x in (27, 30.5, 33, 36.2, 40):
        assert abs(interpolate(pts, x) - interpolate(full, x)) < 0.1
    assert len(pts) <= 16


def test_manual_modes_are_not_selected_by_default(quadro_control):
    plan = aquasuite.plan_import(devices.QUADRO, quadro_control, "q")
    assert all(i.controller.kind == "fixed" and not i.selected for i in plan.items)


def test_find_settings_in_binary_blob(quadro_control):
    blob = b"\x00" * 1000 + b"junk\x03\x03" + quadro_control + b"\xff" * 333
    found = aquasuite.find_settings(blob, "profile.bin")
    assert len(found) == 1 and found[0].spec.kind == "quadro" and found[0].data == quadro_control


def test_find_settings_in_xml_hex_and_base64(quadro_control):
    xml = ("<aquasuite><device type='quadro'><settings>" + quadro_control.hex(" ").upper() +
           "</settings></device></aquasuite>").encode()
    assert [f.spec.kind for f in aquasuite.find_settings(xml, "a.xml")] == ["quadro"]
    b64 = ("{\"data\": \"" + base64.b64encode(quadro_control).decode() + "\"}").encode()
    assert [f.spec.kind for f in aquasuite.find_settings(b64, "b.json")] == ["quadro"]


def test_find_settings_in_zip_and_gzip(quadro_control):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("aquasuite-data/device.dat", b"hdr" + quadro_control)
        zf.writestr("readme.txt", "nothing here")
    found = aquasuite.find_settings(buf.getvalue(), "backup.zip")
    assert len(found) == 1 and "device.dat" in found[0].origin
    assert len(aquasuite.find_settings(gzip.compress(quadro_control), "x.gz")) == 1


def test_find_settings_rejects_damaged_blocks(quadro_control):
    damaged = bytearray(quadro_control)
    damaged[100] ^= 0x10
    assert aquasuite.find_settings(bytes(damaged)) == []


def test_backup_format_roundtrip(quadro_control):
    b = aquasuite.make_backup(devices.QUADRO, "06140-01384", 1028, quadro_control)
    text = json.dumps(b).encode()
    found = aquasuite.find_settings(text, "quadro.json")
    assert found[0].data == quadro_control and found[0].serial == "06140-01384"
    assert aquasuite.backup_bytes(b, devices.QUADRO) == quadro_control
    import pytest

    from aquasuitelinux.core.errors import AquaError
    with pytest.raises(AquaError):
        aquasuite.backup_bytes(b, devices.OCTO)


def aquasuite_device_backup(names=None, serial="12345-67890") -> bytes:
    """An aquasuite device backup laid out like a real one (<DeviceBackup> XML, settings + names)."""
    from aquasuitelinux.core.simulator import name_report
    settings = aquasuite_configured_quadro() + bytes(52)       # aquasuite stores 1013 bytes for the QUADRO
    flash = bytes(name_report("quadro", names or {"temp1": "Water Temp", "temp2": "Ambient", "virt1": "Delta T",
                                                  "virt2": "GPU Core", "virt3": "CPU Package"}))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<DeviceBackup xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <DeviceSerial>{serial}</DeviceSerial>
  <DeviceType>quadro</DeviceType>
  <Software>1033</Software>
  <Time>2026-10-01T03:25:40.6083912Z</Time>
  <Data>
    <DeviceDataItem>
      <Name>settings</Name>
      <Data>{base64.b64encode(settings).decode()}</Data>
    </DeviceDataItem>
    <DeviceDataItem>
      <Name>flash</Name>
      <Data>{base64.b64encode(flash).decode()}</Data>
    </DeviceDataItem>
  </Data>
</DeviceBackup>""".encode()


def test_aquasuite_device_backup_brings_names_and_serial():
    found = aquasuite.find_settings(aquasuite_device_backup(), "quadro_profile.xml")
    assert len(found) == 1
    f = found[0]
    assert f.serial == "12345-67890" and "aquasuite backup of QUADRO 12345-67890, 2026-10-01" in f.origin
    assert f.names["temp1"] == "Water Temp" and f.names["virt1"] == "Delta T" and f.names["fan1"] == "Fan 1"


def test_names_set_up_the_delta_t_and_label_everything():
    f = aquasuite.find_settings(aquasuite_device_backup())[0]
    plan = aquasuite.plan_import(f.spec, f.data, "quadro-12345-67890", f.names)
    assert plan.items[0].description == "Fan 1: curve on software sensor 1 “Delta T”"
    assert plan.delta_t == ("temp1", "temp2")
    assert plan.sensor_names() == {"temp1": "Water Temp", "temp2": "Ambient"}
    slot_map = aquasuite.suggest_sources(plan, Config())
    assert slot_map == {1: aquasuite.NEW_DELTA_T}
    cfg = aquasuite.apply_import(Config(), plan, slot_map)
    [dt] = cfg.virtual_sensors
    assert dt.name == "Delta T" and dt.kind == "difference"
    assert dt.inputs == ["quadro-12345-67890/temp1", "quadro-12345-67890/temp2"]
    curve = next(c for c in cfg.controllers if c.kind == "curve" and c.input == f"virtual/{dt.id}")
    assert curve.name == "Delta T curve"
    assert cfg.outputs["quadro-12345-67890/fan1"].controller == curve.id
    assert cfg.devices["quadro-12345-67890"].sensor_names == {"temp1": "Water Temp", "temp2": "Ambient"}
    # importing again reuses that Delta T instead of making a second one
    assert aquasuite.suggest_sources(plan, cfg) == {1: f"virtual/{dt.id}"}


def test_user_names_are_kept_and_defaults_ignored():
    f = aquasuite.find_settings(aquasuite_device_backup({"temp1": "Sensor 1", "temp2": "Ambient",
                                                         "virt1": "Soft. Sensor 1"}))[0]
    plan = aquasuite.plan_import(f.spec, f.data, "q", f.names)
    assert plan.slot_name(1) == "" and plan.delta_t is None
    assert aquasuite.suggest_sources(plan, Config()) == {}
    cfg = Config()
    cfg.device("q").sensor_names["temp2"] = "Room"
    cfg = aquasuite.apply_import(cfg, plan, {})
    assert cfg.devices["q"].sensor_names == {"temp2": "Room"}


def test_follow_and_manual_outputs_dont_claim_their_stale_input():
    """aquasuite leaves the last input in a fan that now follows another or runs manually."""
    data = bytearray(aquasuite_configured_quadro())
    rep = control.ControlReport(devices.QUADRO, data)
    fc = rep.fan(3)
    fc.mode, fc.source = control.MODE_FOLLOW, 5              # follows fan 1, input field still software sensor 2
    rep.set_fan(3, fc)
    fc = rep.fan(2)
    fc.mode, fc.source = control.MODE_MANUAL, 6
    rep.set_fan(2, fc)
    plan = aquasuite.plan_import(devices.QUADRO, rep.seal(), "q")
    assert plan.soft_slots == [1]


def test_cpu_and_gpu_software_sensors_map_to_pc_sensors():
    data = bytearray(aquasuite_configured_quadro())
    rep = control.ControlReport(devices.QUADRO, data)
    for i, src in ((1, 5), (2, 6)):                           # curves on software sensors 2 and 3
        fc = rep.fan(i)
        fc.mode, fc.source = control.MODE_CURVE, src
        rep.set_fan(i, fc)
    names = {"virt2": "GPU Core", "virt3": "CPU Package"}
    plan = aquasuite.plan_import(devices.QUADRO, rep.seal(), "q", names)
    readings = {"system/k10temp/tctl": {}, "system/amdgpu/edge": {}, "system/amdgpu/junction": {}}
    assert aquasuite.suggest_sources(plan, Config(), readings) == {2: "system/amdgpu/edge", 3: "system/k10temp/tctl"}
    assert aquasuite.suggest_sources(plan, Config(), {}) == {}


def test_coolant_curve_on_a_delta_t_is_pointed_out():
    data = bytearray(aquasuite_configured_quadro())
    rep = control.ControlReport(devices.QUADRO, data)
    fc = rep.fan(0)
    fc.curve_temps = [2500 + 80 * i for i in range(16)]      # 25 … 37 °C, as on a real QUADRO
    rep.set_fan(0, fc)
    plan = aquasuite.plan_import(devices.QUADRO, rep.seal(), "q", {"virt1": "Delta T"})
    assert any("looks like a coolant temperature rather than a Delta T" in n for n in plan.notes)
