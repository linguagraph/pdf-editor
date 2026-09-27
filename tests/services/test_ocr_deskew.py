from __future__ import annotations

import difflib
import math
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, RenderRequest
from pdfeditor.engine.deskew import estimate_skew, rotate_about_center
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Matrix, Point, Quad
from pdfeditor.services.ocr import OcrOptions, apply, installed_languages, recognize

ENGINE = get_engine()
SKEW = 4.0  # scripts/make_fixtures.py SKEW_DEGREES
LOREM_START = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."

needs_eng = pytest.mark.skipif(
    "eng" not in installed_languages(),
    reason="English OCR data not installed (scripts/fetch_tessdata.py)",
)


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, " ".join(a.split()), " ".join(b.split())).ratio()


def page_image(path: Path, zoom: float = 2.0) -> Image.Image:
    doc = ENGINE.open(path.read_bytes())
    try:
        pix = doc.page(0).render(RenderRequest(matrix=Matrix.scale(zoom), color=ColorMode.RGB))
        return Image.frombytes(
            "RGB", (pix.width, pix.height), pix.samples, "raw", "RGB", pix.stride
        )
    finally:
        doc.close()


def test_estimate_skew_of_skewed_scan(fixture_pdf) -> None:
    assert estimate_skew(page_image(fixture_pdf("skewed_scan"))) == pytest.approx(SKEW, abs=0.5)
    assert estimate_skew(page_image(fixture_pdf("scanned"))) == pytest.approx(0, abs=0.5)


@pytest.mark.parametrize("angle", [-8.0, -2.5, 1.0, 6.3])
def test_estimate_skew_accuracy(fixture_pdf, angle: float) -> None:
    straight = page_image(fixture_pdf("scanned"), zoom=1.5)
    assert estimate_skew(rotate_about_center(straight, angle)) == pytest.approx(angle, abs=0.5)


def test_estimate_skew_blank_page() -> None:
    assert estimate_skew(Image.new("RGB", (300, 400), "white")) == 0.0


def _skewed(p: Point, center: Point) -> Point:
    """Where a point of the straight page lands on the skewed one (y-down, CCW on screen)."""
    rad = math.radians(SKEW)
    dx, dy = p.x - center.x, p.y - center.y
    return Point(
        center.x + dx * math.cos(rad) + dy * math.sin(rad),
        center.y - dx * math.sin(rad) + dy * math.cos(rad),
    )


def _ocr(path: Path, deskew: bool):  # type: ignore[no-untyped-def]
    doc = ENGINE.open(path.read_bytes())
    apply(doc, recognize(doc, [0], OcrOptions(dpi=200, deskew=deskew)))
    return doc


@needs_eng
def test_deskewed_ocr_reads_and_lines_up_with_the_scan(fixture_pdf, tmp_path: Path) -> None:
    straight = _ocr(fixture_pdf("scanned"), deskew=False)
    skewed = _ocr(fixture_pdf("skewed_scan"), deskew=True)
    text = skewed.page(0).text_page(with_chars=False).text
    assert similarity(text.split("\n\n")[0], "Scanned document text") > 0.9
    assert similarity(text[text.index("Lorem") :][: len(LOREM_START)], LOREM_START) > 0.9

    # Round trip: the saved layer opens elsewhere and still lines up after reopening.
    out = skewed.save(tmp_path / "deskewed.pdf")
    skewed.close()
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 1
    pdfium_doc = pdfium.PdfDocument(out)
    assert pdfium_doc[0].render(scale=0.5).to_pil().size[0] > 0
    pdfium_doc.close()
    skewed = ENGINE.open(out)

    page_rect = straight.page(0).rect
    center = Point(page_rect.width / 2, page_rect.height / 2)
    for word in ("Scanned document", "consequat", "Lorem ipsum"):
        (want,) = straight.page(0).search(word)[:1]
        (got,) = skewed.page(0).search(word)[:1]
        expected = Quad(*(_skewed(p, center) for p in (want.ul, want.ur, want.ll, want.lr)))
        for g, e in zip(
            (got.ul, got.ur, got.ll, got.lr),
            (expected.ul, expected.ur, expected.ll, expected.lr),
            strict=True,
        ):
            assert g.x == pytest.approx(e.x, abs=3) and g.y == pytest.approx(e.y, abs=3), word
    line = next(
        ln for b in skewed.page(0).text_page().blocks for ln in b.lines if "Scanned" in ln.text
    )
    # the recognized line runs uphill with the scan (y-down: dy/dx = -tan(skew))
    assert math.degrees(math.atan2(-line.direction[1], line.direction[0])) == pytest.approx(
        SKEW, abs=0.5
    )
    straight.close()
    skewed.close()


@needs_eng
def test_deskew_on_a_straight_page_changes_nothing(fixture_pdf) -> None:
    plain = _ocr(fixture_pdf("scanned"), deskew=False)
    deskewed = _ocr(fixture_pdf("scanned"), deskew=True)
    assert deskewed.page(0).search("Scanned document") == plain.page(0).search("Scanned document")
    plain.close()
    deskewed.close()
