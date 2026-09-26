"""DocumentSession: one open document plus the state shared by everything viewing it.

Phase 4 extends this with undo/redo, dirty tracking and autosave.
"""

from __future__ import annotations

import itertools
import logging
import threading
from pathlib import Path

from pdfeditor.engine.base import Document, Engine, PasswordCallback
from pdfeditor.engine.registry import DEFAULT_ENGINE, get_engine

log = logging.getLogger(__name__)
_ids = itertools.count(1)


class DocumentSession:
    """Owns a :class:`Document` and the lock that serializes all engine access to it.

    MuPDF documents aren't thread-safe: any code touching ``document`` off the GUI thread (render
    workers, background jobs) must hold ``lock``. The GUI thread takes it too, so a long job
    can't race an edit.
    """

    def __init__(self, document: Document, engine: Engine) -> None:
        self.id = next(_ids)
        self.document = document
        self.engine = engine
        self.lock = threading.RLock()
        self.closed = False

    @classmethod
    def open(
        cls,
        source: Path | bytes,
        password: str | PasswordCallback | None = None,
        engine_name: str = DEFAULT_ENGINE,
    ) -> DocumentSession:
        engine = get_engine(engine_name)
        doc = engine.open(source, password)
        log.info("opened %s (%d pages)", doc.path or "<memory>", doc.page_count)
        return cls(doc, engine)

    @property
    def path(self) -> Path | None:
        return self.document.path

    @property
    def display_name(self) -> str:
        if self.path is not None:
            return self.path.name
        return f"Untitled-{self.id}"

    @property
    def page_count(self) -> int:
        return self.document.page_count

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.document.close()
                self.closed = True
