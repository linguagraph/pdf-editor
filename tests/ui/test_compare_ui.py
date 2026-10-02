from __future__ import annotations

from pathlib import Path

import pytest

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import ObjectType, ShapeKind, ShapeSpec
from pdfeditor.model.pages import TextStamp
from pdfeditor.ui.dialogs.compare import CompareFilesDialog
from pdfeditor.ui.jobs import wait_for
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1000, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    if w.compare.window is not None:
        w.compare.window.close()
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def files(fixture_pdf, tmp_path: Path) -> tuple[Path, Path]:
    old = fixture_pdf("text_multipage")
    doc = get_engine().open(old)
    heading = next(
        o for o in doc.page(0).content_objects()
        if o.type is ObjectType.TEXT and o.text.startswith("Page 1 heading")
    )  # fmt: skip
    doc.page(0).replace_text(heading.key, "Page 1 title")
    doc.select_pages([0, 1, 3, 4])
    doc.insert_blank_page(3, 595, 842)
    doc.page(3).stamp_text(TextStamp("A brand new page about something else", Point(72, 100)))
    doc.page(4).add_shape(
        ShapeSpec(ShapeKind.RECTANGLE, Rect(300, 300, 400, 380), fill=Color(0, 0, 0))
    )
    new = doc.save(tmp_path / "new.pdf")
    doc.close()
    return old, new


def run(window: MainWindow, old: Path, new: Path):
    d = CompareFilesDialog(old, window)
    d.new.setText(str(new))
    d.accept()
    return wait_for(window.compare.compare(d))


def test_compare_window(qtbot, window: MainWindow, files, tmp_path: Path) -> None:
    old, new = files
    cw = run(window, old, new)
    assert cw is not None and cw.isVisible()
    texts = [cw.changes.item(i).text() for i in range(cw.changes.count())]
    assert any("heading" in t and "title" in t for t in texts)
    assert any("Page 3 deleted" in t for t in texts)
    assert any("Page 4 inserted" in t for t in texts)
    assert any("Appearance changed" in t for t in texts)
    assert cw.old_view.extra_overlays.get(0) and cw.new_view.extra_overlays.get(4)
    # choosing a change shows it on both sides
    row = next(i for i, t in enumerate(texts) if "Appearance" in t)
    cw.changes.setCurrentRow(row)
    assert cw.old_view.current_page == 4 and cw.new_view.current_page == 4
    # scrolling one side follows on the other (old page 4 -> new page 3)
    cw.old_view.go_to_page(3, record=False)
    assert cw.new_view.current_page == 2
    assert cw.partner(2, old=True) is not None  # a deleted page maps to a neighbour
    report = window.compare.export_report(cw, tmp_path / "r.pdf")
    assert report is not None and report.exists()
    cw.close()
    qtbot.waitUntil(lambda: window.compare.window is None, timeout=2000)


def test_identical_and_bad_input(window: MainWindow, fixture_pdf, monkeypatch) -> None:
    cw = run(window, fixture_pdf("report"), fixture_pdf("report"))
    assert cw is not None and "identical" in window.compare.last_message
    assert cw.changes.item(0).text() == "No differences found."
    cw.close()
    warned: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.compare_controller.QMessageBox.warning",
        lambda _p, _t, msg: warned.append(msg),
    )
    assert run(window, fixture_pdf("report"), Path("missing.pdf")) is None
    assert warned and "existing" in warned[0]
