"""Inline text editor shown over a text block (or a new text box) while editing content."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPalette
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import TextStyle, family_of

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView

# Screen fonts that look like the PDF standard families (the PDF result uses the real font).
_SCREEN_FAMILIES = {"serif": "Times New Roman", "mono": "Courier New", "sans": "Arial"}


def screen_font(style: TextStyle, zoom_px_per_pt: float) -> QFont:
    font = QFont(_SCREEN_FAMILIES[family_of(style.font)])
    font.setPixelSize(max(4, round(style.size * zoom_px_per_pt)))
    font.setBold(style.bold)
    font.setItalic(style.italic)
    return font


class InlineTextEditor(QPlainTextEdit):
    """Commits on Ctrl+Enter or when focus leaves; Escape cancels."""

    def __init__(
        self,
        parent: QWidget,
        text: str,
        style: TextStyle,
        zoom_px_per_pt: float,
        on_commit: Callable[[str], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self._done = False
        self.setPlainText(text)
        self.setFont(screen_font(style, zoom_px_per_pt))
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor.fromRgbF(*style.color.rgb()))
        palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 240))
        self.setPalette(palette)
        self.setFrameShape(QPlainTextEdit.Shape.Box)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setPlaceholderText("Type text…")
        self.installEventFilter(self)
        self.show()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.selectAll()

    def place(self, viewport_rect: tuple[int, int, int, int]) -> None:
        x, y, w, h = viewport_rect
        self.setGeometry(x - 4, y - 4, max(w + 8, 120), max(h + 8, 40))

    def commit(self) -> None:
        if self._done:
            return
        self._done = True
        text = self.toPlainText()
        self.hide()
        self.deleteLater()
        self._on_commit(text)

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self.hide()
        self.deleteLater()
        self._on_cancel()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self and event.type() == QEvent.Type.FocusOut:
            self.commit()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.commit()
            return
        super().keyPressEvent(event)


def viewport_rect(view: DocumentView, page: int, rect: Rect) -> tuple[int, int, int, int]:
    scene = view.page_rect_to_scene(page, rect)
    top_left = view.mapFromScene(scene.topLeft())
    bottom_right = view.mapFromScene(scene.bottomRight())
    return (
        top_left.x(),
        top_left.y(),
        bottom_right.x() - top_left.x(),
        bottom_right.y() - top_left.y(),
    )
