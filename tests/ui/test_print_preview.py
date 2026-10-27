"""The Print dialog's integrated preview (#77): shared placement code, live updates, copies."""

from __future__ import annotations

import threading
from pathlib import Path

import pikepdf
import pymupdf  # test-only: rasterise the printed output
import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDialog, QFileDialog

from pdfeditor.core.session import DocumentSession
from pdfeditor.ui import printing
from pdfeditor.ui.dialogs import print_dialog
from pdfeditor.ui.dialogs.print_dialog import PrintDialog
from pdfeditor.ui.dialogs.print_preview import PrintPreviewPane
from pdfeditor.ui.printing import (
    PrintOptions,
    Scaling,
    pdf_printer,
    place_page,
    print_pages,
    render_sheet_image,
    sheet_sequence,
)

A4 = (595.0, 842.0)
AREA = (500.0, 700.0)  # printable area in points (portrait)


def wait_preview(qtbot, dialog: PrintDialog, timeout: int = 10_000) -> QImage:
    qtbot.waitUntil(
        lambda: (
            dialog.preview.current_image() is not None
            and not dialog.preview.is_pending()
            and dialog.preview_settled()
        ),
        timeout=timeout,
    )
    image = dialog.preview.current_image()
    assert image is not None
    return image


@pytest.fixture
def make_dialog(qtbot, make_view):
    def make(name: str = "text_multipage") -> PrintDialog:
        view = make_view(name)
        dialog = PrintDialog(view)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)
        return dialog

    return make


# -- pure placement ------------------------------------------------------------------------
def test_place_page_matches_scaling() -> None:
    fit = min(AREA[0] / A4[0], AREA[1] / A4[1])
    big = (1000.0, 1400.0)
    big_fit = min(AREA[0] / big[0], AREA[1] / big[1])
    for scaling, small_factor, big_factor in (
        (Scaling.FIT, fit, big_fit),
        (Scaling.ACTUAL, 1.0, 1.0),
        (Scaling.SHRINK, min(1.0, fit), big_fit),
    ):
        opts = PrintOptions(pages=(0,), scaling=scaling)
        assert place_page(*A4, *AREA, opts).factor == pytest.approx(small_factor)
        assert place_page(*big, *AREA, opts).factor == pytest.approx(big_factor)
    small = place_page(300.0, 400.0, *AREA, PrintOptions(pages=(0,), scaling=Scaling.FIT))
    assert small.factor > 1  # FIT enlarges small pages too
    rotating = PrintOptions(pages=(0,), scaling=Scaling.FIT, auto_rotate=True)
    placed = place_page(A4[1], A4[0], *AREA, rotating)
    assert placed.rotation == 90 and placed.factor == pytest.approx(fit)
    fixed = PrintOptions(pages=(0,), auto_rotate=False)
    assert place_page(A4[1], A4[0], *AREA, fixed).rotation == 0
    assert place_page(*A4, *AREA, rotating).rotation == 0
    assert place_page(*A4, AREA[1], AREA[0], rotating).rotation == 90  # landscape paper


def test_sheet_sequence_copies() -> None:
    assert sheet_sequence(2, 1) == [0, 1]
    assert sheet_sequence(2, 2, True) == [0, 1, 0, 1]
    assert sheet_sequence(2, 2, False) == [0, 0, 1, 1]
    assert sheet_sequence(0, 3) == []
    assert sheet_sequence(2, 0) == [0, 1]


def _rasters(path: Path) -> list[bytes]:
    """Each printed page as pixels (the printed text is vector outlines, not extractable)."""
    doc = pymupdf.open(path)
    try:
        return [page.get_pixmap(dpi=30).samples for page in doc]
    finally:
        doc.close()


@pytest.mark.gui
def test_print_copies_emulated_on_pdf(qtbot, make_view, tmp_path: Path) -> None:
    view = make_view("text_multipage")
    assert not pdf_printer(str(tmp_path / "x.pdf")).supportsMultipleCopies()
    single = []
    for page in (0, 1):
        out = tmp_path / f"single{page}.pdf"
        print_pages(view.session, pdf_printer(str(out)), PrintOptions(pages=(page,)))
        single.append(_rasters(out)[0])
    assert single[0] != single[1]
    for collate, expected in ((True, [0, 1, 0, 1]), (False, [0, 0, 1, 1])):
        out = tmp_path / f"copies-{collate}.pdf"
        options = PrintOptions(pages=(0, 1), copies=2, collate=collate)
        assert print_pages(view.session, pdf_printer(str(out)), options) == 4
        assert _rasters(out) == [single[n] for n in expected]


# -- preview == print ----------------------------------------------------------------------
@pytest.mark.gui
@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("text_multipage", PrintOptions(pages=(0,), scaling=Scaling.FIT)),
        ("text_multipage", PrintOptions(pages=(0,), scaling=Scaling.ACTUAL)),
        ("rotated_pages", PrintOptions(pages=(1,), scaling=Scaling.SHRINK, auto_rotate=True)),
        ("annotations", PrintOptions(pages=(0,), grayscale=True)),
    ],
    ids=["fit", "actual", "rotated", "annotations-gray"],
)
def test_preview_matches_printed_output(
    qtbot, fixture_pdf, tmp_path: Path, name: str, options: PrintOptions
) -> None:
    session = DocumentSession.open(fixture_pdf(name))
    try:
        out = tmp_path / "printed.pdf"
        printer = pdf_printer(str(out))
        print_pages(session, printer, options)
        image = render_sheet_image(session, options.pages[0], options, printer.pageLayout(), 50)
    finally:
        session.close()
    doc = pymupdf.open(out)
    pix = doc[0].get_pixmap(dpi=50)
    doc.close()
    # sizes may differ by a rounding pixel (the sheet isn't a whole number of pixels)
    assert abs(image.width() - pix.width) <= 1 and abs(image.height() - pix.height) <= 1
    width, height = min(image.width(), pix.width), min(image.height(), pix.height)
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    differing = 0
    for y in range(height):
        row = bytes(rgb.constScanLine(y))[: width * 3]
        ref = pix.samples[y * pix.stride : y * pix.stride + width * 3]
        differing += sum(abs(a - b) > 80 for a, b in zip(row, ref, strict=True))
    assert differing < width * height * 3 * 0.02


def test_render_sheet_image_is_a_white_sheet_with_the_page(qtbot, fixture_pdf) -> None:
    session = DocumentSession.open(fixture_pdf("text_multipage"))
    try:
        printer = pdf_printer("unused.pdf")
        layout = printer.pageLayout()
        image = render_sheet_image(session, 0, PrintOptions(pages=(0,)), layout, 40)
    finally:
        session.close()
    assert image.width() == round(layout.fullRectPoints().width() * 40 / 72)
    assert image.pixelColor(0, 0).rgb() == 0xFFFFFFFF  # the margin is blank paper
    colors = {
        image.pixel(x, y) for x in range(0, image.width(), 5) for y in range(0, image.height(), 5)
    }
    assert len(colors) > 3


# -- the dialog ----------------------------------------------------------------------------
@pytest.mark.gui
def test_dialog_has_integrated_preview_and_no_preview_window(qtbot, make_dialog) -> None:
    dialog = make_dialog()
    assert isinstance(dialog.preview, PrintPreviewPane) and dialog.preview.isVisible()
    assert not hasattr(dialog, "preview_button") and not hasattr(dialog, "preview_window")
    assert "QPrintPreviewDialog" not in vars(print_dialog)
    assert not any(b.text().startswith("Preview") for b in dialog.button_box.buttons())
    assert dialog.preview.x() > dialog.settings_area.x()  # settings left, preview right
    image = wait_preview(qtbot, dialog)
    assert image.height() > image.width() > 0  # a portrait sheet
    assert dialog.preview.position_label.text() == "Page 1 of 5"


@pytest.mark.gui
def test_preview_navigation_follows_range(qtbot, make_dialog) -> None:
    dialog = make_dialog()
    preview = dialog.preview
    wait_preview(qtbot, dialog)
    dialog.range_pages.setChecked(True)
    dialog.range_edit.setText("2-4")
    wait_preview(qtbot, dialog)
    assert preview.sheet_count == 3
    assert preview.position_label.text() == "Page 1 of 3 (document page 2)"
    assert not preview.prev_button.isEnabled() and preview.next_button.isEnabled()
    preview.next_button.click()
    preview.next_button.click()
    wait_preview(qtbot, dialog)
    assert preview.position_label.text() == "Page 3 of 3 (document page 4)"
    assert not preview.next_button.isEnabled() and preview.prev_button.isEnabled()
    dialog.current_page.setChecked(True)
    wait_preview(qtbot, dialog)
    assert preview.sheet_count == 1 and preview.position_label.text() == "Page 1 of 1"
    dialog.range_pages.setChecked(True)
    dialog.range_edit.setText("9")
    qtbot.waitUntil(lambda: not dialog.range_error.isHidden())
    assert preview.current_image() is None and preview.sheet_count == 0
    assert "valid page range" in preview.status_label.text()
    dialog.range_edit.setText("1")
    wait_preview(qtbot, dialog)
    assert dialog.range_error.isHidden() and preview.sheet_count == 1


def _red_pixels(image: QImage) -> int:
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            count += c.red() > 200 and c.green() < 80 and c.blue() < 80
    return count


@pytest.mark.gui
def test_preview_updates_on_settings(qtbot, make_dialog) -> None:
    dialog = make_dialog("annotations")
    assert _red_pixels(wait_preview(qtbot, dialog)) > 0
    dialog.annotations.setChecked(False)
    assert _red_pixels(wait_preview(qtbot, dialog)) == 0
    dialog.annotations.setChecked(True)
    assert _red_pixels(wait_preview(qtbot, dialog)) > 0

    dialog.grayscale.setChecked(True)
    gray = wait_preview(qtbot, dialog)
    for y in range(0, gray.height(), 7):
        for x in range(0, gray.width(), 7):
            c = gray.pixelColor(x, y)
            assert abs(c.red() - c.green()) <= 2 and abs(c.green() - c.blue()) <= 2
    dialog.grayscale.setChecked(False)
    wait_preview(qtbot, dialog)

    dialog.orientation.setCurrentIndex(dialog.orientation.findData(print_dialog.LANDSCAPE))
    landscape = wait_preview(qtbot, dialog)
    assert landscape.width() > landscape.height()
    assert not dialog.auto_rotate and not dialog.options().auto_rotate


@pytest.mark.gui
def test_dialog_prints_with_orientation_and_copies(qtbot, make_dialog, tmp_path: Path) -> None:
    dialog = make_dialog()
    dialog.orientation.setCurrentIndex(dialog.orientation.findData(print_dialog.LANDSCAPE))
    assert not dialog.collate.isEnabled()
    dialog.copies.setValue(2)
    assert dialog.collate.isEnabled()
    dialog.range_pages.setChecked(True)
    dialog.range_edit.setText("1")
    options = dialog.options()
    assert options.copies == 2 and options.collate and options.pages == (0,)
    out = tmp_path / "landscape.pdf"
    assert dialog.run(pdf_printer(str(out))) == 2
    with pikepdf.open(out) as pdf:
        sizes = [(float(p.mediabox[2]), float(p.mediabox[3])) for p in pdf.pages]
    assert len(sizes) == 2 and all(w > h for w, h in sizes)


@pytest.mark.gui
def test_printer_list_and_save_as_pdf(
    qtbot, make_dialog, tmp_path: Path, monkeypatch, fake_printers
) -> None:
    dialog = make_dialog()
    combo = dialog.printer_combo
    texts = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]
    assert texts == ["Fake Printer", "Save as PDF"]
    assert combo.currentText() == "Fake Printer" and dialog.properties_button.isEnabled()
    assert fake_printers[-1] == "Fake Printer"
    combo.setCurrentIndex(combo.findData(None))
    assert fake_printers[-1] is None and not dialog.properties_button.isEnabled()
    wait_preview(qtbot, dialog)

    out = tmp_path / "saved.pdf"
    asked: list[str] = []

    def save_name(parent, title, start, filters):
        asked.append(start)
        return str(out), filters

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(save_name))
    dialog.print_button.click()
    assert asked == ["text_multipage.pdf"] and out.exists()
    assert dialog.result() == QDialog.DialogCode.Accepted
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 5

    # cancelling the file dialog keeps the dialog open
    again = make_dialog()
    again.printer_combo.setCurrentIndex(again.printer_combo.findData(None))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a: ("", "")))
    again.print_button.click()
    assert again.isVisible()
    wait_preview(qtbot, again)  # and its preview still works


@pytest.mark.gui
def test_no_printers_leaves_save_as_pdf(qtbot, make_dialog, monkeypatch) -> None:
    monkeypatch.setattr(printing, "available_printers", lambda: ([], None))
    dialog = make_dialog()
    assert dialog.printer_combo.count() == 1
    assert dialog.printer_combo.currentText() == "Save as PDF"
    assert not dialog.properties_button.isEnabled()
    wait_preview(qtbot, dialog)


@pytest.mark.gui
def test_preview_renders_off_gui_thread(qtbot, make_dialog, monkeypatch) -> None:
    threads: list[threading.Thread] = []
    gate = threading.Event()
    real = printing.render_sheet_image

    def slow(*args, **kwargs):
        threads.append(threading.current_thread())
        gate.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(printing, "render_sheet_image", slow)
    dialog = make_dialog()
    qtbot.waitUntil(lambda: bool(threads))
    assert threads[0] is not threading.main_thread()
    assert dialog.preview.is_pending()  # the GUI thread stayed free while it rendered
    gate.set()  # let the worker finish, then close while its result is queued
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.preview.renderer.wait_idle(5000)
