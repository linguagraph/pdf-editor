from __future__ import annotations

import difflib
import io
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Rect
from pdfeditor.model.pages import ImageStamp
from pdfeditor.services.ocr import (
    OcrOptions,
    apply,
    installed_languages,
    ocr_files,
    pick_languages,
    recognize,
    tessdata_for,
)

ENGINE = get_engine()
LOREM_START = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."

pytestmark = pytest.mark.skipif(
    "eng" not in installed_languages(),
    reason="English OCR data not installed (scripts/fetch_tessdata.py)",
)


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, " ".join(a.split()), " ".join(b.split())).ratio()


def test_scanned_page_becomes_searchable(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    assert doc.page(0).text_page(with_chars=False).text.strip() == ""
    result = recognize(doc, [0], OcrOptions(dpi=200))
    apply(doc, result)
    text = doc.page(0).text_page(with_chars=False).text
    assert similarity(text.split("\n\n")[0], "Scanned document text") > 0.9
    assert similarity(text[text.index("Lorem") :][: len(LOREM_START)], LOREM_START) > 0.9
    hits = doc.page(0).search("Scanned document")
    assert hits and hits[0].rect.x0 == pytest.approx(72, abs=4)  # where the scan shows it
    # the visible page is unchanged: the layer is invisible text only
    doc.close()


def test_skip_pages_with_text_and_missing_language(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("text_multipage").read_bytes())
    result = recognize(doc, [0, 1], OcrOptions())
    assert result.skipped == [0, 1] and result.layers == {}
    with pytest.raises(ValueError, match="xyz"):
        tessdata_for(["xyz"])
    doc.close()


def test_status_names_each_page_before_its_work(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("text_multipage").read_bytes())
    events: list[object] = []
    recognize(
        doc,
        [0, 1],
        OcrOptions(),
        progress=lambda done, total: events.append((done, total)),
        status=events.append,
    )
    assert events == ["page 1 of 2", (1, 2), "page 2 of 2", (2, 2)]
    doc.close()


def test_pick_languages_keeps_installed_saved_ones_in_order() -> None:
    assert pick_languages(["bul", "eng"], {"eng", "bul", "deu"}) == ("bul", "eng")
    assert pick_languages(["bul", "xyz", "bul"], {"eng", "bul"}) == ("bul",)


def test_pick_languages_falls_back_when_none_is_installed() -> None:
    assert pick_languages(["xyz"], {"deu", "eng"}) == ("eng",)
    assert pick_languages([], {"eng"}) == ("eng",)
    assert pick_languages(["xyz"], {"fra", "deu"}) == ("deu",)  # first installed
    assert pick_languages(["eng"], set()) == ()


def test_rotated_scan_is_upright_and_placed(fixture_pdf) -> None:
    # A /Rotate 90 page whose picture reads upright on screen.
    src = ENGINE.open(fixture_pdf("scanned").read_bytes())
    page_img = src.page(0).render(
        __import__("pdfeditor.engine.base", fromlist=["RenderRequest"]).RenderRequest(
            matrix=__import__("pdfeditor.model.geometry", fromlist=["Matrix"]).Matrix.scale(2),
            color=__import__("pdfeditor.engine.base", fromlist=["ColorMode"]).ColorMode.RGB,
        )
    )
    img = Image.frombytes(
        "RGB", (page_img.width, page_img.height), page_img.samples, "raw", "RGB", page_img.stride
    )
    landscape = img.crop((0, 0, img.width, img.height // 2))
    buf = io.BytesIO()
    landscape.save(buf, format="PNG")
    src.close()
    doc = ENGINE.new_document()
    doc.insert_blank_page(0, 421, 595)  # portrait MediaBox ...
    doc.page(0).set_rotation(90)  # ... shown as landscape 595 x 421
    visible = doc.page(0).rect
    doc.page(0).stamp_image(ImageStamp(buf.getvalue(), visible))
    apply(doc, recognize(doc, [0], OcrOptions(dpi=200)))
    tp = doc.page(0).text_page()
    line = next(ln for b in tp.blocks for ln in b.lines if "Scanned" in ln.text)
    assert line.direction[0] == pytest.approx(1, abs=0.01)  # reads left to right on screen
    assert line.bbox.x0 == pytest.approx(72, abs=4)  # a 2x render stamped at half size
    doc.close()


def test_preprocessing_still_reads(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("scanned").read_bytes())
    apply(doc, recognize(doc, [0], OcrOptions(dpi=200, preprocess=True)))
    assert "scanned document" in doc.page(0).text_page(with_chars=False).text.lower()
    doc.close()


def test_batch_ocr(fixture_pdf, tmp_path: Path) -> None:
    statuses: list[str] = []
    written = ocr_files(
        ENGINE,
        [fixture_pdf("scanned"), fixture_pdf("images")],
        tmp_path / "out",
        OcrOptions(dpi=150),
        status=statuses.append,
    )
    assert [p.name for p in written] == ["scanned.pdf", "images.pdf"]
    assert statuses[:2] == ["file 1 of 2: scanned.pdf", "file 1 of 2: scanned.pdf, page 1 of 1"]
    assert "file 2 of 2: images.pdf" in statuses
    with pikepdf.open(written[0]) as pdf:
        assert len(pdf.pages) == 1
    doc = ENGINE.open(written[0])
    assert "Scanned" in doc.page(0).text_page(with_chars=False).text
    doc.close()
    _ = Rect
