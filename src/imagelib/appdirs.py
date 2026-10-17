"""Per-OS default location for the catalog."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_catalog_dir() -> Path:
    override = os.environ.get("IMAGELIB_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "imagelib"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "imagelib"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "imagelib"
