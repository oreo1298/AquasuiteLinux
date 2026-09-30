"""``aquasuited``: the background service that keeps fan control running without the app.

Run by systemd (``aquasuited.service``) as root, it owns the devices and the system
configuration (``/etc/aquasuitelinux/config.json``) and serves the app and ``aquactl``
over ``/run/aquasuitelinux/aquasuited.sock``.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from .. import __version__
from . import config as config_mod
from .api import LocalAPI
from .engine import Engine
from .errors import AquaError
from .ipc import Server, socket_path

log = logging.getLogger("aquasuited")


def sd_notify(message: str) -> None:
    """Tell systemd about our state (Type=notify), without needing libsystemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError:
        pass


def build_engine(cfg_path: Path, demo: bool) -> Engine:
    cfg = config_mod.load(cfg_path)

    def save(c):
        try:
            config_mod.save(c, cfg_path)
        except OSError as exc:
            log.error("cannot save %s: %s", cfg_path, exc)

    if demo:
        from .demo import demo_config, demo_provider
        provider = demo_provider()
        if not cfg_path.exists():
            cfg = demo_config()
    else:
        from .discovery import HardwareProvider
        provider = HardwareProvider()
    return Engine(cfg, provider, mode="service", save_config=save, allow_shutdown=os.geteuid() == 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aquasuited", description="AquasuiteLinux background service")
    p.add_argument("--version", action="version", version=f"aquasuited {__version__}")
    p.add_argument("--config", default=str(config_mod.SYSTEM_CONFIG), help="configuration file")
    p.add_argument("--socket", default=None, help=f"socket path (default {socket_path()})")
    p.add_argument("--demo", action="store_true", help="use simulated devices")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg_path = Path(args.config)
    try:
        engine = build_engine(cfg_path, args.demo)
    except AquaError as exc:
        log.error("%s", exc)
        return 1
    api = LocalAPI(engine)
    try:
        server = Server(api, Path(args.socket) if args.socket else socket_path(),
                        lambda: engine.config.settings.allowed_groups)
    except OSError as exc:
        log.error("cannot create the socket: %s", exc)
        return 1

    stop = threading.Event()

    def on_signal(signum, _frame):
        if signum == signal.SIGHUP:
            try:
                engine.set_config(config_mod.load(cfg_path), save=False)
                log.info("configuration reloaded")
            except AquaError as exc:
                log.error("reload failed: %s", exc)
            return
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    engine.start()
    server.start()
    log.info("aquasuited %s running (socket %s)", __version__, server.path)
    sd_notify("READY=1\nSTATUS=Controlling fans")
    while not stop.wait(5.0):
        snap = engine.snapshot()
        if snap:
            sd_notify(f"STATUS={len(snap.get('devices', []))} device(s), "
                      f"{sum(1 for o in snap.get('outputs', []) if o['placement'] != 'unmanaged')} controlled output(s)")
    sd_notify("STOPPING=1")
    log.info("stopping")
    server.close()
    engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
