"""Alarms: watch a sensor and react when it crosses a limit for longer than a delay."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass

from .config import AlarmConfig
from .model import Reading

log = logging.getLogger(__name__)

ACTIONS = {
    "notify": "Show a notification",
    "fans_max": "Run all controlled fans at full power",
    "command": "Run a command",
    "shutdown": "Shut the computer down (service only)",
}


@dataclass
class AlarmState:
    pending_since: float | None = None
    active_since: float | None = None
    value: float | None = None
    fired: bool = False


def triggered(cfg: AlarmConfig, reading: Reading | None) -> bool:
    value = None if reading is None else reading.value
    if cfg.condition == "missing":
        return value is None
    if value is None:
        return False
    return value > cfg.threshold if cfg.condition == "above" else value < cfg.threshold


class Alarms:
    def __init__(self, allow_shutdown: bool = False, run_commands: bool = True):
        self.states: dict[str, AlarmState] = {}
        self.allow_shutdown = allow_shutdown
        self.run_commands = run_commands
        self.events: list[tuple[str, str]] = []     # (level, text) produced by the last evaluate()

    def evaluate(self, alarms: list[AlarmConfig], readings: dict[str, Reading], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.events = []
        known = set()
        for cfg in alarms:
            known.add(cfg.id)
            st = self.states.setdefault(cfg.id, AlarmState())
            reading = readings.get(cfg.sensor)
            st.value = None if reading is None else reading.value
            if not cfg.enabled or not triggered(cfg, reading):
                if st.active_since is not None:
                    self.events.append(("success", f"{cfg.name}: back to normal"))
                st.pending_since = st.active_since = None
                st.fired = False
                continue
            if st.pending_since is None:
                st.pending_since = now
            if st.active_since is None and now - st.pending_since >= max(0.0, cfg.delay):
                st.active_since = now
            if st.active_since is not None and not st.fired:
                st.fired = True
                self._fire(cfg, reading)
        for gone in set(self.states) - known:
            del self.states[gone]

    def active(self, alarms: list[AlarmConfig]) -> list[AlarmConfig]:
        return [a for a in alarms if self.states.get(a.id) and self.states[a.id].active_since is not None]

    def fans_max(self, alarms: list[AlarmConfig]) -> bool:
        return any("fans_max" in a.actions for a in self.active(alarms))

    def _fire(self, cfg: AlarmConfig, reading: Reading | None) -> None:
        if reading is None or reading.value is None:
            text = f"{cfg.name}: sensor unavailable"
        else:
            text = f"{cfg.name}: {reading.label} is {reading.value:g} {reading.unit}".rstrip()
        self.events.append(("error", text))
        log.warning("alarm: %s", text)
        if "command" in cfg.actions and cfg.command and self.run_commands:
            try:
                env = dict(os.environ, AQUA_ALARM=cfg.name, AQUA_VALUE="" if reading is None else str(reading.value))
                subprocess.Popen(shlex.split(cfg.command), env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except (OSError, ValueError) as exc:
                self.events.append(("warning", f"{cfg.name}: could not run the command ({exc})"))
        if "shutdown" in cfg.actions:
            if self.allow_shutdown:
                self.events.append(("error", f"{cfg.name}: shutting the computer down"))
                try:
                    subprocess.Popen(["systemctl", "poweroff"], start_new_session=True)
                except OSError as exc:
                    self.events.append(("error", f"shutdown failed: {exc}"))
            else:
                self.events.append(("warning", f"{cfg.name}: shutdown is only done by the background service"))

    def snapshot(self, alarms: list[AlarmConfig]) -> list[dict]:
        out = []
        now = time.monotonic()
        for cfg in alarms:
            st = self.states.get(cfg.id, AlarmState())
            out.append({"id": cfg.id, "name": cfg.name, "sensor": cfg.sensor, "enabled": cfg.enabled,
                        "active": st.active_since is not None, "value": st.value,
                        "seconds": 0 if st.active_since is None else round(now - st.active_since)})
        return out
