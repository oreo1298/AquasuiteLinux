"""One interface to the engine, whether it runs in this process or in the background service.

``LocalAPI`` wraps an ``Engine`` object; ``ServiceAPI`` sends the same calls over the
service's UNIX socket. The GUI and ``aquactl`` only use this interface.
"""

from __future__ import annotations

import base64

from .. import __version__
from .config import Config
from .engine import Engine

READ_METHODS = {"hello", "snapshot", "history", "get_config", "device_settings", "backup"}
WRITE_METHODS = {"set_config", "set_profile", "override", "apply_device_settings", "restore", "rescan"}


class LocalAPI:
    def __init__(self, engine: Engine):
        self.engine = engine

    @property
    def mode(self) -> str:
        return self.engine.mode

    def hello(self) -> dict:
        return {"version": __version__, "mode": self.engine.mode, "pid": __import__("os").getpid()}

    def snapshot(self) -> dict:
        return self.engine.snapshot()

    def history(self, ids: list[str], seconds: float = 600) -> dict:
        return self.engine.history_for(list(ids), float(seconds))

    def get_config(self) -> dict:
        return self.engine.get_config()

    def set_config(self, config: dict) -> None:
        self.engine.set_config(Config.from_dict(config))

    def set_profile(self, name: str) -> None:
        self.engine.set_profile(name)

    def override(self, output: str, power: float | None, seconds: float = 10.0) -> None:
        self.engine.override(output, power, seconds)

    def device_settings(self, device: str) -> dict:
        data = self.engine.device_settings(device)
        dev = self.engine.devices[device]
        data["raw"] = base64.b64encode(dev.read_control()).decode()
        return data

    def apply_device_settings(self, device: str, changes: dict) -> None:
        self.engine.apply_device_settings(device, changes)

    def backup(self, device: str) -> dict:
        return self.engine.backup(device)

    def restore(self, device: str, backup: dict) -> None:
        self.engine.restore(device, backup)

    def rescan(self) -> None:
        self.engine.rescan()

    def close(self) -> None:
        self.engine.stop()


class ServiceAPI:
    """The same methods, answered by the background service."""

    def __init__(self, client):
        self.client = client
        self.mode = "service"

    def __getattr__(self, name: str):
        if name in READ_METHODS | WRITE_METHODS:
            return lambda **kw: self.client.call(name, **kw)
        raise AttributeError(name)

    # positional-argument friendly wrappers used by the GUI and the CLI
    def history(self, ids, seconds: float = 600):
        return self.client.call("history", ids=list(ids), seconds=seconds)

    def set_config(self, config: dict):
        return self.client.call("set_config", config=config)

    def set_profile(self, name: str):
        return self.client.call("set_profile", name=name)

    def override(self, output: str, power, seconds: float = 10.0):
        return self.client.call("override", output=output, power=power, seconds=seconds)

    def device_settings(self, device: str):
        return self.client.call("device_settings", device=device)

    def apply_device_settings(self, device: str, changes: dict):
        return self.client.call("apply_device_settings", device=device, changes=changes)

    def backup(self, device: str):
        return self.client.call("backup", device=device)

    def restore(self, device: str, backup: dict):
        return self.client.call("restore", device=device, backup=backup)

    def close(self) -> None:
        self.client.close()
