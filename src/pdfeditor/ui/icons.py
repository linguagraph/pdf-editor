"""Toolbar/menu icons: bundled Lucide SVGs tinted with the current palette.

The SVGs (``data/icons``, ISC licensed, see the LICENSE there) are stroked with
``currentColor``. They are rendered on demand in the palette's text colour instead of being
turned into fixed pixmaps once, so they follow light/dark theme switches and get a proper
disabled look without shipping two sets.
"""

from __future__ import annotations

import functools

from PySide6.QtCore import QByteArray, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

from pdfeditor.bundle import data_path


@functools.cache
def _svg(name: str) -> bytes:
    return data_path("icons", f"{name}.svg").read_bytes()


def svg_data(name: str) -> bytes:
    """The raw SVG of bundled icon ``name`` (stroked with ``currentColor``), for widgets that
    tint it themselves, e.g. icons drawn on an accent fill."""
    return _svg(name)


def _color(mode: QIcon.Mode) -> QColor:
    palette = QGuiApplication.palette()
    if mode is QIcon.Mode.Disabled:
        return palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    # Selected rows use a light accent tint with normal text (see ui/style), so no special case.
    return palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)


class _TintedSvgEngine(QIconEngine):
    def __init__(self, name: str, color: str | None = None) -> None:
        super().__init__()
        self._name = name
        self._fixed = color  # e.g. text on an accent-filled button; None follows the palette
        self._pixmaps: dict[tuple[int, int, str], QPixmap] = {}

    def _render(self, painter: QPainter, rect: QRect, mode: QIcon.Mode) -> None:
        svg = _svg(self._name).replace(b"currentColor", self._color(mode).name().encode())
        QSvgRenderer(QByteArray(svg)).render(painter, rect)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        self._render(painter, rect, mode)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float
    ) -> QPixmap:
        w, h = round(size.width() * scale), round(size.height() * scale)
        key = (w, h, self._color(mode).name())
        pix = self._pixmaps.get(key)
        if pix is None:
            pix = QPixmap(w, h)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            self._render(painter, QRect(QPoint(0, 0), QSize(w, h)), mode)
            painter.end()
            self._pixmaps[key] = pix
        result = QPixmap(pix)
        result.setDevicePixelRatio(scale)
        return result

    def _color(self, mode: QIcon.Mode) -> QColor:
        if self._fixed is not None and mode is not QIcon.Mode.Disabled:
            return QColor(self._fixed)
        return _color(mode)

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        return size

    def clone(self) -> QIconEngine:
        return _TintedSvgEngine(self._name, self._fixed)

    def key(self) -> str:
        return "pdfeditor-tinted-svg"

    def iconName(self) -> str:
        return self._name


def icon(name: str, color: str | None = None) -> QIcon:
    """Bundled icon ``data/icons/<name>.svg``; raises if the file is missing.

    It is drawn in the palette's text color unless ``color`` fixes one (for icons on a colored
    fill, such as an accent button; set it again when the theme changes).
    """
    _svg(name)  # fail early, not at first paint
    return QIcon(_TintedSvgEngine(name, color))


def page_icon(pixmap: QPixmap) -> QIcon:
    """A page image as an item-view icon that looks the same when selected.

    Views draw selected items' icons in Selected mode, which Qt generates by tinting the pixmap
    with the highlight color. That suits glyphs, not page previews: it washed thumbnails out.
    """
    ic = QIcon(pixmap)
    ic.addPixmap(pixmap, QIcon.Mode.Selected)
    return ic
