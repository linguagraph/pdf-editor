from __future__ import annotations

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import ObjectType, ShapeKind, ShapeSpec
from pdfeditor.model.pages import TextStamp
from pdfeditor.services.compare import (
    ChangeKind,
    Word,
    align_pages,
    compare,
    diff_regions,
    diff_words,
    write_report,
)

ENGINE = get_engine()


@pytest.fixture
def docs(fixture_pdf):
    old = ENGINE.open(fixture_pdf("text_multipage"))
    new = ENGINE.open(fixture_pdf("text_multipage"))
    # page 1: a word changes
    heading = next(
        o for o in new.page(0).content_objects()
        if o.type is ObjectType.TEXT and o.text.startswith("Page 1 heading")
    )  # fmt: skip
    new.page(0).replace_text(heading.key, "Page 1 title")
    # page 3 (index 2) is deleted
    new.select_pages([0, 1, 3, 4])
    # a new page goes in after old page 4 (new index 3)
    new.insert_blank_page(3, 595, 842)
    new.page(3).stamp_text(TextStamp("A brand new page about something else", Point(72, 100)))
    # old page 5 (new index 4): only a drawing changes, no text
    new.page(4).add_shape(
        ShapeSpec(ShapeKind.RECTANGLE, Rect(300, 300, 400, 380), fill=Color(0, 0, 0))
    )
    yield old, new
    old.close()
    new.close()


def w(text: str, x: float, line: int = 1) -> Word:
    return Word(text, Rect(x, 10, x + 20, 20), line)


def test_diff_words() -> None:
    a = [w("the", 0), w("quick", 30), w("fox", 60)]
    b = [w("the", 0), w("slow", 30), w("fox", 60), w("jumps", 90)]
    changes = diff_words(a, b, 0, 0)
    assert [(c.kind, c.text_a, c.text_b) for c in changes] == [
        (ChangeKind.REPLACED, "quick", "slow"),
        (ChangeKind.INSERTED, "", "jumps"),
    ]
    assert changes[0].rects_a == [Rect(30, 10, 50, 20)]


def test_align_pages_handles_inserted_and_deleted() -> None:
    a = ["alpha one", "beta two", "gamma three", "delta four"]
    b = ["alpha one", "gamma three changed", "new page text", "delta four"]
    assert align_pages(a, b) == [(0, 0), (1, None), (2, 1), (None, 2), (3, 3)]


def test_diff_regions() -> None:
    a = Image.new("L", (200, 100), 255)
    b = a.copy()
    ImageDraw.Draw(b).rectangle([100, 40, 140, 60], fill=0)
    (box,) = diff_regions(a, b, scale=2.0)  # 2 px per point
    assert box.x0 == pytest.approx(50, abs=3) and box.x1 == pytest.approx(70, abs=3)
    assert diff_regions(a, a.copy(), 2.0) == []


def test_compare_documents(docs) -> None:
    old, new = docs
    result = compare(old, new)
    assert result.pairs == [(0, 0), (1, 1), (2, None), (3, 2), (None, 3), (4, 4)]
    kinds = {(c.kind, c.page_a, c.page_b) for c in result.changes}
    assert (ChangeKind.PAGE_DELETED, 2, None) in kinds
    assert (ChangeKind.PAGE_INSERTED, None, 3) in kinds
    replaced = [c for c in result.changes if c.kind is ChangeKind.REPLACED]
    assert replaced and replaced[0].text_a == "heading" and replaced[0].text_b == "title"
    assert replaced[0].page_a == 0 and replaced[0].rects_b[0].y0 < 80
    visual = [c for c in result.changes if c.kind is ChangeKind.VISUAL]
    assert len(visual) == 1 and visual[0].page_b == 4
    box = visual[0].rects_b[0]
    assert box.x0 == pytest.approx(300, abs=6) and box.y1 == pytest.approx(380, abs=6)
    assert not result.for_page_a(1)  # untouched page: no changes


def test_identical_documents(fixture_pdf) -> None:
    a = ENGINE.open(fixture_pdf("report"))
    b = ENGINE.open(fixture_pdf("report"))
    result = compare(a, b)
    assert result.identical and result.pairs == [(0, 0), (1, 1)]
    a.close()
    b.close()


def test_report(docs, tmp_path) -> None:
    old, new = docs
    result = compare(old, new)
    out = write_report(ENGINE, old, new, result, tmp_path / "report.pdf", "v1.pdf", "v2.pdf")
    with pikepdf.open(out) as pdf:
        pages = len(pdf.pages)
    # summary + (old, new) for the 2 changed pairs + the deleted page + the inserted page
    assert pages == 1 + 2 * 2 + 1 + 1
    report = ENGINE.open(out)
    summary = report.page(0).text_page(with_chars=False).text
    assert "Comparison report" in summary and "v1.pdf" in summary and "heading" in summary
    boxes = [a for a in report.page(1).annotations() if a.type is AnnotationType.SQUARE]
    assert boxes and boxes[0].color is not None and boxes[0].color.r > 0.8  # old side: red
    report.close()
    pdfium.PdfDocument(str(out))
