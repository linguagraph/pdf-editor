"""Inline text editor shown over a text block (or a new text box) while editing content,
with a style bar (font, size, bold, italic, colour, alignment) that applies to the block."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeyEvent, QPalette, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QToolButton,
    QWidget,
)

from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import Align, TextStyle, family_of
from pdfeditor.ui.icons import icon

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView

# Screen fonts that look like the PDF standard families (the PDF result uses the real font).
_SCREEN_FAMILIES = {"serif": "Times New Roman", "mono": "Courier New", "sans": "Arial"}
# Standard families offered in the style bar: (label, PDF font name)
STANDARD_FONTS = (
    ("Sans (Helvetica)", "Helvetica"),
    ("Serif (Times)", "Times-Roman"),
    ("Mono (Courier)", "Courier"),
)
_loaded_fonts: dict[str, str] = {}  # sha1 of font program -> Qt family name


def preview_family(font_data: bytes | None) -> str | None:
    """Register an embedded font program with Qt so the editor shows the page's real font."""
    if not font_data:
        return None
    digest = hashlib.sha1(font_data).hexdigest()
    if digest not in _loaded_fonts:
        font_id = QFontDatabase.addApplicationFontFromData(font_data)
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        _loaded_fonts[digest] = families[0] if families else ""
    return _loaded_fonts[digest] or None


def screen_font(style: TextStyle, zoom_px_per_pt: float, family: str | None = None) -> QFont:
    font = QFont(family or _SCREEN_FAMILIES[family_of(style.font)])
    font.setPixelSize(max(4, round(style.size * zoom_px_per_pt)))
    font.setBold(style.bold)
    font.setItalic(style.italic)
    return font


class TextStyleBar(QFrame):
    """Font / size / bold / italic / colour / alignment for the text being edited."""

    def __init__(self, parent: QWidget, style: TextStyle, on_change: Callable[[], None]) -> None:
        super().__init__(parent)
        self.setObjectName("textStyleBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self._on_change = on_change
        self._color = style.color
        self.font_combo = QComboBox(self)
        self.font_combo.setToolTip("Font")
        standard = {name for _label, name in STANDARD_FONTS}
        if style.font not in standard:
            self.font_combo.addItem(f"{style.font} (document font)", style.font)
        for label, name in STANDARD_FONTS:
            self.font_combo.addItem(label, name)
        self.font_combo.setCurrentIndex(max(0, self.font_combo.findData(style.font)))
        self.size_box = QDoubleSpinBox(self)
        self.size_box.setToolTip("Font size (pt)")
        self.size_box.setRange(4, 144)
        self.size_box.setDecimals(1)
        self.size_box.setSingleStep(0.5)
        self.size_box.setValue(style.size)
        self.bold = self._toggle("bold", "Bold", style.bold)
        self.italic = self._toggle("italic", "Italic", style.italic)
        self.color_button = QToolButton(self)
        self.color_button.setToolTip("Text colour")
        self.color_button.clicked.connect(self._pick_color)
        self._paint_color()
        self.align_combo = QComboBox(self)
        self.align_combo.setToolTip("Alignment")
        for a in Align:
            self.align_combo.addItem(a.value.capitalize(), a)
        self.align_combo.setCurrentIndex(list(Align).index(style.align))
        self._base = style
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)
        for w in (self.font_combo, self.size_box, self.bold, self.italic, self.color_button,
                  self.align_combo):  # fmt: skip
            row.addWidget(w)
        self.font_combo.currentIndexChanged.connect(lambda _i: on_change())
        self.size_box.valueChanged.connect(lambda _v: on_change())
        self.align_combo.currentIndexChanged.connect(lambda _i: on_change())
        self.picking = False
        self.adjustSize()

    def _toggle(self, icon_name: str, tip: str, checked: bool) -> QToolButton:
        b = QToolButton(self)
        b.setIcon(icon(icon_name))
        b.setToolTip(tip)
        b.setCheckable(True)
        b.setChecked(checked)
        b.toggled.connect(lambda _on: self._on_change())
        return b

    def _paint_color(self) -> None:
        self.color_button.setText("A")
        self.color_button.setStyleSheet(
            f"QToolButton {{ color: {self._color.to_hex()}; font-weight: bold; "
            f"border-bottom: 4px solid {self._color.to_hex()}; }}"
        )

    def set_color(self, color: Color) -> None:
        self._color = color
        self._paint_color()
        self._on_change()

    def _pick_color(self) -> None:
        self.picking = True
        try:
            chosen = QColorDialog.getColor(
                QColor.fromRgbF(*self._color.rgb()), self.window(), "Text Colour"
            )
        finally:
            self.picking = False
        if chosen.isValid():
            self.set_color(Color(chosen.redF(), chosen.greenF(), chosen.blueF()))

    def text_style(self) -> TextStyle:
        align = self.align_combo.currentData()
        return replace(
            self._base,
            font=str(self.font_combo.currentData()),
            size=round(self.size_box.value(), 1),
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
            color=self._color,
            align=align if isinstance(align, Align) else self._base.align,
        )


class InlineTextEditor(QPlainTextEdit):
    """Commits on Ctrl+Enter or when focus leaves the editor and its style bar; Escape
    cancels. ``on_commit(text, style)`` runs only if something changed (see ``changed``)."""

    def __init__(
        self,
        parent: QWidget,
        text: str,
        style: TextStyle,
        zoom_px_per_pt: float,
        on_commit: Callable[[str, TextStyle], None],
        on_cancel: Callable[[], None],
        font_data: bytes | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self._done = False
        self._zoom = zoom_px_per_pt
        self._original = (text, style)
        self._doc_family = preview_family(font_data)
        self._doc_font = style.font
        self.setPlainText(text)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 240))
        self.setPalette(palette)
        self.setFrameShape(QPlainTextEdit.Shape.Box)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setPlaceholderText("Type text…")
        self.style_bar = TextStyleBar(parent, style, self._restyle)
        self._restyle()
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        app.focusChanged.connect(self._focus_changed)
        self.show()
        self.style_bar.show()
        self.style_bar.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.selectAll()

    # -- style ------------------------------------------------------------------------------
    def text_style(self) -> TextStyle:
        return self.style_bar.text_style()

    def _restyle(self) -> None:
        style = self.text_style()
        family = self._doc_family if style.font == self._doc_font else None
        self.setFont(screen_font(style, self._zoom, family))
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor.fromRgbF(*style.color.rgb()))
        self.setPalette(palette)
        option = self.document().defaultTextOption()
        option.setAlignment(
            {
                Align.LEFT: Qt.AlignmentFlag.AlignLeft,
                Align.CENTER: Qt.AlignmentFlag.AlignHCenter,
                Align.RIGHT: Qt.AlignmentFlag.AlignRight,
                Align.JUSTIFY: Qt.AlignmentFlag.AlignJustify,
            }[style.align]
        )
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)

    @property
    def changed(self) -> bool:
        return (self.toPlainText(), self.text_style()) != self._original

    # -- geometry ---------------------------------------------------------------------------
    def place(self, viewport_rect: tuple[int, int, int, int]) -> None:
        x, y, w, h = viewport_rect
        self.setGeometry(x - 4, y - 4, max(w + 8, 120), max(h + 8, 40))
        bar = self.style_bar
        bar.adjustSize()
        above = y - 6 - bar.height()
        by = above if above >= 0 else y - 4 + self.height() + 2
        parent = self.parentWidget()
        limit = parent.width() - bar.width() if parent is not None else x
        bx = max(0, min(x - 4, limit))
        bar.move(bx, by)

    # -- finishing --------------------------------------------------------------------------
    def _close(self) -> None:
        self._done = True
        app = QApplication.instance()
        if isinstance(app, QApplication):
            with contextlib.suppress(RuntimeError, TypeError):
                app.focusChanged.disconnect(self._focus_changed)
        self.hide()
        self.style_bar.hide()
        self.style_bar.deleteLater()
        self.deleteLater()

    def commit(self) -> None:
        if self._done:
            return
        text, style, changed = self.toPlainText(), self.text_style(), self.changed
        self._close()
        if changed:
            self._on_commit(text, style)
        else:
            self._on_cancel()

    def cancel(self) -> None:
        if self._done:
            return
        self._close()
        self._on_cancel()

    def _focus_left(self) -> None:
        """Commit once focus is really gone: not in the editor, its style bar, a combo popup
        of the bar, or the colour dialog."""
        if self._done or self.style_bar.picking:
            return
        w = QApplication.focusWidget()
        popup = QApplication.activePopupWidget()
        for widget in (w, popup):
            while widget is not None:
                if widget is self or widget is self.style_bar:
                    return
                widget = widget.parentWidget()
        self.commit()

    def _focus_changed(self, _old: QWidget | None, _new: QWidget | None) -> None:
        # decide after Qt settles focus (popups and dialogs take it in steps)
        QTimer.singleShot(0, self._focus_left)

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
