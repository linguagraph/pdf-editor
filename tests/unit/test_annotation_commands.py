from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from pdfeditor.core.commands import (
    AddAnnotationCommand,
    Change,
    ChangeKind,
    DeleteAnnotationsCommand,
    ReorderAnnotationCommand,
    UpdateAnnotationCommand,
)
from pdfeditor.core.session import DocumentSession
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Rect


@pytest.fixture
def session(fixture_pdf, tmp_path: Path):
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    s = DocumentSession.open(path)
    yield s
    s.close()


DEFAULT_RECT = Rect(100, 100, 200, 200)
RED = Color(1, 0, 0)


def square(rect: Rect = DEFAULT_RECT, color: Color = RED) -> AnnotationModel:
    return AnnotationModel(AnnotationType.SQUARE, 0, rect, color=color)


def names(session: DocumentSession, page: int = 0) -> list[str]:
    return [a.name for a in session.document.page(page).annotations()]


def test_add_undo_redo_keeps_name(session: DocumentSession) -> None:
    cmd = AddAnnotationCommand(square())
    session.execute(cmd)
    assert cmd.changes() == [Change(ChangeKind.ANNOTATIONS, frozenset({0}))]
    name = cmd.model.name
    assert names(session) == [name]
    session.undo()
    assert names(session) == []
    session.redo()
    assert names(session) == [name]  # same identity after re-creation


def test_update_merges_and_survives_recreation(session: DocumentSession) -> None:
    add = AddAnnotationCommand(square())
    session.execute(add)
    current = session.document.page(0).annotations()[0]
    moved = copy.deepcopy(current)
    moved.rect = Rect(300, 300, 400, 400)
    session.execute(UpdateAnnotationCommand(current, moved, "Move", merge_key="move"))
    moved2 = copy.deepcopy(moved)
    moved2.rect = Rect(310, 310, 410, 410)
    session.execute(UpdateAnnotationCommand(moved, moved2, "Move", merge_key="move"))
    assert len(session.undo_stack) == 2  # add + one merged move
    # undo everything, redo everything: the update finds the re-created annotation by name
    session.undo()
    session.undo()
    session.redo()
    session.redo()
    rect = session.document.page(0).annotations()[0].rect
    assert rect.intersects(Rect(310, 310, 410, 410)) and not rect.intersects(
        Rect(100, 100, 200, 200)
    )


def test_delete_with_replies_and_order(session: DocumentSession) -> None:
    for i, color in enumerate((Color(1, 0, 0), Color(0, 0, 1), Color(0, 1, 0))):
        session.execute(
            AddAnnotationCommand(square(Rect(100 + 20 * i, 100, 200 + 20 * i, 200), color))
        )
    parent_name = names(session)[1]
    reply = AnnotationModel(AnnotationType.TEXT, 0, Rect(120, 100, 140, 120), contents="reply")
    reply.extra = {"irt_name": parent_name}
    session.execute(AddAnnotationCommand(reply))
    parent_id = next(a.id for a in session.document.page(0).annotations() if a.name == parent_name)
    stored_reply = next(a for a in session.document.page(0).annotations() if a.contents == "reply")
    assert stored_reply.in_reply_to == parent_id
    order_before = names(session)

    session.execute(DeleteAnnotationsCommand(0, [parent_name]))
    assert parent_name not in names(session) and len(names(session)) == 2  # reply went too
    session.undo()
    assert names(session) == order_before  # same z-order, same names
    anns = {a.name: a for a in session.document.page(0).annotations()}
    new_parent_id = anns[parent_name].id
    assert anns[stored_reply.name].in_reply_to == new_parent_id
    session.redo()
    assert len(names(session)) == 2


def test_reorder(session: DocumentSession) -> None:
    session.execute(AddAnnotationCommand(square()))
    session.execute(AddAnnotationCommand(square(Rect(150, 150, 250, 250))))
    first, second = names(session)
    session.execute(ReorderAnnotationCommand(0, first, to_front=True))
    assert names(session) == [second, first]
    session.undo()
    assert names(session) == [first, second]
    session.execute(ReorderAnnotationCommand(0, second, to_front=False))
    assert names(session) == [second, first]


def test_file_attachment_delete_undo_restores_file(session: DocumentSession) -> None:
    att = AnnotationModel(
        AnnotationType.FILE_ATTACHMENT,
        0,
        Rect(50, 50, 70, 70),
        file_name="x.txt",
        file_data=b"payload",
    )
    session.execute(AddAnnotationCommand(att))
    name = names(session)[0]
    session.execute(DeleteAnnotationsCommand(0, [name]))
    session.undo()
    restored = session.document.page(0).annotations()[0]
    assert restored.file_data == b"payload" and restored.file_name == "x.txt"


def _png() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGBA", (60, 30), (200, 0, 0, 255)).save(out, "PNG")
    return out.getvalue()


def test_delete_undo_recreates_image_stamp_and_callout(session: DocumentSession) -> None:
    from pdfeditor.model.annotations import callout_line
    from pdfeditor.model.geometry import Point

    box = Rect(300, 300, 450, 340)
    stamp = AnnotationModel(AnnotationType.STAMP, 0, Rect(100, 400, 220, 460), image=_png())
    callout = AnnotationModel(
        AnnotationType.FREE_TEXT,
        0,
        box,
        contents="Callout",
        text_color=RED,
        vertices=callout_line(Point(120, 200), box),
        line_endings=("OpenArrow", "None"),
    )
    session.execute(AddAnnotationCommand(stamp))
    session.execute(AddAnnotationCommand(callout))
    before = {a.name: a for a in session.document.page(0).annotations()}
    session.execute(DeleteAnnotationsCommand(0, list(before)))
    assert session.document.page(0).annotations() == []
    session.undo()
    after = {a.name: a for a in session.document.page(0).annotations()}
    assert set(after) == set(before)
    restored_stamp = next(a for a in after.values() if a.type is AnnotationType.STAMP)
    assert restored_stamp.image is not None
    assert restored_stamp.rect.x0 == pytest.approx(100, abs=1)
    restored_callout = next(a for a in after.values() if a.type is AnnotationType.FREE_TEXT)
    assert restored_callout.is_callout
    assert restored_callout.vertices[0].x == pytest.approx(120, abs=1)
    assert restored_callout.rect.x0 == pytest.approx(300, abs=1)
    # moving the callout is one undoable step that restores the old geometry
    moved = copy.deepcopy(restored_callout)
    moved.rect = moved.rect.translated(0, 100)
    moved.vertices = tuple(p + Point(0, 100) for p in moved.vertices)
    session.execute(UpdateAnnotationCommand(restored_callout, moved))
    session.undo()
    again = next(
        a for a in session.document.page(0).annotations() if a.name == restored_callout.name
    )
    assert again.vertices[0].y == pytest.approx(200, abs=1)
