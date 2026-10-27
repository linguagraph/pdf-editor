"""Elanero branding: the application icon, the startup splash screen and the About artwork.

Everything comes from the approved pack in ``data/branding/elanero`` (provenance, hashes and
usage rules are in its ``README.txt`` and ``CLAUDE_CODE_INTEGRATION.md``). The images are used
as they are: scaled proportionally, never redrawn, recolored or cropped.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap, QScreen
from PySide6.QtWidgets import QSplashScreen

from pdfeditor.bundle import data_path

ICON_SIZES = (16, 24, 32, 48, 64, 96, 128, 256, 512)
# The splash art is 1920 x 1200 (16:10): shown at half that in logical pixels, which a 200%
# display draws from the full-resolution image.
SPLASH_SIZE = QSize(960, 600)
# A small screen gets a smaller splash: at most this share of its work area.
SPLASH_MAX_SHARE = 0.8


def brand_path(*parts: str) -> Path:
    return data_path("branding", "elanero", *parts)


def icon_png(size: int) -> Path:
    return brand_path("icons", "png", f"elanero-{size}.png")


def windows_icon() -> Path:
    """The multi-resolution .ico (executable, installer, file associations from source)."""
    return brand_path("icons", "platform", "elanero.ico")


def app_icon() -> QIcon:
    """Window and taskbar icon with every bundled size, so Qt picks a sharp one at any DPI."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addFile(str(icon_png(size)), QSize(size, size))
    return icon


def splash_scheme(theme: str) -> str:
    """``light`` or ``dark`` for the saved theme preference (``system`` follows Windows)."""
    if theme in ("light", "dark"):
        return theme
    scheme = QGuiApplication.styleHints().colorScheme()
    return "dark" if scheme == Qt.ColorScheme.Dark else "light"


def splash_image(scheme: str) -> Path:
    return brand_path("splash", "png", f"elanero-splash-{scheme}-1920x1200.png")


def splash_logical_size(available: QSize) -> QSize:
    """The splash's size on a screen with ``available`` work area: 960 x 600, smaller (same
    aspect ratio) when that doesn't fit comfortably."""
    scale = min(
        1.0,
        available.width() * SPLASH_MAX_SHARE / SPLASH_SIZE.width(),
        available.height() * SPLASH_MAX_SHARE / SPLASH_SIZE.height(),
    )
    return QSize(round(SPLASH_SIZE.width() * scale), round(SPLASH_SIZE.height() * scale))


def splash_pixmap(scheme: str, screen: QScreen | None = None) -> QPixmap:
    """The splash for ``scheme``, scaled to fit ``screen`` at its device pixel ratio."""
    screen = screen or QGuiApplication.primaryScreen()
    available = screen.availableGeometry().size() if screen is not None else SPLASH_SIZE
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    logical = splash_logical_size(available)
    source = QPixmap(str(splash_image(scheme)))
    pixmap = source.scaled(
        logical * ratio,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def show_splash(theme: str) -> QSplashScreen:
    """Show the startup splash in the theme the main window will open in.

    The artwork already has its own (static) "Loading..." bar, so no live progress is drawn
    on top. The caller closes it with ``finish(window)`` once the window is up, or ``close()``
    when startup fails.
    """
    splash = QSplashScreen(splash_pixmap(splash_scheme(theme)))
    splash.setAccessibleName("Elanero is starting")
    splash.show()
    QGuiApplication.processEvents()  # paint it before the slow imports start
    return splash


def wordmark(height: int, ratio: float = 1.0) -> QPixmap:
    """The horizontal lockup (10:3), ``height`` logical pixels high. Its lettering is deep
    teal, made for light backgrounds."""
    source = QPixmap(str(brand_path("branding", "elanero-wordmark.png")))
    smooth = Qt.TransformationMode.SmoothTransformation
    pixmap = source.scaledToHeight(round(height * ratio), smooth)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap
