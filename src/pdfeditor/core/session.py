"""DocumentSession: one open document plus its lock, undo history and save policy."""

from __future__ import annotations

import itertools
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pdfeditor.core.commands.base import Change, Command, UndoStack
from pdfeditor.core.commands.snapshot import SnapshotStore
from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.base import Document, Engine, PasswordCallback, SaveError, SaveOptions
from pdfeditor.engine.registry import DEFAULT_ENGINE, get_engine

log = logging.getLogger(__name__)
_ids = itertools.count(1)


class EventKind(Enum):
    CHANGED = "changed"  # document content changed (see ``changes``)
    SAVED = "saved"
    DIRTY = "dirty"  # dirty flag or undo/redo availability changed
    CLOSED = "closed"
    BLOCKED = "blocked"  # an edit, undo or redo was refused while a task works on the document


@dataclass(frozen=True, slots=True)
class SessionEvent:
    kind: EventKind
    changes: tuple[Change, ...] = field(default_factory=tuple)


Listener = Callable[[SessionEvent], None]


class DocumentSession:
    """Owns a :class:`Document` and the lock that serializes all engine access to it.

    MuPDF isn't thread-safe: code touching ``document`` (render workers, background jobs, and the
    GUI thread) must hold ``lock``, which is the process-wide ``ENGINE_LOCK``. Every mutation
    goes through :meth:`execute` so it's undoable and listeners (views, panels) learn what
    changed.
    """

    def __init__(self, document: Document, engine: Engine) -> None:
        self.id = next(_ids)
        self.uid = uuid.uuid4().hex  # stable name for recovery files
        self.document = document
        self.engine = engine
        # One lock for the whole process, not per document: see core/engine_lock.py.
        self.lock = ENGINE_LOCK
        self.closed = False
        self.undo_stack = UndoStack()
        self.undo_stack.on_change(lambda: self._emit(SessionEvent(EventKind.DIRTY)))
        self.save_path_hint: Path | None = None  # e.g. original path of a recovered document
        self.name_hint = ""  # shown for unsaved new documents, e.g. "Combined.pdf"
        # Set after redaction/sanitizing: an incremental save would keep the removed content
        # in earlier revisions of the file, so the next save must be a full rewrite.
        self.require_full_save = False
        self._snapshots: SnapshotStore | None = None
        self._listeners: list[Listener] = []
        self._tasks: list[str] = []  # background tasks reading the document (see begin_task)

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

    # -- properties -----------------------------------------------------------------------
    @property
    def path(self) -> Path | None:
        return self.document.path

    @property
    def display_name(self) -> str:
        path = self.path or self.save_path_hint
        if path is not None:
            return path.name
        return self.name_hint or f"Untitled-{self.id}"

    @property
    def page_count(self) -> int:
        return self.document.page_count

    @property
    def is_dirty(self) -> bool:
        return not self.undo_stack.is_clean

    @property
    def snapshots(self) -> SnapshotStore:
        """Temp storage for this session's undo snapshots (created on first use)."""
        if self._snapshots is None:
            self._snapshots = SnapshotStore()
        return self._snapshots

    # -- events ---------------------------------------------------------------------------
    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener; returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: SessionEvent) -> None:
        for listener in list(self._listeners):
            listener(event)

    # -- background tasks -----------------------------------------------------------------
    @property
    def busy(self) -> str:
        """Label of the background task working on this document, or "" when there is none.

        A task (OCR, export, optimize...) reads the document between steps while the user
        keeps working elsewhere, and some apply their result to it when done. Edits made in
        between would change the pages under the task's feet, so the document is read-only
        until every task has ended: :meth:`execute`, :meth:`undo` and :meth:`redo` refuse with
        a ``BLOCKED`` event and :meth:`save` raises.
        """
        return self._tasks[-1] if self._tasks else ""

    def begin_task(self, label: str) -> None:
        self._tasks.append(label)
        self._emit(SessionEvent(EventKind.DIRTY))  # listeners refresh undo/redo availability

    def end_task(self, label: str) -> None:
        if label in self._tasks:
            self._tasks.remove(label)
        self._emit(SessionEvent(EventKind.DIRTY))

    def _refuse_while_busy(self) -> bool:
        if not self._tasks:
            return False
        log.info("refused an edit of %s while %r runs", self.display_name, self.busy)
        self._emit(SessionEvent(EventKind.BLOCKED))
        return True

    # -- editing --------------------------------------------------------------------------
    def execute(self, command: Command) -> None:
        if self._refuse_while_busy():
            return
        with self.lock:
            self.undo_stack.push(command, self.document)
        self._emit(SessionEvent(EventKind.CHANGED, tuple(command.changes())))

    def undo(self) -> bool:
        if self._refuse_while_busy():
            return False
        with self.lock:
            command = self.undo_stack.undo(self.document)
        if command is None:
            return False
        self._emit(SessionEvent(EventKind.CHANGED, tuple(command.changes())))
        return True

    def redo(self) -> bool:
        if self._refuse_while_busy():
            return False
        with self.lock:
            command = self.undo_stack.redo(self.document)
        if command is None:
            return False
        self._emit(SessionEvent(EventKind.CHANGED, tuple(command.changes())))
        return True

    # -- saving ---------------------------------------------------------------------------
    def save_target(self) -> Path | None:
        return self.path or self.save_path_hint

    def save(self, path: Path | None = None) -> Path:
        """Save to ``path`` (save-as) or to the current file.

        Signed documents saved in place use an incremental update, so existing signatures stay
        valid. Everything else gets a full, garbage-collected rewrite.
        """
        target = path or self.save_target()
        if target is None:
            raise SaveError("document has no file name; use Save As")
        if self._tasks:
            raise SaveError(f"wait until “{self.busy}” has finished, or cancel it, then save")
        target = target.resolve()
        with self.lock:
            doc = self.document
            in_place = doc.path is not None and target == doc.path
            incremental = (
                in_place
                and not self.require_full_save
                and doc.info().has_signatures
                and doc.can_save_incrementally()
            )
            options = SaveOptions(
                incremental=incremental, garbage=4 if self.require_full_save else 3
            )
            saved = doc.save(target, options)
        self.require_full_save = False
        self.save_path_hint = None
        self.undo_stack.set_clean()
        log.info("saved %s%s", saved, " (incremental)" if incremental else "")
        self._emit(SessionEvent(EventKind.SAVED))
        return saved

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.undo_stack.clear()
            self.document.close()
            self.closed = True
            if self._snapshots is not None:
                self._snapshots.cleanup()
        self._emit(SessionEvent(EventKind.CLOSED))
        self._listeners.clear()
