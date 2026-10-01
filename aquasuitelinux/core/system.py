"""Sensors of the PC itself: CPU and GPU temperatures, drives, CPU load.

These can drive controllers directly or be sent to a device's software sensors, like
aquasuite does on Windows. Sources: every hwmon chip except the Aquacomputer ones and network
hardware, ``nvidia-smi`` for NVIDIA's proprietary driver, and ``/proc/stat`` for CPU load.

Network hardware (Ethernet and Wi-Fi adapters, Ethernet PHYs) is never read: on some chips a
temperature read goes through the registers the driver uses to run the link, and reading it every
second broke a user's Ethernet connection while the service ran.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .devices import FAN_CONTROLLER_HWMON, HWMON_NAMES
from .model import Reading

HWMON_ROOT = Path("/sys/class/hwmon")

_FRIENDLY = {
    "k10temp": "CPU", "zenpower": "CPU", "coretemp": "CPU", "cpu_thermal": "CPU", "amdgpu": "GPU",
    "radeon": "GPU", "nouveau": "GPU", "i915": "iGPU", "xe": "GPU", "nvme": "NVMe", "drivetemp": "Drive",
    "acpitz": "ACPI",
}
# names of network drivers' sensors, for when sysfs doesn't show the device behind a chip
_NETWORK_NAMES = re.compile(r"^(r8169|r8125|r8126|r8152|r8156|atlantic|aqc|igb|igc|ixgbe|i40e|ice|e1000|bnxt|mlx|"
                            r"tg3|alx|atl1|be2net|iwlwifi|mt76|mt79|mt7\d|ath\d|rtw|brcmf|wil6210)|phy|mdio", re.I)


def is_network_hardware(chip_dir: Path, name: str) -> bool:
    """Whether a hwmon chip belongs to an Ethernet or Wi-Fi adapter or an Ethernet PHY."""
    dev = chip_dir / "device"
    if (dev / "net").is_dir() or (dev / "ieee80211").is_dir():
        return True
    try:
        if os.path.basename(os.path.realpath(dev / "subsystem")) in ("mdio_bus", "net", "ieee80211"):
            return True
    except OSError:
        pass
    return bool(_NETWORK_NAMES.search(name))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "temp"


class _Nvidia(threading.Thread):
    """Polls nvidia-smi in the background (it takes ~50-150 ms per call)."""

    def __init__(self, exe: str, period: float = 2.0):
        super().__init__(daemon=True, name="nvidia-smi")
        self.exe = exe
        self.period = period
        self.values: list[tuple[int, str, float | None]] = []
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                out = subprocess.run([self.exe, "--query-gpu=index,name,temperature.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                vals = []
                for line in out.stdout.splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        try:
                            vals.append((int(parts[0]), parts[1], float(parts[2])))
                        except ValueError:
                            vals.append((int(parts[0]) if parts[0].isdigit() else 0, parts[1], None))
                self.values = vals
            except (OSError, subprocess.SubprocessError):
                self.values = []
                self.stop_event.wait(30)
            self.stop_event.wait(self.period)


class SystemSensors:
    def __init__(self, root: Path = HWMON_ROOT, nvidia: bool = True, proc_stat: Path = Path("/proc/stat")):
        self.root = root
        self.proc_stat = proc_stat
        self._chips: list[tuple[str, str, Path, str]] = []    # (id, label, input file, unit kind)
        self.skipped: list[str] = []                          # chips left alone (network hardware)
        self._scanned = 0.0
        self._cpu_prev: tuple[int, int] | None = None
        self._nvidia: _Nvidia | None = None
        if nvidia:
            exe = shutil.which("nvidia-smi")
            if exe:
                self._nvidia = _Nvidia(exe)
                self._nvidia.start()

    def close(self) -> None:
        if self._nvidia:
            self._nvidia.stop_event.set()

    def _scan(self) -> None:
        chips: list[tuple[str, str, Path, str]] = []
        skipped: list[str] = []
        seen: dict[str, int] = {}
        aqua = set(HWMON_NAMES) | set(FAN_CONTROLLER_HWMON)
        if self.root.is_dir():
            for d in sorted(self.root.iterdir(), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
                name = _read(d / "name") or d.name
                if name in aqua:
                    continue
                if is_network_hardware(d, name):
                    skipped.append(name)
                    continue
                n = seen.get(name, 0)
                seen[name] = n + 1
                chip = name if n == 0 else f"{name}{n + 1}"
                friendly = _FRIENDLY.get(name, name)
                for inp in sorted(d.glob("temp*_input"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
                    idx = inp.name[4:-6]
                    label = _read(d / f"temp{idx}_label") or f"temp{idx}"
                    sid = f"system/{chip}/{_slug(label)}"
                    nice = f"{friendly} {label}"
                    if n:
                        nice += f" #{n + 1}"
                    chips.append((sid, nice, inp, "temperature"))
        self._chips = chips
        self.skipped = skipped
        self._scanned = time.monotonic()

    def chips(self) -> list[str]:
        """The hwmon chips read (after a scan), e.g. ``["k10temp", "nvme", "amdgpu"]``."""
        if not self._scanned:
            self._scan()
        return list(dict.fromkeys(sid.split("/")[1] for sid, *_rest in self._chips))

    def _cpu_load(self) -> float | None:
        text = _read(self.proc_stat)
        if not text or not text.startswith("cpu "):
            return None
        vals = [int(v) for v in text.splitlines()[0].split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        prev = self._cpu_prev
        self._cpu_prev = (idle, total)
        if not prev or total <= prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)

    def poll(self) -> list[Reading]:
        if time.monotonic() - self._scanned > 30 or not self._scanned:
            self._scan()
        out: list[Reading] = []
        for sid, label, path, kind in self._chips:
            raw = _read(path)
            try:
                value = None if raw is None else round(int(raw) / 1000, 3)
            except ValueError:
                value = None
            out.append(Reading(sid, label, kind, value, "System", "system"))
        load = self._cpu_load()
        out.append(Reading("system/cpu_load", "CPU load", "percent", load, "System", "system"))
        if self._nvidia:
            for idx, name, temp in self._nvidia.values:
                out.append(Reading(f"system/nvidia{idx}/gpu", f"GPU {name}", "temperature", temp, "System", "system"))
        return out
