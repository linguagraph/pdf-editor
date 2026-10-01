from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMessageBox

from pdfeditor.model.annotations import AnnotationType, ReviewState
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.tools import annotate

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.prefs.keep_tools = True  # these tests draw several comments with one tool
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


def vp(view, page: int, x: float, y: float) -> QPoint:
    """Viewport position of a page-space point."""
    return view.mapFromScene(view.page_point_to_scene(page, Point(x, y)))


def drag(
    qtbot, view, page: int, a: tuple[float, float], b: tuple[float, float], steps: int = 5
) -> None:
    start, end = vp(view, page, *a), vp(view, page, *b)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    for i in range(1, steps + 1):
        p = start + (end - start) * i / steps
        qtbot.mouseMove(view.viewport(), pos=p)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def annots(view, page: int = 0) -> list:
    return [a for a in view.page_annotations(page) if a.type is not AnnotationType.POPUP]


def test_rectangle_tool_and_undo(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (250, 400))
    (sq,) = annots(view)
    assert sq.type is AnnotationType.SQUARE and sq.author == window.prefs.author
    assert sq.rect.inflated(3).contains(Point(100, 300)) and sq.rect.inflated(3).contains(
        Point(250, 400)
    )
    assert view.selected_annotations == [(0, sq.name)]
    assert window.tool_actions["rectangle"].isChecked()  # "keep tools selected" is on here
    window.act_undo.trigger()
    assert annots(view) == []


def test_shape_tools_on_rotated_view(qtbot, window: MainWindow, view) -> None:
    view.rotate_view(90)
    window.set_tool("oval")
    drag(qtbot, view, 0, (100, 300), (250, 400))
    (oval,) = annots(view)
    # page-space geometry is independent of the view rotation
    assert oval.type is AnnotationType.CIRCLE
    assert oval.rect.inflated(3).contains(Point(100, 300)) and oval.rect.inflated(3).contains(
        Point(250, 400)
    )


def test_line_arrow_pen_polygon(qtbot, window: MainWindow, view) -> None:
    window.set_tool("arrow")
    drag(qtbot, view, 0, (100, 500), (300, 520))
    window.set_tool("pen")
    drag(qtbot, view, 0, (320, 500), (420, 560), steps=12)
    window.set_tool("polygon")
    for x, y in ((100, 600), (200, 600), (150, 680)):
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 0, x, y))
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 0, 150, 680))
    kinds = {a.type: a for a in annots(view)}
    assert kinds[AnnotationType.LINE].line_endings == ("None", "OpenArrow")
    assert len(kinds[AnnotationType.INK].ink[0]) >= 2
    assert len(kinds[AnnotationType.POLYGON].vertices) == 3


def test_highlight_by_dragging_and_from_selection(qtbot, window: MainWindow, view) -> None:
    index = view.text_cache.get(0)
    h = index.chars[index.text.index("heading") + 6].bbox  # the final "g"
    window.set_tool("highlight")
    drag(qtbot, view, 0, (index.chars[0].bbox.x0 + 1, h.center.y), (h.x1 - 1, h.center.y))
    (hl,) = annots(view)
    assert hl.type is AnnotationType.HIGHLIGHT and hl.contents == "Page 1 heading"
    # select text first, then click Underline: applied immediately, Select tool stays
    window.set_tool("select")
    view.select_all()
    window.tool_actions["underline"].trigger()
    assert window.current_tool == "select"
    underlines = [
        a
        for p in range(view.page_count)
        for a in annots(view, p)
        if a.type is AnnotationType.UNDERLINE
    ]
    assert len(underlines) == 5  # one per page
    assert window.act_undo.text() == "&Undo Underline"


def test_note_textbox_stamp_attach(
    qtbot, window: MainWindow, view, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(annotate, "ask_text", lambda v, title, initial="": f"{title} text")
    attachment = tmp_path / "data.csv"
    attachment.write_bytes(b"x,y\n")
    monkeypatch.setattr(annotate, "ask_file", lambda v: attachment)
    window.set_tool("note")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 0, 400, 100))
    window.set_tool("textbox")
    drag(qtbot, view, 0, (100, 700), (300, 740))
    window.choose_stamp("Draft")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 0, 400, 500))
    window.set_tool("attach")
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp(view, 0, 500, 100))
    kinds = {a.type: a for a in annots(view)}
    assert kinds[AnnotationType.TEXT].contents == "Sticky Note text"
    assert kinds[AnnotationType.FREE_TEXT].contents == "Text Box text"
    assert kinds[AnnotationType.STAMP].icon.endswith("Draft")
    assert kinds[AnnotationType.FILE_ATTACHMENT].file_data == b"x,y\n"


def test_select_move_resize_delete(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    window.set_tool("select")
    view.set_annotation_selection([])
    # click selects, drag moves by (50, 30)
    drag(qtbot, view, 0, (150, 302), (200, 332))
    (sq,) = annots(view)
    assert sq.rect.center.x == pytest.approx(200, abs=3) and sq.rect.center.y == pytest.approx(
        380, abs=3
    )
    assert window.act_undo.text() == "&Undo Move Comment"
    # drag the bottom-right handle outwards by (40, 20)
    bounds = sq.rect
    drag(qtbot, view, 0, (bounds.x1, bounds.y1), (bounds.x1 + 40, bounds.y1 + 20))
    (sq2,) = annots(view)
    assert sq2.rect.width == pytest.approx(bounds.width + 40, abs=3)
    assert sq2.rect.x0 == pytest.approx(bounds.x0, abs=2)
    # Delete key removes it; undo brings it back
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert annots(view) == []
    window.act_undo.trigger()
    assert len(annots(view)) == 1


def test_locked_comment_does_not_move(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    window.set_tool("select")
    window.inspector.rebuild()
    window.inspector.locked.setChecked(True)
    drag(qtbot, view, 0, (150, 302), (250, 352))
    (sq,) = annots(view)
    assert sq.locked and sq.rect.center.x == pytest.approx(150, abs=3)


def test_copy_paste_between_documents(qtbot, window: MainWindow, view, fixture_pdf) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    window.copy_selection()
    assert window.act_paste.isEnabled()
    other = window.open_path(fixture_pdf("images"))
    window.paste_annotations()
    (pasted,) = annots(other)
    assert pasted.type is AnnotationType.SQUARE and pasted.rect.x0 == pytest.approx(112, abs=3)
    assert pasted.name != annots(view)[0].name


def test_inspector_edits_merge(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    assert not window.inspector.isVisible()  # collapsed to its rail until asked for
    window.show_inspector()
    inspector = window.inspector
    assert inspector.model is not None and inspector.editor.isVisible()
    depth = len(view.session.undo_stack)
    inspector.line_width.setValue(3)
    inspector.line_width.setValue(4)
    assert len(view.session.undo_stack) == depth + 1  # consecutive width edits merge
    inspector.set_color("color", Color(0, 0, 1))
    inspector.opacity.setValue(50)
    (sq,) = annots(view)
    assert sq.border_width == 4 and sq.color == Color(0, 0, 1) and sq.opacity == pytest.approx(0.5)
    inspector.contents.setPlainText("hello")
    inspector._apply("contents", "hello", "Edit")
    assert annots(view)[0].contents == "hello"


def test_comments_panel_threads_filters_and_io(
    qtbot, window: MainWindow, view, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(annotate, "ask_text", lambda v, title, initial="": "a reply")
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    window.set_tool("oval")
    drag(qtbot, view, 0, (300, 300), (400, 400))
    panel = window.comments_panel
    window.show_comments()
    assert panel.visible_count() == 2
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    panel.reply_to_current()
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    panel.set_status(ReviewState.ACCEPTED)
    top = panel.tree.topLevelItem(0)
    assert top.childCount() == 2 and "[Accepted]" in top.text(0)
    panel.status_filter.setCurrentText("Accepted")
    assert panel.visible_count() == 1
    panel.status_filter.setCurrentText("All")
    panel.search.setText("reply")
    assert panel.visible_count() == 1
    panel.search.clear()
    # clicking a thread navigates to and selects the comment
    panel._on_clicked(panel.tree.topLevelItem(1))
    assert view.selected_annotations and view.selected_annotations[0][0] == 0

    xfdf = panel.export_xfdf(tmp_path / "c.xfdf")
    csv_path = panel.export_csv(tmp_path / "c.csv")
    pdf = panel.export_summary_pdf(tmp_path / "summary.pdf")
    assert "<square" in xfdf.read_text(encoding="utf-8")
    assert "a reply" in csv_path.read_text(encoding="utf-8-sig")
    with pikepdf.open(pdf) as summary:
        assert len(summary.pages) >= 1
    count = panel.import_xfdf(xfdf)
    assert count == 4 and len(annots(view)) == 8
    window.act_undo.trigger()
    assert len(annots(view)) == 4


def test_flatten_all_and_save_roundtrip(qtbot, window: MainWindow, view, monkeypatch) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    assert window.flatten_all() == 1
    assert annots(view) == []
    window.act_undo.trigger()
    assert len(annots(view)) == 1
    window.save()
    path = view.session.path
    doc = pdfium.PdfDocument(path)
    assert len(doc) == 5
    doc.close()
    with pikepdf.open(path) as pdf:
        assert any(a.Subtype == "/Square" for a in pdf.pages[0].Annots)


def test_context_menu_reorder(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    drag(qtbot, view, 0, (250, 450), (150, 350))  # start outside the first box
    first, second = annots(view)
    from pdfeditor.core.commands import ReorderAnnotationCommand

    view.session.execute(ReorderAnnotationCommand(0, first.name, True))
    assert [a.name for a in annots(view)] == [second.name, first.name]


def test_escape_returns_to_select(qtbot, window: MainWindow, view) -> None:
    window.set_tool("pen")
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    assert window.current_tool == "select" and view.tool.name == "select"


def test_hit_testing_prefers_topmost(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    drag(qtbot, view, 0, (250, 450), (150, 350))  # start outside the first box
    top = annots(view)[-1]
    hit = view.annotation_at(view.page_point_to_scene(0, Point(170, 370)))
    assert hit is not None and hit.name == top.name
    assert view.annotation_at(view.page_point_to_scene(0, Point(500, 700))) is None


def test_flatten_selected_only(qtbot, window: MainWindow, view) -> None:
    window.set_tool("rectangle")
    drag(qtbot, view, 0, (100, 300), (200, 400))
    drag(qtbot, view, 0, (300, 300), (400, 400))
    keep, burn = annots(view)
    view.set_annotation_selection([(0, burn.name)])
    assert window.flatten_selected() == 1
    assert [a.name for a in annots(view)] == [keep.name]
    window.act_undo.trigger()
    assert len(annots(view)) == 2
