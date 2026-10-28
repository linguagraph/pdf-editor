"""Per-user writable locations (logs, crash reports, recovery copies)."""

from __future__ import annotations

import os
from pathlib import Path

from pdfeditor import bundle

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


def visible_path(path: Path) -> Path:
    """Where ``path`` really is, for showing to the user or opening in Explorer.

    Inside the MSIX package Windows silently redirects the app's writes under
    ``%LOCALAPPDATA%`` to the package's own folder, so other programs (Explorer, the user)
    must be given that folder instead. Elsewhere the path is returned unchanged.
    """
    family = bundle.package_family_name()
    local = os.environ.get("LOCALAPPDATA")
    if not family or not local:
        return path
    try:
        relative = path.relative_to(local)
    except ValueError:
        return path
    redirected = Path(local) / "Packages" / family / "LocalCache" / "Local" / relative
    return redirected if redirected.exists() or not path.exists() else path
