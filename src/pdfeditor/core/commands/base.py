"""Undoable commands and the undo stack (Qt-free)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from pdfeditor.engine.base import Document


class ChangeKind(Enum):
    CONTENT = "content"  # page pixels changed on ``pages``
    STRUCTURE = "structure"  # pages added/removed/reordered, or the whole document reloaded
    METADATA = "metadata"
    OUTLINE = "outline"
    ANNOTATIONS = "annotations"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    pages: frozenset[int] = field(default_factory=frozenset)


class Command(ABC):
    """One user-visible edit. ``do`` runs first; ``undo``/``redo`` may then alternate.

    Commands receive the Document and must only touch it through the engine interface. The
    session holds the document lock while they run.
    """

    label = "Edit"

    @abstractmethod
    def do(self, doc: Document) -> None: ...

    @abstractmethod
    def undo(self, doc: Document) -> None: ...

    def redo(self, doc: Document) -> None:
        self.do(doc)

    def merge_with(self, other: Command) -> bool:
        """Absorb ``other`` (which was just done after this one). Return True if merged."""
        return False

    def changes(self) -> list[Change]:
        """What views must refresh after do/undo/redo."""
        return [Change(ChangeKind.STRUCTURE)]

    @property
    def disk_bytes(self) -> int:
        """Temp-disk space held for undo (snapshots); counts toward the stack's budget."""
        return 0

    def discard(self) -> None:  # noqa: B027 - optional hook, deliberately a no-op
        """Release resources when the command leaves the stack for good."""


class UndoStack:
    """Linear undo/redo history with a clean marker for dirty tracking."""

    def __init__(self, limit: int = 200, max_disk_bytes: int = 2 * 1024**3) -> None:
        self.limit = limit
        self.max_disk_bytes = max_disk_bytes
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._clean: int | None = 0  # len(_undo) at the last save; None = unreachable
        self._listeners: list[Callable[[], None]] = []
        self._version = 0

    # -- state ----------------------------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    @property
    def is_clean(self) -> bool:
        return self._clean == len(self._undo)

    def __len__(self) -> int:
        return len(self._undo)

    @property
    def version(self) -> int:
        """Increments on every do/undo/redo: tells whether the document changed since a check."""
        return self._version

    def set_clean(self) -> None:
        self._clean = len(self._undo)
        self._notify()

    def mark_dirty(self) -> None:
        """Make the current state count as unsaved (e.g. a recovered document)."""
        self._clean = None
        self._notify()

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _notify(self, changed: bool = False) -> None:
        if changed:
            self._version += 1
        for fn in list(self._listeners):
            fn()

    # -- operations -----------------------------------------------------------------------
    def push(self, command: Command, doc: Document) -> Command:
        """Do ``command`` and record it. Returns the command now on top (merged or new)."""
        command.do(doc)
        for dropped in self._redo:
            dropped.discard()
        if self._clean is not None and self._clean > len(self._undo):
            self._clean = None  # the saved state was in the redo branch we just dropped
        self._redo.clear()
        top = self._undo[-1] if self._undo else None
        if top is not None and top.merge_with(command):
            command.discard()
            if self._clean == len(self._undo):
                self._clean = None  # the saved state was merged away
            self._notify(changed=True)
            return top
        self._undo.append(command)
        self._trim()
        self._notify(changed=True)
        return command

    def undo(self, doc: Document) -> Command | None:
        if not self._undo:
            return None
        command = self._undo.pop()
        command.undo(doc)
        self._redo.append(command)
        self._notify(changed=True)
        return command

    def redo(self, doc: Document) -> Command | None:
        if not self._redo:
            return None
        command = self._redo.pop()
        command.redo(doc)
        self._undo.append(command)
        self._notify(changed=True)
        return command

    def clear(self) -> None:
        for command in self._undo + self._redo:
            command.discard()
        self._undo.clear()
        self._redo.clear()
        self._clean = 0
        self._notify()

    def _trim(self) -> None:
        def disk() -> int:
            return sum(c.disk_bytes for c in self._undo)

        while len(self._undo) > 1 and (
            len(self._undo) > self.limit or disk() > self.max_disk_bytes
        ):
            self._undo.pop(0).discard()
            if self._clean is not None:
                self._clean = self._clean - 1 if self._clean > 0 else None
