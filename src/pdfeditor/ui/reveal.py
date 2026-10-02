"""Show a file in the system file manager."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtGui import QDesktopServices

log = logging.getLogger(__name__)


def _launch_explorer(path: Path) -> bool:
    """Windows: open Explorer with ``path`` selected. Explorer ships with Windows, so this
    needs nothing installed; its odd ``/select,"path"`` syntax must reach it unquoted."""
    process = QProcess()
    process.setProgram("explorer.exe")
    process.setNativeArguments(f'/select,"{path}"')
    return bool(process.startDetached())


def show_in_folder(path: Path) -> bool:
    """Open the folder holding ``path``, with the file selected where the platform can.

    Returns False if nothing could be opened (the folder is gone, or no file manager).
    """
    path = Path(path)
    if sys.platform == "win32" and path.exists():
        try:
            if _launch_explorer(path.resolve()):
                return True
        except Exception:  # fall back to opening the folder
            log.warning("couldn't start Explorer for %s", path, exc_info=True)
    folder = path.parent
    if not folder.is_dir():
        return False
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))))
