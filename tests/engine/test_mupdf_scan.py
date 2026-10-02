"""MuPDF backend: OCR layers and editable scans (issues #60 and #62)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdfeditor.engine.mupdf.document import MuDocument, open_document
from pdfeditor.model.objects import ObjectType
from pdfeditor.model.scan import ScanCleanup
from pdfeditor.services.ocr import OcrOptions, installed_languages, recognize

pytestmark = pytest.mark.skipif(
    "eng" not in installed_languages(), reason="English OCR data not installed"
)


def _open(path: Path) -> MuDocument:
    doc = open_document(path, None)
    assert isinstance(doc, MuDocument)
    return doc


def _layer(path: Path) -> bytes:
    doc = _open(path)
    try:
        return recognize(doc, [0], OcrOptions(dpi=200)).layers[0]
    finally:
        doc.close()


def test_old_style_ocr_form_is_hidden_and_convertible(fixture_pdf, tmp_path: Path) -> None:
    """Files OCR'd before #62 hold the invisible text in a page-sized form XObject."""
    fz = pymupdf.open(fixture_pdf("scanned"))
    page = fz[0]
    page.show_pdf_page(page.rect, pymupdf.open("pdf", _layer(fixture_pdf("scanned"))), 0)
    assert [x[1] for x in page.get_xobjects() if not x[2]] == ["fzFrm0"]  # the OCR form
    fz.save(tmp_path / "old.pdf")
    fz.close()
    doc = _open(tmp_path / "old.pdf")
    objects = doc.page(0).content_objects()
    assert not [o for o in objects if o.type is ObjectType.FORM]  # nothing to drag by mistake
    assert [o.type for o in objects if o.type is not ObjectType.TEXT] == [ObjectType.IMAGE]
    plan = doc.page(0).scan_text_plan(None, ScanCleanup.ERASE)
    assert plan is not None
    doc.page(0).apply_scan_text_plan(plan)
    assert doc.page(0).scan_info().invisible_chars == 0  # the text inside the form went too
    assert doc.page(0).text_page(with_chars=False).text.count("Scanned document") == 1
    doc.close()


def test_erasing_a_shared_scan_image_leaves_other_pages_alone(fixture_pdf, tmp_path: Path) -> None:
    fz = pymupdf.open(fixture_pdf("scanned"))
    xref = fz[0].get_images()[0][0]
    fz.new_page(width=fz[0].rect.width, height=fz[0].rect.height).insert_image(
        fz[0].rect, xref=xref
    )
    fz.save(tmp_path / "shared.pdf")
    fz.close()
    doc = _open(tmp_path / "shared.pdf")
    other_before = doc.fz[1].get_pixmap(dpi=50).samples
    layer = doc.page(0).ocr_text_layer("eng", 200, _tessdata())
    plan = doc.page(0).scan_text_plan(layer, ScanCleanup.ERASE)
    assert plan is not None and plan.images
    doc.page(0).add_text_layer(layer)
    doc.page(0).apply_scan_text_plan(plan)
    assert doc.fz[1].get_pixmap(dpi=50).samples == other_before
    assert doc.fz[0].get_images()[0][0] != xref  # page 0 draws its own, cleaned copy
    doc.close()


def test_text_written_once_per_font(fixture_pdf) -> None:
    doc = _open(fixture_pdf("scanned"))
    page = doc.page(0)
    layer = page.ocr_text_layer("eng", 200, _tessdata())
    plan = page.scan_text_plan(layer, ScanCleanup.ERASE)
    assert plan is not None and len(plan.lines) > 3
    page.add_text_layer(layer)
    page.apply_scan_text_plan(plan)
    fonts = [f for f in doc.fz[0].get_fonts(full=True) if "GlyphLess" not in f[3]]
    assert len(fonts) == 1  # one resource name, not one per line
    doc.close()


def _tessdata() -> Path:
    from pdfeditor.services.ocr import tessdata_for

    return tessdata_for(["eng"])


def test_tesseract_runs_in_a_helper_process(fixture_pdf) -> None:
    """MuPDF's OCR holds the GIL for a whole page; in the app's process it froze the window."""
    import os

    from pdfeditor.engine.mupdf import ocr_worker

    doc = _open(fixture_pdf("scanned"))
    layer = doc.page(0).ocr_text_layer("eng", 150, _tessdata())
    doc.close()
    assert layer
    pool = ocr_worker._pool
    assert pool is not None
    assert pool.submit(os.getpid).result() != os.getpid()


def test_ocr_falls_back_to_the_app_process(fixture_pdf, monkeypatch) -> None:
    from concurrent.futures.process import BrokenProcessPool

    from pdfeditor.engine.mupdf import ocr_worker

    class Broken:
        def submit(self, *_args: object) -> None:
            raise BrokenProcessPool("gone")

        def shutdown(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(ocr_worker, "_get_pool", lambda: Broken())
    doc = _open(fixture_pdf("scanned"))
    page = doc.page(0)
    layer = page.ocr_text_layer("eng", 150, _tessdata())
    page.add_text_layer(layer)
    assert "Scanned" in page.text_page(with_chars=False).text
    doc.close()
