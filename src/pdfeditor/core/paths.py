"""Per-user writable locations (logs, crash reports, recovery copies)."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "pdfeditor"


def data_dir() -> Path:
    """Per-user writable directory, created on first use.

    ``PDFEDITOR_DATA_DIR`` overrides it (tests, portable setups).
    """
    override = os.environ.get("PDFEDITOR_DATA_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
        root = Path(base) if base else Path.home() / ".local" / "state"
        path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def recovery_dir() -> Path:
    return data_dir() / "recovery"
