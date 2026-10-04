"""Inline text editor shown over a text block (or a new text box) while editing content,
with a style bar (font, size, bold, italic, colour, alignment) that applies to the block."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QKeyEvent,
    QPalette,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QToolButton,
    QWidget,
)

from pdfeditor.model.color import Color
from pdfeditor.model.fonts import FontRef, FontRefKind
from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import Align, TextStyle, family_of
from pdfeditor.services.fonts import cached_catalog, installed_ref_for_font_name
from pdfeditor.ui.color_picker import pick_color
from pdfeditor.ui.font_picker import STANDARD_FONTS, FontPicker, remember_font
from pdfeditor.ui.icons import icon

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView

# Screen fonts that look like the PDF standard families (the PDF result uses the real font).
_SCREEN_FAMILIES = {"serif": "Times New Roman", "mono": "Courier New", "sans": "Arial"}
_STANDARD_NAMES = {name for _label, name in STANDARD_FONTS}
_loaded_fonts: dict[str, str] = {}  # sha1 of font program -> Qt family name


def _private_copy(font_data: bytes, family: str) -> bytes:
    """``font_data`` renamed to ``family``.

    An embedded program keeps its family name ("Arial"). Registered under that name it would
    replace the installed font of the same name everywhere in the app, often with only the
    few glyphs the PDF used, so other text would show with the wrong metrics or no glyphs.
    """
    import io

    from fontTools.ttLib import TTFont

    try:
        font = TTFont(io.BytesIO(font_data))
        names = font["name"]
        for record in list(names.names):
            if record.nameID in (1, 3, 4, 6, 16, 17, 21, 22):
                names.removeNames(nameID=record.nameID)
        names.setName(family, 1, 3, 1, 0x409)
        names.setName("Regular", 2, 3, 1, 0x409)
        names.setName(family, 3, 3, 1, 0x409)
        names.setName(family, 4, 3, 1, 0x409)
        names.setName(family.replace(" ", ""), 6, 3, 1, 0x409)
        out = io.BytesIO()
        font.save(out)
        font.close()
        return out.getvalue()
    except Exception:
        return b""  # a program fontTools can't rewrite is not registered at all


def preview_family(font_data: bytes | None) -> str | None:
    """Register an embedded font program with Qt, under a private family name, so the
    editor shows the page's real font without hiding the installed font of that name."""
    if not font_data:
        return None
    digest = hashlib.sha1(font_data).hexdigest()
    if digest not in _loaded_fonts:
        family = f"PDF Font {digest[:12]}"
        data = _private_copy(font_data, family)
        font_id = QFontDatabase.addApplicationFontFromData(data) if data else -1
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        _loaded_fonts[digest] = families[0] if families else ""
    return _loaded_fonts[digest] or None


def _initial_font_ref(style: TextStyle) -> tuple[FontRef, str]:
    """What the font picker should preselect for ``style``.

    Prefers an explicit ``font_ref``. Otherwise, a standard name goes straight to its
    ``FontRef.standard``; anything else tries to recognize an installed family from the name
    (stripping a subset prefix/style suffix MuPDF may have written on a previous save), falling
    back to a plain document-font entry.
    """
    if style.font_ref is not None:
        return style.font_ref, style.font
    if style.font in _STANDARD_NAMES:
        return FontRef.standard(style.font), style.font
    installed = installed_ref_for_font_name(cached_catalog(), style.font, style.bold, style.italic)
    if installed is not None:
        return installed, installed.name
    return FontRef.document(style.font), style.font


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
        document_fonts = [] if style.font in _STANDARD_NAMES else [style.font]
        self.font_picker = FontPicker(self, document_fonts=document_fonts)
        self.font_picker.setToolTip("Font")
        self.font_picker.set_selection(*_initial_font_ref(style))
        self.font_picker.font_chosen.connect(self._on_font_chosen)
        self.size_box = QDoubleSpinBox(self)
        self.size_box.setToolTip("Font size (pt)")
        self.size_box.setRange(4, 144)
        self.size_box.setDecimals(1)
        self.size_box.setSingleStep(0.5)
        self.size_box.setValue(style.size)
        self.bold = self._toggle("bold", "Bold", style.bold)
        self.italic = self._toggle("italic", "Italic", style.italic)
        self.bold.toggled.connect(self._switch_variant)
        self.italic.toggled.connect(self._switch_variant)
        self.color_button = QToolButton(self)
        self.color_button.setToolTip("Text colour")
        self.color_button.clicked.connect(self._pick_color)
        self._paint_color()
        self.align_combo = QComboBox(self)
        self.align_combo.setToolTip("Alignment")
        for a in Align:
            self.align_combo.addItem(a.value.capitalize(), a)
        self.align_combo.setCurrentIndex(list(Align).index(style.align))
        self.line_spacing = QDoubleSpinBox(self)
        self.line_spacing.setToolTip("Line spacing (multiple of the font size)")
        self.line_spacing.setAccessibleName("Line spacing")
        self.line_spacing.setRange(0.8, 3.0)
        self.line_spacing.setDecimals(2)
        self.line_spacing.setSingleStep(0.1)
        self.line_spacing.setValue(style.line_height)
        self.line_spacing.setPrefix("↕ ")
        self._base = style
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)
        for w in (self.font_picker, self.size_box, self.bold, self.italic, self.color_button,
                  self.align_combo, self.line_spacing):  # fmt: skip
            row.addWidget(w)
        self.size_box.valueChanged.connect(lambda _v: on_change())
        self.align_combo.currentIndexChanged.connect(lambda _i: on_change())
        self.line_spacing.valueChanged.connect(lambda _v: on_change())
        self.picking = False
        self._refresh_bold_italic_enabled()
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
            chosen = pick_color(self._color, self, "Text Color")
        finally:
            self.picking = False
        if chosen is not None:
            self.set_color(chosen)

    def _family_faces(self) -> list[object]:
        ref = self.font_picker.current_ref()
        if ref.kind is not FontRefKind.FILE:
            return []
        return list(cached_catalog().faces_of(ref.name))

    def _refresh_bold_italic_enabled(self) -> None:
        faces = self._family_faces()
        if not faces:
            self.bold.setEnabled(True)
            self.italic.setEnabled(True)
            return
        self.bold.setEnabled(any(f.weight >= 600 for f in faces))  # type: ignore[attr-defined]
        self.italic.setEnabled(any(f.italic for f in faces))  # type: ignore[attr-defined]

    def _switch_variant(self, _checked: bool) -> None:
        ref = self.font_picker.current_ref()
        if ref.kind is FontRefKind.FILE:
            face = cached_catalog().find(ref.name, self.bold.isChecked(), self.italic.isChecked())
            if face is not None:
                self.font_picker.set_variant(FontRef.file(face.path, face.index, ref.name))
        self._on_change()

    def _on_font_chosen(self, ref: FontRef, _display: str) -> None:
        if ref.kind is FontRefKind.FILE:
            face = cached_catalog().find(ref.name, self.bold.isChecked(), self.italic.isChecked())
            if face is not None:
                self.font_picker.set_variant(FontRef.file(face.path, face.index, ref.name))
        self._refresh_bold_italic_enabled()
        self._on_change()

    def text_style(self) -> TextStyle:
        align = self.align_combo.currentData()
        ref = self.font_picker.current_ref()
        font_ref = ref if ref.kind in (FontRefKind.FILE, FontRefKind.STANDARD) else None
        return replace(
            self._base,
            font=ref.name,
            font_ref=font_ref,
            size=round(self.size_box.value(), 1),
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
            color=self._color,
            align=align if isinstance(align, Align) else self._base.align,
            line_height=round(self.line_spacing.value(), 2),
        )


class InlineTextEditor(QPlainTextEdit):
    """Commits on Ctrl+Enter or when focus leaves the editor and its style bar; Escape
    cancels. ``on_commit(text, style)`` runs only if something changed (see ``changed``).

    The mouse wheel over a box whose text doesn't fit scrolls that text and never the page,
    even at the first or last line: a flick that ran on into the page would carry the box away
    from under the pointer mid-edit. A box with nothing to scroll lets the wheel scroll the
    page. Ctrl+wheel always zooms the page (not the editor's font), and the box follows the
    zoom when it was placed with :meth:`follow`."""

    _anchor: tuple[DocumentView, int, Rect] | None = None

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
        family: str | None
        if style.font_ref is not None and style.font_ref.kind is FontRefKind.FILE:
            family = style.font_ref.name  # a system family Qt can resolve by name
        else:
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
        # never shorter than one line of its font: the text would be cut off (and a box
        # with a page step of 0 can't be scrolled with the wheel)
        first = self.document().firstBlock()
        line = max(self.fontMetrics().lineSpacing(), self.blockBoundingRect(first).height())
        one_line = (
            round(line) + 2 * self.frameWidth() + 2 * round(self.document().documentMargin()) + 2
        )
        self.setGeometry(x - 4, y - 4, max(w + 8, 120), max(h + 8, 40, one_line))
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
            if style.font_ref is not None:
                remember_font(style.font_ref)
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

    # -- wheel and zoom ---------------------------------------------------------------------
    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta()
        horizontal = abs(delta.x()) > abs(delta.y())
        bar = self.horizontalScrollBar() if horizontal else self.verticalScrollBar()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._pass_to_page(event)  # zoom the page, not the editor's font
        elif bar.maximum() <= bar.minimum():
            self._pass_to_page(event)  # nothing to scroll here
        elif bar.pageStep() < 1:
            # A box shorter than one line has a page step of 0, and Qt limits a wheel step to
            # the page step, so it would never scroll: step through the lines here instead.
            notches = (delta.x() if horizontal else delta.y()) / 120
            lines = round(-notches * QApplication.wheelScrollLines()) or (-1 if notches > 0 else 1)
            bar.setValue(bar.value() + lines * bar.singleStep())
            event.accept()
        else:
            super().wheelEvent(event)
            event.accept()  # at the first or last line too: see the class docstring

    def _pass_to_page(self, event: QWheelEvent) -> None:
        """Hand the wheel to the page area under the editor.

        Sent explicitly rather than by ignoring the event: Qt propagates only spontaneous wheel
        events to the parent, so this way it behaves the same for every source."""
        page_area = self.parentWidget()
        if page_area is None:
            event.ignore()
            return
        forwarded = QWheelEvent(
            QPointF(self.viewport().mapTo(page_area, event.position())),
            event.globalPosition(),
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
            event.source(),
            event.pointingDevice(),
        )
        QApplication.sendEvent(page_area, forwarded)
        event.accept()

    def follow(self, view: DocumentView, page: int, rect: Rect) -> None:
        """Place the box over ``rect`` of ``page`` and keep it there when the view zooms."""
        self._anchor = (view, page, rect)
        self.place(viewport_rect(view, page, rect))
        view.zoom_changed.connect(self._on_zoom_changed)

    def _on_zoom_changed(self, _zoom: float) -> None:
        # after the view has scrolled to keep its zoom anchor in place
        QTimer.singleShot(0, self._replace)

    def _replace(self) -> None:
        if self._done or self._anchor is None:
            return
        view, page, rect = self._anchor
        self._zoom = view.transform().m11()
        self._restyle()
        self.place(viewport_rect(view, page, rect))


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
