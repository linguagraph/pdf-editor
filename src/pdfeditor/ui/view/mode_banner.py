"""The "you're in a mode" indicator of the current document view.

While a tool other than Select/Hand is active, a slim row above the pages holds an accent chip
("Editing text & images · Esc or Done to finish" with a Done button). The row is a viewport
margin of the view, so it never covers page content (or the pill at the bottom): the pages
move down by its height instead. Modes that change what a click on existing content does
(content editing, redaction marking) also get a thin frame around the page area.

The frame edges are children of the viewport rather than siblings: a sibling overlapping the
viewport stops Qt from scrolling it by blitting (every scroll step would repaint the whole page
area). They are blitted along with the pages and put back in place right after, as the pill is.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    QByteArray,
    QCoreApplication,
    QRect,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget

from pdfeditor.ui.i18n import QT_TRANSLATE_NOOP
from pdfeditor.ui.icons import svg_data
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.view.document_view import DocumentView

FRAME = 2  # px width of the frame around the page area
ICON = 16
ROW_PADDING = 5  # px above and below the chip in its row
DEFAULT_HINT = QT_TRANSLATE_NOOP("ModeBanner", "Esc or Done to finish")


@dataclass(frozen=True)
class Mode:
    """What the banner says about the active tool."""

    text: str  # "Editing text & images" (shown translated, context "ModeBanner")
    icon: str  # bundled icon name
    framed: bool = False  # also draw the frame around the page area
    danger: bool = False  # destructive mode (redaction): danger color instead of accent
    hint: str = DEFAULT_HINT


class _Edge(QWidget):
    """One side of the frame: a solid strip that lets the mouse through."""

    def __init__(self, banner: ModeBanner) -> None:
        super().__init__(banner)
        self._banner = banner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._banner.fill_color())
        painter.end()


class _Chip(QFrame):
    """The rounded chip: icon, mode, hint, Done."""

    def __init__(self, banner: ModeBanner) -> None:
        super().__init__(banner)
        self._banner = banner
        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(ICON, ICON)
        self.text_label = QLabel(self)
        self.text_label.setObjectName("modeText")
        self.hint_label = QLabel(self)
        self.hint_label.setObjectName("modeHint")
        self.done_button = QToolButton(self)
        self.done_button.setText(QCoreApplication.translate("ModeBanner", "Done"))
        self.done_button.setToolTip(
            QCoreApplication.translate("ModeBanner", "Back to the Select tool (Esc)")
        )
        self.done_button.setCursor(Qt.CursorShape.ArrowCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(METRICS.space(3), 4, 5, 4)
        layout.setSpacing(METRICS.space(2))
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.done_button)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = body.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._banner.fill_color())
        painter.drawRoundedRect(body, radius, radius)
        painter.end()


class ModeBanner(QWidget):
    """One per window; it moves into whichever view is current (see :meth:`attach`)."""

    done = Signal()  # the Done button: back to the Select tool

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("modeBanner")
        self.setAccessibleName(QCoreApplication.translate("ModeBanner", "Active tool"))
        self.view: DocumentView | None = None
        self.mode: Mode | None = None
        self._home = parent  # where the banner waits while no document is open
        self.chip = _Chip(self)
        self.done_button = self.chip.done_button
        self.done_button.clicked.connect(self.done)
        self.text_label = self.chip.text_label
        self.hint_label = self.chip.hint_label
        self.edges = [_Edge(self) for _ in range(4)]
        theme_manager().changed.connect(self._restyle)
        self.hide()

    # -- binding --------------------------------------------------------------------------
    def attach(self, view: DocumentView | None) -> None:
        """Move into ``view`` (the current document), or out of sight when there's none."""
        if view is self.view:
            self._place()
            return
        old = self.view
        if old is not None:
            try:
                old.geometry_changed.disconnect(self._place)
                old.verticalScrollBar().valueChanged.disconnect(self._on_scrolled)
                old.horizontalScrollBar().valueChanged.disconnect(self._on_scrolled)
            except (RuntimeError, TypeError):  # the view is already being destroyed
                pass
        self.view = view
        self.setParent(view if view is not None else self._home)
        for edge in self.edges:
            edge.setParent(view.viewport() if view is not None else self)
        if view is not None:
            view.geometry_changed.connect(self._place)
            # after QAbstractScrollArea's own handler, so this runs after the blit
            view.verticalScrollBar().valueChanged.connect(self._on_scrolled)
            view.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        self._place()

    def set_mode(self, mode: Mode | None) -> None:
        """Show ``mode`` for the current view (None: the default tool, nothing to show)."""
        if mode != self.mode:
            self.mode = mode
            if mode is not None:
                text = QCoreApplication.translate("ModeBanner", mode.text)
                hint = QCoreApplication.translate("ModeBanner", mode.hint) if mode.hint else ""
                self.text_label.setText(text)
                self.hint_label.setText(f"·  {hint}" if hint else "")
                self.setAccessibleName(
                    QCoreApplication.translate("ModeBanner", "Mode: {mode}").format(mode=text)
                )
                self.setAccessibleDescription(hint)
                self.chip.setAccessibleName(text)
                self.done_button.setAccessibleName(
                    QCoreApplication.translate("ModeBanner", "Done: {mode}").format(mode=text)
                )
                self._restyle()
        if self.view is not None:
            self.reserve(self.view, mode is not None)
        self._place()

    def row_height(self) -> int:
        """Height of the row above the pages: the chip and some space around it."""
        self.hint_label.setVisible(True)
        return self.chip.sizeHint().height() + 2 * ROW_PADDING

    def reserve(self, view: DocumentView, on: bool) -> None:
        """Give ``view`` (or take away) the margin above its pages that holds the banner.

        Each view keeps the margin of its own mode, so switching documents doesn't make the
        pages jump."""
        view.set_top_margin(self.row_height() if on else 0)

    # -- placement ------------------------------------------------------------------------
    def _on_scrolled(self, _value: int) -> None:
        self._place_edges()  # the viewport scroll moved the frame along with the pages

    def _place(self) -> None:
        view, mode = self.view, self.mode
        if view is None or mode is None:
            self.hide()
            self._place_edges()
            return
        port = view.viewport().geometry()
        row = QRect(port.left(), 0, port.width(), port.top())
        if self.geometry() != row:
            self.setGeometry(row)
        self.hint_label.setVisible(True)
        size = self.chip.sizeHint()
        if size.width() > row.width() - 2 * METRICS.space(2):
            self.hint_label.setVisible(False)  # narrow window: keep the mode and Done
            size = self.chip.sizeHint()
        width = min(size.width(), row.width())
        self.chip.setGeometry(
            (row.width() - width) // 2, (row.height() - size.height()) // 2, width, size.height()
        )
        if self.isHidden():
            self.show()
        self.raise_()
        self._place_edges()

    def _place_edges(self) -> None:
        view, mode = self.view, self.mode
        if view is None or mode is None or not mode.framed:
            for edge in self.edges:
                edge.hide()
            return
        area = view.viewport().rect()
        w, h = area.width(), area.height()
        rects = (
            QRect(0, 0, w, FRAME),
            QRect(0, h - FRAME, w, FRAME),
            QRect(0, FRAME, FRAME, h - 2 * FRAME),
            QRect(w - FRAME, FRAME, FRAME, h - 2 * FRAME),
        )
        for edge, rect in zip(self.edges, rects, strict=True):
            if edge.geometry() != rect:
                edge.setGeometry(rect)
            if edge.isHidden():
                edge.show()
            edge.raise_()

    # -- look -----------------------------------------------------------------------------
    def fill_color(self) -> QColor:
        colors = current_colors()
        danger = self.mode is not None and self.mode.danger
        return QColor(colors.danger if danger else colors.accent)

    def _restyle(self) -> None:
        colors = current_colors()
        # Text on the chip follows the fill through the palette as well as the style sheet,
        # so it stays readable with High Contrast (no style sheet then).
        palette = QPalette(self.chip.palette())
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
            palette.setColor(role, QColor(colors.on_accent))
        self.chip.setPalette(palette)
        if self.mode is not None:
            self.chip.icon_label.setPixmap(
                _tinted(self.mode.icon, colors.on_accent, self.devicePixelRatioF())
            )
        self.update()
        self.chip.update()
        for edge in self.edges:
            edge.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(current_colors().canvas))
        painter.end()

    # Clicks on the row's own background stay here instead of reaching the page.
    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()


def _tinted(name: str, color: str, dpr: float) -> QPixmap:
    pix = QPixmap(round(ICON * dpr), round(ICON * dpr))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    svg = svg_data(name).replace(b"currentColor", color.encode())
    QSvgRenderer(QByteArray(svg)).render(painter)
    painter.end()
    pix.setDevicePixelRatio(dpr)
    return pix
