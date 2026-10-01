"""Contract tests for page operations (capability ``page_ops``)."""

from __future__ import annotations

import io
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, Engine, EngineError, RenderRequest
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.pages import ImageStamp, LabelStyle, PageLabelRule, TextStamp


@pytest.fixture
def ops(engine: Engine) -> Engine:
    if not engine.capabilities.page_ops:
        pytest.skip("engine lacks page operations")
    return engine


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


def _first_line(doc, index: int) -> str:
    return doc.page(index).text_page(with_chars=False).blocks[0].lines[0].text


def test_reorder_keeps_bookmarks_links_labels(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(ops, fixture_pdf, tmp_path, "outline")
    doc.select_pages([5, 4, 3, 2, 1, 0])
    assert _first_line(doc, 0).startswith("Chapter 3, section 2")
    roots = doc.outline()
    assert roots[0].title == "Chapter 1" and roots[0].dest.page_index == 5
    # the link that pointed to old page 3 (now at index 2) moved with page 0 (now index 5)
    (goto,) = (ln for ln in doc.page(5).links() if ln.dest is not None)
    assert goto.dest.page_index == 2
    assert doc.page_label_rules() == [PageLabelRule(0, LabelStyle.ROMAN_LOWER)]
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 6


def test_delete_prunes_dangling_bookmarks(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(ops, fixture_pdf, tmp_path, "outline")
    doc.select_pages([0, 1, 4, 5])  # drop chapter 2's pages
    titles = [i.title for i in doc.outline()]
    assert titles == ["Chapter 1", "Chapter 3"]
    assert doc.outline()[1].dest.page_index == 2
    with pytest.raises(EngineError):
        doc.select_pages([])
    with pytest.raises(EngineError):
        doc.select_pages([9])
    doc.close()


def test_duplicate_insert_blank_and_images(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(ops, fixture_pdf, tmp_path, "text_multipage")
    doc.select_pages([0, 0, 1, 2, 3, 4])
    assert doc.page_count == 6 and _first_line(doc, 1) == _first_line(doc, 0)
    doc.insert_blank_page(1, 300, 400)
    assert doc.page_count == 7 and doc.page(1).rect == Rect(0, 0, 300, 400)
    doc.insert_blank_page(99, 200, 200)  # past the end = append
    assert doc.page(doc.page_count - 1).rect.width == 200
    buf = io.BytesIO()
    Image.new("RGB", (192, 96), (255, 0, 0)).save(buf, format="PNG")
    doc.insert_image_page(0, buf.getvalue())
    assert doc.page(0).rect.width == pytest.approx(192 * 72 / 96, abs=1)
    with pytest.raises(EngineError):
        doc.insert_image_page(0, b"not an image")
    doc.save()
    doc.close()
    pd = pdfium.PdfDocument(path)
    assert len(pd) == 9
    pd.close()


def _objgens(items) -> set[tuple[int, int]]:
    return {o.objgen for o in items}


def _contents(page: pikepdf.Page) -> set[tuple[int, int]]:
    contents = page.obj.Contents
    return _objgens(contents) if isinstance(contents, pikepdf.Array) else {contents.objgen}


def test_duplicate_is_an_independent_copy(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    """Issue #49: editing one duplicate (content or annotations) must not change the other."""
    doc, path = _open(ops, fixture_pdf, tmp_path, "annotations")
    notes = doc.page(0).annotations()
    doc.select_pages([0, 0])
    copy = doc.page(1)
    assert len(copy.annotations()) == len(notes)
    copy.stamp_text(TextStamp("ONLY ON THE COPY", Point(72, 800)))
    copy.delete_annotation(copy.annotations()[0].id)
    moved = copy.annotations()[0]
    copy.update_annotation(replace(moved, rect=moved.rect.translated(5, 5), contents="moved"))
    assert "ONLY ON THE COPY" not in doc.page(0).text_page(with_chars=False).text
    assert doc.page(0).annotations() == notes
    doc.save()
    doc.close()

    doc = ops.open(path)
    assert "ONLY ON THE COPY" in doc.page(1).text_page(with_chars=False).text
    assert "ONLY ON THE COPY" not in doc.page(0).text_page(with_chars=False).text
    assert [(a.type, a.rect, a.contents) for a in doc.page(0).annotations()] == [
        (a.type, a.rect, a.contents) for a in notes
    ]
    assert len(doc.page(1).annotations()) == len(notes) - 1
    assert len({a.name for p in (0, 1) for a in doc.page(p).annotations()}) == 2 * len(notes) - 1
    doc.close()

    with pikepdf.open(path) as pdf:
        first, second = pdf.pages
        assert first.objgen != second.objgen
        assert not _contents(first) & _contents(second)
        assert not _objgens(first.obj.Annots) & _objgens(second.obj.Annots)
        for page in (first, second):
            for annot in page.obj.Annots:
                assert "/P" not in annot or annot.P.objgen == page.objgen
                if "/Popup" in annot:  # the popup pairs up within the same page
                    assert annot.Popup.objgen in _objgens(page.obj.Annots)
                    assert annot.Popup.Parent.objgen == annot.objgen
    pd = pdfium.PdfDocument(path)
    try:
        texts = [pd[i].get_textpage().get_text_range() for i in range(len(pd))]
        assert "ONLY ON THE COPY" not in texts[0] and "ONLY ON THE COPY" in texts[1]
        for i in range(len(pd)):
            assert pd[i].render(scale=0.5).to_pil().size[0] > 0
    finally:
        pd.close()


def test_duplicate_shares_images_and_fonts(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    """Copies get their own page and content streams but not another copy of the images."""
    doc, path = _open(ops, fixture_pdf, tmp_path, "images")
    before = path.stat().st_size
    doc.select_pages([0, 0, 0])
    doc.page(2).stamp_text(TextStamp("third", Point(72, 72)))
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        images = [{x.objgen for x in page.obj.Resources.XObject.values()} for page in pdf.pages]
        assert images[0] and images[0] == images[1] == images[2]
        assert len({page.objgen for page in pdf.pages}) == 3
        streams = [_contents(page) for page in pdf.pages]
        assert not streams[0] & streams[1] and not streams[1] & streams[2]
        image_bytes = sum(len(x.read_raw_bytes()) for x in pdf.pages[0].Resources.XObject.values())
    assert path.stat().st_size < before + image_bytes // 2


def test_insert_pages_from_other_document(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(ops, fixture_pdf, tmp_path, "text_multipage")
    other = ops.open(fixture_pdf("annotations"))
    doc.insert_pages(other, [0], at=2)
    assert doc.page_count == 6
    assert len([a for a in doc.page(2).annotations()]) >= 5  # annotations came along
    other.close()
    doc.close()


def test_rotation_and_crop(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(ops, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(0)
    w, h = page.rect.width, page.rect.height
    page.set_rotation(90)
    page = doc.page(0)
    assert page.rotation == 90 and (page.rect.width, page.rect.height) == (h, w)
    with pytest.raises(EngineError):
        page.set_rotation(45)
    # crop in visible space on the already-cropped landscape page
    land = doc.page(4)
    word = land.text_page().blocks[0].lines[0].bbox
    land.set_crop(Rect(word.x0 - 10, word.y0 - 10, word.x0 + 200, word.y0 + 100))
    land = doc.page(4)
    assert land.rect.width == pytest.approx(210, abs=1) and land.rect.height == pytest.approx(
        110, abs=1
    )
    moved = land.text_page().blocks[0].lines[0].bbox
    assert moved.x0 == pytest.approx(10, abs=1) and moved.y0 == pytest.approx(10, abs=1)
    with pytest.raises(EngineError):
        land.set_crop(Rect(5000, 5000, 6000, 6000))
    doc.save()
    doc.close()


@pytest.mark.parametrize("page_index", [0, 1, 2], ids=["rot0", "rot90", "rot180"])
def test_text_stamp_is_upright_and_placed(
    ops: Engine, fixture_pdf, tmp_path: Path, page_index: int
) -> None:
    doc, _ = _open(ops, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(page_index)
    page.stamp_text(TextStamp("STAMPED", Point(100, 200), font_size=20, color=Color(1, 0, 0)))
    line = next(
        ln
        for b in doc.page(page_index).text_page().blocks
        for ln in b.lines
        if "STAMPED" in ln.text
    )
    assert line.direction[0] == pytest.approx(1, abs=1e-3)  # horizontal in the visible page
    assert line.bbox.x0 == pytest.approx(100, abs=2) and line.bbox.y1 == pytest.approx(200, abs=8)
    # 45° watermark reads upward to the right
    page = doc.page(page_index)
    page.stamp_text(TextStamp("DIAGONAL", Point(100, 500), font_size=30, angle=45, opacity=0.3))
    diag = next(
        ln
        for b in doc.page(page_index).text_page().blocks
        for ln in b.lines
        if "DIAGONAL" in ln.text
    )
    dx, dy = diag.direction
    assert dx == pytest.approx(0.707, abs=0.02) and dy == pytest.approx(-0.707, abs=0.02)
    doc.close()


def test_background_behind_content_and_image_stamp(
    ops: Engine, fixture_pdf, tmp_path: Path
) -> None:
    doc, _ = _open(ops, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(1)  # rotated 90
    page.fill_background(Color(0.8, 1, 0.8))
    img = doc.page(1).render(RenderRequest(color=ColorMode.RGB, clip=Rect(400, 500, 401, 501)))
    assert tuple(img.samples[:3]) == (204, 255, 204)
    tp = doc.page(1).text_page(with_chars=False)
    assert "Rotation 90" in tp.text  # text still drawn on top
    # an image that's red on its left half must appear red on the left in the visible page
    buf = io.BytesIO()
    im = Image.new("RGB", (100, 50), (0, 0, 255))
    im.paste((255, 0, 0), (0, 0, 50, 50))
    im.save(buf, format="PNG")
    page = doc.page(1)
    page.stamp_image(ImageStamp(buf.getvalue(), Rect(100, 100, 300, 200)))
    arr = doc.page(1).render(RenderRequest(color=ColorMode.RGB, clip=Rect(100, 100, 300, 200)))
    a = np.frombuffer(arr.samples, np.uint8).reshape(arr.height, arr.stride)[:, : arr.width * 3]
    a = a.reshape(arr.height, arr.width, 3)
    left, right = a[50, 20], a[50, 180]
    assert left[0] > 200 and left[2] < 80 and right[2] > 200 and right[0] < 80
    doc.close()


def test_label_rules_roundtrip(ops: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(ops, fixture_pdf, tmp_path, "text_multipage")
    doc.set_page_label_rules(
        [
            PageLabelRule(0, LabelStyle.ROMAN_LOWER),
            PageLabelRule(2, LabelStyle.DECIMAL, prefix="A-", first=1),
        ]
    )
    assert [doc.page_label(i) for i in range(5)] == ["i", "ii", "A-1", "A-2", "A-3"]
    doc.save()
    doc.close()
    again = ops.open(path)
    assert again.page_label_rules()[1] == PageLabelRule(2, LabelStyle.DECIMAL, "A-", 1)
    again.close()


def test_text_width(ops: Engine) -> None:
    assert ops.text_width("", "helv", 10) == 0
    assert ops.text_width("MMMM", "helv", 20) > ops.text_width("iiii", "helv", 20) > 0
    # characters outside WinAnsi are measured too (a bullet used to shorten the line)
    assert ops.text_width("• Material", "Helvetica", 11.5) > ops.text_width(
        "Material", "helv", 11.5
    )
    assert ops.text_width("• Material", "Helvetica", 11.5) == pytest.approx(48.1, abs=0.1)
