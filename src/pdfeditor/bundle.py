"""Locate data shipped with the app, both from source and inside the frozen executable.

All bundled files live in the ``pdfeditor/data`` package directory. PyInstaller copies it into
the executable (see ``packaging/pdfeditor.spec``); ``importlib.resources`` finds it either way.
Never read data relative to the source tree or rely on tools being on PATH.
"""

from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path


def is_frozen() -> bool:
    """True inside the PyInstaller/Nuitka executable."""
    return bool(getattr(sys, "frozen", False))


def app_command() -> list[str]:
    """The command that starts this copy of the app: the frozen exe, or ``pythonw -m
    pdfeditor`` from a source checkout (``python`` when there's no ``pythonw``)."""
    exe = Path(sys.executable)
    if is_frozen():
        return [str(exe)]
    windowed = exe.with_name("pythonw.exe")
    return [str(windowed if windowed.exists() else exe), "-m", "pdfeditor"]


def data_path(*parts: str) -> Path:
    """Filesystem path of a bundled data file, e.g. ``data_path("selftest.pdf")``."""
    root = resources.files("pdfeditor") / "data"
    path = Path(str(root.joinpath(*parts)))
    if not path.exists():
        raise FileNotFoundError(f"bundled data file missing: {'/'.join(parts)}")
    return path
