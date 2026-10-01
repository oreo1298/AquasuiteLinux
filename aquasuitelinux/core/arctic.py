"""ARCTIC Fan Controller: 10 PWM fan channels through the Linux ``arctic_fan`` driver (Linux 7.2+).

The driver (``drivers/hwmon/arctic_fan_controller.c``, written by ARCTIC) is the only way in: it
binds the device without a hidraw node and offers ``fan1..10_input`` (rpm, from a report the
device sends about once a second) and ``pwm1..10`` (0-255). The controller has no curves or modes
of its own: it runs every fan at 40 % until it gets its first command, then holds what it was
told. Quirks handled here:

* Each ``pwmN`` write sends one report with all ten channels, the others taken from the driver's
  cache, which starts at 0 (the device can't be asked for its state) and is cleared on resume. So
  the first write would stop every fan nobody has set yet. Before it, channels without a
  controller are set to 40 %, the speed the controller runs them at anyway; channels someone else
  already set keep their value.
* A write waits up to a second for the device to confirm it, so writes happen on a background
  thread and never hold up the engine; a failed write is retried and reported on the next call.
* After a suspend the device and the driver forget the duties (the driver's cache reads 0 where
  we had set something): everything is sent again.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from .device import BaseDevice, _read
from .devices import ARCTIC_FAN, FAN_CONTROLLER_HWMON, DeviceSpec
from .errors import DeviceError, NotSupported
from .model import Reading

log = logging.getLogger(__name__)

HWMON_NAME = "arctic_fan"
DEFAULT_PERCENT = 40.0          # what the controller runs fans at until it gets a command
RETRY_SECONDS = 2.0


def duty(percent: float) -> int:
    return round(max(0.0, min(100.0, percent)) * 255 / 100)


def _usb_serial(hwmon_dir: Path) -> str:
    """The USB serial number of the controller behind a hwmon chip, if it has one."""
    try:
        p = Path(os.path.realpath(hwmon_dir / "device"))
    except OSError:
        return ""
    for _ in range(4):
        if (p / "idVendor").exists():
            return re.sub(r"[^A-Za-z0-9]+", "", _read(p / "serial") or "")[:24]
        p = p.parent
    return ""


class ArcticFanDevice(BaseDevice):
    backend = "kernel"              # the device's own Linux driver, not a fallback

    def __init__(self, spec: DeviceSpec, hwmon_dir: Path, index: int = 0):
        super().__init__()
        self.spec = spec
        self.dir = hwmon_dir
        self.path = str(hwmon_dir)
        self.index = index
        self.serial = _usb_serial(hwmon_dir)
        try:
            self.hid_id = os.path.basename(os.path.realpath(hwmon_dir / "device"))
        except OSError:
            self.hid_id = ""
        self._desired: dict[int, int] = {}       # channel (1-10) -> duty 0-255 we want
        self._acked: dict[int, int] = {}         # channel -> duty the device confirmed (through the driver)
        self._failed = ""                         # a write error not reported yet
        self._cond = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None

    @property
    def key(self) -> str:
        if self.serial:
            return f"{self.spec.kind}-{self.serial}"
        return self.spec.kind if self.index == 0 else f"{self.spec.kind}{self.index + 1}"

    # ----------------------------------------------------------------- capabilities
    def _writable(self) -> bool:
        return os.access(self.dir / "pwm1", os.W_OK)

    def capabilities(self) -> set[str]:
        return {"monitor", "manual"} if self._writable() else {"monitor"}

    def can_control(self, output_key: str) -> bool:
        idx = self.spec.fan_index(output_key)
        return idx >= 0 and (self.dir / f"pwm{idx + 1}").exists() and self._writable()

    # ----------------------------------------------------------------- reading
    def _num(self, name: str) -> int | None:
        raw = _read(self.dir / name)
        try:
            return None if raw is None else int(raw)
        except ValueError:
            return None

    def poll(self) -> list[Reading]:
        if not (self.dir / "name").exists():
            self.online = False
            raise DeviceError(f"{self.spec.name} ({self.dir}) was disconnected")
        values = []
        caches: dict[int, int | None] = {}
        for i, f in enumerate(self.spec.fans, start=1):
            rpm = self._num(f"fan{i}_input")
            caches[i] = self._num(f"pwm{i}")
            values.append((f"{f.key}.rpm", f"{f.label} speed", "rpm", None if rpm is None else float(rpm), "Fans"))
            values.append((f"{f.key}.percent", f"{f.label} output", "percent", self._percent(i, caches[i]), "Fans"))
        self._check_reset(caches)
        self.online = any(v[3] is not None for v in values if v[2] == "rpm")
        self.error = self._failed or ("" if self.online else "no fan speeds from the driver")
        return self._readings(values)

    def _percent(self, channel: int, cache: int | None) -> float | None:
        """The output we know the fan runs at: what the device confirmed, else what someone else set."""
        with self._cond:
            d = self._acked.get(channel)
        if d is None and cache:
            d = cache
        return None if d is None else round(d * 100 / 255, 1)

    def _check_reset(self, caches: dict[int, int | None]) -> None:
        """The driver clears its cache on resume, when the device has forgotten its duties too."""
        with self._cond:
            if not any(d > 0 and caches.get(i) == 0 for i, d in self._acked.items()):
                return
            log.info("%s: the controller lost its fan speeds (resume?); setting them again", self.key)
            self._acked.clear()
            self._cond.notify_all()

    # ----------------------------------------------------------------- writing
    def set_manual(self, powers: dict[str, float]) -> None:
        with self._cond:
            if self._failed:
                text, self._failed = self._failed, ""
                raise DeviceError(text)
            for key, pct in powers.items():
                idx = self.spec.fan_index(key)
                if idx < 0:
                    raise NotSupported(f"{self.spec.name} has no output {key!r}")
                self._desired[idx + 1] = duty(pct)
            for i in range(1, len(self.spec.fans) + 1):
                if i in self._desired:
                    continue
                cache = self._num(f"pwm{i}")
                if cache:                          # set by someone else: the driver resends it anyway
                    self._desired[i] = self._acked[i] = cache
                else:                              # unknown: keep the controller's own default
                    self._desired[i] = duty(DEFAULT_PERCENT)
            if self._thread is None or not self._thread.is_alive():
                self._stop = False
                self._thread = threading.Thread(target=self._writer, name=f"{self.key}-writer", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _pending(self) -> list[tuple[int, int]]:
        return sorted((i, d) for i, d in self._desired.items() if self._acked.get(i) != d)

    def _writer(self) -> None:
        while True:
            with self._cond:
                while not self._pending() and not self._stop:
                    self._cond.wait(5.0)
                todo = self._pending()
                if not todo and self._stop:
                    return
            for channel, d in todo:
                try:
                    (self.dir / f"pwm{channel}").write_text(str(d))   # returns once the device confirmed it
                except OSError as exc:
                    why = exc.strerror or str(exc)
                    if isinstance(exc, PermissionError):
                        why += " (writing needs root: use the background service)"
                    with self._cond:
                        self._failed = f"{self.spec.name}: setting fan {channel} failed: {why}"
                        self.error = self._failed
                        if self._stop:
                            return                  # don't hold up shutdown with retries
                        self._cond.wait(RETRY_SECONDS)
                    break
                with self._cond:
                    self._acked[channel] = d
                    self.writes += 1
                    self._failed = ""

    def flush(self, timeout: float = 15.0) -> bool:
        """Wait until every wanted duty is confirmed (or ``timeout``)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._cond:
                if not self._pending():
                    return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        """Send what is still pending (e.g. the fallback power on exit), then stop the writer."""
        thread = self._thread
        if thread is None:
            return
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        thread.join(timeout=15.0)


def arctic_devices(root: Path) -> list[ArcticFanDevice]:
    found: list[ArcticFanDevice] = []
    if not root.is_dir():
        return found
    for d in sorted(root.iterdir(), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        if FAN_CONTROLLER_HWMON.get(_read(d / "name") or "") == ARCTIC_FAN.kind:
            found.append(ArcticFanDevice(ARCTIC_FAN, d, len(found)))
    return found
