import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = Path(__file__).parent / "data"


def quadro_sysfs(tmp_path, hid_interrupt_out=False, vendor_bulk=True):
    """The QUADRO's USB layout: interface 0 vendor-specific (bulk 0x81 in, 0x02 out), interface 1 HID."""
    usb = tmp_path / "devices" / "1-3"
    usb.mkdir(parents=True)
    (usb / "busnum").write_text("1\n")
    (usb / "devnum").write_text("5\n")

    def iface(num, cls, eps):
        d = usb / f"1-3:1.{num}"
        d.mkdir()
        (d / "bInterfaceNumber").write_text(f"{num:02x}\n")
        (d / "bInterfaceClass").write_text(cls + "\n")
        for addr, kind, direction in eps:
            e = d / f"ep_{addr:02x}"
            e.mkdir()
            (e / "bEndpointAddress").write_text(f"{addr:02x}\n")
            (e / "type").write_text(kind + "\n")
            (e / "direction").write_text(direction + "\n")
        return d

    if vendor_bulk:
        iface(0, "ff", [(0x81, "Bulk", "in"), (0x02, "Bulk", "out")])
    hid_eps = [(0x83, "Interrupt", "in")] + ([(0x04, "Interrupt", "out")] if hid_interrupt_out else [])
    hid = iface(1, "03", hid_eps)
    hid_dev = hid / "0003:0C70:F00D.0007"
    hid_dev.mkdir()
    (hid_dev / "uevent").write_text("HID_ID=0003:00000C70:0000F00D\nHID_NAME=Aqua Computer GmbH & Co. KG QUADRO\n"
                                    "HID_PHYS=usb-0000:00:14.0-3/input1\n")
    (hid_dev / "report_descriptor").write_bytes(bytes.fromhex(
        "0600ff0901a101"          # vendor usage page, application collection
        "8501750896db008102"      # report 0x01: input, 1 + 219 bytes
        "85027508950a9102"        # report 0x02: output, 11 bytes
        "8503750896c003b102"      # report 0x03: feature, 961 bytes
        "c0"))
    root = tmp_path / "class" / "hidraw"
    (root / "hidraw8").mkdir(parents=True)
    os.symlink(hid_dev, root / "hidraw8" / "device")
    return root


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AQUASUITELINUX_SOCKET", str(tmp_path / "no-service.sock"))


@pytest.fixture
def quadro_control() -> bytes:
    return (DATA / "quadro_control.bin").read_bytes()


@pytest.fixture
def quadro_status() -> bytes:
    return (DATA / "quadro_sensors.bin").read_bytes()


@pytest.fixture
def quadro_soft() -> bytes:
    return (DATA / "quadro_virt_sensors.bin").read_bytes()


@pytest.fixture
def demo_engine():
    from aquasuitelinux.core.demo import demo_config, demo_provider
    from aquasuitelinux.core.engine import Engine
    eng = Engine(demo_config(), demo_provider(speed=4.0), mode="demo")
    yield eng
    eng.stop()


def run_ticks(engine, n: int, pause: float = 0.22):
    import time
    snap = None
    for _ in range(n):
        snap = engine.tick()
        time.sleep(pause)
    return snap
