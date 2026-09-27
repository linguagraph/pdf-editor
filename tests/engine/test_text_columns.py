"""Editing text laid out in columns (regression tests from user testing, Phase E)."""

from __future__ import annotations

import io

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.engine.registry import get_engine
from pdfeditor.engine.textlayout import editable_blocks, is_tabular, split_line
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import ObjectType, TextStyle, family_of
from pdfeditor.model.text import Block, Char, FontFlags, Line, Span

ENGINE = get_engine()


def texts(page) -> dict[str, object]:
    return {o.text: o for o in page.content_objects() if o.type is ObjectType.TEXT}


@pytest.fixture
def doc(fixture_pdf):
    d = ENGINE.open(fixture_pdf("two_columns"))
    yield d
    d.close()


def _line(text: str, x0: float, y0: float = 10, advance: float = 5) -> Line:
    chars = []
    x = x0
    for c in text:
        chars.append(Char(c, Rect(x, y0, x + advance, y0 + 10), Point(x, y0 + 8)))
        x += advance
    span = Span(text, Rect(x0, y0, x, y0 + 10), "F", 10, Color(0, 0, 0), FontFlags(0),
                Point(x0, y0 + 8), tuple(chars))  # fmt: skip
    return Line((span,), span.bbox)


def test_split_line_at_gutter_keeps_word_spaces() -> None:
    parts = split_line(_line("ab cd" + " " * 12 + "ef", 0))
    assert [p.text for p in parts] == ["ab cd", "ef"]
    assert parts[1].bbox.x0 == pytest.approx(85)
    assert split_line(_line("ab cd", 0))[0].text == "ab cd"
    assert split_line(_line("ab   ", 0))[0].text == "ab"  # trailing blanks trimmed


def test_tabular_detection() -> None:
    assert is_tabular([_line("left", 0), _line("right", 100)])
    assert not is_tabular([_line("one", 0, 10), _line("two", 0, 22)])
    paragraph = Block(Rect(0, 10, 30, 32), (_line("one", 0, 10), _line("two", 0, 22)))
    assert [b.text for b in editable_blocks([paragraph])] == ["one\ntwo"]


def test_columns_are_separate_paragraphs(doc) -> None:
    found = texts(doc.page(0))
    for label in ("Бързо", "Заключване на вратата", "Предпране", "Старт/Пауза"):
        assert label in found, label
    left, right = found["Бързо"], found["Заключване на вратата"]
    assert left.bbox.x1 < 80 < 200 < right.bbox.x0  # no gutter inside either box
    # a normal wrapped paragraph is still one editable block
    assert any(t.startswith("Продължителността") and t.count("\n") == 2 for t in found)


def test_edit_one_column_leaves_the_other(doc, tmp_path) -> None:
    page = doc.page(0)
    before = texts(page)
    right_box = before["Заключване на вратата"].bbox
    choice = page.replace_text(before["Бързо"].key, "Бързо и лесно")
    assert choice is not None and choice.embedded_reused and not choice.substituted
    after = texts(page)
    assert "Бързо и лесно" in after  # stays one line (the box widened)
    edited = after["Бързо и лесно"]
    assert edited.bbox.x0 == pytest.approx(40, abs=1.5)
    assert edited.bbox.y0 == pytest.approx(before["Бързо"].bbox.y0, abs=2.5)
    assert edited.style.color.to_hex() == before["Бързо"].style.color.to_hex()
    right = after["Заключване на вратата"]
    assert right.bbox.x0 == pytest.approx(right_box.x0, abs=0.5)  # untouched
    # a second edit still finds the embedded font (it was renamed by the first)
    again = page.replace_text(edited.key, "Бързо")
    assert again is not None and again.embedded_reused and not again.substituted
    out = doc.save(tmp_path / "edited.pdf")
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 1
    text = pdfium.PdfDocument(out)[0].get_textpage().get_text_range()
    assert "Бързо" in text and "Заключване на вратата" in text


def test_unchanged_text_keeps_spaces_and_position(doc) -> None:
    page = doc.page(0)
    obj = texts(page)["Допълнително вода"]
    page.replace_text(obj.key, obj.text)
    now = texts(page)["Допълнително вода"]
    assert now.bbox.x0 == pytest.approx(obj.bbox.x0, abs=1)
    assert now.bbox.width == pytest.approx(obj.bbox.width, rel=0.05)


def test_style_changes(doc) -> None:
    page = doc.page(0)
    obj = texts(page)["Предпране"]
    style = obj.style
    bold = TextStyle(style.font, 14, Color(0.8, 0, 0), True, False, style.align, style.line_height)
    choice = page.replace_text(obj.key, "Предпране", bold)
    # the document font has no bold face: a standard bold face is used and reported
    assert choice is not None and choice.substituted and "Bold" in choice.name
    now = texts(page)["Предпране"]
    assert now.style.bold and now.style.size == pytest.approx(14)
    assert now.style.color.r > 0.7
    serif = TextStyle("Times-Roman", 10, Color(0, 0, 0))
    obj = texts(page)["Старт/Пауза"]
    choice = page.replace_text(obj.key, obj.text, serif)
    assert choice is not None and not choice.substituted
    assert family_of(texts(page)["Старт/Пауза"].style.font) == "serif"


def test_font_program_for_preview(doc) -> None:
    page = doc.page(0)
    obj = texts(page)["Бързо"]
    data = page.font_program(obj.style.font)
    assert data is not None and len(data) > 10_000
    assert page.font_program("NoSuchFont") is None


def test_repeated_edits_do_not_drift(doc) -> None:
    page = doc.page(0)
    start = texts(page)["Старт/Пауза"].bbox
    current = "Старт/Пауза"
    for n in range(4):
        new = f"Старт/Пауза {n}"
        page.replace_text(texts(page)[current].key, new)
        current = new
        box = texts(page)[current].bbox
        assert box.y0 == pytest.approx(start.y0, abs=0.3)
        assert box.x0 == pytest.approx(start.x0, abs=0.3)


def _font_with_duplicate_mapping() -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "parenleft"])
    fb.setupCharacterMap({0x28: "parenleft", 0xFD3E: "parenleft"})
    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((100, 700))
    pen.lineTo((300, 700))
    pen.closePath()
    glyph = pen.glyph()
    fb.setupGlyf({".notdef": glyph, "parenleft": glyph})
    fb.setupHorizontalMetrics({".notdef": (500, 100), "parenleft": (400, 100)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Dup", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    out = io.BytesIO()
    fb.save(out)
    return out.getvalue()


def test_single_codepoint_cmap() -> None:
    from fontTools.ttLib import TTFont

    from pdfeditor.engine.mupdf.content import _single_codepoint_cmap

    fixed = _single_codepoint_cmap(_font_with_duplicate_mapping(), {"("})
    cmap = TTFont(io.BytesIO(fixed)).getBestCmap()
    assert cmap == {0x28: "parenleft"}  # "(" copies as "(", not the ornate U+FD3E
