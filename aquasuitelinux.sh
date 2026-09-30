#!/usr/bin/env bash
# Run AquasuiteLinux straight from this folder, without installing it.
# Needs Python 3.10+ and PySide6 (see the README for your distribution).
#   ./aquasuitelinux.sh            # the app
#   ./aquasuitelinux.sh --demo     # with simulated devices
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m aquasuitelinux "$@"
