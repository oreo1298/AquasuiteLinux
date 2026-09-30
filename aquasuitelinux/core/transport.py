"""Talk to devices through Linux hidraw nodes (``/dev/hidrawN``), with no extra libraries.

hidraw works alongside the in-kernel ``aquacomputer_d5next`` driver: both receive the
status reports, and feature reports can be read and written from user space. Access
needs the udev rule shipped in ``packaging/udev`` (or root).
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DeviceError, PermissionDenied

SYSFS_HIDRAW = Path("/sys/class/hidraw")

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


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

    def get_feature(self, report_id: int, length: int) -> bytes:
        self._pace()
        buf = bytearray(length)
        buf[0] = report_id
        try:
            n = fcntl.ioctl(self.fd, HIDIOCGFEATURE(length), buf, True)
        except OSError as exc:
            raise DeviceError(f"{self.path}: reading feature report {report_id:#04x} failed: {exc.strerror}") from exc
        finally:
            self._last_feature = time.monotonic()
        return bytes(buf[:n if isinstance(n, int) and 0 < n <= length else length])

    def set_feature(self, data: bytes) -> None:
        self._pace()
        buf = bytearray(data)
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as exc:
            raise DeviceError(f"{self.path}: writing feature report {data[0]:#04x} failed: {exc.strerror}") from exc
        finally:
            self._last_feature = time.monotonic()

    def write_output(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError as exc:
            raise DeviceError(f"{self.path}: writing report {data[0]:#04x} failed: {exc.strerror}") from exc
