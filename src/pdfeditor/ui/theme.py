"""Light/dark application theme."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication


class Theme(Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


def apply_theme(app: QApplication, theme: Theme) -> None:
    """Use Fusion (consistent across platforms) and let Qt derive the palette for the scheme."""
    app.setStyle("Fusion")
    scheme = {
        Theme.SYSTEM: Qt.ColorScheme.Unknown,
        Theme.LIGHT: Qt.ColorScheme.Light,
        Theme.DARK: Qt.ColorScheme.Dark,
    }[theme]
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "setColorScheme"):  # Qt >= 6.8
        hints.setColorScheme(scheme)
