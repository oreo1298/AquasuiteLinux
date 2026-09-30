"""Per-user GUI preferences (``~/.config/aquasuitelinux/gui.json``)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..core.config import user_config_dir


@dataclass
class GuiSettings:
    theme: str = "system"
    pinned: list[str] = field(default_factory=list)      # sensors shown as tiles on the overview
    chart: list[str] = field(default_factory=list)       # sensors in the overview chart
    chart_span: int = 600
    prefer_service: bool = True
    tray: bool = True
    close_to_tray: bool = False
    show_unavailable: bool = False
    window: dict = field(default_factory=dict)
    welcomed: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> GuiSettings:
        path = path or user_config_dir() / "gui.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names}) if isinstance(data, dict) else cls()

    def save(self, path: Path | None = None) -> None:
        path = path or user_config_dir() / "gui.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gui-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass
