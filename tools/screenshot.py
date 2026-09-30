#!/usr/bin/env python3
"""Render the GUI with the simulated devices and save screenshots of every page.

Usage: tools/screenshot.py OUTPUT_DIR [--theme dark|light] [--size 1480x920] [--pages overview,controllers]
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="aqua-shot-"))
os.environ.setdefault("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="aqua-shot-cache-"))
os.environ.setdefault("AQUASUITELINUX_SOCKET", os.path.join(tempfile.gettempdir(), "aqua-shot-none.sock"))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aquasuitelinux.core.session import local_engine  # noqa: E402
from aquasuitelinux.gui.app import _app_icon  # noqa: E402
from aquasuitelinux.gui.bridge import Bridge  # noqa: E402
from aquasuitelinux.gui.main_window import MainWindow  # noqa: E402
from aquasuitelinux.gui.settings import GuiSettings  # noqa: E402
from aquasuitelinux.gui.theme import theme  # noqa: E402

PAGES = {"overview": 0, "sensors": 1, "controllers": 2, "fans": 3, "alarms": 4}


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--theme", default="dark")
    ap.add_argument("--size", default="1480x920")
    ap.add_argument("--pages", default=",".join(PAGES))
    ap.add_argument("--warmup", type=float, default=40.0, help="seconds of simulated history before capturing")
    ap.add_argument("--dialogs", action="store_true", help="also capture the import and Delta T dialogs")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    w, h = (int(v) for v in args.size.split("x"))

    app = QApplication([])
    if "Noto Sans" in __import__("PySide6.QtGui", fromlist=["QFontDatabase"]).QFontDatabase.families():
        app.setFont(QFont("Noto Sans", 10))
    theme.apply(app, args.theme)
    api = local_engine(demo=True)
    api.engine.provider._devices[0].transport.device.world.speed = 12.0   # build up history quickly
    bridge = Bridge(api)
    gs = GuiSettings(tray=False)
    win = MainWindow(bridge, gs, _app_icon())
    win.resize(w, h)
    bridge.start()
    win.show()
    pump(args.warmup)
    api.engine.provider._devices[0].transport.device.world.speed = 4.0
    for name in args.pages.split(","):
        win.show_page(PAGES[name])
        if name == "controllers":
            win.controllers.select("radiator")
        if name == "fans":
            win.outputs.select("quadro-10234-55001/fan1")
        pump(3.0)
        win.toast.hide()
        path = args.out / f"{name}-{args.theme}.png"
        win.grab().save(str(path))
        print(path)
    if args.dialogs:
        from aquasuitelinux.core.demo import QUADRO
        from aquasuitelinux.gui.dialogs import ImportDialog, VirtualSensorDialog
        win.show_page(0)
        dlg = ImportDialog(bridge, win, QUADRO)
        dlg.resize(780, 660)
        dlg.show()
        pump(2.5)
        path = args.out / f"import-{args.theme}.png"
        dlg.grab().save(str(path))
        print(path)
        dlg.close()
        dlg = VirtualSensorDialog(bridge, win, kind="difference", inputs=[f"{QUADRO}/temp1", f"{QUADRO}/temp2"])
        dlg.name.setText("Coolant ΔT")
        dlg.show()
        pump(1.5)
        path = args.out / f"deltat-{args.theme}.png"
        dlg.grab().save(str(path))
        print(path)
        dlg.close()
    win.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
