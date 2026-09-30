#!/usr/bin/env bash
# Remove an installation made by packaging/install.sh (your settings are kept).
#   ./packaging/uninstall.sh            the per-user installation
#   sudo ./packaging/uninstall.sh --system
set -euo pipefail
APP_ID="io.github.oreo1298.AquasuiteLinux"
if [ "${1:-}" = "--system" ]; then
  PREFIX="${PREFIX:-/opt/aquasuitelinux}"
  systemctl disable --now aquasuited.service 2>/dev/null || true
  rm -f /etc/systemd/system/aquasuited.service
  systemctl daemon-reload 2>/dev/null || true
  rm -rf "$PREFIX"
  BIN=/usr/local/bin
  SHARE=/usr/local/share
  rm -f /etc/udev/rules.d/70-aquasuitelinux.rules /etc/sysusers.d/aquasuitelinux.conf
  udevadm control --reload-rules 2>/dev/null || true
  echo "Removed. /etc/aquasuitelinux (the service settings) was kept."
else
  PREFIX="${PREFIX:-$HOME/.local}"
  rm -rf "$PREFIX/lib/aquasuitelinux"
  BIN="$PREFIX/bin"
  SHARE="$PREFIX/share"
  echo "Removed from $PREFIX. Your settings in ~/.config/aquasuitelinux were kept."
  echo "The udev rule stays in /etc/udev/rules.d/70-aquasuitelinux.rules (remove it with sudo if you like)."
fi
rm -f "$BIN/aquasuitelinux" "$BIN/aquactl" "$BIN/aquasuited" \
      "$SHARE/applications/$APP_ID.desktop" \
      "$SHARE/metainfo/$APP_ID.metainfo.xml" \
      "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg"
