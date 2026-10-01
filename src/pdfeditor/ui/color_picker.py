"""One way to pick a color, the same everywhere: Qt's color dialog in the app's theme.

A dialog inherits its parent's palette and style sheet. Opening it from a swatch button
(painted with its own background) or from the inline text editor (pale page-like palette)
made the whole dialog take on that color, OK button included. So the dialog is parented to
the top-level window and given the application palette, and swatches are painted as icons
rather than with per-widget style sheets.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QColorDialog, QDialog, QPushButton, QSizePolicy, QWidget

from pdfeditor.model.color import Color
from pdfeditor.ui.theme import current_colors

DEFAULT_TITLE = "Select Color"
SWATCH = QSize(28, 14)


def to_qcolor(color: Color | QColor | str) -> QColor:
    if isinstance(color, Color):
        return QColor.fromRgbF(*color.rgb())
    return QColor(color)


def color_dialog(
    initial: Color | QColor | str, parent: QWidget | None = None, title: str = DEFAULT_TITLE
) -> QColorDialog:
    """A themed color dialog owned by ``parent``'s window (shown by :func:`pick_color`)."""
    window = parent.window() if parent is not None else QApplication.activeWindow()
    dialog = QColorDialog(to_qcolor(initial), window)
    dialog.setWindowTitle(title)
    # Qt's own dialog on every platform, so it follows the app theme and looks the same.
    dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
    dialog.setPalette(QApplication.palette())
    dialog.setStyleSheet("")
    dialog.setAccessibleName(title)
    return dialog


def pick_color(
    initial: Color | QColor | str, parent: QWidget | None = None, title: str = DEFAULT_TITLE
) -> Color | None:
    """Ask for a color; None if the user cancelled."""
    dialog = color_dialog(initial, parent, title)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        chosen = dialog.selectedColor()
    finally:
        dialog.deleteLater()
    if not chosen.isValid():
        return None
    return Color(chosen.redF(), chosen.greenF(), chosen.blueF())


def swatch_icon(color: Color | None, size: QSize = SWATCH) -> QIcon:
    """A filled rectangle with a hairline border (visible on any background)."""
    ratio = 2.0  # crisp on high-DPI screens
    pixmap = QPixmap(size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    if color is not None:
        painter = QPainter(pixmap)
        painter.setPen(QColor(current_colors().border_strong))
        painter.setBrush(to_qcolor(color))
        painter.drawRect(0, 0, size.width() - 1, size.height() - 1)
        painter.end()
    return QIcon(pixmap)


class ColorButton(QPushButton):
    """A button showing a color swatch; clicking it picks a new color."""

    def __init__(
        self, color: Color, parent: QWidget | None = None, title: str = DEFAULT_TITLE
    ) -> None:
        super().__init__(parent)
        self.color = color
        self.title = title
        self.setIconSize(SWATCH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.clicked.connect(self._pick)
        self._paint()

    def _paint(self) -> None:
        self.setIcon(swatch_icon(self.color))
        self.setToolTip(self.color.to_hex())

    def _pick(self) -> None:
        chosen = pick_color(self.color, self, self.title)
        if chosen is not None:
            self.set(chosen)

    def set(self, color: Color) -> None:
        self.color = color
        self._paint()
