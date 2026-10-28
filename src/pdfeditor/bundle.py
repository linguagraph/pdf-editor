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


def _package_name(function: str) -> str | None:
    """``GetCurrentPackageFullName``/``FamilyName`` for this process, or None when it isn't
    running from an MSIX package (or not on Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        get = getattr(ctypes.windll.kernel32, function)
    except AttributeError:  # Windows 7: no packages
        return None
    length = ctypes.c_uint32(0)
    if get(ctypes.byref(length), None) != 122:  # ERROR_INSUFFICIENT_BUFFER: there is one
        return None  # APPMODEL_ERROR_NO_PACKAGE
    buffer = ctypes.create_unicode_buffer(length.value)
    if get(ctypes.byref(length), buffer) != 0:
        return None
    return buffer.value


def is_packaged() -> bool:
    """True when running from the Microsoft Store (MSIX) package."""
    return _package_name("GetCurrentPackageFullName") is not None


def package_family_name() -> str | None:
    return _package_name("GetCurrentPackageFamilyName")


def channel() -> str:
    """Where this copy came from, for Help > About and support requests."""
    if is_packaged():
        return "Microsoft Store"
    if not is_frozen():
        return "Source"
    # The Inno Setup installer leaves its uninstaller next to the exe.
    return "Installer" if (Path(sys.executable).parent / "unins000.exe").exists() else "Portable"


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
