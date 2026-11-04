"""Elanero branding: the application icon, the startup splash screen and the About artwork.

Everything comes from the approved pack in ``data/branding/elanero`` (provenance, hashes and
usage rules are in its ``README.txt`` and ``CLAUDE_CODE_INTEGRATION.md``). The images are used
as they are: scaled proportionally, never redrawn, recolored or cropped. The one derivative is
the splash's progress bar, which is painted live over the artwork's static example bar in the
artwork's own colors; the files themselves stay untouched.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEventLoop, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from pdfeditor.bundle import data_path

ICON_SIZES = (16, 24, 32, 48, 64, 96, 128, 256, 512)
# The splash art is 1920 x 1200 (16:10): shown at half that in logical pixels, which a 200%
# display draws from the full-resolution image.
SPLASH_SIZE = QSize(960, 600)
# A small screen gets a smaller splash: at most this share of its work area.
SPLASH_MAX_SHARE = 0.8
# The static example bar baked into both splash images, in the art's 1920 x 1200 pixels, and
# points just beside and inside it where its background, track and fill colors are sampled.
SPLASH_ART_WIDTH = 1920
SPLASH_BAR = QRectF(720, 752, 481, 13)
SPLASH_BACKGROUND_AT = (710, 758)
SPLASH_TRACK_AT = (1100, 758)
SPLASH_FILL_AT = (860, 758)
# How long finish() waits for the main window to reach the screen, so that nothing is left
# showing in between; it normally already has.
SPLASH_HANDOVER_MS = 1000


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


class LoadingSplash(QWidget):
    """The splash screen with a live progress bar over the artwork's static one.

    Startup reports real progress with :meth:`set_progress` between its stages; the GUI thread
    is busy during each stage, so every update repaints at once instead of waiting for the
    event loop. It stays up only as long as that work takes: the branding pack's rules say not
    to delay startup for the sake of the branding.

    A plain frameless widget, not a ``QSplashScreen``: on Windows (Qt 6.11) that class's
    ``show()`` sits idle for a full second before its first paint (issue #95).
    """

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self._pixmap = pixmap
        self._progress = 0.0
        self.resize(pixmap.deviceIndependentSize().toSize())
        self.move(self.screen().geometry().center() - self.rect().center())
        image = pixmap.toImage()
        scale = image.width() / SPLASH_ART_WIDTH

        def sample(at: tuple[int, int]) -> QColor:
            return image.pixelColor(round(at[0] * scale), round(at[1] * scale))

        self._background = sample(SPLASH_BACKGROUND_AT)
        self._track = sample(SPLASH_TRACK_AT)
        self._fill = sample(SPLASH_FILL_AT)

    def pixmap(self) -> QPixmap:
        return self._pixmap

    def progress(self) -> float:
        return self._progress

    def set_progress(self, fraction: float) -> None:
        """Move the bar to ``fraction`` (0 to 1); it never moves backwards."""
        fraction = min(max(fraction, 0.0), 1.0)
        if fraction <= self._progress:
            return
        self._progress = fraction
        if self.isVisible():
            self.repaint()

    def finish(self, window: QWidget) -> None:
        """Close the splash once the shown ``window`` is on screen (or after a short while)."""
        handle = window.windowHandle()
        clock = QElapsedTimer()
        clock.start()
        while (
            handle is not None and not handle.isExposed() and clock.elapsed() < SPLASH_HANDOVER_MS
        ):
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        self.close()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.hide()  # a click dismisses it

    def bar_rect(self) -> QRectF:
        """The bar in widget (logical) coordinates."""
        scale = self.pixmap().deviceIndependentSize().width() / SPLASH_ART_WIDTH
        bar = SPLASH_BAR
        return QRectF(bar.x() * scale, bar.y() * scale, bar.width() * scale, bar.height() * scale)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        bar = self.bar_rect()
        # Cover the example bar (and its antialiased edge), then draw the real one in its place.
        painter.fillRect(bar.adjusted(-2, -2, 2, 2), self._background)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QPainterPath()
        radius = bar.height() / 2
        track.addRoundedRect(bar, radius, radius)
        painter.fillPath(track, self._track)
        if self._progress > 0:
            painter.setClipPath(track)
            filled = QRectF(bar)
            filled.setWidth(max(bar.width() * self._progress, bar.height()))
            fill = QPainterPath()
            fill.addRoundedRect(filled, radius, radius)
            painter.fillPath(fill, self._fill)


def show_splash(theme: str) -> LoadingSplash:
    """Show the startup splash in the theme the main window will open in.

    The caller advances its bar with ``set_progress`` as startup proceeds, then shows the
    window and calls ``finish(window)``, or ``close()`` when startup fails.
    """
    splash = LoadingSplash(splash_pixmap(splash_scheme(theme)))
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
