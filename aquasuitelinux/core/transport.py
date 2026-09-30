"""Talk to devices through Linux hidraw nodes (``/dev/hidrawN``) and usbfs, with no extra libraries.

hidraw works alongside the in-kernel ``aquacomputer_d5next`` driver: both receive the
status reports, and feature reports can be read and written from user space.

Software sensor values don't go through HID at all: the QUADRO (and its relatives) have a
second, vendor-specific USB interface with a bulk OUT endpoint (0x02), and aquasuite sends
them there. ``UsbBulkChannel`` does the same through ``/dev/bus/usb`` (usbfs). No kernel driver
uses that interface, so claiming it doesn't disturb hidraw or the hwmon driver.

Access to both needs the udev rule shipped in ``packaging/udev`` (or root).
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import re
import select
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DeviceError, PermissionDenied

SYSFS_HIDRAW = Path("/sys/class/hidraw")
DEV_USB = Path("/dev/bus/usb")

_IOC_WRITE, _IOC_READ = 1, 2

# Errors a busy or briefly confused device can return; worth one more try.
TRANSIENT_ERRNOS = (errno.EPROTO, errno.ETIMEDOUT, errno.EPIPE, errno.EAGAIN)
RETRY_DELAY = 0.3


def _ioc(direction: int, nr: int, size: int, kind: str = "H") -> int:
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


def HIDIOCGFEATURE(length: int) -> int:  # noqa: N802
    return _ioc(_IOC_WRITE | _IOC_READ, 0x07, length)


def HIDIOCSFEATURE(length: int) -> int:  # noqa: N802
    return _ioc(_IOC_WRITE | _IOC_READ, 0x06, length)


@dataclass
class HidReport:
    report_id: int
    kind: str          # input | output | feature
    length: int        # bytes including the report ID byte


@dataclass
class HidNode:
    """A hidraw node found in sysfs."""

    path: str
    vendor: int
    product: int
    name: str = ""
    phys: str = ""
    uniq: str = ""
    interface: int = -1
    reports: list[HidReport] = field(default_factory=list)

    def report(self, report_id: int, kind: str) -> HidReport | None:
        for r in self.reports:
            if r.report_id == report_id and r.kind == kind:
                return r
        return None

    @property
    def usb_path(self) -> str:
        """The physical USB location shared by all interfaces of one device."""
        return self.phys.split("/input")[0] if self.phys else self.path


def parse_report_descriptor(desc: bytes) -> list[HidReport]:
    """Collect report IDs and sizes from a HID report descriptor (short items only)."""
    sizes: dict[tuple[int, str], int] = {}
    report_id = 0
    report_size = 0
    report_count = 0
    stack: list[tuple[int, int, int]] = []
    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:                     # long item
            if i + 1 >= len(desc):
                break
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        typ = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        value = int.from_bytes(desc[i + 1:i + 1 + size], "little") if size else 0
        i += 1 + size
        if typ == 1:                           # global
            if tag == 0x7:
                report_size = value
            elif tag == 0x9:
                report_count = value
            elif tag == 0x8:
                report_id = value
            elif tag == 0xA:
                stack.append((report_id, report_size, report_count))
            elif tag == 0xB and stack:
                report_id, report_size, report_count = stack.pop()
        elif typ == 0:                         # main
            kind = {0x8: "input", 0x9: "output", 0xB: "feature"}.get(tag)
            if kind:
                sizes[(report_id, kind)] = sizes.get((report_id, kind), 0) + report_size * report_count
    out = []
    for (rid, kind), bits in sorted(sizes.items()):
        out.append(HidReport(rid, kind, (bits + 7) // 8 + (1 if rid else 0)))
    return out


def _read_uevent(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k] = v
    except OSError:
        pass
    return data


def enumerate_nodes(vendor: int | None = None, root: Path = SYSFS_HIDRAW) -> list[HidNode]:
    """All hidraw nodes (optionally of one USB vendor), with parsed report descriptors."""
    nodes: list[HidNode] = []
    if not root.is_dir():
        return nodes
    for entry in sorted(root.iterdir(), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        dev = entry / "device"
        ue = _read_uevent(dev / "uevent")
        m = re.match(r"([0-9A-Fa-f]+):([0-9A-Fa-f]+):([0-9A-Fa-f]+)", ue.get("HID_ID", ""))
        if not m:
            continue
        vid, pid = int(m.group(2), 16), int(m.group(3), 16)
        if vendor is not None and vid != vendor:
            continue
        node = HidNode(f"/dev/{entry.name}", vid, pid, ue.get("HID_NAME", ""), ue.get("HID_PHYS", ""),
                       ue.get("HID_UNIQ", ""))
        mi = re.search(r"input(\d+)$", node.phys)
        if mi:
            node.interface = int(mi.group(1))
        try:
            node.reports = parse_report_descriptor((dev / "report_descriptor").read_bytes())
        except OSError:
            node.reports = []
        nodes.append(node)
    return nodes


class HidrawTransport:
    """Blocking-free access to one hidraw node."""

    def __init__(self, path: str):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError as exc:
            raise PermissionDenied(f"No permission to open {path}. Install the udev rule "
                                   "(see the README) and re-plug the device, or run as root.") from exc
        except OSError as exc:
            raise DeviceError(f"Cannot open {path}: {exc.strerror}") from exc
        self._last_feature = 0.0

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def read_input(self, timeout: float = 0.0) -> list[bytes]:
        """Every input report queued right now (waiting up to ``timeout`` for the first)."""
        reports: list[bytes] = []
        deadline = time.monotonic() + timeout
        while True:
            wait = max(0.0, deadline - time.monotonic()) if not reports else 0.0
            try:
                ready, _, _ = select.select([self.fd], [], [], wait)
            except (OSError, ValueError) as exc:
                raise DeviceError(f"{self.path}: {exc}") from exc
            if not ready:
                return reports
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                return reports
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EIO, errno.EPIPE):
                    raise DeviceError(f"{self.path} was disconnected") from exc
                raise DeviceError(f"{self.path}: {exc.strerror}") from exc
            if not data:
                return reports
            reports.append(data)

    def _pace(self) -> None:
        # The devices need ~200 ms between control report operations (see the kernel driver).
        delta = time.monotonic() - self._last_feature
        if delta < 0.2:
            time.sleep(0.2 - delta)

    def _feature_ioctl(self, request: int, buf: bytearray, what: str):
        """One feature report ioctl, retried once after a transient USB error."""
        for attempt in (1, 2):
            self._pace()
            try:
                return fcntl.ioctl(self.fd, request, buf, True)
            except OSError as exc:
                if attempt == 1 and exc.errno in TRANSIENT_ERRNOS:
                    time.sleep(RETRY_DELAY)
                    continue
                raise DeviceError(f"{self.path}: {what} failed: {exc.strerror}") from exc
            finally:
                self._last_feature = time.monotonic()

    def get_feature(self, report_id: int, length: int) -> bytes:
        buf = bytearray(length)
        buf[0] = report_id
        n = self._feature_ioctl(HIDIOCGFEATURE(length), buf, f"reading feature report {report_id:#04x}")
        return bytes(buf[:n if isinstance(n, int) and 0 < n <= length else length])

    def set_feature(self, data: bytes) -> None:
        buf = bytearray(data)
        self._feature_ioctl(HIDIOCSFEATURE(len(buf)), buf, f"writing feature report {data[0]:#04x}")

    def write_output(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError as exc:
            raise DeviceError(f"{self.path}: writing report {data[0]:#04x} failed: {exc.strerror}") from exc


# ---------------------------------------------------------------------- usbfs bulk transfers
def USBDEVFS_BULK() -> int:  # noqa: N802
    return _ioc(_IOC_WRITE | _IOC_READ, 2, struct.calcsize("@IIIP"), "U")


USBDEVFS_CLAIMINTERFACE = _ioc(_IOC_READ, 15, 4, "U")
USBDEVFS_RELEASEINTERFACE = _ioc(_IOC_READ, 16, 4, "U")


def bulk_transfer_struct(endpoint: int, length: int, timeout_ms: int, address: int) -> bytes:
    """``struct usbdevfs_bulktransfer {unsigned ep, len, timeout; void *data;}`` in native layout."""
    return struct.pack("@IIIP", endpoint, length, timeout_ms, address)


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass
class UsbEndpoint:
    interface: int
    address: int
    kind: str          # Bulk | Interrupt | ...
    direction: str     # in | out


def usb_layout(hidraw_path: str, root: Path = SYSFS_HIDRAW) -> tuple[Path | None, int, list[UsbEndpoint]]:
    """The USB device a hidraw node belongs to: (sysfs dir, HID interface number, all endpoints)."""
    try:
        hid_dev = Path(os.path.realpath(root / os.path.basename(hidraw_path) / "device"))
    except OSError:
        return None, -1, []
    intf_dir = hid_dev.parent                  # …/1-3:1.1
    usb_dir = intf_dir.parent                  # …/1-3
    if not (usb_dir / "busnum").exists():
        return None, -1, []
    hid_if = -1
    try:
        hid_if = int(_read_attr(intf_dir / "bInterfaceNumber") or "-1", 16)
    except ValueError:
        pass
    endpoints: list[UsbEndpoint] = []
    for intf in sorted(usb_dir.glob(f"{usb_dir.name}:*")):
        try:
            number = int(_read_attr(intf / "bInterfaceNumber"), 16)
        except ValueError:
            continue
        for ep in sorted(intf.glob("ep_*")):
            try:
                address = int(_read_attr(ep / "bEndpointAddress"), 16)
            except ValueError:
                continue
            endpoints.append(UsbEndpoint(number, address, _read_attr(ep / "type"), _read_attr(ep / "direction")))
    return usb_dir, hid_if, endpoints


class UsbBulkChannel:
    """Sends data to a bulk OUT endpoint through usbfs, claiming only that endpoint's interface."""

    def __init__(self, devnode: str, interface: int, endpoint: int, timeout_ms: int = 1000):
        self.devnode = devnode
        self.interface = interface
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self.fd = -1

    def describe(self) -> str:
        return f"USB bulk endpoint {self.endpoint:#04x} (interface {self.interface}, {self.devnode})"

    def open(self) -> None:
        if self.fd >= 0:
            return
        try:
            fd = os.open(self.devnode, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as exc:
            raise PermissionDenied(f"No permission to open {self.devnode} for software sensor data. Install the "
                                   "current udev rule (packaging/udev) and re-plug the device, or use the "
                                   "background service.") from exc
        except OSError as exc:
            raise DeviceError(f"Cannot open {self.devnode}: {exc.strerror}") from exc
        try:
            fcntl.ioctl(fd, USBDEVFS_CLAIMINTERFACE, struct.pack("I", self.interface))
        except OSError as exc:
            os.close(fd)
            if exc.errno == errno.EBUSY:
                raise DeviceError(f"USB interface {self.interface} of {self.devnode} is in use by another driver "
                                  "or program") from exc
            raise DeviceError(f"Cannot claim USB interface {self.interface} of {self.devnode}: "
                              f"{exc.strerror}") from exc
        self.fd = fd

    def write(self, data: bytes) -> None:
        self.open()
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        req = bulk_transfer_struct(self.endpoint, len(data), self.timeout_ms, ctypes.addressof(buf))
        try:
            fcntl.ioctl(self.fd, USBDEVFS_BULK(), req)
        except OSError as exc:
            self.close()
            raise DeviceError(f"{self.devnode}: sending to endpoint {self.endpoint:#04x} failed: "
                              f"{exc.strerror}") from exc

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, struct.pack("I", self.interface))
        except OSError:
            pass
        try:
            os.close(self.fd)
        finally:
            self.fd = -1


def feed_channel(hidraw_path: str, root: Path = SYSFS_HIDRAW, dev_usb: Path = DEV_USB):
    """How software sensor data reaches the device behind ``hidraw_path``.

    Returns ``("bulk", UsbBulkChannel)`` for a bulk OUT endpoint (preferring 0x02, as aquasuite
    uses), ``("hid", None)`` if the HID interface itself has an interrupt OUT endpoint (then a
    hidraw write reaches the device), or ``("none", None)``.
    """
    usb_dir, hid_if, endpoints = usb_layout(hidraw_path, root)
    if usb_dir is None:
        return "none", None
    bulk_out = [e for e in endpoints if e.kind == "Bulk" and e.direction == "out"]
    if bulk_out:
        ep = next((e for e in bulk_out if e.address == 0x02), bulk_out[0])
        try:
            bus = int(_read_attr(usb_dir / "busnum"))
            dev = int(_read_attr(usb_dir / "devnum"))
        except ValueError:
            return "none", None
        return "bulk", UsbBulkChannel(str(dev_usb / f"{bus:03d}" / f"{dev:03d}"), ep.interface, ep.address)
    if any(e.interface == hid_if and e.kind == "Interrupt" and e.direction == "out" for e in endpoints):
        return "hid", None
    return "none", None
