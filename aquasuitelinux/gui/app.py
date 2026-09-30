"""GUI entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .. import APP_ID, __app_name__, __version__

ICON_PATH = Path(__file__).resolve().parent.parent / "data" / "aquasuitelinux.svg"


def _app_icon():
    from PySide6.QtGui import QIcon

    themed = QIcon.fromTheme(APP_ID)
    if not themed.isNull():
        return themed
    if ICON_PATH.exists():
        return QIcon(str(ICON_PATH))
    return QIcon()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aquasuitelinux",
                                     description=f"{__app_name__}: monitoring and fan control for Aquacomputer devices")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    parser.add_argument("--demo", action="store_true", help="use simulated devices (nothing touches your hardware)")
    parser.add_argument("--standalone", action="store_true",
                        help="run the control engine in the app even if the background service is available")
    parser.add_argument("--theme", choices=("system", "dark", "light"), help="override the colour theme")
    parser.add_argument("--config", help="configuration file for standalone mode")
    parser.add_argument("-v", "--verbose", action="store_true")
    args, qt_args = parser.parse_known_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    from PySide6.QtCore import QLockFile, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ..core import session
    from ..core.errors import AquaError
    from .bridge import Bridge
    from .main_window import MainWindow
    from .settings import GuiSettings
    from .theme import theme

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setApplicationName("aquasuitelinux")
    QApplication.setApplicationDisplayName(__app_name__)
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName("aquasuitelinux")
    QGuiApplication.setDesktopFileName(APP_ID)

    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    icon = _app_icon()
    app.setWindowIcon(icon)
    app.setQuitOnLastWindowClosed(False)

    gs = GuiSettings.load()
    theme.apply(app, args.theme or os.environ.get("AQUASUITELINUX_THEME") or gs.theme)
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda _s: theme.apply(app) if theme.mode == "system" else None)

    use_service = gs.prefer_service and not args.standalone and not args.demo
    if not args.demo and not use_service and session.service_running():
        QMessageBox.warning(None, __app_name__, "The background service is running and controls the devices. "
                            "AquasuiteLinux will connect to it instead of running a second engine.")
        use_service = True
    lock = None
    if not use_service or not session.service_running():
        cache = Path(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        cache.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(cache / f"aquasuitelinux{'-demo' if args.demo else ''}.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            QMessageBox.information(None, __app_name__, f"{__app_name__} is already running.")
            return 0
    try:
        api = session.connect(demo=args.demo, prefer_service=use_service,
                              config_path=Path(args.config) if args.config else None)
    except AquaError as exc:
        QMessageBox.critical(None, __app_name__, str(exc))
        return 1

    bridge = Bridge(api)
    win = MainWindow(bridge, gs, icon)
    bridge.start()
    win.show()
    code = app.exec()
    if lock is not None:
        lock.unlock()
    return code


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
