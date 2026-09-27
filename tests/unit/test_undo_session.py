from __future__ import annotations

import os
import shutil
from pathlib import Path

import pikepdf
import pytest

from pdfeditor.core.autosave import Autosaver, RecoveryStore, _pid_alive
from pdfeditor.core.commands import (
    Change,
    ChangeKind,
    Command,
    SetMetadataCommand,
    SetOutlineCommand,
    SnapshotCommand,
    UndoStack,
)
from pdfeditor.core.session import DocumentSession, EventKind, SessionEvent
from pdfeditor.engine.base import Document, SaveError
from pdfeditor.model.outline import Destination, OutlineItem


class Counter(Command):
    """Test command: adds ``n`` to a shared list-backed counter."""

    def __init__(self, box: list[int], n: int = 1, mergeable: bool = False) -> None:
        self.box, self.n, self.mergeable = box, n, mergeable
        self.discarded = False
        self.label = f"add {n}"

    def do(self, doc: Document) -> None:
        self.box[0] += self.n

    def undo(self, doc: Document) -> None:
        self.box[0] -= self.n

    def merge_with(self, other: Command) -> bool:
        if self.mergeable and isinstance(other, Counter) and other.mergeable:
            self.n += other.n
            return True
        return False

    def discard(self) -> None:
        self.discarded = True


DOC: Document = None  # type: ignore[assignment]  # Counter ignores the document


def test_undo_redo_and_labels() -> None:
    box = [0]
    stack = UndoStack()
    stack.push(Counter(box, 1), DOC)
    stack.push(Counter(box, 10), DOC)
    assert box == [11] and stack.undo_label == "add 10"
    stack.undo(DOC)
    assert box == [1] and stack.redo_label == "add 10"
    stack.redo(DOC)
    assert box == [11]
    stack.undo(DOC)
    stack.undo(DOC)
    assert box == [0] and not stack.can_undo and stack.undo(DOC) is None


def test_new_command_discards_redo_branch() -> None:
    box = [0]
    stack = UndoStack()
    stack.push(Counter(box, 1), DOC)
    dropped = Counter(box, 2)
    stack.push(dropped, DOC)
    stack.undo(DOC)
    stack.push(Counter(box, 5), DOC)
    assert dropped.discarded and not stack.can_redo and box == [6]


def test_clean_tracking() -> None:
    box = [0]
    stack = UndoStack()
    assert stack.is_clean
    stack.push(Counter(box), DOC)
    assert not stack.is_clean
    stack.set_clean()
    stack.push(Counter(box), DOC)
    stack.undo(DOC)
    assert stack.is_clean  # back at the saved state
    stack.undo(DOC)
    assert not stack.is_clean
    stack.redo(DOC)
    assert stack.is_clean
    # saved state lost in a discarded redo branch
    stack.push(Counter(box), DOC)
    stack.undo(DOC)
    stack.undo(DOC)
    stack.push(Counter(box, 7), DOC)
    stack.undo(DOC)
    assert not stack.is_clean


def test_merge_and_clean() -> None:
    box = [0]
    stack = UndoStack()
    first = stack.push(Counter(box, 1, mergeable=True), DOC)
    assert stack.push(Counter(box, 2, mergeable=True), DOC) is first
    assert len(stack) == 1 and first.n == 3 and box == [3]
    stack.set_clean()
    stack.push(Counter(box, 1, mergeable=True), DOC)  # merges into the saved command
    assert not stack.is_clean
    stack.undo(DOC)
    assert box == [0] and not stack.is_clean


def test_limit_and_version() -> None:
    box = [0]
    stack = UndoStack(limit=3)
    commands = [Counter(box) for _ in range(5)]
    for c in commands:
        stack.push(c, DOC)
    assert len(stack) == 3 and commands[0].discarded and commands[1].discarded
    v = stack.version
    stack.undo(DOC)
    stack.redo(DOC)
    assert stack.version == v + 2
    stack.set_clean()
    assert stack.version == v + 2  # saving isn't a content change


@pytest.fixture
def session(fixture_pdf, tmp_path: Path):
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("outline"), path)
    s = DocumentSession.open(path)
    yield s
    s.close()


def test_session_events_and_dirty(session: DocumentSession) -> None:
    events: list[SessionEvent] = []
    unsubscribe = session.subscribe(events.append)
    meta = session.document.metadata()
    meta.title = "Edited"
    session.execute(SetMetadataCommand(meta))
    assert session.is_dirty
    changed = [e for e in events if e.kind is EventKind.CHANGED]
    assert changed and changed[0].changes == (Change(ChangeKind.METADATA),)
    assert session.undo() and session.document.metadata().title == "outline"
    assert not session.is_dirty
    assert session.redo() and session.document.metadata().title == "Edited"
    unsubscribe()
    session.undo()
    assert len([e for e in events if e.kind is EventKind.CHANGED]) == 3


def test_metadata_commands_merge(session: DocumentSession) -> None:
    for title in ("A", "B", "C"):
        meta = session.document.metadata()
        meta.title = title
        session.execute(SetMetadataCommand(meta))
    assert len(session.undo_stack) == 1
    session.undo()
    assert session.document.metadata().title == "outline"


def test_outline_command(session: DocumentSession) -> None:
    before = [i.title for i in session.document.outline()]
    session.execute(SetOutlineCommand([OutlineItem("Only", dest=Destination(0))]))
    assert [i.title for i in session.document.outline()] == ["Only"]
    session.undo()
    assert [i.title for i in session.document.outline()] == before


def _delete_last_page(doc: Document) -> None:
    fz = doc.fz  # test-only: a destructive edit with no engine API yet
    fz.delete_page(doc.page_count - 1)
    doc.structure_changed()  # type: ignore[attr-defined]


def test_snapshot_command_undo_redo(session: DocumentSession) -> None:
    cmd = SnapshotCommand("Delete page", _delete_last_page, session.snapshots)
    session.execute(cmd)
    assert session.page_count == 5 and cmd.disk_bytes > 0
    session.undo()
    assert session.page_count == 6
    assert session.document.outline()[0].title == "Chapter 1"
    session.redo()
    assert session.page_count == 5
    snapshots_dir = session.snapshots.dir
    assert len(list(snapshots_dir.glob("*.pdf"))) == 2
    session.close()
    assert not snapshots_dir.exists()


def test_snapshot_command_rolls_back_on_failure(session: DocumentSession) -> None:
    def broken(doc: Document) -> None:
        _delete_last_page(doc)
        raise RuntimeError("halfway")

    with pytest.raises(RuntimeError):
        session.execute(SnapshotCommand("Broken", broken, session.snapshots))
    assert session.page_count == 6 and not session.undo_stack.can_undo


def test_save_after_snapshot_undo(session: DocumentSession) -> None:
    session.execute(SnapshotCommand("Delete page", _delete_last_page, session.snapshots))
    session.undo()  # document now lives in memory (restored from a snapshot)
    assert session.is_dirty is False
    session.execute(SnapshotCommand("Delete page", _delete_last_page, session.snapshots))
    path = session.save()
    assert not session.is_dirty
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 5


def test_save_as_and_errors(session: DocumentSession, tmp_path: Path, fixture_pdf) -> None:
    out = session.save(tmp_path / "copy.pdf")
    assert session.path == out
    new = DocumentSession(session.engine.new_document(), session.engine)
    with pytest.raises(SaveError):
        new.save()
    new.close()


def test_signed_documents_save_incrementally(fixture_pdf, tmp_path: Path) -> None:
    import pymupdf  # build a document whose catalog says it's signed

    src = pymupdf.open(fixture_pdf("images"))
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Sig1"
    widget.rect = pymupdf.Rect(50, 50, 200, 100)
    src[0].add_widget(widget)
    path = tmp_path / "signed.pdf"
    src.save(path)
    src.close()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm.SigFlags = 3
        pdf.save(path)

    before = path.read_bytes()
    session = DocumentSession.open(path)
    assert session.document.info().has_signatures
    meta = session.document.metadata()
    meta.subject = "incremental"
    session.execute(SetMetadataCommand(meta))
    session.save()
    session.close()
    after = path.read_bytes()
    assert after.startswith(before) and len(after) > len(before)


def test_recovery_store_roundtrip(session: DocumentSession, tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "recovery")
    autosaver = Autosaver(store)
    assert autosaver.tick([session]) == 0  # clean: nothing to do
    meta = session.document.metadata()
    meta.title = "Unsaved"
    session.execute(SetMetadataCommand(meta))
    assert autosaver.tick([session]) == 1
    assert autosaver.tick([session]) == 0  # unchanged since last copy
    # our own live copy isn't offered for recovery; it is once the owner is gone
    assert store.entries() == []
    (entry,) = store.entries(include_running=True)
    assert entry.original_path == session.path and entry.display_name == "doc.pdf"
    recovered = DocumentSession.open(entry.pdf)
    assert recovered.document.metadata().title == "Unsaved"
    recovered.close()
    session.save()
    autosaver.tick([session])
    assert store.entries(include_running=True) == []


def test_recovery_entries_from_dead_process(tmp_path: Path, fixture_pdf) -> None:
    store = RecoveryStore(tmp_path)
    (tmp_path / "abc.pdf").write_bytes(fixture_pdf("images").read_bytes())
    (tmp_path / "abc.json").write_text(
        '{"original_path": null, "display_name": "x.pdf", "saved_at": 1, "pid": 999999999}'
    )
    (tmp_path / "orphan.json").write_text('{"pid": 1}')  # pdf missing -> cleaned up
    (entry,) = store.entries()
    assert entry.uid == "abc" and entry.original_path is None
    assert not (tmp_path / "orphan.json").exists()
    assert _pid_alive(os.getpid()) and not _pid_alive(999999999)


def test_required_full_save_overrides_incremental(fixture_pdf, tmp_path: Path) -> None:
    """After redaction a signed file must be rewritten, not appended to."""
    import pymupdf

    src = pymupdf.open(fixture_pdf("images"))
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Sig1"
    widget.rect = pymupdf.Rect(50, 50, 200, 100)
    src[0].add_widget(widget)
    path = tmp_path / "signed.pdf"
    src.save(path)
    src.close()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm.SigFlags = 3
        pdf.save(path)
    before = path.read_bytes()
    session = DocumentSession.open(path)
    meta = session.document.metadata()
    meta.subject = "sanitized"
    session.execute(SetMetadataCommand(meta))
    session.require_full_save = True
    session.save()
    assert not path.read_bytes().startswith(before)  # rewritten, not an appended revision
    assert session.require_full_save is False
    session.close()
