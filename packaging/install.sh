#!/usr/bin/env bash
# Install AquasuiteLinux on any Linux distribution.
#
#   ./packaging/install.sh             the app and aquactl for your user (~/.local), plus the udev rule
#                                      (asks for your password once, via sudo) so the app can reach devices
#   sudo ./packaging/install.sh --system
#                                      everything system-wide in /opt/aquasuitelinux, including the
#                                      background service (aquasuited), enabled and started
#
# Options: --no-udev (skip the udev rule), --no-service (with --system: don't enable the service).
# The virtualenv sees your system's Python packages, so a PySide6 from your distribution is reused;
# otherwise PySide6-Essentials is downloaded from PyPI (~100 MB).
set -euo pipefail

SYSTEM=0
UDEV=1
SERVICE=1
for arg in "$@"; do
  case "$arg" in
    --system) SYSTEM=1 ;;
    --no-udev) UDEV=0 ;;
    --no-service) SERVICE=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="io.github.oreo1298.AquasuiteLinux"
PY="${PYTHON:-python3}"

say() { printf '\033[1;34m>>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [ "$SYSTEM" = 1 ]; then
  [ "$(id -u)" = 0 ] || { echo "--system needs root: sudo $0 --system"; exit 1; }
  PREFIX="${PREFIX:-/opt/aquasuitelinux}"
  LIB="$PREFIX"
  BIN="/usr/local/bin"
  SHARE="/usr/local/share"
  SUDO=""
else
  PREFIX="${PREFIX:-$HOME/.local}"
  LIB="$PREFIX/lib/aquasuitelinux"
  BIN="$PREFIX/bin"
  SHARE="$PREFIX/share"
  SUDO="sudo"
  [ "$(id -u)" = 0 ] && SUDO=""
fi
VENV="$LIB/venv"

command -v "$PY" >/dev/null || { echo "python3 is required"; exit 1; }
"$PY" - <<'PYCHECK' || { echo "Python 3.10 or newer is required"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYCHECK

say "Creating virtualenv at $VENV"
mkdir -p "$LIB"
"$PY" -m venv --system-site-packages "$VENV" || {
  echo "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
"$VENV/bin/python" -m pip install --quiet --upgrade pip

extras=""
if "$VENV/bin/python" -c "import PySide6.QtWidgets, PySide6.QtSvg" 2>/dev/null; then
  say "Using the PySide6 installed on this system"
else
  say "PySide6 not found on the system; installing PySide6-Essentials from PyPI"
  extras="gui"
fi

say "Installing AquasuiteLinux"
"$VENV/bin/python" -m pip install --quiet "$HERE${extras:+[$extras]}"

say "Installing launchers into $BIN"
mkdir -p "$BIN"
for exe in aquasuitelinux aquactl aquasuited; do
  ln -sf "$VENV/bin/$exe" "$BIN/$exe"
done

say "Installing the menu entry and icon"
install -Dm644 "$HERE/aquasuitelinux/data/aquasuitelinux.svg" "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg"
mkdir -p "$SHARE/applications"
sed "s#^Exec=aquasuitelinux#Exec=$BIN/aquasuitelinux#" "$HERE/data/$APP_ID.desktop" \
  > "$SHARE/applications/$APP_ID.desktop"
install -Dm644 "$HERE/data/$APP_ID.metainfo.xml" "$SHARE/metainfo/$APP_ID.metainfo.xml"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$SHARE/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" 2>/dev/null || true

if [ "$UDEV" = 1 ]; then
  say "Installing the udev rule (device access without root)"
  if [ -n "$SUDO" ] && ! command -v sudo >/dev/null; then
    warn "sudo not found; install packaging/udev/70-aquasuitelinux.rules into /etc/udev/rules.d yourself"
  else
    if command -v systemd-sysusers >/dev/null; then
      $SUDO install -Dm644 "$HERE/packaging/sysusers/aquasuitelinux.conf" /etc/sysusers.d/aquasuitelinux.conf
      $SUDO systemd-sysusers /etc/sysusers.d/aquasuitelinux.conf || true
    else
      $SUDO groupadd -f aquasuite 2>/dev/null || true
    fi
    $SUDO install -Dm644 "$HERE/packaging/udev/70-aquasuitelinux.rules" /etc/udev/rules.d/70-aquasuitelinux.rules
    $SUDO udevadm control --reload-rules 2>/dev/null || true
    $SUDO udevadm trigger --subsystem-match=hidraw 2>/dev/null || true
    $SUDO udevadm trigger --subsystem-match=usb --attr-match=idVendor=0c70 2>/dev/null || true
  fi
fi

if [ "$SYSTEM" = 1 ]; then
  say "Installing the background service"
  sed "s#^ExecStart=.*#ExecStart=$VENV/bin/aquasuited#" "$HERE/packaging/systemd/aquasuited.service" \
    > /etc/systemd/system/aquasuited.service
  systemctl daemon-reload
  if [ "$SERVICE" = 1 ]; then
    systemctl enable --now aquasuited.service
    say "aquasuited is running: systemctl status aquasuited"
  else
    say "Enable it later with: systemctl enable --now aquasuited"
  fi
fi

echo
say "Done. Start AquasuiteLinux from your application menu, or run: aquasuitelinux"
say "If a device was plugged in during the install, re-plug it once."
if [ "$SYSTEM" = 0 ]; then
  case ":$PATH:" in
    *":$BIN:"*) : ;;
    *) warn "Add $BIN to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile" ;;
  esac
fi
