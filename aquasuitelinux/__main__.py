"""``python -m aquasuitelinux`` launches the GUI (``python -m aquasuitelinux.cli`` for the CLI)."""

import sys

from .gui.app import main

sys.exit(main())
