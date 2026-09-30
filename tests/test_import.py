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
