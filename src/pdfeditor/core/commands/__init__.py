"""Undoable edits: every document mutation goes through a Command on the session's stack."""

from pdfeditor.core.commands.annotation_cmds import (
    AddAnnotationCommand,
    DeleteAnnotationsCommand,
    ReorderAnnotationCommand,
    UpdateAnnotationCommand,
)
from pdfeditor.core.commands.base import Change, ChangeKind, Command, MacroCommand, UndoStack
from pdfeditor.core.commands.document_cmds import (
    SetMetadataCommand,
    SetOutlineCommand,
    SetPageLabelsCommand,
)
from pdfeditor.core.commands.snapshot import SnapshotCommand, SnapshotStore

__all__ = [
    "AddAnnotationCommand",
    "Change",
    "ChangeKind",
    "Command",
    "DeleteAnnotationsCommand",
    "MacroCommand",
    "ReorderAnnotationCommand",
    "SetMetadataCommand",
    "SetOutlineCommand",
    "SetPageLabelsCommand",
    "SnapshotCommand",
    "SnapshotStore",
    "UndoStack",
    "UpdateAnnotationCommand",
]
