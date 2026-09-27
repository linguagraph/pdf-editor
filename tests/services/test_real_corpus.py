"""Real-world PDFs from ``tests/fixtures/real/`` through the app's Protocols and services.

Each file is optional: a missing one skips its tests (see the README there for sources).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.core.commands import AddAnnotationCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import ColorMode, RenderRequest
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.services.accessibility import Status, check
from pdfeditor.services.ocr import OcrOptions, apply, installed_languages, recognize
from pdfeditor.services.pdfa import preflight

ENGINE = get_engine()
REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"

FORM = "irs_fw9.pdf"
SCAN = "scan_navy_letter_1953.pdf"
SIGNED = "signed_selfsigned.pdf"
# file -> (words its text layer must contain; None: no text layer, it's an image scan)
CORPUS: dict[str, tuple[str, ...] | None] = {
    FORM: ("Request for Taxpayer", "Identification Number"),
    SCAN: None,
    SIGNED: ("Signed sample agreement",),
}


def real(name: str) -> Path:
    path = REAL / name
    if not path.exists():
        pytest.skip(f"real-world sample {name} is not available")
    return path


def working_copy(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copyfile(real(name), target)
    return target


def note(page: int = 0) -> AnnotationModel:
    return AnnotationModel(
        AnnotationType.TEXT, page, Rect(20, 20, 40, 40), contents="Reviewed", author="Tester"
    )


def is_blank(samples: bytes) -> bool:
    return min(samples) == max(samples)


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_open_render_and_extract(name: str) -> None:
    doc = ENGINE.open(real(name))
    try:
        assert doc.page_count > 0
        for i in range(doc.page_count):
            page = doc.page(i)
            result = page.render(RenderRequest(matrix=Matrix.scale(0.5), color=ColorMode.GRAY))
            assert result.width > 0 and not is_blank(result.samples), f"page {i} is blank"
        text = " ".join(doc.page(i).text_page(with_chars=False).text for i in range(doc.page_count))
        expected = CORPUS[name]
        if expected is None:
            assert text.strip() == ""
        else:
            flat = " ".join(text.split())
            for words in expected:
                assert words in flat
    finally:
        doc.close()


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_accessibility_and_pdfa_checks_run(name: str) -> None:
    path = real(name)
    doc = ENGINE.open(path)
    try:
        findings = check(doc)
    finally:
        doc.close()
    assert findings and {f.rule for f in findings} >= {"tagged", "language"}
    issues = preflight(path.read_bytes(), ENGINE.standard_font_program)
    assert all(i.code and i.message for i in issues)


def test_tagged_form_accessibility_settings() -> None:
    """fw9 stores /Lang and /DisplayDocTitle as indirect objects: both must still be read."""
    doc = ENGINE.open(real(FORM))
    try:
        settings = doc.accessibility_settings()
        assert settings.language == "en-US" and settings.display_doc_title
        rules = {f.rule: f for f in check(doc)}
        assert rules["tagged"].status is Status.PASSED
        assert rules["language"].status is Status.PASSED
        assert rules["display-title"].status is Status.PASSED
        assert doc.structure_tree()
    finally:
        doc.close()


def acroform_fields(path: Path) -> dict[str, str]:
    """Fully qualified field name -> field type, read independently of MuPDF."""
    out: dict[str, str] = {}

    def walk(field: pikepdf.Dictionary, prefix: str, inherited: str) -> None:
        name = str(field.get("/T", ""))
        full = f"{prefix}.{name}" if prefix and name else (name or prefix)
        ftype = str(field.get("/FT", inherited))
        kids = field.get("/Kids")
        if kids is not None and any("/T" in k for k in kids):
            for kid in kids:
                walk(kid, full, ftype)
        else:
            out[full] = ftype

    with pikepdf.open(path) as pdf:
        for field in pdf.Root.AcroForm.Fields:
            walk(field, "", "")
    return out


def test_form_fields_survive_save(tmp_path: Path) -> None:
    source = real(FORM)
    before = acroform_fields(source)
    assert len(before) > 10 and "/Tx" in before.values() and "/Btn" in before.values()
    with pikepdf.open(source) as pdf:
        assert "/XFA" in pdf.Root.AcroForm

    session = DocumentSession.open(source)
    assert session.document.info().has_forms
    session.execute(AddAnnotationCommand(note()))
    out = session.save(tmp_path / "fw9-annotated.pdf")  # save-as: a full rewrite
    session.close()

    assert acroform_fields(out) == before
    with pikepdf.open(out) as pdf:
        assert "/XFA" in pdf.Root.AcroForm  # never silently dropped or flattened
        widgets = [a for p in pdf.pages for a in p.get("/Annots", []) if a.Subtype == "/Widget"]
        assert len(widgets) >= len(before)
    reopened = ENGINE.open(out)
    try:
        assert reopened.info().has_forms
        assert [a.contents for a in reopened.page(0).annotations()] == ["Reviewed"]
    finally:
        reopened.close()
    pdf = pdfium.PdfDocument(str(out))
    try:
        assert len(pdf) == 6
        bitmap = pdf[0].render(scale=0.5, may_draw_forms=True).to_pil()
        assert bitmap.getextrema() != ((255, 255), (255, 255), (255, 255))
    finally:
        pdf.close()


def test_form_with_usage_rights_saves_incrementally(tmp_path: Path) -> None:
    """fw9 carries a /Perms /UR3 usage-rights signature and SigFlags AppendOnly: saving it in
    place appends an update, so the original revision (and Reader's usage rights) stay intact."""
    path = working_copy(FORM, tmp_path)
    original = path.read_bytes()
    with pikepdf.open(path) as pdf:
        assert "/UR3" in pdf.Root.Perms and int(pdf.Root.AcroForm.SigFlags) & 2
    session = DocumentSession.open(path)
    session.execute(AddAnnotationCommand(note()))
    session.save()
    session.close()
    assert path.read_bytes().startswith(original)
    assert acroform_fields(path) == acroform_fields(real(FORM))


def test_form_renders_its_widgets(tmp_path: Path) -> None:
    """A ticked checkbox's appearance stream (the form's own, not a synthesized one) is drawn
    with annotations on and hidden with them off; the empty form renders like the bare page."""
    path = working_copy(FORM, tmp_path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        box = next(a for a in pdf.pages[0].Annots if a.get("/FT") == "/Btn")
        on = next(k for k in box.AP.N if k != "/Off")
        box.AS = pikepdf.Name(on)
        pdf.save(path)

    def render(annotations: bool) -> bytes:
        request = RenderRequest(Matrix.scale(1), color=ColorMode.GRAY, annotations=annotations)
        return doc.page(0).render(request).samples

    doc = ENGINE.open(real(FORM))
    try:
        assert render(True) == render(False)  # nothing filled in: widgets draw nothing
    finally:
        doc.close()
    doc = ENGINE.open(path)
    try:
        assert render(True) != render(False)
    finally:
        doc.close()


def test_signature_survives_annotation_save(tmp_path: Path) -> None:
    path = working_copy(SIGNED, tmp_path)
    original = path.read_bytes()
    with pikepdf.open(path) as pdf:
        sig = pdf.Root.AcroForm.Fields[0].V
        byte_range = [int(x) for x in sig.ByteRange]
        contents = bytes(sig.Contents)

    session = DocumentSession.open(path)
    assert session.document.info().has_signatures
    session.execute(AddAnnotationCommand(note()))
    session.save()  # in place: must be an incremental update
    session.close()

    saved = path.read_bytes()
    assert saved.startswith(original) and len(saved) > len(original)
    with pikepdf.open(path) as pdf:
        sig = pdf.Root.AcroForm.Fields[0].V
        assert [int(x) for x in sig.ByteRange] == byte_range
        assert bytes(sig.Contents) == contents
    reopened = ENGINE.open(path)
    try:
        assert reopened.info().has_signatures
        assert [a.contents for a in reopened.page(0).annotations()] == ["Reviewed"]
    finally:
        reopened.close()
    pdf = pdfium.PdfDocument(str(path))
    try:
        assert len(pdf) == 1
        pdf[0].render(scale=0.5).to_pil()
    finally:
        pdf.close()

    validation = pytest.importorskip("pyhanko.sign.validation")
    reader_mod = pytest.importorskip("pyhanko.pdf_utils.reader")
    with path.open("rb") as fh:
        (embedded,) = reader_mod.PdfFileReader(fh).embedded_signatures
        context = pytest.importorskip("pyhanko_certvalidator").ValidationContext(
            trust_roots=[embedded.signer_cert]  # self-signed test certificate
        )
        status = validation.validate_pdf_signature(embedded, context)
        assert status.intact and status.valid and status.trusted  # signed bytes unchanged
        assert status.coverage.name == "ENTIRE_REVISION"  # our update is a later revision


@pytest.mark.skipif("eng" not in installed_languages(), reason="English OCR data not installed")
def test_ocr_reads_the_scan(tmp_path: Path) -> None:
    doc = ENGINE.open(real(SCAN))
    try:
        apply(doc, recognize(doc, [0], OcrOptions(dpi=300)))
        text = " ".join(doc.page(0).text_page(with_chars=False).text.split())
        for words in ("DEPARTMENT OF THE NAVY", "BUREAU OF SHIPS", "Dear Dr. Purdy", "laboratory"):
            assert words in text
        assert doc.page(0).search("Sincerely yours")
        out = tmp_path / "ocr.pdf"
        doc.save(out)
    finally:
        doc.close()
    with pikepdf.open(out):
        pass
    pdf = pdfium.PdfDocument(str(out))
    try:
        assert "BUREAU OF SHIPS" in pdf[0].get_textpage().get_text_range()
    finally:
        pdf.close()
