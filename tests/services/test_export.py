from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from pdfeditor.engine.registry import get_engine
from pdfeditor.services.export import (
    TextFormat,
    export_document,
    find_tables,
    tables_to_xlsx,
)
from pdfeditor.services.export.images import (
    ImageFormat,
    PageImageOptions,
    export_pages,
    extract_fonts,
    extract_images,
)
from pdfeditor.services.export.office import convert_to_pdf, find_soffice
from pdfeditor.services.export.ooxml import cell_number, sheet_names
from pdfeditor.services.export.structure import (
    PageBreak,
    Paragraph,
    Picture,
    Table,
    analyze,
    base_font_name,
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


def test_page_break_only_between_pages(report) -> None:
    elements = analyze(report, [1])
    assert not any(isinstance(e, PageBreak) for e in elements)
