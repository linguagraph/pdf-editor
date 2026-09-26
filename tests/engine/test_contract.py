"""Engine contract tests, written only against ``pdfeditor.engine.base`` and the model.

A new backend passes when this whole module passes for it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.engine.base import (
    ColorMode,
    Document,
    Engine,
    OpenError,
    Page,
    PasswordRequired,
    RenderRequest,
    SaveError,
    SaveOptions,
)
from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.geometry import Matrix, Rect
from pdfeditor.model.metadata import EncryptionMethod, Metadata
from pdfeditor.model.outline import Destination, LinkKind, OutlineItem


def assert_valid_elsewhere(path: Path, pages: int, password: str = "") -> None:
    """Saved output must be readable by independent implementations (qpdf and pdfium)."""
    with pikepdf.open(path, password=password) as pdf:
        assert len(pdf.pages) == pages
    doc = pdfium.PdfDocument(path, password=password or None)
    assert len(doc) == pages
    doc.close()


def _copy(fixture: Path, tmp_path: Path) -> Path:
    dst = tmp_path / fixture.name
    shutil.copy2(fixture, dst)
    return dst


# -- opening -------------------------------------------------------------------------------


def test_open_path_and_bytes(engine: Engine, fixture_pdf) -> None:
    path = fixture_pdf("text_multipage")
    for source in (path, path.read_bytes()):
        doc = engine.open(source)
        assert isinstance(doc, Document)
        assert doc.page_count == 5
        assert isinstance(doc.page(0), Page)
        assert not doc.is_dirty
        doc.close()


def test_open_errors(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(OpenError):
        engine.open(tmp_path / "missing.pdf")
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"this is not a pdf at all")
    with pytest.raises(OpenError):
        engine.open(junk)


def test_password(engine: Engine, fixture_pdf) -> None:
    path = fixture_pdf("encrypted")
    with pytest.raises(PasswordRequired):
        engine.open(path)
    with pytest.raises(PasswordRequired):
        engine.open(path, password="wrong")
    attempts: list[int] = []

    def ask(n: int) -> str | None:
        attempts.append(n)
        return "nope" if n == 1 else "user"

    doc = engine.open(path, password=ask)
    assert attempts == [1, 2]
    info = doc.info()
    assert info.encryption is EncryptionMethod.AES_256
    assert info.permissions.print and not info.permissions.modify
    doc.close()
    with pytest.raises(PasswordRequired):
        engine.open(path, password=lambda n: None)


def test_repair_on_open(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("broken_xref"))
    assert doc.info().is_repaired
    assert "Recovered" in doc.page(0).text_page().text
    doc.close()


def test_page_index_bounds(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("images"))
    with pytest.raises(IndexError):
        doc.page(1)
    with pytest.raises(IndexError):
        doc.page(-1)
    doc.close()


# -- pages & rendering ---------------------------------------------------------------------


def test_page_geometry_and_rotation(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("rotated_pages"))
    p0, p1 = doc.page(0), doc.page(1)
    assert p0.rotation == 0 and p1.rotation == 90
    assert p0.rect.width == pytest.approx(595, abs=1)
    assert p1.rect.width == pytest.approx(p0.rect.height, abs=1)  # rotated: width/height swap
    landscape = doc.page(4)
    assert landscape.boxes.crop != landscape.boxes.media
    assert landscape.rect.width == pytest.approx(landscape.boxes.crop.width)
    doc.close()


def test_render(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("images"))
    page = doc.page(0)
    full = page.render(RenderRequest(matrix=Matrix.scale(0.5)))
    assert full.width == pytest.approx(page.rect.width / 2, abs=1)
    assert full.channels == 4
    assert len(full.samples) == full.stride * full.height
    assert len(set(full.samples[::97])) > 5  # not blank
    clip = page.render(
        RenderRequest(matrix=Matrix.scale(2), clip=Rect(72, 80, 172, 180), color=ColorMode.RGB)
    )
    assert (clip.width, clip.height) == (200, 200)
    assert clip.stride >= clip.width * 3
    doc.close()


def test_render_annotation_toggle(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("annotations"))
    page = doc.page(0)
    with_annots = page.render(RenderRequest(annotations=True, color=ColorMode.RGB)).samples
    without = page.render(RenderRequest(annotations=False, color=ColorMode.RGB)).samples
    assert with_annots != without
    doc.close()


# -- text -----------------------------------------------------------------------------------


def test_text_page_structure(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("text_multipage"))
    tp = doc.page(0).text_page()
    assert "Page 1 heading" in tp.text
    heading = tp.blocks[0].lines[0].spans[0]
    assert heading.size == pytest.approx(20)
    assert heading.is_bold
    chars = tp.chars()
    assert chars and all(tp.width >= c.bbox.x0 >= 0 for c in chars)
    assert "".join(c.c for c in chars).startswith("Page 1 heading")
    doc.close()


def test_text_cjk(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("cjk_text"))
    assert "日本語" in doc.page(0).text_page(with_chars=False).text
    doc.close()


def test_search(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("text_multipage"))
    page = doc.page(2)
    hits = page.search("needle-3")
    assert len(hits) == 1
    assert hits[0].rect.y0 > page.rect.height - 60
    assert page.search("NEEDLE-3")
    assert not page.search("NEEDLE-3", case_sensitive=True)
    assert page.search("") == []
    doc.close()


# -- links, annotations, outline, metadata ---------------------------------------------------


def test_annotations_read(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("annotations"))
    annots = doc.page(0).annotations()
    types = {a.type for a in annots}
    assert {
        AnnotationType.HIGHLIGHT,
        AnnotationType.TEXT,
        AnnotationType.FREE_TEXT,
        AnnotationType.SQUARE,
        AnnotationType.INK,
        AnnotationType.LINE,
    } <= types
    highlight = next(a for a in annots if a.type is AnnotationType.HIGHLIGHT)
    assert highlight.quads and highlight.id is not None
    note = next(a for a in annots if a.type is AnnotationType.TEXT)
    assert note.author == "Reviewer" and note.contents == "A sticky note"
    ink = next(a for a in annots if a.type is AnnotationType.INK)
    assert len(ink.ink) == 1 and len(ink.ink[0]) == 4
    line = next(a for a in annots if a.type is AnnotationType.LINE)
    assert len(line.vertices) == 2
    square = next(a for a in annots if a.type is AnnotationType.SQUARE)
    assert square.color is not None and square.color.r == 1
    doc.close()


def test_outline_and_labels(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("outline"))
    roots = doc.outline()
    assert [r.title for r in roots] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert [c.title for c in roots[1].children] == ["Section 2.1", "Section 2.2"]
    assert roots[1].dest is not None and roots[1].dest.page_index == 2
    assert [doc.page_label(i) for i in range(3)] == ["i", "ii", "iii"]
    doc.close()


def test_links(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("outline"))
    links = sorted(doc.page(0).links(), key=lambda ln: ln.rect.y0)
    assert [ln.kind for ln in links] == [LinkKind.GOTO, LinkKind.URI]
    assert links[0].dest is not None and links[0].dest.page_index == 3
    assert links[0].rect.x0 == pytest.approx(72)
    assert links[1].uri == "https://example.org/"
    assert doc.page(1).links() == []
    doc.close()


def test_fonts_and_properties(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("subset_fonts"))
    fonts = doc.fonts()
    assert any(f.subset and f.embedded for f in fonts)
    assert all("+" not in f.name for f in fonts)
    info = doc.info()
    assert info.page_count == 1 and info.pdf_version.startswith("1.")
    assert info.encryption is EncryptionMethod.NONE
    assert not info.has_signatures and not info.has_javascript
    doc.close()


def test_embedded_files(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("images"))
    (emb,) = doc.embedded_files()
    assert (emb.name, emb.filename, emb.size) == ("notes", "notes.txt", 15)
    assert emb.description == "Attached notes"
    doc.close()


def test_extract_embedded_file(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("images"))
    assert doc.extract_embedded_file("notes") == b"attached notes\n"
    doc.close()


def test_layers_toggle_rendering(engine: Engine, fixture_pdf) -> None:
    if not engine.capabilities.layers:
        pytest.skip("engine lacks layers")
    doc = engine.open(fixture_pdf("layers"))
    layers = doc.layers()
    assert [(ly.name, ly.visible) for ly in layers] == [
        ("Shown layer", True),
        ("Hidden layer", False),
    ]
    page = doc.page(0)
    request = RenderRequest(clip=Rect(100, 150, 110, 160), color=ColorMode.RGB)
    assert set(page.render(request).samples) == {255}  # hidden red box not drawn
    rev = page.revision
    doc.set_layer_visible(layers[1].id, True)
    assert page.revision != rev
    red = page.render(request).samples
    assert red[0:3] == bytes([255, 0, 0])
    assert not doc.is_dirty  # a view toggle is not an edit
    doc.close()


def test_metadata_type_is_model(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("images"))
    assert isinstance(doc.metadata(), Metadata)
    assert doc.metadata().title == "images"
    doc.close()


# -- saving ----------------------------------------------------------------------------------


def test_metadata_outline_roundtrip_full_save(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    path = _copy(fixture_pdf("outline"), tmp_path)
    doc = engine.open(path)
    meta = doc.metadata()
    meta.title, meta.author = "New title", "Ünïcødé Author"
    doc.set_metadata(meta)
    items = doc.outline()
    web = OutlineItem("Web", uri="https://example.org")
    items.append(OutlineItem("Appendix", dest=Destination(5), children=[web]))
    doc.set_outline(items)
    assert doc.is_dirty
    saved = doc.save()
    assert saved == path.resolve() and not doc.is_dirty
    # the document stays usable after save (it was transparently reopened)
    assert doc.page(0).text_page().text
    doc.close()

    assert_valid_elsewhere(path, 6)
    doc = engine.open(path)
    assert doc.metadata().title == "New title"
    assert doc.metadata().author == "Ünïcødé Author"
    appendix = doc.outline()[-1]
    assert appendix.title == "Appendix"
    assert appendix.dest is not None and appendix.dest.page_index == 5
    assert appendix.children[0].uri == "https://example.org"
    assert not list(tmp_path.glob(".*.tmp"))  # temp file cleaned up
    doc.close()


def test_save_as_rebinds_path(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    src = _copy(fixture_pdf("images"), tmp_path)
    doc = engine.open(src)
    out = tmp_path / "sub" / "copy.pdf"
    doc.save(out)
    assert doc.path == out.resolve()
    doc.close()
    assert_valid_elsewhere(out, 1)
    assert src.exists()


def test_incremental_save_appends(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    if not engine.capabilities.incremental_save:
        pytest.skip("engine lacks incremental save")
    path = _copy(fixture_pdf("text_multipage"), tmp_path)
    before = path.read_bytes()
    doc = engine.open(path)
    meta = doc.metadata()
    meta.subject = "incremental"
    doc.set_metadata(meta)
    doc.save(options=SaveOptions(incremental=True))
    doc.close()
    after = path.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    assert_valid_elsewhere(path, 5)
    doc = engine.open(path)
    assert doc.metadata().subject == "incremental"
    doc.close()
    assert not list(tmp_path.glob(".*.bak"))


def test_incremental_save_rejects_other_target(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    doc = engine.open(_copy(fixture_pdf("images"), tmp_path))
    with pytest.raises(SaveError):
        doc.save(tmp_path / "other.pdf", SaveOptions(incremental=True))
    doc.close()


def test_encrypted_save_keeps_encryption(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    path = _copy(fixture_pdf("encrypted"), tmp_path)
    doc = engine.open(path, password="owner")
    meta = doc.metadata()
    meta.keywords = "kept"
    doc.set_metadata(meta)
    doc.save()
    doc.close()
    with pytest.raises(PasswordRequired):
        engine.open(path)
    assert_valid_elsewhere(path, 1, password="user")
    doc = engine.open(path, password="user")
    assert doc.metadata().keywords == "kept"
    doc.close()


def test_new_document_needs_save_as(engine: Engine) -> None:
    doc = engine.new_document()
    assert doc.path is None
    with pytest.raises(SaveError):
        doc.save()
    doc.close()


def test_xmp_roundtrip(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    doc = engine.open(_copy(fixture_pdf("images"), tmp_path))
    xmp = (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>'
        '<?xpacket end="w"?>'
    )
    doc.set_xmp(xmp)
    doc.save()
    assert doc.info().has_xmp
    assert "xmpmeta" in doc.xmp()
    doc.close()


def test_to_bytes(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("vector_art"))
    data = doc.to_bytes()
    assert data.startswith(b"%PDF-")
    doc.close()
    again = engine.open(data)
    assert again.page_count == 1 and again.path is None
    again.close()


def test_page_revision_changes_after_save(engine: Engine, tmp_path: Path, fixture_pdf) -> None:
    doc = engine.open(_copy(fixture_pdf("images"), tmp_path))
    rev = doc.page(0).revision
    doc.save()
    assert doc.page(0).revision != rev  # reloaded pages must invalidate render caches
    doc.close()


@pytest.mark.parametrize("page_index", [0, 1, 2, 3, 4])
def test_coordinates_match_rendering_on_rotated_pages(
    engine: Engine, fixture_pdf, page_index: int
) -> None:
    """Text boxes and search hits must land where the glyphs are actually drawn."""
    import numpy as np

    doc = engine.open(fixture_pdf("rotated_pages"))
    page = doc.page(page_index)
    tp = page.text_page()
    word_bbox = tp.blocks[0].lines[0].bbox
    img = page.render(RenderRequest(color=ColorMode.GRAY, annotations=False))
    arr = np.frombuffer(img.samples, dtype=np.uint8).reshape(img.height, img.stride)
    ys, xs = np.nonzero(arr[:, : img.width] < 128)
    ink = Rect(float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
    assert word_bbox.inflated(3).contains(ink.top_left)
    assert word_bbox.inflated(3).contains(ink.bottom_right)
    needle = tp.blocks[0].lines[0].text.split()[0]
    hit = page.search(needle)[0].rect
    assert hit.intersects(word_bbox)
    doc.close()


def test_page_handles_are_lock_free_bookkeeping(engine: Engine, fixture_pdf, monkeypatch) -> None:
    """page(), page_count and revision must not call into the engine (used without the lock)."""
    doc = engine.open(fixture_pdf("text_multipage"))
    fz = getattr(doc, "fz", None)
    if fz is None:
        pytest.skip("backend exposes no native handle to guard")

    class Guard:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"engine touched: {name}")

    monkeypatch.setattr(type(doc), "fz", property(lambda self: Guard()))
    assert doc.page_count == 5
    assert isinstance(doc.page(4).revision, int)
    monkeypatch.undo()
    doc.close()
