"""Connect to the background service, or run the engine in this process when it isn't running."""

from __future__ import annotations

import logging
from pathlib import Path

from . import config as config_mod
from .api import LocalAPI, ServiceAPI
from .engine import Engine
from .ipc import Client, socket_path

log = logging.getLogger(__name__)


def service_running(path: Path | None = None) -> bool:
    client = Client(path, timeout=2.0)
    try:
        return client.available()
    finally:
        client.close()


def local_engine(demo: bool = False, config_path: Path | None = None, start: bool = True,
                 control: bool = True) -> LocalAPI:
    path = config_path or config_mod.user_config_path()
    if demo:
        from .demo import demo_config, demo_provider
        cfg = demo_config() if config_path is None or not path.exists() else config_mod.load(path)
        provider = demo_provider()
        save = (lambda c: config_mod.save(c, path, 0o600)) if config_path else None
        mode = "demo"
    else:
        from .discovery import HardwareProvider
        cfg = config_mod.load(path)
        provider = HardwareProvider()

        def save(c):
            config_mod.save(c, path, 0o600)
        mode = "standalone"
    engine = Engine(cfg, provider, mode=mode, save_config=save, control=control)
    if start:
        engine.tick()
        engine.start()
    return LocalAPI(engine)


def connect(demo: bool = False, prefer_service: bool = True, config_path: Path | None = None,
            socket: Path | None = None):
    """The service's API if it runs (and demo mode wasn't asked for), else a local engine."""
    if prefer_service and not demo:
        client = Client(socket or socket_path())
        if client.available():
            return ServiceAPI(client)
        client.close()
    return local_engine(demo=demo, config_path=config_path)
