"""The ARCTIC Fan Controller through a fake ``arctic_fan`` hwmon chip (Linux 7.2+ driver)."""

import os

import pytest
from conftest import run_ticks

from aquasuitelinux.core import arctic
from aquasuitelinux.core.config import Config, ControllerConfig, OutputConfig, VirtualSensorConfig
from aquasuitelinux.core.devices import ARCTIC_FAN, BY_KIND, BY_PRODUCT
from aquasuitelinux.core.engine import Engine, StaticProvider
from aquasuitelinux.core.errors import DeviceError


def fake_arctic(tmp_path, serial="AFC0012345", pwm=None):
    """/sys/class/hwmon/hwmonN for the arctic_fan driver, below its HID and USB devices."""
    usb = tmp_path / "devices" / "usb3" / "3-4"
    hid = usb / "3-4:1.0" / "0003:3904:F001.0007"
    hid.mkdir(parents=True)
    (usb / "idVendor").write_text("3904\n")
    if serial:
        (usb / "serial").write_text(serial + "\n")
    root = tmp_path / "hwmon"
    d = root / "hwmon7"
    d.mkdir(parents=True)
    os.symlink(hid, d / "device")
    (d / "name").write_text("arctic_fan\n")
    for i in range(1, 11):
        (d / f"fan{i}_input").write_text(f"{600 + i * 10}\n")
        (d / f"pwm{i}").write_text(f"{(pwm or {}).get(i, 0)}\n")
    return root, d


def pwm(d, i):
    return int((d / f"pwm{i}").read_text())


def test_found_with_its_serial_and_not_mistaken_for_an_aquaero(tmp_path):
    root, _d = fake_arctic(tmp_path)
    [dev] = arctic.arctic_devices(root)
    assert dev.key == "arcticfan-AFC0012345" and dev.backend == "kernel"
    assert BY_KIND["arcticfan"] is ARCTIC_FAN and BY_PRODUCT[0xF001].kind == "aquaero"
    vals = {r.id.split("/", 1)[1]: r.value for r in dev.poll()}
    assert vals["fan1.rpm"] == 610 and vals["fan10.rpm"] == 700
    assert vals["fan1.percent"] is None                    # the driver can't know it before a write
    assert dev.online and dev.capabilities() == {"monitor", "manual"}
    assert [k for k in dev.info().outputs] == [f"fan{i}" for i in range(1, 11)]


def test_unassigned_channels_keep_the_controllers_default_instead_of_stopping(tmp_path):
    root, d = fake_arctic(tmp_path, pwm={5: 200})           # someone else set fan 5
    [dev] = arctic.arctic_devices(root)
    try:
        dev.set_manual({"fan1": 60})
        assert dev.flush()
        assert pwm(d, 1) == 153                              # 60 %
        assert pwm(d, 5) == 200                              # kept as it was set
        assert all(pwm(d, i) == 102 for i in (2, 3, 4, 6, 7, 8, 9, 10))   # 40 %, the controller's default
        assert dev.writes == 9                               # fan 5 needed no write
        vals = {r.id.split("/", 1)[1]: r.value for r in dev.poll()}
        assert vals["fan1.percent"] == 60.0 and vals["fan2.percent"] == 40.0 and vals["fan5.percent"] == 78.4
        writes = dev.writes
        dev.set_manual({"fan1": 61})
        assert dev.flush() and dev.writes == writes + 1      # only what changed
    finally:
        dev.close()


def test_speeds_are_sent_again_after_a_resume(tmp_path):
    root, d = fake_arctic(tmp_path)
    [dev] = arctic.arctic_devices(root)
    try:
        dev.set_manual({"fan1": 60, "fan2": 80})
        assert dev.flush()
        for i in range(1, 11):                               # the driver clears its cache on resume
            (d / f"pwm{i}").write_text("0\n")
        dev.poll()
        assert dev.flush()
        assert (pwm(d, 1), pwm(d, 2), pwm(d, 3)) == (153, 204, 102)
    finally:
        dev.close()


def test_write_errors_are_reported_and_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(arctic, "RETRY_SECONDS", 0.05)
    root, d = fake_arctic(tmp_path, serial="")
    [dev] = arctic.arctic_devices(root)
    assert dev.key == "arcticfan"
    (d / "pwm3").unlink()
    (d / "pwm3").mkdir()                                     # writing it fails
    try:
        dev.set_manual({"fan1": 50})
        assert not dev.flush(timeout=0.5)
        dev.poll()
        assert "setting fan 3 failed" in dev.error
        with pytest.raises(DeviceError, match="setting fan 3 failed"):
            dev.set_manual({"fan1": 50})
        (d / "pwm3").rmdir()                                 # writable again: the next retry gets through
        assert dev.flush(timeout=2.0) and pwm(d, 3) == 102
    finally:
        dev.close()


def test_unplugging_drops_the_device(tmp_path):
    root, d = fake_arctic(tmp_path)
    [dev] = arctic.arctic_devices(root)
    (d / "name").unlink()
    with pytest.raises(DeviceError, match="disconnected"):
        dev.poll()


def test_engine_runs_a_delta_t_curve_on_it_in_software(tmp_path):
    root, d = fake_arctic(tmp_path)
    [dev] = arctic.arctic_devices(root)
    key = dev.key
    cfg = Config()
    cfg.virtual_sensors = [VirtualSensorConfig(id="dt", kind="constant", value=6.0, unit="K")]
    cfg.controllers = [ControllerConfig(id="c", kind="curve", input="virtual/dt", points=[[2, 20], [10, 100]])]
    cfg.outputs[f"{key}/fan1"] = OutputConfig(controller="c", min_power=0)
    eng = Engine(cfg, StaticProvider([dev]), mode="service")
    try:
        snap = run_ticks(eng, 3, pause=0.05)
        assert dev.flush()
        o = {o["id"]: o for o in snap["outputs"]}[f"{key}/fan1"]
        assert o["placement"] == "software" and "can't run controllers itself" in o["reason"]
        assert pwm(d, 1) == 153                              # 60 % from the curve at 6 K
        assert pwm(d, 2) == 102                              # unassigned: the controller's default
        assert len(snap["outputs"]) == 10
    finally:
        eng.stop()
    assert pwm(d, 1) == 255                                  # exit: software outputs go to their fallback (100 %)


def test_discovery_and_doctor_list_it(tmp_path):
    from aquasuitelinux.core import doctor
    from aquasuitelinux.core.discovery import HardwareProvider
    root, _d = fake_arctic(tmp_path)
    empty = tmp_path / "no-hidraw"
    empty.mkdir()
    found, problems = HardwareProvider(hidraw_root=empty, hwmon_root=root).scan({})
    assert [d.key for d in found] == ["arcticfan-AFC0012345"] and not problems
    lines = []
    doc = doctor.Doctor(out=lines.append, hidraw_root=empty, hwmon_root=root, udev_dirs=[], api=None)
    doc.hardware()
    text = "\n".join(lines)
    assert "ARCTIC Fan Controller AFC0012345" in text and "610, 620" in text and "can set speeds: yes" in text
    assert not doc.findings                                  # no "no Aquacomputer device found"
