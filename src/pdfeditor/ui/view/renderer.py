"""Background tile rendering: a worker pool that fills the shared render cache."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtGui import QImage

from pdfeditor.core.render_cache import RenderCache, TileKey
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import ColorMode, RenderRequest, RenderResult
from pdfeditor.model.geometry import Matrix, Rect

log = logging.getLogger(__name__)

PRIORITY_VISIBLE = 10
PRIORITY_THUMBNAIL = 0


def to_qimage(result: RenderResult, invert: bool = False) -> QImage:
    fmt = {
        ColorMode.RGB: QImage.Format.Format_RGB888,
        ColorMode.RGBA: QImage.Format.Format_RGBA8888,
        ColorMode.GRAY: QImage.Format.Format_Grayscale8,
    }[result.color]
    # QImage doesn't own the buffer: copy() detaches it from the Python bytes.
    image = QImage(result.samples, result.width, result.height, result.stride, fmt).copy()
    if invert:
        image.invertPixels()
    return image


class TileRenderer(QObject):
    """Renders :class:`TileKey` requests off the GUI thread.

    Workers hold the session lock while touching the engine. ``tile_ready`` is emitted from the
    worker thread; Qt queues it to receivers on the GUI thread.
    """

    tile_ready = Signal(object)  # TileKey

    def __init__(self, cache: RenderCache[QImage], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cache = cache
        self._pool = QThreadPool(self)
        # The session lock serializes engine access anyway; one worker keeps the queue ordered.
        self._pool.setMaxThreadCount(1)
        self._pending: set[TileKey] = set()
        self._mutex = threading.Lock()
        self._sessions: dict[int, DocumentSession] = {}

    def register(self, session: DocumentSession) -> None:
        self._sessions[session.id] = session

    def unregister(self, session: DocumentSession) -> None:
        self.cancel(lambda k: k.doc_id == session.id)
        self._sessions.pop(session.id, None)
        self.cache.drop_document(session.id)

    def request(
        self, key: TileKey, matrix: Matrix, clip: Rect | None, priority: int = PRIORITY_VISIBLE
    ) -> None:
        with self._mutex:
            if key in self._pending:
                return
            self._pending.add(key)
        self._pool.start(lambda: self._run(key, matrix, clip), priority)

    def is_pending(self, key: TileKey) -> bool:
        with self._mutex:
            return key in self._pending

    def cancel(self, predicate: Callable[[TileKey], bool]) -> None:
        """Forget queued requests matching ``predicate``; workers skip them when dequeued."""
        with self._mutex:
            self._pending = {k for k in self._pending if not predicate(k)}

    def wait_idle(self, msecs: int = 10_000) -> bool:
        return bool(self._pool.waitForDone(msecs))

    def _run(self, key: TileKey, matrix: Matrix, clip: Rect | None) -> None:
        with self._mutex:
            if key not in self._pending:
                return
        session = self._sessions.get(key.doc_id)
        try:
            if session is None:
                return
            with session.lock:
                if session.closed or key.page_index >= session.page_count:
                    return
                page = session.document.page(key.page_index)
                if page.revision != key.revision:
                    return  # page changed since the request; the view will ask again
                result = page.render(RenderRequest(matrix=matrix, clip=clip, color=ColorMode.RGB))
            image = to_qimage(result, invert=key.variant == "night")
            self.cache.put(key, image, image.sizeInBytes())
        except Exception:
            log.exception("render failed for %s", key)
            return
        finally:
            with self._mutex:
                self._pending.discard(key)
        self.tile_ready.emit(key)
