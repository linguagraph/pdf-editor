"""MuPDF backend: duplicated pages are separate objects (issue #49)."""

from __future__ import annotations

import re
from pathlib import Path

import pikepdf
import pymupdf
import pypdfium2 as pdfium

from pdfeditor.engine.mupdf.document import MuDocument, open_document

_REF = re.compile(r"(\d+) 0 R")


def _refs(fz: pymupdf.Document, xref: int, key: str) -> set[int]:
    return {int(r) for r in _REF.findall(fz.xref_get_key(xref, key)[1])}


def _open(path: Path) -> MuDocument:
    doc = open_document(path, None)
    assert isinstance(doc, MuDocument)
    return doc


def test_copy_owns_what_edits_write_to(fixture_pdf) -> None:
    doc = _open(fixture_pdf("annotations"))
    doc.select_pages([0, 0])
    fz = doc.fz
    a, b = fz.page_xref(0), fz.page_xref(1)
    assert a != b
    assert not _refs(fz, a, "Contents") & _refs(fz, b, "Contents")
    assert fz.xref_get_key(a, "Resources") != fz.xref_get_key(b, "Resources")
    annots_a, annots_b = _refs(fz, a, "Annots"), _refs(fz, b, "Annots")
    assert len(annots_a) == len(annots_b) and not annots_a & annots_b
    appearances_a = {x for annot in annots_a for x in _refs(fz, annot, "AP")}
    appearances_b = {x for annot in annots_b for x in _refs(fz, annot, "AP")}
    appearances_a |= {r for annot in annots_a for r in _refs(fz, annot, "AP/N")}
    appearances_b |= {r for annot in annots_b for r in _refs(fz, annot, "AP/N")}
    assert appearances_b and not appearances_a & appearances_b
    for annot in annots_b:
        assert fz.xref_get_key(annot, "P")[1] == f"{b} 0 R"
        popup = _refs(fz, annot, "Popup")
        assert popup <= annots_b
    # decoded content is identical, so the copy renders the same
    assert doc.page(0).text_page(with_chars=False).text == (
        doc.page(1).text_page(with_chars=False).text
    )
    doc.close()


def test_inherited_attributes_and_form_fields(tmp_path: Path) -> None:
    src = pymupdf.open()
    page = src.new_page(width=400, height=300)
    page.insert_text((50, 50), "form page")
    field = pymupdf.Widget()
    field.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    field.field_name = "name"
    field.field_value = "Ada"
    field.rect = pymupdf.Rect(50, 100, 250, 130)
    page.add_widget(field)
    # move MediaBox and Resources up to the page tree root: the copy must still have them
    pages_root = int(src.xref_get_key(src.pdf_catalog(), "Pages")[1].split()[0])
    pxref = src.page_xref(0)
    for key in ("MediaBox", "Resources"):
        src.xref_set_key(pages_root, key, src.xref_get_key(pxref, key)[1])
        src.xref_set_key(pxref, key, "null")
    path = tmp_path / "form.pdf"
    src.save(path)
    src.close()

    doc = _open(path)
    doc.select_pages([0, 0])
    assert doc.page(1).rect.width == 400 and "form page" in doc.page(1).text_page().text
    doc.save()
    doc.close()

    with pikepdf.open(path) as pdf:
        names = [str(f.T) for f in pdf.Root.AcroForm.Fields]
        assert len(names) == 2 and len(set(names)) == 2 and names[0] == "name"
        assert [len(p.obj.Annots) for p in pdf.pages] == [1, 1]
        assert pdf.pages[0].obj.Annots[0].objgen != pdf.pages[1].obj.Annots[0].objgen
    pd = pdfium.PdfDocument(path)
    try:
        assert [round(pd[i].get_width()) for i in range(len(pd))] == [400, 400]
        assert "form page" in pd[1].get_textpage().get_text_range()
    finally:
        pd.close()


def test_reorder_and_delete_keep_the_catalog(tmp_path: Path) -> None:
    """MuPDF's page selection rebuilds the catalog; forms, language, etc. must survive it."""
    src = pymupdf.open()
    for n in range(3):
        src.new_page().insert_text((72, 72), f"page {n}")
    field = pymupdf.Widget()
    field.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    field.field_name = "agree"
    field.rect = pymupdf.Rect(72, 100, 90, 118)
    src[2].add_widget(field)
    catalog = src.pdf_catalog()
    src.xref_set_key(catalog, "Lang", pymupdf.get_pdf_str("de-CH"))
    src.xref_set_key(catalog, "MarkInfo", "<</Marked true>>")
    src.xref_set_key(catalog, "ViewerPreferences", "<</DisplayDocTitle true>>")
    src.xref_set_key(catalog, "PageMode", "/UseOutlines")
    path = tmp_path / "catalog.pdf"
    src.save(path)
    src.close()

    doc = _open(path)
    doc.select_pages([2, 0, 1])  # move
    doc.select_pages([0, 1])  # delete
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        root = pdf.Root
        assert str(root.Lang) == "de-CH" and bool(root.MarkInfo.Marked)
        assert bool(root.ViewerPreferences.DisplayDocTitle) and root.PageMode == "/UseOutlines"
        assert [str(f.T) for f in root.AcroForm.Fields] == ["agree"]
        assert len(pdf.pages) == 2 and len(pdf.pages[0].obj.Annots) == 1
