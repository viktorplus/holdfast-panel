"""Where the panel keeps its own files: %APPDATA%\\HoldfastPanel on Windows."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / "HoldfastPanel"
    path.mkdir(parents=True, exist_ok=True)
    return path
