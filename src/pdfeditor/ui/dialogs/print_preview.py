"""Live print preview for the Print dialog: the sheet as it will come out of the printer.

Sheets are painted by ``printing.paint_sheet``, the routine that prints, on a worker thread
(never the GUI thread: a big or scanned page can take a while under the engine lock). The pane
only shows the latest request's result and keeps a small cache so paging back is instant.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import replace
from functools import partial

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
    QPageLayout,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.session import DocumentSession
from pdfeditor.ui import printing
from pdfeditor.ui.dialogs.base import set_role
from pdfeditor.ui.icons import icon
from pdfeditor.ui.printing import PrintOptions
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.view.document_view import DocumentView

log = logging.getLogger(__name__)

MIN_DPI = 36
MAX_DPI = 150
DPI_STEP = 12  # preview resolutions snap to steps so a slow resize doesn't re-render each pixel
RESIZE_DEBOUNCE_MS = 150
CACHE_SIZE = 12
MARGIN = 16  # px of canvas around the paper


class PreviewRenderer(QObject):
    """Renders sheets off the GUI thread; one worker, stale requests are skipped.

    The worker holds the engine lock only inside ``render_sheet_image``. It gets a copy of the
    page layout: a QPrinter must never leave the GUI thread.
    """

    ready = Signal(int, object)  # generation, QImage or an error message (str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False

    def new_generation(self) -> int:
        """Start a new generation; earlier requests that haven't started are dropped."""
        with self._lock:
            self._generation += 1
            return self._generation

    def request(
        self,
        generation: int,
        session: DocumentSession,
        index: int,
        options: PrintOptions,
        layout: QPageLayout,
        dpi: int,
    ) -> None:
        if self._closed:
            return
        job = partial(self._run, generation, session, index, options, QPageLayout(layout), dpi)
        self._pool.start(job)

    def wait_idle(self, msecs: int = 10_000) -> bool:
        return bool(self._pool.waitForDone(msecs))

    def shutdown(self) -> None:
        """Drop queued work and wait for the running render (the dialog is closing)."""
        self._closed = True
        with self._lock:
            self._generation += 1
        self._pool.waitForDone(5000)

    def restart(self) -> None:
        """Accept requests again after a ``shutdown`` that didn't end in closing."""
        self._closed = False

    def _run(
        self,
        generation: int,
        session: DocumentSession,
        index: int,
        options: PrintOptions,
        layout: QPageLayout,
        dpi: int,
    ) -> None:
        with self._lock:
            if generation != self._generation or self._closed:
                return
        payload: QImage | str
        try:
            with session.lock:
                missing = session.closed or index >= session.page_count
            if missing:
                payload = "The page is no longer available"
            else:
                payload = printing.render_sheet_image(session, index, options, layout, dpi)
        except Exception as exc:
            log.exception("print preview failed for page %d", index)
            payload = f"Could not render the preview: {exc}"
        if not self._closed:
            self.ready.emit(generation, payload)


class _Canvas(QWidget):
    """Paints the paper, centred and scaled to fit, over the document canvas color."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image: QImage | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(QSize(200, 200))

    def paper_area(self) -> QSize:
        return QSize(max(1, self.width() - 2 * MARGIN), max(1, self.height() - 2 * MARGIN))

    def paintEvent(self, event: QPaintEvent) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(colors.canvas))
        image = self.image
        if image is None or image.isNull():
            return
        area = self.paper_area()
        size = QSize(image.width(), image.height()).scaled(area, Qt.AspectRatioMode.KeepAspectRatio)
        left = (self.width() - size.width()) / 2
        top = (self.height() - size.height()) / 2
        target = QRectF(QPointF(left, top), size.toSizeF())
        shadow = QColor(colors.shadow)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((6, 14), (4, 22), (2, 34)):  # soft edge, darkest next to the paper
            shadow.setAlpha(alpha)
            painter.setBrush(shadow)
            painter.drawRoundedRect(target.adjusted(-spread, -spread + 2, spread, spread + 2), 3, 3)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, image)
        painter.setPen(QColor(colors.page_outline))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(target.adjusted(-0.5, -0.5, 0.5, 0.5))


class PrintPreviewPane(QWidget):
    """The sheet preview, with previous/next navigation through the pages being printed."""

    def __init__(self, view: DocumentView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.renderer = PreviewRenderer(self)
        self.renderer.ready.connect(self._on_ready)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(self.tr("Print preview"))

        self._session = view.session
        self._pages: tuple[int, ...] = ()
        self._options = PrintOptions(pages=())
        self._layout: QPageLayout | None = None
        self._position = 0
        self._image: QImage | None = None
        self._pending = False
        self._pending_key: tuple[object, ...] = ()
        self._generation = 0
        self._cache: OrderedDict[tuple[object, ...], QImage] = OrderedDict()
        self._wheel = 0

        self.canvas = _Canvas(self)
        self.status_label = QLabel(self.canvas)
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_role(self.status_label, "muted")
        canvas_layout = QVBoxLayout(self.canvas)
        canvas_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        canvas_layout.addWidget(self.status_label)

        self.prev_button = self._nav_button("chevron-left", self.tr("Previous page"), -1)
        self.next_button = self._nav_button("chevron-right", self.tr("Next page"), 1)
        self.position_label = QLabel(self)
        self.position_label.setAccessibleName(self.tr("Preview page"))
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(METRICS.space(2))
        nav.addStretch(1)
        nav.addWidget(self.prev_button)
        nav.addWidget(self.position_label)
        nav.addWidget(self.next_button)
        nav.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(METRICS.space(2))
        outer.addWidget(self.canvas, 1)
        outer.addLayout(nav)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(RESIZE_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._request_current)
        view.document_changed.connect(self._on_document_changed)
        theme_manager().changed.connect(self._refresh_icons)
        self._update_nav()
        self._show_status(self.tr("Rendering…"))

    def _nav_button(self, name: str, tip: str, delta: int) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icon(name))
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setAutoRaise(True)
        button.clicked.connect(partial(self.go, delta))
        return button

    def _refresh_icons(self, *_args: object) -> None:
        self.prev_button.setIcon(icon("chevron-left"))
        self.next_button.setIcon(icon("chevron-right"))
        self.canvas.update()

    # -- public API ------------------------------------------------------------------------
    @property
    def sheet_index(self) -> int:
        """Position within the selected pages (0-based)."""
        return self._position

    @property
    def sheet_count(self) -> int:
        return len(self._pages)

    def current_image(self) -> QImage | None:
        return self._image

    def is_pending(self) -> bool:
        return self._pending or self._debounce.isActive()

    def set_job(
        self,
        session: DocumentSession,
        pages: tuple[int, ...],
        options: PrintOptions,
        layout: QPageLayout,
    ) -> None:
        """Show ``pages`` printed with ``options`` on ``layout``; keeps the current document
        page when it is still in the range, else the nearest sheet."""
        current = self._pages[self._position] if self._pages else None
        self._session = session
        self._pages = tuple(pages)
        self._options = options
        self._layout = QPageLayout(layout)
        if current is not None and current in self._pages:
            self._position = self._pages.index(current)
        else:
            self._position = min(self._position, max(0, len(self._pages) - 1))
        self._debounce.stop()
        self._request_current()

    def show_message(self, text: str, *, error: bool = False) -> None:
        """Replace the preview by a message (an invalid range, nothing to print)."""
        self._pages = ()
        self._position = 0
        self._set_image(None)
        self._generation = self.renderer.new_generation()  # drop any render in flight
        self._pending = False
        self._debounce.stop()
        self._show_status(text, error=error)
        self._update_nav()

    def go(self, delta: int) -> None:
        target = max(0, min(len(self._pages) - 1, self._position + delta))
        if not self._pages or target == self._position:
            return
        self._position = target
        self._request_current()

    # -- rendering -------------------------------------------------------------------------
    def _preview_dpi(self) -> int:
        layout = self._layout
        if layout is None:
            return MIN_DPI
        full = layout.fullRectPoints()
        area = self.canvas.paper_area()
        fit = min(area.width() / max(1.0, full.width()), area.height() / max(1.0, full.height()))
        wanted = fit * self.devicePixelRatioF() * 72
        stepped = -(-wanted // DPI_STEP) * DPI_STEP
        return int(max(MIN_DPI, min(MAX_DPI, stepped)))

    def _request_current(self) -> None:
        layout = self._layout
        self._update_nav()
        if layout is None or not self._pages:
            self.show_message(self.tr("No pages to print"))
            return
        index = self._pages[self._position]
        dpi = self._preview_dpi()
        shared = sheet_options(self._options)
        full, paint = layout.fullRectPoints(), layout.paintRectPoints()
        layout_key = tuple(
            round(v, 1)
            for v in (
                full.width(),
                full.height(),
                paint.left(),
                paint.top(),
                paint.width(),
                paint.height(),
            )
        )
        key = (index, shared, layout_key, dpi)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._generation = self.renderer.new_generation()
            self._pending = False
            self._set_image(cached)
            return
        self._pending = True
        self._pending_key = key
        if self._image is None:
            self._show_status(self.tr("Rendering…"))
        self._generation = self.renderer.new_generation()
        self.renderer.request(self._generation, self._session, index, shared, layout, dpi)

    def _on_ready(self, generation: int, payload: object) -> None:
        if generation != self._generation:
            return  # superseded by a newer request
        self._pending = False
        if isinstance(payload, QImage):
            self._cache[self._pending_key] = payload
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            self._set_image(payload)
        else:
            self._set_image(None)
            self._show_status(str(payload), error=True)

    def _set_image(self, image: QImage | None) -> None:
        self._image = image
        self.canvas.image = image
        if image is not None:
            self.status_label.hide()
        self.canvas.update()

    def _show_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        set_role(self.status_label, "error" if error else "muted")
        self.status_label.show()
        self.status_label.setAccessibleName(text)

    def _on_document_changed(self, _changes: object) -> None:
        self._cache.clear()
        if self._pages and self._layout is not None:
            self._request_current()

    # -- navigation chrome and input -------------------------------------------------------
    def _update_nav(self) -> None:
        count = len(self._pages)
        self.prev_button.setEnabled(count > 0 and self._position > 0)
        self.next_button.setEnabled(count > 0 and self._position < count - 1)
        if not count:
            self.position_label.clear()
            return
        text = self.tr("Page {k} of {n}").format(k=self._position + 1, n=count)
        index = self._pages[self._position]
        label = self.view.page_label(index)
        if label != str(self._position + 1):
            text += self.tr(" (document page {label})").format(label=label)
        self.position_label.setText(text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._pages:
            self._debounce.start()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = {
            Qt.Key.Key_PageUp: -1,
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_PageDown: 1,
            Qt.Key.Key_Right: 1,
        }.get(Qt.Key(event.key()))
        if step is None:
            super().keyPressEvent(event)
            return
        self.go(step)

    def wheelEvent(self, event: QWheelEvent) -> None:
        self._wheel += event.angleDelta().y()
        while abs(self._wheel) >= 120:
            self.go(-1 if self._wheel > 0 else 1)
            self._wheel -= 120 if self._wheel > 0 else -120
        event.accept()


def sheet_options(options: PrintOptions) -> PrintOptions:
    """Options with what doesn't change a single sheet's look (page list, copies) cleared, so
    the cache and the renderer see one key for it."""
    return replace(options, pages=(), copies=1, collate=True)
