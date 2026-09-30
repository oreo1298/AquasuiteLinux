"""Find connected Aquacomputer devices: hidraw first, the kernel's hwmon files as a fallback."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .device import HidDevice, HwmonDevice, hwmon_devices
from .devices import BY_PRODUCT, VENDOR_ID, DeviceSpec, status_span
from .engine import DeviceProvider
from .errors import DeviceError, PermissionDenied
from .transport import SYSFS_HIDRAW, HidNode, HidrawTransport, enumerate_nodes

log = logging.getLogger(__name__)


def hid_id_of_node(path: str, root: Path = SYSFS_HIDRAW) -> str:
    try:
        return os.path.basename(os.path.realpath(root / os.path.basename(path) / "device"))
    except OSError:
        return ""


def hid_id_of_hwmon(dev: HwmonDevice) -> str:
    try:
        return os.path.basename(os.path.realpath(dev.dir / "device"))
    except OSError:
        return ""


def _candidates(spec: DeviceSpec, nodes: list[HidNode]) -> list[HidNode]:
    """Interfaces most likely to carry the device protocol first (Aquaero and Leakshield have several)."""
    def score(n: HidNode) -> tuple:
        if spec.status_via_feature:
            rep = n.report(spec.status_id, "feature")
        else:
            rep = n.report(spec.status_id, "input")
        has_ctrl = spec.ctrl_id is not None and n.report(spec.ctrl_id, "feature") is not None
        length = rep.length if rep else 0
        return (length >= min(status_span(spec), 64), has_ctrl, length, -n.interface)
    return sorted(nodes, key=score, reverse=True)


def describe_node(n: HidNode) -> str:
    return f"{n.path} ({n.name or 'Aquacomputer'} {n.vendor:04x}:{n.product:04x})"


class HardwareProvider(DeviceProvider):
    def __init__(self, hidraw_root: Path = SYSFS_HIDRAW, hwmon_root: Path | None = None, use_hwmon: bool = True):
        self.hidraw_root = hidraw_root
        self.hwmon_root = hwmon_root
        self.use_hwmon = use_hwmon
        self._failed: dict[str, int] = {}

    def scan(self, current):
        found: list = []
        problems: list[str] = []
        open_paths = {d.path for d in current.values()}
        open_hid = {getattr(d, "hid_id", "") for d in current.values()} - {""}
        nodes = enumerate_nodes(VENDOR_ID, self.hidraw_root)
        groups: dict[tuple[int, str], list[HidNode]] = {}
        for n in nodes:
            groups.setdefault((n.product, n.usb_path), []).append(n)
        denied: set[str] = set()
        for (product, _usb), members in groups.items():
            spec = BY_PRODUCT.get(product)
            if spec is None:
                problems.append(f"Unknown Aquacomputer device {product:04x} at {members[0].path} is not supported yet")
                continue
            if any(m.path in open_paths for m in members):
                continue
            for node in _candidates(spec, members):
                hid_id = hid_id_of_node(node.path, self.hidraw_root)
                if hid_id in open_hid:
                    break
                try:
                    transport = HidrawTransport(node.path)
                except PermissionDenied:
                    denied.add(hid_id)
                    problems.append(f"No permission for {spec.name} ({node.path}). Install the udev rule "
                                    "(packaging/udev) and re-plug the device, or use the background service.")
                    break
                except DeviceError as exc:
                    problems.append(str(exc))
                    continue
                try:
                    dev = HidDevice(spec, transport, node.path)
                except DeviceError as exc:
                    transport.close()
                    self._failed[node.path] = self._failed.get(node.path, 0) + 1
                    if not spec.multi_interface:
                        problems.append(str(exc))
                    continue
                dev.hid_id = hid_id
                open_hid.add(hid_id)
                found.append(dev)
                break
        if self.use_hwmon:
            for hw in hwmon_devices(self.hwmon_root) if self.hwmon_root else hwmon_devices():
                hw.hid_id = hid_id_of_hwmon(hw)
                if hw.path in open_paths or (hw.hid_id and hw.hid_id in open_hid):
                    continue
                if hw.hid_id and hw.hid_id not in denied and nodes and any(
                        hid_id_of_node(n.path, self.hidraw_root) == hw.hid_id for n in nodes):
                    # its hidraw node exists and is usable (or will be retried); don't duplicate it
                    continue
                found.append(hw)
        return found, problems

    def system_sensors(self):
        from .system import SystemSensors
        return SystemSensors()
