"""Editable OCR output: scanned text replaced with real text (issues #60, #61, #62)."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, Document, RenderRequest
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.objects import ObjectType
from pdfeditor.model.pages import ImageStamp
from pdfeditor.model.scan import ScanCleanup
from pdfeditor.services.ocr import (
    OcrOptions,
    apply,
    installed_languages,
    make_scans_editable,
    recognize,
    scan_pages_to_edit,
)

ENGINE = get_engine()
REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"

pytestmark = pytest.mark.skipif(
    "eng" not in installed_languages(),
    reason="English OCR data not installed (scripts/fetch_tessdata.py)",
)


def ocr(doc: Document, cleanup: ScanCleanup) -> None:
    apply(doc, recognize(doc, [0], OcrOptions(dpi=200, cleanup=cleanup)))


def save_and_reopen(doc: Document, path: Path) -> Document:
    doc.save(path)
    doc.close()
    with pikepdf.open(path) as pdf:  # an independent parser accepts it
        assert len(pdf.pages) == 1
    return ENGINE.open(path)


def pdfium_render(path: Path, scale: float = 2.0) -> Image.Image:
    pdf = pdfium.PdfDocument(str(path))
    try:
        image: Image.Image = pdf[0].render(scale=scale).to_pil().convert("L")
    finally:
        pdf.close()
    return image


def dark_share(image: Image.Image, box: Rect, scale: float = 2.0) -> float:
    crop = image.crop(
        (int(box.x0 * scale), int(box.y0 * scale), int(box.x1 * scale), int(box.y1 * scale))
    )
    hist = crop.histogram()
    return sum(hist[:128]) / max(1, sum(hist))


def text_objects(doc: Document) -> list:
    return [o for o in doc.page(0).content_objects() if o.type is ObjectType.TEXT]


def line_box(doc: Document, needle: str) -> Rect:
    tp = doc.page(0).text_page()
    return next(ln.bbox for b in tp.blocks for ln in b.lines if needle in ln.text)


# -- #62: the OCR layer is not a draggable object ----------------------------------------------
def test_searchable_layer_adds_no_form_object(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    before = [o.type for o in doc.page(0).content_objects()]
    ocr(doc, ScanCleanup.KEEP)
    after = [o.type for o in doc.page(0).content_objects() if o.type is not ObjectType.TEXT]
    assert after == before  # just the scan image: nothing invisible to select and drag
    assert text_objects(doc)
    info = doc.page(0).scan_info()
    assert info.has_hidden_ocr and info.visible_chars == 0
    doc.close()


# -- #60: editable output ----------------------------------------------------------------------
def test_erase_replaces_scanned_text_with_real_text(fixture_pdf, tmp_path: Path) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    ocr(doc, ScanCleanup.ERASE)
    info = doc.page(0).scan_info()
    assert info.is_scan and info.visible_chars > 100 and info.invisible_chars == 0
    title = line_box(doc, "Scanned")
    assert title.x0 == pytest.approx(72, abs=4)  # where the scan showed it
    doc = save_and_reopen(doc, tmp_path / "erased.pdf")
    assert "Scanned document text" in doc.page(0).text_page(with_chars=False).text
    objects = text_objects(doc)
    assert objects and all(o.editable for o in objects)
    # without the new text, the scan is blank where the recognized lines were
    doc.page(0).delete_objects([o.key for o in objects])
    doc.save(tmp_path / "no_text.pdf")
    doc.close()
    assert dark_share(pdfium_render(tmp_path / "erased.pdf"), title) > 0.05
    assert dark_share(pdfium_render(tmp_path / "no_text.pdf"), title) < 0.002


def test_remove_drops_the_scan_image(fixture_pdf, tmp_path: Path) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    ocr(doc, ScanCleanup.REMOVE)
    doc = save_and_reopen(doc, tmp_path / "text_only.pdf")
    info = doc.page(0).scan_info()
    assert info.coverage == 0 and info.visible_chars > 100
    assert not [o for o in doc.page(0).content_objects() if o.type is ObjectType.IMAGE]
    assert "Scanned document text" in doc.page(0).text_page(with_chars=False).text
    doc.close()
    assert dark_share(pdfium_render(tmp_path / "text_only.pdf"), Rect(72, 70, 400, 110)) > 0.03


def test_searchable_page_is_converted_without_new_ocr(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    ocr(doc, ScanCleanup.KEEP)
    assert scan_pages_to_edit(doc) == [0]
    result = make_scans_editable(doc, OcrOptions(dpi=200))
    assert result.layers == {} and list(result.plans) == [0]  # the existing layer is reused
    apply(doc, result)
    text = doc.page(0).text_page(with_chars=False).text
    assert text.count("Scanned document") == 1  # replaced, not duplicated
    assert doc.page(0).scan_info().invisible_chars == 0
    assert scan_pages_to_edit(doc) == []
    doc.close()


def test_pages_with_real_text_are_left_alone(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("text_multipage").read_bytes())
    assert scan_pages_to_edit(doc) == []
    result = recognize(doc, [0], OcrOptions(cleanup=ScanCleanup.ERASE))
    assert result.plans == {} and result.skipped == [0]
    doc.close()


def test_rotated_scan_reads_upright_and_stays_editable(fixture_pdf) -> None:
    src = ENGINE.open(fixture_pdf("scanned").read_bytes())
    shot = src.page(0).render(RenderRequest(matrix=Matrix.scale(2), color=ColorMode.RGB))
    img = Image.frombytes("RGB", (shot.width, shot.height), shot.samples, "raw", "RGB", shot.stride)
    buf = io.BytesIO()
    img.crop((0, 0, img.width, img.height // 2)).save(buf, format="PNG")
    src.close()
    doc = ENGINE.new_document()
    doc.insert_blank_page(0, 421, 595)
    doc.page(0).set_rotation(90)
    doc.page(0).stamp_image(ImageStamp(buf.getvalue(), doc.page(0).rect))
    ocr(doc, ScanCleanup.ERASE)
    tp = doc.page(0).text_page()
    line = next(ln for b in tp.blocks for ln in b.lines if "Scanned" in ln.text)
    assert line.direction[0] == pytest.approx(1, abs=0.01)
    assert line.bbox.x0 == pytest.approx(72, abs=4)
    assert any(o.editable and "Scanned" in o.text for o in text_objects(doc))
    doc.close()


# -- #62 (moving) on an editable scan ----------------------------------------------------------
def test_moving_editable_text_leaves_no_scanned_glyphs(fixture_pdf, tmp_path: Path) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    ocr(doc, ScanCleanup.ERASE)
    title = next(o for o in text_objects(doc) if "Scanned" in o.text)
    doc.page(0).transform_objects([title.key], Matrix.translate(0, 500))
    doc.save(tmp_path / "moved.pdf")
    doc.close()
    image = pdfium_render(tmp_path / "moved.pdf")
    assert dark_share(image, title.bbox) < 0.002  # nothing left where it was
    assert (
        dark_share(image, Rect(*title.bbox.transform(Matrix.translate(0, 500)).as_tuple())) > 0.05
    )


# -- the real scanned manual page (local sample, see tests/fixtures/real/README.md) -------------
def test_real_scan_becomes_editable(tmp_path: Path) -> None:
    path = REAL / "scanned_dryer_manual.pdf"
    if not path.exists():
        pytest.skip("real-world sample scanned_dryer_manual.pdf is not available")
    doc = ENGINE.open(path)
    result = make_scans_editable(doc, OcrOptions())
    apply(doc, result)
    doc = save_and_reopen(doc, tmp_path / "manual.pdf")
    text = doc.page(0).text_page(with_chars=False).text
    assert "tumble dryer" in text
    assert "•Remove all objects" in text  # the bullet read as "@" is put back
    objects = doc.page(0).content_objects()
    assert sum(o.editable for o in objects if o.type is ObjectType.TEXT) > 10
    info = doc.page(0).scan_info()
    assert info.is_scan and info.visible_chars > 1500
    doc.close()
