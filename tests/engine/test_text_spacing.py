"""Character spacing (Tc) and word spacing (Tw) survive text editing."""

from __future__ import annotations

import shutil
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from statistics import median

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.engine.base import Engine
from pdfeditor.engine.contentstream.parser import HexString, Name, Operation, parse
from pdfeditor.engine.contentstream.spacing import LineSpacing, Spacing, apply_spacing
from pdfeditor.engine.textspacing import detect_spacing, spaced_text
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import Align, ObjectType, TextStyle
from pdfeditor.model.text import Block, Char, FontFlags, Line, Span

HEADING = "Tracked heading"
LINE = "Spaced words in one line"
PARAGRAPH = "A spaced paragraph with\nseveral lines of text that\nall share the spacing."
CENTERED = "Centered spaced line"


@pytest.fixture
def editable(engine: Engine) -> Engine:
    if not engine.capabilities.content_edit:
        pytest.skip("engine lacks content editing")
    return engine


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str = "letter_spacing"):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


def _texts(page) -> dict[str, object]:
    return {o.text: o for o in page.content_objects() if o.type is ObjectType.TEXT}


def _glyphs(page, area: Rect) -> list[list[Char]]:
    """Drawn glyphs (no invented spaces) of each text line inside ``area``, top to bottom."""
    out = []
    for block in page.text_page(with_chars=True).blocks:
        for line in block.lines:
            if area.contains(line.bbox.center):
                out.append([c for s in line.spans for c in s.chars if not c.synthetic])
    return sorted(out, key=lambda chars: chars[0].origin.y)


def _gaps(chars: list[Char]) -> tuple[list[float], list[float]]:
    letter, word = [], []
    for a, b in pairwise(chars):
        gap = b.origin.x - a.origin.x - a.bbox.width
        (word if a.c == " " else letter).append(gap)
    return letter, word


# -- detection ------------------------------------------------------------------------------------
def test_detects_spacing_of_each_block(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path)
    found = _texts(doc.page(0))
    # extraction reports the tracked heading as "T r a c k e d ..."; editing shows the real text
    assert set(found) == {HEADING, LINE, PARAGRAPH, CENTERED}
    expected = {HEADING: (2, 0), LINE: (0.6, 4), PARAGRAPH: (0.4, 2), CENTERED: (0.5, 3)}
    for text, (tc, tw) in expected.items():
        style = found[text].style
        assert style.char_spacing == pytest.approx(tc, abs=0.02), text
        assert style.word_spacing == pytest.approx(tw, abs=0.02), text
    doc.close()


def test_plain_text_has_no_spacing(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path, "mixed_content")
    for obj in _texts(doc.page(0)).values():
        assert obj.style.char_spacing == 0 and obj.style.word_spacing == 0
    doc.close()


def _block(text: str, tc: float, tw: float, advance: float = 5.0) -> Block:
    chars, x = [], 10.0
    for c in text:
        chars.append(Char(c, Rect(x, 0, x + advance, 10), Point(x, 8)))
        x += advance + tc + (tw if c == " " else 0)
    span = Span(text, Rect(10, 0, x, 10), "F", 10, Color(0, 0, 0), FontFlags(0), Point(10, 8),
                tuple(chars))  # fmt: skip
    return Block(span.bbox, (Line((span,), span.bbox),))


def test_detect_spacing_unit() -> None:
    assert detect_spacing(_block("abc def ghi", 0, 0)) == (0, 0)
    assert detect_spacing(_block("abc def ghi", 1.5, 0)) == (1.5, 0)
    assert detect_spacing(_block("abc def ghi", 0.5, 3)) == (0.5, 3)
    assert detect_spacing(_block("abc def ghi", -0.3, 0)) == (-0.3, 0)
    assert detect_spacing(_block("ab", 2, 0)) == (0, 0)  # too little evidence


def test_spaced_text_drops_only_letter_gaps() -> None:
    def char(c: str, x0: float, x1: float, synthetic: bool = False) -> Char:
        return Char(c, Rect(x0, 0, x1, 10), Point(x0, 8), synthetic)

    chars = (
        char("a", 0, 5), char(" ", 5, 7, True), char("b", 7, 12),  # letter gap (2pt)
        char(" ", 12, 20, True), char("c", 20, 25),  # a real word gap MuPDF made up a space for
    )  # fmt: skip
    span = Span("a b c", Rect(0, 0, 25, 10), "F", 10, Color(0, 0, 0), FontFlags(0), Point(0, 8),
                chars)  # fmt: skip
    block = Block(span.bbox, (Line((span,), span.bbox),))
    assert spaced_text(block, 2.0) == "ab c"
    assert spaced_text(block, 0.0) == "a b c"


# -- re-typesetting -------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [HEADING, LINE, PARAGRAPH, CENTERED])
def test_same_text_lands_on_the_same_glyph_positions(
    editable: Engine, fixture_pdf, tmp_path: Path, text: str
) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    obj = _texts(page)[text]
    area = Rect(obj.bbox.x0 - 5, obj.bbox.y0 - 3, obj.bbox.x1 + 60, obj.bbox.y1 + 3)
    before = _glyphs(page, area)
    page.replace_text(obj.key, obj.text)

    def check(page) -> None:
        after = _glyphs(page, area)
        assert ["".join(c.c for c in ln) for ln in after] == [
            "".join(c.c for c in ln) for ln in before
        ]
        for old, new in zip(before, after, strict=True):
            for a, b in zip(old, new, strict=True):
                assert b.origin.x == pytest.approx(a.origin.x, abs=1.0), (a.c, text)
                assert b.origin.y == pytest.approx(a.origin.y, abs=1.0), (a.c, text)

    check(doc.page(0))
    edited = _texts(doc.page(0))[text]
    assert edited.style.char_spacing == pytest.approx(obj.style.char_spacing, abs=0.02)
    assert edited.style.word_spacing == pytest.approx(obj.style.word_spacing, abs=0.02)
    # round trip: save, reopen, independent readers
    doc.save()
    doc.close()
    doc = editable.open(path)
    check(doc.page(0))
    doc.close()
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 1
    pdf = pdfium.PdfDocument(path)
    try:
        textpage = pdf[0].get_textpage()
        assert text.split("\n")[0] in textpage.get_text_range().replace("\r", "")
        assert pdf[0].render(scale=0.5).to_pil().size[0] > 0
    finally:
        pdf.close()


def test_edited_text_keeps_the_spacing(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    obj = _texts(page)[LINE]
    new = "Spaced words in a much longer line"
    page.replace_text(obj.key, new)
    edited = _texts(doc.page(0))[new]
    assert edited.style.char_spacing == pytest.approx(0.6, abs=0.02)
    assert edited.style.word_spacing == pytest.approx(4, abs=0.02)
    assert "\n" not in edited.text  # a one-line label stays one line
    (line,) = _glyphs(doc.page(0), edited.bbox)
    letter, word = _gaps(line)
    assert median(letter) == pytest.approx(0.6, abs=0.05)
    assert median(word) == pytest.approx(4.6, abs=0.05)
    assert line[0].origin.x == pytest.approx(72, abs=1)
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 1


def test_move_keeps_the_spacing(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    from pdfeditor.model.geometry import Matrix

    doc, _ = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    obj = _texts(page)[HEADING]
    page.transform_objects([obj.key], Matrix.translate(10, 400))
    moved = _texts(doc.page(0))[HEADING]  # not "T r a c k e d ..."
    assert moved.style.char_spacing == pytest.approx(2, abs=0.02)
    assert moved.bbox.x0 == pytest.approx(obj.bbox.x0 + 10, abs=1)
    doc.close()


@pytest.mark.parametrize("align", [Align.LEFT, Align.CENTER, Align.RIGHT, Align.JUSTIFY])
def test_add_spaced_text_aligns(editable: Engine, fixture_pdf, tmp_path: Path, align) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(1)  # /Rotate 90: spacing is applied along the visible baseline
    box = Rect(60, 60, 260, 300)
    style = TextStyle(size=10, align=align, char_spacing=1.0, word_spacing=2.0)
    text = "Letter spaced words that wrap over a few lines in a narrow box.\nEnd."
    page.add_text(box, text, style)
    lines = _glyphs(doc.page(1), Rect(box.x0 - 5, box.y0 - 5, box.x1 + 5, box.y1 + 5))
    assert len(lines) >= 3
    for i, line in enumerate(lines):
        letter, word = _gaps(line)
        assert median(letter) == pytest.approx(1.0, abs=0.05)
        left, right = line[0].bbox.x0, line[-1].bbox.x1
        assert right <= box.x1 + 1  # nothing spills out of the box
        last_of_paragraph = i >= len(lines) - 2
        if align is Align.LEFT or (align is Align.JUSTIFY and last_of_paragraph):
            assert left == pytest.approx(box.x0, abs=2)
        elif align is Align.CENTER:
            assert (left + right) / 2 == pytest.approx(box.center.x, abs=1.5)
        elif align is Align.RIGHT:
            assert right == pytest.approx(box.x1, abs=2)
        else:  # justified: fills the box
            assert left == pytest.approx(box.x0, abs=2) and right == pytest.approx(box.x1, abs=2)
        if align is not Align.JUSTIFY and word:
            assert median(word) == pytest.approx(3.0, abs=0.05)
    doc.close()


# -- content-stream rewriting (pure) --------------------------------------------------------------
def test_apply_spacing_rewrites_text_operators() -> None:
    ops = parse(
        b"q BT /F1 10 Tf 1 0 0 -1 0 20 Tm [<000100020003000200010002>] TJ 0 -12 Td [<0001>] TJ ET Q"
    )
    space = b"\x00\x02"
    spacing = Spacing(0.5, 2.0, space, (LineSpacing(shift=3.0), LineSpacing(fill=1.0)))
    out = apply_spacing(ops, spacing)
    assert out[2] == Operation("Tc", [0.5])  # right after BT
    first, second = (op for op in out if op.operator == "TJ")
    assert first.operands[0] == [
        -300.0,  # the shift, in thousandths of the 10pt font size
        HexString(b"\x00\x01\x00\x02"),
        -200.0,
        HexString(b"\x00\x03\x00\x02"),
        -200.0,
        HexString(b"\x00\x01\x00\x02"),
        -200.0,
    ]
    assert second.operands[0] == [HexString(b"\x00\x01")]
    assert isinstance(first.operands[0][1], HexString)  # the string form is kept


def test_apply_spacing_ignores_line_plan_that_does_not_match() -> None:
    ops = parse(b"BT /F1 10 Tf 2 0 0 -2 0 0 Tm [<0001>] TJ [<0001>] TJ ET")
    out = apply_spacing(ops, Spacing(1.0, 0.0, None, (LineSpacing(shift=5),)))
    assert out[1] == Operation("Tc", [0.5])  # text space is scaled 2x
    assert [op.operands[0] for op in out if op.operator == "TJ"] == [
        [HexString(b"\x00\x01")],
        [HexString(b"\x00\x01")],
    ]
    # text in another (fallback) font gets no word spacing: its space code differs
    ops = parse(b"BT /F1 10 Tf [<0002>] TJ /F2 10 Tf [<00020002>] TJ ET")
    out = apply_spacing(ops, Spacing(0.0, 1.0, b"\x00\x02"))
    shown = [op.operands[0] for op in out if op.operator == "TJ"]
    assert shown == [[HexString(b"\x00\x02"), -100.0], [HexString(b"\x00\x02\x00\x02")]]
    assert out[2] == Operation("Tf", [Name("F1"), 10])


def test_style_defaults_have_no_spacing() -> None:
    style = TextStyle()
    assert style.char_spacing == 0 and style.word_spacing == 0
    assert replace(style, char_spacing=1).char_spacing == 1
