# AquasuiteLinux — convenience targets.
# Arch-based distributions: use the PKGBUILD instead (makepkg -si).

PREFIX ?= $(HOME)/.local
PYTHON ?= python3

.PHONY: help run demo install install-system uninstall test lint gui-check screenshots dist clean

help:
	@echo "AquasuiteLinux make targets:"
	@echo "  make run             Start the app from this folder"
	@echo "  make demo            Start it with simulated devices"
	@echo "  make install         Install for your user (PREFIX=$(PREFIX)) plus the udev rule"
	@echo "  make install-system  Install system-wide with the background service (needs sudo)"
	@echo "  make uninstall       Remove the per-user installation"
	@echo "  make test            Run the test suite"
	@echo "  make lint            Run ruff"
	@echo "  make gui-check       Headless start-up check"
	@echo "  make screenshots     Regenerate docs/screenshots"
	@echo "  make dist            Build a wheel and sdist"

run:
	./aquasuitelinux.sh

demo:
	./aquasuitelinux.sh --demo

install:
	PREFIX="$(PREFIX)" ./packaging/install.sh

install-system:
	sudo ./packaging/install.sh --system

uninstall:
	PREFIX="$(PREFIX)" ./packaging/uninstall.sh

test:
	QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest -q

lint:
	ruff check aquasuitelinux tests tools

gui-check:
	QT_QPA_PLATFORM=offscreen AQUASUITELINUX_SOCKET=/nonexistent $(PYTHON) -c "from PySide6.QtWidgets import QApplication; \
	app = QApplication([]); from aquasuitelinux.core.session import local_engine; \
	from aquasuitelinux.gui.bridge import Bridge; from aquasuitelinux.gui.main_window import MainWindow; \
	from aquasuitelinux.gui.settings import GuiSettings; api = local_engine(demo=True); \
	w = MainWindow(Bridge(api), GuiSettings(tray=False)); api.close(); print('GUI OK')"

screenshots:
	for t in dark light; do $(PYTHON) tools/screenshot.py docs/screenshots --theme $$t --dialogs; done

dist:
	$(PYTHON) -m build

clean:
	rm -rf build dist ./*.egg-info .pytest_cache .ruff_cache src pkg *.pkg.tar.*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
