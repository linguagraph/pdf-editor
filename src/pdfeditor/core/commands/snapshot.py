"""Undo for destructive edits: snapshot the whole document to a temp file before changing it."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from pdfeditor.core.commands.base import Change, ChangeKind, Command
from pdfeditor.engine.base import Document, SaveOptions

# Fast snapshots: no garbage collection or recompression; streams are copied as they are.
SNAPSHOT_OPTIONS = SaveOptions(garbage=0, deflate=False)


class SnapshotStore:
    """Temp directory holding one session's undo snapshots; removed on :meth:`cleanup`."""

    def __init__(self, root: Path | None = None) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="pdfeditor-undo-", dir=root))

    def write(self, data: bytes) -> Path:
        path = self.dir / f"{uuid.uuid4().hex}.pdf"
        path.write_bytes(data)
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class SnapshotCommand(Command):
    """Wraps an operation that has no exact inverse (content edits, redaction, OCR, optimize).

    ``do`` saves a "before" snapshot then runs ``operation``. ``undo`` saves an "after" snapshot
    (once) and restores "before"; ``redo`` restores "after" instead of re-running the operation,
    so redo is exact even for non-deterministic operations.
    """

    def __init__(
        self,
        label: str,
        operation: Callable[[Document], None],
        store: SnapshotStore,
        changes: list[Change] | None = None,
    ) -> None:
        self.label = label
        self._operation = operation
        self._store = store
        self._changes = changes or [Change(ChangeKind.STRUCTURE)]
        self._before: Path | None = None
        self._after: Path | None = None

    def do(self, doc: Document) -> None:
        self._before = self._store.write(doc.to_bytes(SNAPSHOT_OPTIONS))
        try:
            self._operation(doc)
        except Exception:
            # Leave the document exactly as it was if the operation fails halfway.
            doc.load_state(self._before.read_bytes())
            self.discard()
            raise

    def undo(self, doc: Document) -> None:
        assert self._before is not None, "undo before do"
        if self._after is None:
            self._after = self._store.write(doc.to_bytes(SNAPSHOT_OPTIONS))
        doc.load_state(self._before.read_bytes())

    def redo(self, doc: Document) -> None:
        assert self._after is not None, "redo before undo"
        doc.load_state(self._after.read_bytes())

    def changes(self) -> list[Change]:
        # A restore reloads the whole document, so views must rebuild whatever the edit was.
        return [Change(ChangeKind.STRUCTURE), *self._changes]

    @property
    def disk_bytes(self) -> int:
        total = 0
        for path in (self._before, self._after):
            if path is not None and path.exists():
                total += path.stat().st_size
        return total

    def discard(self) -> None:
        for path in (self._before, self._after):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()
        self._before = self._after = None
