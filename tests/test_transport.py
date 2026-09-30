"""hidraw retries and finding the USB endpoint for software sensor data."""

import errno
import os

import pytest

from aquasuitelinux.core import transport
from aquasuitelinux.core.errors import DeviceError


def _quadro_sysfs(tmp_path, hid_interrupt_out=False, vendor_bulk=True):
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
    root = tmp_path / "class" / "hidraw"
    (root / "hidraw8").mkdir(parents=True)
    os.symlink(hid_dev, root / "hidraw8" / "device")
    return root


def test_feed_channel_finds_the_vendor_bulk_endpoint(tmp_path):
    root = _quadro_sysfs(tmp_path)
    mode, ch = transport.feed_channel("/dev/hidraw8", root, tmp_path / "dev")
    assert mode == "bulk"
    assert (ch.interface, ch.endpoint) == (0, 0x02)
    assert ch.devnode == str(tmp_path / "dev" / "001" / "005")


def test_feed_channel_hid_or_none(tmp_path):
    root = _quadro_sysfs(tmp_path / "a", hid_interrupt_out=True, vendor_bulk=False)
    assert transport.feed_channel("/dev/hidraw8", root)[0] == "hid"
    root = _quadro_sysfs(tmp_path / "b", vendor_bulk=False)
    assert transport.feed_channel("/dev/hidraw8", root)[0] == "none"


def test_usbfs_ioctl_numbers():
    assert transport.USBDEVFS_CLAIMINTERFACE == 0x8004550F
    assert transport.USBDEVFS_RELEASEINTERFACE == 0x80045510
    if len(transport.bulk_transfer_struct(2, 67, 1000, 0)) == 24:      # 64-bit
        assert transport.USBDEVFS_BULK() == 0xC0185502


def test_bulk_channel_reports_permission_problem(tmp_path):
    node = tmp_path / "005"
    node.write_bytes(b"")
    node.chmod(0)
    ch = transport.UsbBulkChannel(str(node), 0, 2)
    if os.geteuid() == 0:
        pytest.skip("root can open anything")
    with pytest.raises(transport.PermissionDenied):
        ch.write(b"\x04" + b"\x00" * 66)


def _fake_transport(monkeypatch, failures):
    t = transport.HidrawTransport.__new__(transport.HidrawTransport)
    t.path, t.fd, t._last_feature = "/dev/hidraw8", 99, 0.0
    calls = []

    def ioctl(fd, req, buf, mutate=True):
        calls.append(req)
        if failures:
            raise OSError(failures.pop(0), os.strerror(errno.EPROTO))
        buf[1] = 0xAB
        return len(buf)
    monkeypatch.setattr(transport.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(transport, "RETRY_DELAY", 0.0)
    return t, calls


def test_feature_report_retries_once_after_transient_error(monkeypatch):
    t, calls = _fake_transport(monkeypatch, [errno.EPROTO])
    data = t.get_feature(0x03, 0x3C1)
    assert data[1] == 0xAB and len(calls) == 2


def test_feature_report_gives_up_after_second_error(monkeypatch):
    t, calls = _fake_transport(monkeypatch, [errno.EPROTO, errno.EPROTO])
    with pytest.raises(DeviceError, match="Protocol error"):
        t.get_feature(0x03, 0x3C1)
    assert len(calls) == 2
    t, calls = _fake_transport(monkeypatch, [errno.EINVAL])
    with pytest.raises(DeviceError):
        t.set_feature(b"\x03" + b"\x00" * 10)
    assert len(calls) == 1                                               # not a transient error


def test_bulk_channel_claims_interface_and_sends_exact_bytes(monkeypatch):
    import ctypes
    import struct
    calls = []
    monkeypatch.setattr(transport.os, "open", lambda path, flags: 42)
    monkeypatch.setattr(transport.os, "close", lambda fd: calls.append(("close", fd)))

    def ioctl(fd, req, arg, *rest):
        if req == transport.USBDEVFS_BULK():
            ep, length, timeout, addr = struct.unpack("@IIIP", arg)
            calls.append(("bulk", ep, length, timeout, ctypes.string_at(addr, length)))
        else:
            calls.append((req, struct.unpack("I", arg)[0]))
        return 0
    monkeypatch.setattr(transport.fcntl, "ioctl", ioctl)
    from aquasuitelinux.core import control, devices
    report = control.soft_sensor_report(devices.QUADRO.soft_sensors, [(6.2, 3)])
    ch = transport.UsbBulkChannel("/dev/bus/usb/001/005", 0, 0x02)
    ch.write(report)
    ch.write(report)
    assert calls[0] == (transport.USBDEVFS_CLAIMINTERFACE, 0)             # claimed once, interface 0
    assert calls[1] == ("bulk", 0x02, 67, 1000, report)
    assert calls[2][0] == "bulk" and len(calls) == 3
    ch.close()
    assert calls[3] == (transport.USBDEVFS_RELEASEINTERFACE, 0) and calls[4] == ("close", 42)
