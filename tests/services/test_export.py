from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fontTools.ttLib import TTFont
from PIL import Image

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.text import Block, Char, FontFlags, Line, Span
from pdfeditor.services.export import (
    TextFormat,
    export_document,
    find_tables,
    tables_to_xlsx,
)
from pdfeditor.services.export.fonts import metric_twin, obfuscate
from pdfeditor.services.export.images import (
    ImageFormat,
    PageImageOptions,
    export_pages,
    extract_fonts,
    extract_images,
)
from pdfeditor.services.export.language import Languages, guess_cyrillic
from pdfeditor.services.export.layout import (
    Frame,
    LayoutPage,
    _crossed,
    _fit_spacing,
    analyze_layout,
)
from pdfeditor.services.export.office import convert_to_pdf, find_soffice
from pdfeditor.services.export.ooxml import cell_number, sheet_names, word_font
from pdfeditor.services.export.structure import (
    PageBreak,
    Paragraph,
    Picture,
    Run,
    StructureOptions,
    Table,
    analyze,
    base_font_name,
    is_page_number,
    main_size,
)

ENGINE = get_engine()
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@pytest.fixture
def report(fixture_pdf):
    doc = ENGINE.open(fixture_pdf("report"))
    yield doc
    doc.close()


def test_structure_of_report(report) -> None:
    elements = analyze(report, [0, 1])
    headings = [(e.level, e.text) for e in elements if isinstance(e, Paragraph) and e.level]
    assert headings == [
        (1, "Quarterly Report"),
        (2, "Summary"),
        (2, "Figures"),
        (2, "Chart"),
        (2, "Appendix"),
    ]
    table = next(e for e in elements if isinstance(e, Table))
    assert table.rows[0] == ["Region", "Units", "Revenue"] and len(table.rows) == 4
    # table cell text is not repeated as paragraphs
    assert not any(isinstance(e, Paragraph) and "North" in e.text for e in elements)
    pic = next(e for e in elements if isinstance(e, Picture))
    assert pic.png.startswith(b"\x89PNG") and pic.width == pytest.approx(240)
    order = [e.text if isinstance(e, Paragraph) else type(e).__name__ for e in elements]
    assert order.index("Figures") < order.index("Table") < order.index("Chart")
    assert order.index("Chart") < order.index("Picture") < order.index("End of report.")
    assert order.index("PageBreak") < order.index("Appendix")
    summary = next(e for e in elements if isinstance(e, Paragraph) and "Sales grew" in e.text)
    assert "\n" not in summary.text and summary.runs[0].font == "Helvetica"


def test_base_font_name() -> None:
    assert base_font_name("ABCDEF+Arial-BoldMT") == "Arial"
    assert base_font_name("TimesNewRomanPSMT") == "TimesNewRoman"
    assert base_font_name("Helvetica") == "Helvetica"


def test_export_text_markdown_html(report, tmp_path: Path) -> None:
    txt = export_document(report, [0, 1], tmp_path / "r", TextFormat.TEXT)
    text = txt.read_text(encoding="utf-8")
    assert txt.suffix == ".txt" and "Quarterly Report" in text and text.count("\f") == 1

    md = export_document(report, [0, 1], tmp_path / "r", TextFormat.MARKDOWN)
    body = md.read_text(encoding="utf-8")
    assert "# Quarterly Report" in body and "## Figures" in body
    assert "| Region | Units | Revenue |" in body and "| --- | --- | --- |" in body
    assert "![Image 1](r_images/image-1.png)" in body
    assert (tmp_path / "r_images" / "image-1.png").exists()

    html = export_document(report, [0], tmp_path / "r", TextFormat.HTML).read_text("utf-8")
    assert "<h1>Quarterly Report</h1>" in html and "<th>Region</th>" in html
    assert "data:image/png;base64," in html and "<title>report</title>" in html


def test_export_word(report, tmp_path: Path) -> None:
    path = export_document(report, [0, 1], tmp_path / "r", TextFormat.WORD)
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        names = set(z.namelist())
        assert {"[Content_Types].xml", "word/document.xml", "word/media/image1.png"} <= names
        for name in names:
            if name.endswith((".xml", ".rels")):
                ET.fromstring(z.read(name))  # well-formed
        doc = ET.fromstring(z.read("word/document.xml"))
    paras = doc.iter(f"{W}p")
    styles = [
        (p.find(f"{W}pPr/{W}pStyle").get(f"{W}val"), "".join(t.text or "" for t in p.iter(f"{W}t")))
        for p in paras
        if p.find(f"{W}pPr/{W}pStyle") is not None
    ]
    assert ("Heading1", "Quarterly Report") in styles and ("Heading2", "Appendix") in styles
    table = doc.find(f".//{W}tbl")
    assert table is not None and len(table.findall(f"{W}tr")) == 4
    assert len(table.findall(f"{W}tblGrid/{W}gridCol")) == 3
    assert doc.find(f".//{W}drawing") is not None
    assert doc.find(f".//{W}br[@{W}type='page']") is not None
    fonts = {f.get(f"{W}ascii") for f in doc.iter(f"{W}rFonts")}
    assert "Arial" in fonts  # Helvetica maps to a font Word has


def test_tables_to_excel(report, tmp_path: Path) -> None:
    tables = find_tables(report, [0, 1])
    assert len(tables) == 1 and tables[0].page_index == 0
    path = tables_to_xlsx(tables + tables, tmp_path / "t")
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    assert [s.get("name") for s in wb.iter(f"{S}sheet")] == ["Page 1", "Page 1 (2)"]
    cells = {c.get("r"): c for c in sheet.iter(f"{S}c")}
    assert cells["A1"].find(f"{S}is/{S}t").text == "Region" and cells["A1"].get("s") == "1"
    assert cells["C2"].find(f"{S}v").text == "1440" and cells["C4"].find(f"{S}v").text == "2520.75"
    assert cells["A2"].get("t") == "inlineStr"
    with pytest.raises(ValueError):
        tables_to_xlsx([], tmp_path / "none")


def test_cell_numbers_and_sheet_names() -> None:
    assert cell_number("1,440.50") == 1440.5 and cell_number("-3") == -3
    assert cell_number("007") is None and cell_number("12a") is None
    assert cell_number("0.5") == 0.5 and cell_number("1,23") is None
    assert sheet_names(["a/b", "A_b", "x" * 40]) == ["a_b", "A_b (2)", "x" * 31]


def test_pages_to_images(report, tmp_path: Path) -> None:
    pngs = export_pages(report, [0, 1], tmp_path / "p.png", PageImageOptions(dpi=72))
    assert [p.name for p in pngs] == ["p-1.png", "p-2.png"]
    with Image.open(pngs[0]) as img:
        assert img.size == (595, 842) and img.mode == "RGB"
    one = export_pages(
        report,
        [1],
        tmp_path / "single",
        PageImageOptions(ImageFormat.JPEG, dpi=36, grayscale=True),
    )
    assert [p.name for p in one] == ["single.jpg"]
    with Image.open(one[0]) as img:
        assert img.mode == "L" and img.format == "JPEG"
    tif = export_pages(report, [0, 1], tmp_path / "all", PageImageOptions(ImageFormat.TIFF, 50))
    with Image.open(tif[0]) as img:
        assert img.n_frames == 2 and tif[0].name == "all.tif"


def test_extract_images_and_fonts(fixture_pdf, report, tmp_path: Path) -> None:
    images = extract_images(report, tmp_path / "img")
    assert [p.name for p in images] == ["page1-image1.png"]
    with Image.open(images[0]) as img:
        assert img.size == (320, 200)
    assert extract_images(report, tmp_path / "none", pages=[1]) == []
    written, skipped = extract_fonts(report, tmp_path / "fonts")
    assert written == [] and set(skipped) == {"Helvetica", "Helvetica-Bold"}
    doc = ENGINE.open(fixture_pdf("subset_fonts"))
    written, _ = extract_fonts(doc, tmp_path / "fonts")
    doc.close()
    assert written and all(p.stat().st_size > 1000 for p in written)
    assert all("+" not in p.name for p in written)


def test_office_conversion_without_libreoffice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("pdfeditor.services.export.office.find_soffice", lambda: None)
    with pytest.raises(RuntimeError, match="LibreOffice"):
        convert_to_pdf(tmp_path / "a.docx")


@pytest.mark.skipif(find_soffice() is None, reason="LibreOffice not installed")
def test_office_conversion(tmp_path: Path) -> None:
    src = tmp_path / "note.txt"
    src.write_text("Hello from LibreOffice", encoding="utf-8")
    doc = ENGINE.open(convert_to_pdf(src))
    assert "Hello from LibreOffice" in doc.page(0).text_page(with_chars=False).text
    doc.close()


def test_structure_of_print_to_pdf_output(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("print_to_pdf"))
    elements = analyze(doc, [0])
    doc.close()
    order = [e.text if isinstance(e, Paragraph) else type(e).__name__ for e in elements]
    # the page number is dropped, so the picture stays between its neighbours
    assert "1" not in order
    assert order.index("Бутон за температурата на пералнята.") < order.index("Picture")
    assert order.index("Picture") < len(order) - 1
    paras = [e for e in elements if isinstance(e, Paragraph)]
    # the embedded program's own name replaces "CIDFont+F1"
    assert {r.font for p in paras for r in p.runs} == {"Droid Sans Fallback"}
    assert paras[0].level == 1
    bullet = next(p for p in paras if p.text.startswith("•"))
    assert bullet.level == 0  # a larger bullet doesn't make body text a heading


def test_word_export_language_fonts_and_tabs(fixture_pdf, tmp_path: Path) -> None:
    doc = ENGINE.open(fixture_pdf("print_to_pdf"))
    path = export_document(doc, [0], tmp_path / "p", TextFormat.WORD)
    doc.close()
    with zipfile.ZipFile(path) as z:
        styles = ET.fromstring(z.read("word/styles.xml"))
        settings = ET.fromstring(z.read("word/settings.xml"))
        body = ET.fromstring(z.read("word/document.xml"))
        assert 'Target="settings.xml"' in z.read("word/_rels/document.xml.rels").decode()
        assert "/word/settings.xml" in z.read("[Content_Types].xml").decode()
    # Bulgarian proofing, not the user's default language
    lang = styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr/{W}lang")
    assert lang is not None and lang.get(f"{W}val") == "bg-BG"
    mode = settings.find(f"{W}compat/{W}compatSetting")
    assert mode is not None and mode.get(f"{W}val") == "15"
    assert {f.get(f"{W}ascii") for f in body.iter(f"{W}rFonts")} == {"Droid Sans Fallback"}
    columns = next(p for p in body.iter(f"{W}p") if p.find(f".//{W}tab") is not None)
    assert [t.text for t in columns.iter(f"{W}t")] == ["Предпране", "Забавено стартиране"]


def test_languages() -> None:
    bulgarian = "Натиснете бутона, за да изберете цикъл на пране. Бутон за температурата."
    assert guess_cyrillic(bulgarian) == "bg-BG"
    assert guess_cyrillic("Выберите режим стирки, это займёт минуту.") == "ru-RU"
    assert guess_cyrillic("Україна має багато історичних міст.") == "uk-UA"
    langs = Languages(bulgarian + " PUSH & GO")
    assert langs.default == "bg-BG"
    assert langs.for_run("PUSH & GO") == "en-US" and langs.for_run("цикъл") == ""
    assert langs.for_run("12:30") == ""
    latin = Languages("Plain English text with a Кириллица word")
    assert latin.default == "" and latin.for_run("Кириллица") == "ru-RU"
    declared = Languages("Hallo Welt", "de-DE")
    assert declared.default == "de-DE" and declared.for_run("Hallo") == ""


def test_word_font_names() -> None:
    assert word_font("Helvetica") == "Arial"
    assert word_font("SegoeUI") == "Segoe UI" and word_font("Segoe UI") == "Segoe UI"
    assert word_font("DejaVuSans") == "DejaVu Sans"
    assert base_font_name("CIDFont+F1") == "F1"
    assert base_font_name("Segoe UI-Bold") == "Segoe UI"


def test_main_size_and_page_numbers() -> None:
    assert main_size([Run("•", 14), Run(" body text", 10)]) == 10
    assert main_size([Run("ab", 12), Run("cd", 10)]) == 10  # a tie stays body text

    def block(text: str, y0: float) -> Block:
        box = Rect(100, y0, 120, y0 + 10)
        span = Span(text, box, "F1", 9, Color(0, 0, 0), FontFlags(0), Point(100, y0 + 8))
        return Block(bbox=box, lines=(Line(spans=(span,), bbox=box),))

    assert is_page_number(block("12", 800), 842) and is_page_number(block("- 3 -", 10), 842)
    assert not is_page_number(block("12", 400), 842)
    assert not is_page_number(block("Page 12", 800), 842)


def _text(frame: Frame) -> str:
    return "\n".join("".join(r.text for r in ln.runs) for ln in frame.lines)


def _frame_texts(page: LayoutPage) -> dict[str, Frame]:
    return {"\n".join("".join(r.text for r in ln.runs) for ln in f.lines): f for f in page.frames}


def test_layout_keeps_positions_and_art(report) -> None:
    first, second = analyze_layout(report, [0, 1], measure=ENGINE.text_width)
    assert (first.width, first.height) == pytest.approx((595, 842))
    texts = _frame_texts(first)
    assert texts["Quarterly Report"].rect.x0 == pytest.approx(72, abs=1)
    # the ruled table is a real table at its place: 4 rows x 3 columns, borders as drawn
    [table] = first.tables
    assert table.ruled and len(table.cells) == 12
    assert table.xs == pytest.approx([72, 212, 352, 492], abs=1)
    assert table.ys == pytest.approx([245, 267, 289, 311, 333], abs=1)
    north = next(c for c in table.cells if c.content and "North" in _text(c.content))
    assert (north.row, north.col) == (1, 0) and north.content.rect.x0 == pytest.approx(77, abs=1)
    assert all(north.borders[side] is not None for side in ("top", "left", "bottom", "right"))
    assert "North" not in texts  # not also a loose frame
    [(area, pic)] = first.pictures
    assert (area.x0, area.y0, area.x1, area.y1) == pytest.approx((72, 385, 312, 535), abs=0.5)
    assert pic.png.startswith(b"\x89PNG")
    # the rules became the table's borders: nothing else is drawn on either page
    assert first.background is None and second.background is None


def test_layout_background_keeps_vector_art(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("vector_art"))
    [page] = analyze_layout(doc, [0])
    doc.close()
    assert page.background is not None
    with Image.open(io.BytesIO(page.background.png)) as bg:
        assert bg.mode == "RGBA"  # transparent where nothing is drawn: can't hide pictures
        assert bg.getextrema()[3][0] == 0


def test_inline_boxes_are_not_tables() -> None:
    def piece(text: str, x0: float, x1: float, y0: float) -> Line:
        box = Rect(x0, y0, x1, y0 + 10)
        span = Span(text, box, "F", 10, Color(0, 0, 0), FontFlags(0), Point(x0, y0 + 8))
        return Line((span,), box)

    box = Rect(100, 100, 160, 112)
    assert _crossed(box, [piece("see the code here", 50, 200, 101)])
    assert not _crossed(box, [piece("code", 105, 150, 101), piece("above", 50, 200, 80)])


def test_layout_splits_space_aligned_columns(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("print_to_pdf"))
    [page] = analyze_layout(doc, [0], background=False)
    doc.close()
    texts = _frame_texts(page)
    assert texts["Забавено стартиране"].rect.x0 > texts["Предпране"].rect.x1 + 20
    assert "1" in texts  # the page keeps its page number where it was
    assert texts["Предпране"].lines[0].runs[0].font == "Droid Sans Fallback"


def test_fit_spacing_reproduces_letter_and_word_spacing() -> None:
    # "ab cd": natural advances 5pt, set with 1pt letter spacing and 3pt more on the space
    xs = [0.0, 6.0, 12.0, 21.0, 27.0]
    chars = [Char(c, Rect(x, 0, x + 5, 10), Point(x, 8)) for c, x in zip("ab cd", xs, strict=True)]
    span = Span("ab cd", Rect(0, 0, 32, 10), "F", 10, Color(0, 0, 0), FontFlags(0),
                Point(0, 8), tuple(chars))  # fmt: skip
    line = Line((span,), span.bbox)

    class Widths:
        def __init__(self, advance: float | None) -> None:
            self.advance = advance

        def advances(self, s: Span) -> list[float] | None:
            return None if self.advance is None else [self.advance] * len(s.text)

    runs = [Run("ab cd", 10)]
    assert _fit_spacing(line, runs, Widths(5.0), justified=False)  # type: ignore[arg-type]
    assert [(r.text, round(r.spacing, 2)) for r in runs] == [("ab", 1), (" ", 4), ("cd", 1)]
    # justified: Word stretches the spaces itself, only letter spacing is kept
    runs = [Run("ab cd", 10)]
    _fit_spacing(line, runs, Widths(5.0), justified=True)  # type: ignore[arg-type]
    assert [round(r.spacing, 2) for r in runs] == [1]
    # a cell edge the text must stay clear of tightens the spacing
    runs = [Run("ab cd", 10)]
    assert _fit_spacing(line, runs, Widths(5.0), False, limit=30.0)  # type: ignore[arg-type]
    assert runs[0].spacing < 1
    # a font that can't be measured leaves the text alone
    runs = [Run("ab cd", 10)]
    _fit_spacing(line, runs, Widths(None), justified=False)  # type: ignore[arg-type]
    assert runs[0].spacing == 0


def test_word_export_keeping_layout(report, tmp_path: Path) -> None:
    options = StructureOptions(keep_layout=True)
    path = export_document(report, [0, 1], tmp_path / "l", TextFormat.WORD, options)
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(z.read(name))
        body = ET.fromstring(z.read("word/document.xml"))
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert len(media) == 1  # page 1's picture; the table's rules are Word borders
    # one section per page, at the PDF's page size
    sections = list(body.iter(f"{W}sectPr"))
    assert len(sections) == 2
    assert {s.find(f"{W}pgSz").get(f"{W}w") for s in sections} == {"11900"}
    frames = [f for f in body.iter(f"{W}framePr")]
    assert frames and all(f.get(f"{W}hAnchor") == "page" for f in frames)
    title = next(p for p in body.iter(f"{W}p") if "Quarterly" in "".join(p.itertext()))
    assert title.find(f"{W}pPr/{W}framePr").get(f"{W}x") == "1440"  # 72pt from the left
    # the table floats at its place with a fixed grid and its borders
    [table] = body.iter(f"{W}tbl")
    position = table.find(f"{W}tblPr/{W}tblpPr")
    assert (position.get(f"{W}tblpX"), position.get(f"{W}tblpY")) == ("1440", "4900")
    assert [c.get(f"{W}w") for c in table.iter(f"{W}gridCol")] == ["2800"] * 3
    assert len(table.findall(f"{W}tr")) == 4
    assert table.find(f".//{W}tcBorders/{W}top").get(f"{W}val") == "single"
    assert "North" in "".join(table.itertext())
    anchors = list(
        body.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor")
    )
    assert len(anchors) == 1 and anchors[0].get("behindDoc") == "1"


def test_word_export_embeds_fonts(fixture_pdf, tmp_path: Path) -> None:
    doc = ENGINE.open(fixture_pdf("print_to_pdf"))
    path = export_document(doc, [0], tmp_path / "f", TextFormat.WORD)
    doc.close()
    with zipfile.ZipFile(path) as z:
        table = ET.fromstring(z.read("word/fontTable.xml"))
        [font] = table.findall(f"{W}font")
        embed = font.find(f"{W}embedRegular")
        key = embed.get(f"{W}fontKey")
        data = z.read("word/fonts/font1.odttf")
        assert b"embedTrueTypeFonts" in z.read("word/settings.xml")
        assert b"obfuscatedFont" in z.read("[Content_Types].xml")
    assert font.get(f"{W}name") == "Droid Sans Fallback"
    program = obfuscate(data, key)  # the XOR is its own inverse
    assert program[:4] == b"\x00\x01\x00\x00"  # a TrueType program again
    names = TTFont(io.BytesIO(program))["name"]
    # the Windows names the PDF's subset lacked were added, so Windows loads it
    assert {r.nameID for r in names.names if r.platformID == 3} >= {1, 2, 3, 4, 6}


def test_metric_twins() -> None:
    assert word_font("NimbusSans") == "Arial" and word_font("Liberation Serif") == "Times New Roman"
    assert word_font("CourierNew") == "Courier New" and word_font("Noto Sans") == "Noto Sans"
    assert metric_twin("Helvetica") == "sans" and metric_twin("Segoe UI") is None


def test_page_break_only_between_pages(report) -> None:
    elements = analyze(report, [1])
    assert not any(isinstance(e, PageBreak) for e in elements)
