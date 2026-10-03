"""Engine support for typesetting with an explicit font file (``FontRef.FILE``): F3.

Covers choosing the font (``content._font_for``), subsetting on save, stamps, and keeping the
font ref's family on a later edit of the same block.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pymupdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.core.commands.snapshot import SnapshotCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import Engine, UnsupportedFeature
from pdfeditor.model.fonts import FontRef
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import ObjectType, TextStyle
from pdfeditor.model.pages import TextStamp


@pytest.fixture
def editable(engine: Engine) -> Engine:
    if not engine.capabilities.content_edit:
        pytest.skip("engine lacks content editing")
    return engine


@pytest.fixture
def fonts_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "fonts"


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str = "mixed_content"):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


def _by_type(page, t: ObjectType):
    return [o for o in page.content_objects() if o.type is t]


def _embedded_fontinfo(doc, name_contains: str = ""):
    return [
        f
        for f in doc.fonts()
        if f.embedded and (not name_contains or name_contains.lower() in f.name.lower())
    ]


def test_replace_text_with_file_font_embeds_and_subsets(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    body = _by_type(page, ObjectType.TEXT)[1]
    ref = FontRef.file(str(fonts_dir / "TestSans-Regular.ttf"), name="Test Sans")
    style = TextStyle(font="Test Sans", size=11, font_ref=ref)
    choice = page.replace_text(body.key, "ABC abc", style)
    assert choice is not None and not choice.substituted
    assert choice.name == "Test Sans"
    doc.save()
    doc.close()

    reopened = editable.open(path)
    try:
        texts = [o.text for o in _by_type(reopened.page(0), ObjectType.TEXT)]
        assert "ABC abc" in texts
        fonts = _embedded_fontinfo(reopened, "test sans")
        assert fonts, "the font file must be embedded"
        assert any(f.subset for f in fonts), "BaseFont should carry a subset tag"
    finally:
        reopened.close()

    with pikepdf.open(path):
        pass  # must open cleanly
    pdf = pdfium.PdfDocument(str(path))
    try:
        assert "ABC abc" in pdf[0].get_textpage().get_text_range()
    finally:
        pdf.close()


def test_missing_glyphs_substitute_and_report(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    doc, _path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    body = _by_type(page, ObjectType.TEXT)[1]
    # TestSans only has glyphs for "ABCabc"; "xyz" is missing.
    ref = FontRef.file(str(fonts_dir / "TestSans-Regular.ttf"), name="Test Sans")
    style = TextStyle(font="Test Sans", size=11, font_ref=ref)
    choice = page.replace_text(body.key, "xyz", style)
    assert choice is not None
    assert choice.substituted
    assert set(choice.missing) == {"x", "y", "z"}
    doc.close()


def test_restricted_font_raises_unsupported(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    doc, _path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    body = _by_type(page, ObjectType.TEXT)[1]
    ref = FontRef.file(str(fonts_dir / "TestRestricted-Regular.ttf"), name="Test Restricted")
    style = TextStyle(font="Test Restricted", size=11, font_ref=ref)
    with pytest.raises(UnsupportedFeature):
        page.replace_text(body.key, "ABC", style)
    doc.close()


def test_ttc_face_index_honored(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    body = _by_type(page, ObjectType.TEXT)[1]
    ref = FontRef.file(str(fonts_dir / "TestCollection.ttc"), index=1, name="Test Collection Two")
    style = TextStyle(font="Test Collection Two", size=11, font_ref=ref)
    choice = page.replace_text(body.key, "ABC abc", style)
    assert choice is not None and not choice.substituted
    doc.save()
    doc.close()

    reopened = editable.open(path)
    try:
        fonts = _embedded_fontinfo(reopened, "test collection two")
        assert fonts
    finally:
        reopened.close()


def test_later_edit_reuses_embedded_program(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    doc, _path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    body = _by_type(page, ObjectType.TEXT)[1]
    ref = FontRef.file(str(fonts_dir / "TestSans-Regular.ttf"), name="Test Sans")
    style = TextStyle(font="Test Sans", size=11, font_ref=ref)
    first = page.replace_text(body.key, "ABC abc", style)
    assert first is not None and not first.substituted

    page = doc.page(0)
    edited = _by_type(page, ObjectType.TEXT)[1]
    assert edited.style is not None and edited.style.font_ref is None
    # The block's own extracted style now names the embedded program (no font_ref needed):
    # a later edit must find and reuse it through font_program(), not fall back to a standard
    # font.
    second = page.replace_text(edited.key, "ABC abc abc", edited.style)
    assert second is not None
    assert second.embedded_reused and not second.substituted
    doc.close()


def test_stamp_with_file_font_round_trips(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    ref = FontRef.file(str(fonts_dir / "TestSans-Bold.ttf"), name="Test Sans")
    page.stamp_text(TextStamp("ABC", Point(300, 700), font_size=14, font_ref=ref))
    doc.save()
    doc.close()

    reopened = editable.open(path)
    try:
        texts = [o.text for o in _by_type(reopened.page(0), ObjectType.TEXT)]
        assert any("ABC" in t for t in texts)
        fonts = _embedded_fontinfo(reopened, "test sans")
        assert fonts
    finally:
        reopened.close()
    pdf = pdfium.PdfDocument(str(path))
    try:
        pdf[0].render(scale=0.5).to_pil()
    finally:
        pdf.close()


def test_large_font_save_subsets_small(
    editable: Engine, fixture_pdf, tmp_path: Path, fonts_dir: Path
) -> None:
    font_path = fonts_dir / "TestLarge-Regular.ttf"
    full_size = font_path.stat().st_size
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    ref = FontRef.file(str(font_path), name="Test Large")
    style = TextStyle(font="Test Large", size=11, font_ref=ref)
    # one of the glyphs this font covers (codepoints 0x100.. per scripts/make_fixtures.py)
    text = chr(0x101) + chr(0x102) + chr(0x103)
    page.add_text(Rect(72, 300, 300, 330), text, style)
    doc.save()
    doc.close()

    with pymupdf.open(path) as fz:
        xref = next(
            entry[0]
            for entry in fz.get_page_fonts(0)
            if "Test Large" in entry[3] or "TestLarge" in entry[3]
        )
        _name, _ext, _ftype, buffer = fz.extract_font(xref)
        assert buffer, "font must still be embedded after save"
        assert len(buffer) < full_size / 3, "a subset of 3 glyphs must be much smaller"

    with pikepdf.open(path):
        pass
    pdf = pdfium.PdfDocument(str(path))
    try:
        pdf[0].render(scale=0.5).to_pil()
    finally:
        pdf.close()


def test_incremental_save_of_signed_document_does_not_subset(
    tmp_path: Path, fonts_dir: Path, fixtures_dir: Path
) -> None:
    real = fixtures_dir.parent / "real" / "signed_selfsigned.pdf"
    if not real.exists():
        pytest.skip("real-world sample signed_selfsigned.pdf is not available")
    path = tmp_path / "signed.pdf"
    shutil.copy2(real, path)
    original = path.read_bytes()
    with pikepdf.open(path) as pdf:
        sig = pdf.Root.AcroForm.Fields[0].V
        byte_range = [int(x) for x in sig.ByteRange]
        contents = bytes(sig.Contents)

    session = DocumentSession.open(path)
    try:
        assert session.document.info().has_signatures
        ref = FontRef.file(str(fonts_dir / "TestSans-Regular.ttf"), name="Test Sans")

        def op(doc):
            page = doc.page(0)
            # a stamp (unlike a content edit) doesn't disturb the page objects pikepdf/MuPDF
            # need to keep an incremental save possible.
            page.stamp_text(TextStamp("ABC", Point(400, 760), font_size=10, font_ref=ref))

        session.execute(SnapshotCommand("Add stamp", op, session.snapshots))
        session.save()  # must stay incremental: the signature's byte range can't move
    finally:
        session.close()

    saved = path.read_bytes()
    assert saved.startswith(original) and len(saved) > len(original)
    with pikepdf.open(path) as pdf:
        sig = pdf.Root.AcroForm.Fields[0].V
        assert [int(x) for x in sig.ByteRange] == byte_range
        assert bytes(sig.Contents) == contents


def test_save_subsets_only_fonts_added_since_loading(
    editable: Engine, tmp_path: Path, fonts_dir: Path
) -> None:
    """The file's own complete fonts stay complete, so later edits can add characters."""
    src = pymupdf.open()
    page = src.new_page(width=300, height=200)
    page.insert_font(fontname="orig", fontfile=str(fonts_dir / "TestLarge-Regular.ttf"))
    page.insert_text((20, 50), "abc", fontname="orig", fontsize=12)
    src.save(tmp_path / "orig.pdf")  # no subsetting: the whole program is embedded
    src.close()
    doc = editable.open(tmp_path / "orig.pdf")
    (before,) = _embedded_fontinfo(doc)
    assert not before.subset
    doc.page(0).add_text(
        Rect(20, 80, 280, 120),
        "abc",
        TextStyle(font="Test Sans", font_ref=FontRef.file(str(fonts_dir / "TestSans-Regular.ttf"))),
    )
    doc.save(tmp_path / "edited.pdf")
    doc.close()
    reopened = editable.open(tmp_path / "edited.pdf")
    try:
        fonts = {f.name: f for f in _embedded_fontinfo(reopened)}
    finally:
        reopened.close()
    original = next(f for n, f in fonts.items() if "large" in n.lower())
    added = next(f for n, f in fonts.items() if "sans" in n.lower())
    assert not original.subset  # left alone
    assert added.subset  # the font this session embedded was trimmed
