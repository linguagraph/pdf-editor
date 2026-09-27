from __future__ import annotations

import io

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, RenderRequest, SaveOptions
from pdfeditor.engine.mupdf import standard_font_code
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.geometry import Matrix
from pdfeditor.services.compare import diff_regions
from pdfeditor.services.pdfa import convert_to_pdfa, preflight, validate_with_verapdf

ENGINE = get_engine()
FONTS = ENGINE.standard_font_program


def data_of(fixture_pdf, name: str, password: str | None = None) -> bytes:
    doc = ENGINE.open(fixture_pdf(name), password)
    try:
        return doc.to_bytes(SaveOptions(decrypt=True))
    finally:
        doc.close()


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def gray(data: bytes, index: int) -> Image.Image:
    doc = ENGINE.open(data)
    s = doc.page(index).render(RenderRequest(matrix=Matrix.scale(2), color=ColorMode.GRAY))
    doc.close()
    return Image.frombytes("L", (s.width, s.height), s.samples, "raw", "L", s.stride)


def test_standard_font_names() -> None:
    assert standard_font_code("Helvetica-BoldOblique") == "hebi"
    assert standard_font_code("ABCDEF+ArialMT") == "helv"
    assert standard_font_code("TimesNewRomanPS-BoldMT") == "tibo"
    assert standard_font_code("CourierNewPSMT") == "cour"
    assert standard_font_code("ZapfDingbats") == "zadb"
    assert standard_font_code("Calibri") is None
    assert FONTS("Helvetica") and FONTS("Calibri") is None


def test_preflight_report(fixture_pdf) -> None:
    issues = preflight(data_of(fixture_pdf, "report"), FONTS)
    assert codes(issues) == {"output-intent", "identification", "fonts"}
    assert all(i.fixable for i in issues)
    fonts = next(i for i in issues if i.code == "fonts")
    assert "Helvetica-Bold" in fonts.message and fonts.count == 2


def test_convert_report(fixture_pdf) -> None:
    original = data_of(fixture_pdf, "report")
    result = convert_to_pdfa(original, FONTS)
    assert result.conforming and preflight(result.data, FONTS) == []
    assert any("embedded fonts" in f for f in result.fixed)
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        meta = pdf.open_metadata()
        assert meta["pdfaid:part"] == "2" and meta["pdfaid:conformance"] == "B"
        assert meta["dc:title"] == "report"  # Info and XMP agree
        assert pdf.pdf_version == "1.7" and not pdf.is_encrypted
        (intent,) = pdf.Root.OutputIntents
        assert intent.S == "/GTS_PDFA1" and intent.DestOutputProfile.N == 3
        for page in pdf.pages:
            for font in page.Resources.Font.values():
                fd = font.FontDescriptor
                assert fd.FontFile3.Subtype == "/Type1C"
                assert len(font.Widths) == int(font.LastChar) - int(font.FirstChar) + 1
    # same look: MuPDF draws base-14 fonts with the programs we embedded
    for i in range(2):
        assert diff_regions(gray(original, i), gray(result.data, i), 2) == []
    # an independent reader sees the same text at the same places (widths are right)
    a, b = (
        pdfium.PdfDocument(original)[0].get_textpage(),
        pdfium.PdfDocument(result.data)[0].get_textpage(),
    )
    assert a.get_text_range() == b.get_text_range()
    shifts = [abs(a.get_charbox(k)[0] - b.get_charbox(k)[0]) for k in range(a.count_chars())]
    assert max(shifts) < 1.0


def test_convert_removes_forbidden_features(fixture_pdf) -> None:
    before = preflight(data_of(fixture_pdf, "sensitive"), FONTS)
    assert {"javascript", "embedded-files", "annotation-flags"} <= codes(before)
    result = convert_to_pdfa(data_of(fixture_pdf, "sensitive"), FONTS)
    assert result.conforming
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        assert "/OpenAction" not in pdf.Root
        assert "/EmbeddedFiles" not in pdf.Root.get("/Names", {})
        for a in pdf.pages[0].obj.get("/Annots", []):
            if a.get("/Subtype") != "/Popup":  # popups are exempt from the print flag
                assert int(a.F) & 4 and not int(a.F) & 2
    # sensitive.pdf also carries an empty <x:xmpmeta/> packet: replaced, not a crash
    assert any("valid XMP" in f for f in result.fixed)


def test_unfixable_font_is_reported(fixture_pdf) -> None:
    result = convert_to_pdfa(data_of(fixture_pdf, "cjk_text"), FONTS)
    assert not result.conforming
    (issue,) = result.remaining
    assert issue.code == "fonts-unfixable" and not issue.fixable and "Heiti" in issue.message


def test_encrypted_source(fixture_pdf) -> None:
    data = data_of(fixture_pdf, "encrypted", "owner")
    assert "encryption" in codes(preflight(data, FONTS, encrypted=True))
    result = convert_to_pdfa(data, FONTS)
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        assert not pdf.is_encrypted


def test_verapdf_is_optional(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("pdfeditor.services.pdfa.find_verapdf", lambda: None)
    with pytest.raises(RuntimeError, match="veraPDF"):
        validate_with_verapdf(tmp_path / "x.pdf")
