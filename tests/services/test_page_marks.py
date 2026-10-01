"""Headers/footers, Bates numbers, watermarks and backgrounds can be updated and removed
(issues #39, #40) instead of piling up a new copy each time."""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.color import Color
from pdfeditor.model.pages import MarkKind
from pdfeditor.services.stamping import (
    HeaderFooter,
    Slot,
    Watermark,
    apply_background,
    apply_header_footer,
    apply_watermark,
    background_from_settings,
    background_settings,
    content_bounds,
    find_marks,
    header_footer_from_settings,
    header_footer_settings,
    remove_marks,
    watermark_from_settings,
    watermark_settings,
)

ENGINE = get_engine()
pytestmark = pytest.mark.skipif(not ENGINE.capabilities.page_marks, reason="no page marks")


@pytest.fixture
def path(fixture_pdf, tmp_path: Path) -> Path:
    dst = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), dst)
    return dst


def _save_reopen(doc, path: Path):
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:  # opens in an independent implementation
        assert len(pdf.pages) == 5
    return ENGINE.open(path)


def _pdfium_texts(path: Path) -> list[str]:
    pd = pdfium.PdfDocument(path)
    try:
        out = []
        for page in pd:
            page.render(scale=0.3)
            out.append(page.get_textpage().get_text_range())
        return out
    finally:
        pd.close()


def test_settings_round_trip() -> None:
    hf = HeaderFooter(
        texts={Slot.HEADER_LEFT: "Ünïcode <<page>>", Slot.FOOTER_RIGHT: "<<bates>>"},
        font="tibo",
        font_size=11.5,
        color=Color(0.2, 0.4, 0.6),
        margin_x=40,
        margin_top=30,
        margin_bottom=30,
        bates_prefix="ABC-",
        bates_start=17,
        bates_digits=5,
        bates_suffix="-Z",
    )
    assert header_footer_from_settings(header_footer_settings(hf)) == hf
    wm = Watermark(text="DRAFT", font_size=80, opacity=0.25, angle=30, on_top=False)
    assert watermark_from_settings(watermark_settings(wm)) == wm
    image = Watermark(image=b"png bytes", image_path="C:/logo.png", scale=0.4)
    loaded = watermark_from_settings(watermark_settings(image))
    assert loaded is not None and loaded.image is None and loaded.image_path == "C:/logo.png"
    assert background_from_settings(background_settings(Color(1, 0.5, 0), 0.5)) == (
        Color(1, 0.5, 0),
        0.5,
    )
    assert header_footer_from_settings("not json") is None
    assert header_footer_from_settings(watermark_settings(wm)) is None  # wrong kind


def test_header_footer_update_and_remove_round_trip(path: Path) -> None:
    doc = ENGINE.open(path)
    old = HeaderFooter(texts={Slot.HEADER_CENTER: "OLD-HEADER <<page>>"})
    apply_header_footer(ENGINE, doc, old, [0, 1, 2], "doc.pdf")
    doc = _save_reopen(doc, path)

    info = find_marks(ENGINE, doc)[MarkKind.HEADER_FOOTER]
    assert info.pages == [0, 1, 2] and not info.foreign
    assert header_footer_from_settings(info.settings) == old

    # Update: remove what's there, then draw the new settings (one undoable step in the UI)
    new = HeaderFooter(texts={Slot.FOOTER_LEFT: "NEW-FOOTER <<page>>/<<pages>>"})
    assert remove_marks(ENGINE, doc, [MarkKind.HEADER_FOOTER]) == 3
    apply_header_footer(ENGINE, doc, new, [0, 1, 2, 3, 4], "doc.pdf")
    doc = _save_reopen(doc, path)
    for index in range(5):
        text = doc.page(index).text_page(with_chars=False).text
        assert "OLD-HEADER" not in text
        assert text.count(f"NEW-FOOTER {index + 1}/5") == 1
        assert f"needle-{index + 1}" in text  # the page's own content is untouched
    info = find_marks(ENGINE, doc)[MarkKind.HEADER_FOOTER]
    assert info.pages == [0, 1, 2, 3, 4]
    assert header_footer_from_settings(info.settings) == new
    texts = _pdfium_texts(path)
    assert all(t.count("NEW-FOOTER") == 1 and "OLD-HEADER" not in t for t in texts)

    assert remove_marks(ENGINE, doc, [MarkKind.HEADER_FOOTER]) == 5
    doc = _save_reopen(doc, path)
    assert find_marks(ENGINE, doc) == {}
    for index in range(5):
        text = doc.page(index).text_page(with_chars=False).text
        assert "NEW-FOOTER" not in text and f"Page {index + 1} heading" in text
    doc.close()
    assert all("NEW-FOOTER" not in t and "needle" in t for t in _pdfium_texts(path))


def test_bates_header_watermark_and_background_are_independent(path: Path) -> None:
    doc = ENGINE.open(path)
    bates = HeaderFooter(texts={Slot.FOOTER_RIGHT: "<<bates>>"}, bates_prefix="DOC")
    apply_header_footer(ENGINE, doc, bates, range(5), kind=MarkKind.BATES)
    apply_header_footer(ENGINE, doc, HeaderFooter(texts={Slot.HEADER_LEFT: "HDR"}), [0])
    apply_watermark(ENGINE, doc, Watermark(text="WM-ONE"), [1, 2])
    apply_background(doc, Color(0.8, 0.9, 1), [3], engine=ENGINE)
    doc = _save_reopen(doc, path)
    found = find_marks(ENGINE, doc)
    assert {k: v.pages for k, v in found.items()} == {
        MarkKind.BATES: [0, 1, 2, 3, 4],
        MarkKind.HEADER_FOOTER: [0],
        MarkKind.WATERMARK: [1, 2],
        MarkKind.BACKGROUND: [3],
    }
    assert background_from_settings(found[MarkKind.BACKGROUND].settings) == (
        Color(0.8, 0.9, 1),
        1.0,
    )

    # update the watermark: the old one is replaced, not stacked
    remove_marks(ENGINE, doc, [MarkKind.WATERMARK])
    apply_watermark(ENGINE, doc, Watermark(text="WM-TWO"), [1, 2])
    for index in (1, 2):
        text = doc.page(index).text_page(with_chars=False).text
        assert "WM-ONE" not in text and text.count("WM-TWO") == 1
        assert f"DOC{index + 1:06d}" in text
    # remove the Bates numbers only
    remove_marks(ENGINE, doc, [MarkKind.BATES])
    remove_marks(ENGINE, doc, [MarkKind.BACKGROUND])
    doc = _save_reopen(doc, path)
    assert set(find_marks(ENGINE, doc)) == {MarkKind.HEADER_FOOTER, MarkKind.WATERMARK}
    assert "DOC000001" not in doc.page(0).text_page(with_chars=False).text
    assert "HDR" in doc.page(0).text_page(with_chars=False).text
    assert content_bounds(doc, 3) is not None
    bounds = content_bounds(doc, 3)
    assert bounds is not None and bounds.width < doc.page(3).rect.width - 50  # untinted again
    doc.close()
    texts = _pdfium_texts(path)
    assert "DOC000002" not in texts[1] and texts[1].count("WM-TWO") == 1
