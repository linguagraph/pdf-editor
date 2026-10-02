"""OCR and the Edit tool: editable output, OCR when editing a scan (#60, #61, #62)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pdfeditor.model.geometry import Rect
from pdfeditor.model.objects import ObjectType
from pdfeditor.model.scan import ScanCleanup
from pdfeditor.services.ocr import installed_languages
from pdfeditor.ui.dialogs.ocr import OcrDialog
from pdfeditor.ui.jobs import wait_for
from pdfeditor.ui.main_window import MainWindow

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif("eng" not in installed_languages(), reason="English OCR data missing"),
]


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1000, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.jobs.cancel_all()
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def scan(fixture_pdf, tmp_path: Path) -> Path:
    path = tmp_path / "scan.pdf"
    shutil.copy2(fixture_pdf("scanned"), path)
    return path


def text_objects(view) -> list:
    return [o for o in view.page_objects(0) if o.type is ObjectType.TEXT]


def test_output_choice_maps_to_cleanup(qtbot) -> None:
    dialog = OcrDialog(1, 0, [])
    qtbot.addWidget(dialog)
    assert dialog.box.options().cleanup is ScanCleanup.KEEP
    dialog.box.output.setCurrentIndex(dialog.box.output.findData(ScanCleanup.ERASE))
    assert dialog.box.options().cleanup is ScanCleanup.ERASE


def test_output_choice_is_remembered(window: MainWindow, scan: Path) -> None:
    view = window.open_path(scan)
    dialog = OcrDialog(view.page_count, 0, [], window)
    dialog.box.dpi.setValue(150)
    dialog.box.output.setCurrentIndex(dialog.box.output.findData(ScanCleanup.REMOVE))
    dialog.accept()
    wait_for(window.tools.recognize(dialog))
    again = OcrDialog(view.page_count, 0, [], window)
    assert again.box.output.currentData() is ScanCleanup.REMOVE
    assert window.prefs.ocr_options().cleanup is ScanCleanup.REMOVE


def test_recognize_editable_output(window: MainWindow, scan: Path) -> None:
    view = window.open_path(scan)
    dialog = OcrDialog(view.page_count, 0, [], window)
    dialog.box.dpi.setValue(200)
    dialog.box.output.setCurrentIndex(dialog.box.output.findData(ScanCleanup.ERASE))
    dialog.accept()
    result = wait_for(window.tools.recognize(dialog))
    assert result is not None and list(result.plans) == [0]
    with view.session.lock:
        info = view.session.document.page(0).scan_info()
    assert info.visible_chars > 100 and info.invisible_chars == 0
    view.session.undo()
    with view.session.lock:
        assert view.session.document.page(0).scan_info().needs_ocr


def test_edit_tool_recognizes_a_scan(window: MainWindow, scan: Path) -> None:
    view = window.open_path(scan)
    assert text_objects(view) == []
    window.set_tool("edit")
    (job,) = window.jobs.running()
    result = wait_for(job, 120_000)
    assert result is not None and list(result.plans) == [0]
    assert "so you can edit it" in window.tools.last_message
    objects = text_objects(view)
    assert objects and all(o.editable for o in objects)
    assert any("Scanned document text" in o.text for o in objects)
    view.session.undo()  # one step back to the bare scan
    with view.session.lock:
        assert view.session.document.page(0).scan_info().needs_ocr
    window.set_tool("select")
    window.set_tool("edit")
    assert window.jobs.running() == []  # offered once per document: the undo is respected


def test_edit_tool_leaves_scans_alone_when_turned_off(window: MainWindow, scan: Path) -> None:
    window.prefs.ocr_when_editing = False
    window.open_path(scan)
    window.set_tool("edit")
    assert window.jobs.running() == []


def test_edit_tool_ignores_documents_with_text(window: MainWindow, fixture_pdf) -> None:
    window.open_path(fixture_pdf("text_multipage"))
    window.set_tool("edit")
    assert window.jobs.running() == []


def test_click_between_ocr_lines_grabs_no_invisible_layer(window: MainWindow, scan: Path) -> None:
    window.prefs.ocr_when_editing = False
    view = window.open_path(scan)
    dialog = OcrDialog(view.page_count, 0, [], window)
    dialog.box.dpi.setValue(200)
    dialog.accept()
    wait_for(window.tools.recognize(dialog))
    window.set_tool("edit")
    blocks = sorted(text_objects(view), key=lambda o: o.bbox.y0)
    first, second = blocks[0], blocks[1]
    gap_y = (first.bbox.y1 + second.bbox.y0) / 2
    point = view.page_rect_to_scene(
        0, Rect(first.bbox.x0 + 5, gap_y, first.bbox.x0 + 6, gap_y + 1)
    ).center()
    hit = view.object_at(point)
    # before #62 this was a page-sized form holding the invisible text
    assert hit is None or hit.type is ObjectType.IMAGE
