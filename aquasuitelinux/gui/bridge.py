"""Connects the GUI to the engine (in this process or in the background service).

A QTimer asks for a snapshot every second on a worker thread and republishes it as a Qt
signal; configuration changes go through one ordered queue so they reach the engine in
the order they were made.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal

from ..core.config import Config
from ..core.errors import AquaError
from . import worker

log = logging.getLogger(__name__)


class Bridge(QObject):
    snapshot = Signal(dict)
    config_changed = Signal(object)
    message = Signal(str, str)            # text, kind (success | info | warning | error)
    lost = Signal(str)

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.snap: dict = {}
        self.config = Config()
        self.readings: dict[str, dict] = {}
        self.outputs: dict[str, dict] = {}
        self._busy = False
        self._failures = 0
        self._seen_events: float = 0.0
        self.queue = worker.SerialQueue()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

    @property
    def mode(self) -> str:
        return getattr(self.api, "mode", "standalone")

    def start(self) -> None:
        self.reload_config()
        self.refresh()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()
        self.queue.wait(3000)

    # ------------------------------------------------------------------ polling
    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        worker.run(self.api.snapshot, self._got_snapshot, self._snapshot_failed)

    def _got_snapshot(self, snap) -> None:
        self._busy = False
        self._failures = 0
        if not snap:
            return
        self.snap = snap
        self.readings = {r["id"]: r for r in snap.get("readings", [])}
        self.outputs = {o["id"]: o for o in snap.get("outputs", [])}
        for t, level, text in snap.get("events", []):
            if t > self._seen_events:
                self._seen_events = t
                if self._primed:
                    self.message.emit(text, level)
        self._primed = True
        self.snapshot.emit(snap)

    _primed = False

    def _snapshot_failed(self, exc: Exception) -> None:
        self._busy = False
        self._failures += 1
        if self._failures == 3:
            self.lost.emit(str(exc))

    # ------------------------------------------------------------------ config
    def reload_config(self, done=None) -> None:
        def got(data):
            self.config = Config.from_dict(data)
            self.config_changed.emit(self.config)
            if done:
                done(self.config)
        worker.run(self.api.get_config, got, lambda e: self.message.emit(f"Could not read the settings: {e}", "error"))

    def apply(self, cfg: Config, success: str = "Saved", done=None) -> None:
        """Send a whole configuration to the engine (validated there), then reload it."""
        data = cfg.to_dict()

        def ok(_r):
            self.config = Config.from_dict(data)
            self.config_changed.emit(self.config)
            if success:
                self.message.emit(success, "success")
            self.refresh()
            if done:
                done(True)

        def fail(exc):
            text = str(exc)
            if isinstance(exc, PermissionError):
                text = f"Not allowed: {exc}"
            self.message.emit(text, "error")
            if done:
                done(False)

        self.queue.run(lambda: self.api.set_config(data), ok, fail)

    def edit(self, change, success: str = "Saved", done=None) -> None:
        """Apply ``change(cfg)`` to a copy of the current configuration and send it."""
        cfg = self.config.copy()
        change(cfg)
        self.apply(cfg, success, done)

    def call(self, method: str, *args, done=None, error=None, success: str = "") -> None:
        fn = getattr(self.api, method)

        def ok(result):
            if success:
                self.message.emit(success, "success")
            if done:
                done(result)
            self.refresh()

        def fail(exc):
            if error:
                error(exc)
            else:
                self.message.emit(str(exc), "error")

        self.queue.run(lambda: fn(*args), ok, fail)

    def history(self, ids: list[str], seconds: float, done) -> None:
        worker.run(lambda: self.api.history(ids, seconds), done, lambda _e: None)

    # ------------------------------------------------------------------ helpers
    def reading(self, sid: str) -> dict | None:
        return self.readings.get(sid)

    def label(self, sid: str) -> str:
        r = self.readings.get(sid)
        if r:
            return self.display(r)
        if sid.startswith("virtual/"):
            v = self.config.virtual(sid[8:])
            if v:
                return v.name
        return sid or "—"

    def device_name(self, key: str) -> str:
        for d in self.snap.get("devices", []):
            if d["key"] == key:
                return d.get("name") or d.get("model") or key
        cfg = self.config.devices.get(key)
        return (cfg.name if cfg and cfg.name else key)

    def display(self, r: dict) -> str:
        src = r.get("source", "")
        if src in ("virtual", "system"):
            return r["label"]
        return f"{r['label']} ({self.device_name(src)})"

    def sensor_list(self, kinds: set[str] | None = None, include_na: bool = False) -> list[dict]:
        """Readings for pickers: device sensors first, then virtual and system sensors."""
        order = {"virtual": 1, "system": 2}
        items = []
        for r in self.readings.values():
            if kinds and r.get("kind") not in kinds:
                continue
            if r.get("value") is None and not include_na:
                continue
            item = dict(r)
            item["_display"] = self.display(r)
            item["_group"] = r.get("source", "")
            items.append(item)
        items.sort(key=lambda r: (order.get(r["_group"], 0), r["_group"], _natural(r["id"])))
        return items

    def close(self) -> None:
        self.stop()
        try:
            self.api.close()
        except (AquaError, OSError) as exc:
            log.warning("closing: %s", exc)


def _natural(text: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


TEMPERATURE_KINDS = {"temperature", "delta"}
CONTROL_INPUT_KINDS = {"temperature", "delta", "percent", "power", "flow", "rpm", "number"}
