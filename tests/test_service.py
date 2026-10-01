"""The service socket, the API, the config file and discovery against a fake sysfs."""

import json
import os
import threading
import time

import pytest

from aquasuitelinux.core import config as config_mod
from aquasuitelinux.core.api import LocalAPI, ServiceAPI
from aquasuitelinux.core.demo import QUADRO, demo_config, demo_provider
from aquasuitelinux.core.engine import Engine
from aquasuitelinux.core.ipc import Client, Server


@pytest.fixture
def service(tmp_path):
    eng = Engine(demo_config(), demo_provider(), mode="service")
    eng.tick()
    path = tmp_path / "s.sock"
    server = Server(LocalAPI(eng), path)
    server.start()
    yield eng, path
    server.close()
    eng.stop()


def test_service_roundtrip(service):
    eng, path = service
    api = ServiceAPI(Client(path))
    assert api.hello()["mode"] == "service"
    snap = api.snapshot()
    assert any(d["key"] == QUADRO for d in snap["devices"])
    cfg = api.get_config()
    cfg["outputs"][f"{QUADRO}/fan1"]["name"] = "Renamed"
    api.set_config(cfg)
    assert eng.config.outputs[f"{QUADRO}/fan1"].name == "Renamed"
    api.set_profile("Quiet")
    assert eng.config.profile(eng.config.active_profile).name == "Quiet"
    settings = api.device_settings(QUADRO)
    assert settings["kind"] == "quadro" and settings["raw"]
    backup = api.backup(QUADRO)
    api.restore(QUADRO, backup)
    h = api.history([f"{QUADRO}/temp1"], 60)
    assert "times" in h
    api.close()


def test_service_errors_are_reported(service):
    _eng, path = service
    client = Client(path)
    from aquasuitelinux.core.errors import ServiceError
    with pytest.raises(ServiceError):
        client.call("set_profile", name="does-not-exist")
    with pytest.raises(ServiceError):
        client.call("nope")
    client.close()


def test_write_permission_policy(service, monkeypatch):
    _eng, path = service
    server = Server.__new__(Server)
    server._groups_fn = lambda: ["nobody-group-xyz"]
    assert server.may_write(0, 0)
    assert server.may_write(os.getuid(), os.getgid())
    assert not server.may_write(-1, -1)
    assert not server.may_write(65534 if os.getuid() != 65534 else 65533, 65534)


def test_concurrent_clients(service):
    _eng, path = service
    errors = []

    def worker():
        try:
            c = Client(path)
            for _ in range(20):
                c.call("snapshot")
            c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_config_save_load_roundtrip(tmp_path):
    cfg = demo_config()
    p = tmp_path / "c" / "config.json"
    config_mod.save(cfg, p, 0o600)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    again = config_mod.load(p)
    assert again.to_dict() == cfg.to_dict()
    data = json.loads(p.read_text())
    data["unknown_future_key"] = 1
    data["outputs"][f"{QUADRO}/fan1"]["future_field"] = True
    p.write_text(json.dumps(data))
    assert config_mod.load(p).outputs[f"{QUADRO}/fan1"].name == "Radiator top 1"
    assert config_mod.load(tmp_path / "missing.json").outputs == {}


def test_remove_controller_cleans_references():
    cfg = demo_config()
    cfg.remove_controller("radiator")
    assert all(o.controller != "radiator" for o in cfg.outputs.values())
    assert "radiator" not in cfg.controller("case").sources


def _fake_sysfs(tmp_path, product="F00D"):
    hidraw = tmp_path / "hidraw" / "hidraw3"
    dev = tmp_path / "devices" / f"0003:0C70:{product}.0007"
    dev.mkdir(parents=True)
    hidraw.parent.mkdir(parents=True)
    (dev / "uevent").write_text(f"HID_ID=0003:00000C70:0000{product}\nHID_NAME=aquacomputer QUADRO\n"
                                "HID_PHYS=usb-0000:00:14.0-3/input0\nHID_UNIQ=\n")
    (dev / "report_descriptor").write_bytes(bytes([0x85, 0x01, 0x75, 0x08, 0x95, 0xDB, 0x81, 0x02]))
    hidraw.mkdir()
    os.symlink(dev, hidraw / "device")
    return tmp_path / "hidraw"


def test_enumerate_nodes_from_sysfs(tmp_path):
    from aquasuitelinux.core.transport import enumerate_nodes
    root = _fake_sysfs(tmp_path)
    nodes = enumerate_nodes(0x0C70, root)
    assert len(nodes) == 1
    n = nodes[0]
    assert (n.path, n.product, n.interface) == ("/dev/hidraw3", 0xF00D, 0)
    assert n.report(1, "input").length == 0xDC


def test_discovery_reports_permission_problem(tmp_path, monkeypatch):
    from aquasuitelinux.core import discovery
    from aquasuitelinux.core.errors import PermissionDenied
    root = _fake_sysfs(tmp_path)

    def denied(path):
        raise PermissionDenied("no")
    monkeypatch.setattr(discovery, "HidrawTransport", denied)
    prov = discovery.HardwareProvider(hidraw_root=root, hwmon_root=tmp_path / "nohwmon")
    found, problems = prov.scan({})
    assert found == [] and "udev rule" in problems[0]


def test_hwmon_backend(tmp_path):
    from aquasuitelinux.core.device import hwmon_devices
    d = tmp_path / "hwmon" / "hwmon5"
    d.mkdir(parents=True)
    files = {"name": "quadro", "temp1_input": "31250", "temp2_input": "24000", "fan1_input": "812",
             "power1_input": "310000", "in0_input": "12130", "curr1_input": "25", "fan5_input": "1203",
             "pwm1": "128"}
    for k, v in files.items():
        (d / k).write_text(v)
    devs = hwmon_devices(tmp_path / "hwmon")
    assert len(devs) == 1 and devs[0].key == "quadro"
    vals = {r.id.split("/", 1)[1]: r.value for r in devs[0].poll()}
    assert vals["temp1"] == 31.25 and vals["fan1.rpm"] == 812 and vals["flow"] == pytest.approx(120.3)
    assert vals["fan1.power"] == pytest.approx(0.31) and vals["fan1.percent"] == pytest.approx(50.2, abs=0.1)
    devs[0].set_manual({"fan1": 100})
    assert (d / "pwm1").read_text() == "255"


def test_system_sensors(tmp_path):
    from aquasuitelinux.core.system import SystemSensors
    root = tmp_path / "hwmon"
    for i, (name, temps) in enumerate({"k10temp": {"1": ("Tctl", "55125")}, "quadro": {"1": ("Sensor 1", "30000")},
                                       "nvme": {"1": ("Composite", "41850")}}.items()):
        d = root / f"hwmon{i}"
        d.mkdir(parents=True)
        (d / "name").write_text(name)
        for idx, (label, val) in temps.items():
            (d / f"temp{idx}_input").write_text(val)
            (d / f"temp{idx}_label").write_text(label)
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    s = SystemSensors(root, nvidia=False, proc_stat=stat)
    vals = {r.id: r for r in s.poll()}
    assert vals["system/k10temp/tctl"].value == pytest.approx(55.125)
    assert vals["system/k10temp/tctl"].label == "CPU Tctl"
    assert "system/quadro/sensor_1" not in vals          # Aquacomputer chips come from the devices
    assert vals["system/nvme/composite"].value == pytest.approx(41.85)
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")
    time.sleep(0.01)
    assert {r.id: r for r in s.poll()}["system/cpu_load"].value == pytest.approx(50.0)


def test_system_sensors_leave_network_hardware_alone(tmp_path, monkeypatch):
    """Reading a NIC's or PHY's temperature every second broke a user's Ethernet: never read them."""
    import os

    from aquasuitelinux.core import system
    sys_root = tmp_path / "sys"
    nic = sys_root / "devices" / "pci0000:00" / "0000:05:00.0"               # an Ethernet card
    (nic / "net" / "enp5s0").mkdir(parents=True)
    wifi = sys_root / "devices" / "pci0000:00" / "0000:06:00.0"              # a Wi-Fi card
    (wifi / "ieee80211" / "phy0").mkdir(parents=True)
    phy = nic / "mdio_bus" / "r8169-0-500" / "r8169-0-500:00"                # the Ethernet card's PHY
    phy.mkdir(parents=True)
    (sys_root / "bus" / "mdio_bus").mkdir(parents=True)
    os.symlink(sys_root / "bus" / "mdio_bus", phy / "subsystem")
    cpu = sys_root / "devices" / "pci0000:00" / "0000:00:18.3"
    cpu.mkdir(parents=True)
    root = tmp_path / "hwmon"
    chips = [("k10temp", cpu), ("r8169_0_500:00", phy), ("whatever_nic", nic), ("mt7921_phy0", wifi),
             ("atlantic", None), ("nvme", None)]
    for i, (name, dev) in enumerate(chips):
        d = root / f"hwmon{i}"
        d.mkdir(parents=True)
        (d / "name").write_text(name)
        (d / "temp1_input").write_text("40000")
        if dev is not None:
            os.symlink(dev, d / "device")
    reads = []
    real_read = system._read
    monkeypatch.setattr(system, "_read", lambda path: (reads.append(str(path)), real_read(path))[1])
    s = system.SystemSensors(root, nvidia=False, proc_stat=tmp_path / "nostat")
    ids = {r.id for r in s.poll()}
    assert {i for i in ids if i != "system/cpu_load"} == {"system/k10temp/temp1", "system/nvme/temp1"}
    assert s.chips() == ["k10temp", "nvme"]
    assert sorted(s.skipped) == sorted(["r8169_0_500:00", "whatever_nic", "mt7921_phy0", "atlantic"])
    touched = {p.split("/hwmon/")[1].split("/")[0] for p in reads if "/hwmon/" in p and p.endswith("_input")}
    assert touched == {"hwmon0", "hwmon5"}                   # not a single temperature read from them
