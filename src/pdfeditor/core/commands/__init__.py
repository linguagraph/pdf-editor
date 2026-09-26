"""Undoable edits: every document mutation goes through a Command on the session's stack."""

from pdfeditor.core.commands.base import Change, ChangeKind, Command, UndoStack
from pdfeditor.core.commands.document_cmds import SetMetadataCommand, SetOutlineCommand
from pdfeditor.core.commands.snapshot import SnapshotCommand, SnapshotStore

__all__ = [
    "Change",
    "ChangeKind",
    "Command",
    "SetMetadataCommand",
    "SetOutlineCommand",
    "SnapshotCommand",
    "SnapshotStore",
    "UndoStack",
]
