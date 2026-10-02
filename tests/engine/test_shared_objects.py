"""Copy-on-write: editing one page never changes another page that shares its objects.

Pages can share content streams, resource dictionaries or annotations without being
duplicates (Optimize merges identical objects; other producers share them on purpose). Every
in-place page mutation must leave the other pages exactly as they were (issue #49 follow-up).
Fixtures are built with pikepdf so the sharing is exactly what each test says.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from pikepdf import Array, Dictionary, Name, String
from PIL import Image

from pdfeditor.engine.base import Document, Engine, Page, SaveOptions
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.objects import ObjectType, ShapeKind, ShapeSpec, TextStyle
from pdfeditor.model.pages import ImageStamp, MarkKind, TextStamp
from pdfeditor.model.redaction import RedactOptions
from pdfeditor.services.ocr import installed_languages, tessdata_for
from pdfeditor.services.optimize import preset, reduce_size

RED = Color(1, 0, 0)
SHARES = ("contents", "resources", "annotation")


# -- fixtures -----------------------------------------------------------------------------------
def _content(pdf: pikepdf.Pdf, text: str) -> pikepdf.Stream:
    ops = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET q 100 0 0 100 72 400 cm /Im1 Do Q"
    return pdf.make_stream(ops.encode())


def _shared_pdf(path: Path, share: str) -> Path:
    """Two pages that share ``share``: their content (stream and array), one resource
    dictionary (with indirect category dictionaries) or one annotation (and the /Annots array
    listing it). Font and image are always shared."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.Type1,
            BaseFont=Name.Helvetica,
            Encoding=Name.WinAnsiEncoding,
        )
    )
    image = pdf.make_stream(
        bytes(4 * (x ^ y) for y in range(8) for x in range(8)),
        Type=Name.XObject,
        Subtype=Name.Image,
        Width=8,
        Height=8,
        ColorSpace=Name.DeviceGray,
        BitsPerComponent=8,
    )

    def resources() -> Dictionary:
        return Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im1=image))

    shared_resources = pdf.make_indirect(
        Dictionary(
            Font=pdf.make_indirect(Dictionary(F1=font)),
            XObject=pdf.make_indirect(Dictionary(Im1=image)),
        )
    )
    # an indirect array holding one stream: stamps append to the array, edits rewrite the stream
    shared_contents = pdf.make_indirect(Array([_content(pdf, "Shared words")]))
    annot = pdf.make_indirect(
        Dictionary(
            Type=Name.Annot,
            Subtype=Name.Square,
            Rect=[300, 300, 400, 400],
            C=[1, 0, 0],
            NM=String("shared-square"),
            AP=Dictionary(
                N=pdf.make_stream(
                    b"1 0 0 RG 4 w 2 2 96 96 re S",
                    Type=Name.XObject,
                    Subtype=Name.Form,
                    BBox=[0, 0, 100, 100],
                )
            ),
        )
    )
    shared_annots = pdf.make_indirect(Array([annot]))  # adding to it would add to both pages
    for n in range(2):
        page = Dictionary(
            Type=Name.Page,
            MediaBox=[0, 0, 612, 792],
            Contents=shared_contents
            if share == "contents"
            else _content(pdf, f"Page {n + 1} words"),
            Resources=shared_resources if share == "resources" else resources(),
        )
        if share == "annotation":
            page.Annots = shared_annots
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page)))
    if share == "annotation":
        annot.P = pdf.pages[0].obj
    pdf.save(path)
    return path


# -- mutations of page 0 ------------------------------------------------------------------------
def _stamp(doc: Document, page: Page, engine: Engine) -> None:
    mark = MarkKind.HEADER_FOOTER if engine.capabilities.page_marks else None
    page.stamp_text(TextStamp("HEADER", Point(72, 40), 12, mark=mark))
    if mark is not None:
        page.set_mark_settings(mark, '{"kind": "test"}')


def _watermark(doc: Document, page: Page, engine: Engine) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (0, 128, 255)).save(buf, format="PNG")
    page.stamp_image(ImageStamp(buf.getvalue(), Rect(400, 600, 500, 700), opacity=0.5))
    page.fill_background(Color(0.9, 0.9, 1.0), mark=MarkKind.BACKGROUND)


def _edit_text(doc: Document, page: Page, engine: Engine) -> None:
    key = next(o.key for o in page.content_objects() if o.type is ObjectType.TEXT)
    page.replace_text(key, "Edited words")


def _delete_image(doc: Document, page: Page, engine: Engine) -> None:
    key = next(o.key for o in page.content_objects() if o.type is ObjectType.IMAGE)
    page.delete_objects([key])


def _add_content(doc: Document, page: Page, engine: Engine) -> None:
    page.add_text(Rect(72, 200, 300, 230), "Added text", TextStyle())
    page.add_shape(ShapeSpec(ShapeKind.RECTANGLE, Rect(72, 250, 200, 300), stroke=RED))


def _redact(doc: Document, page: Page, engine: Engine) -> None:
    hits = page.search("words")
    assert hits
    page.add_annotation(AnnotationModel(AnnotationType.REDACT, 0, hits[0].rect))
    assert page.apply_redactions(None, RedactOptions()) == 1


def _add_annotation(doc: Document, page: Page, engine: Engine) -> None:
    page.add_annotation(AnnotationModel(AnnotationType.CIRCLE, 0, Rect(100, 500, 200, 600)))


def _change_annotations(doc: Document, page: Page, engine: Engine) -> None:
    """Update, reorder, flatten and delete what's on the page (for the shared annotation:
    the shared one)."""
    for x in (100, 250):
        page.add_annotation(AnnotationModel(AnnotationType.CIRCLE, 0, Rect(x, 500, x + 100, 600)))
    first = page.annotations()[0]
    moved = page.update_annotation(
        AnnotationModel(
            first.type, 0, Rect(20, 20, 80, 80), id=first.id, name=first.name, color=RED
        )
    )
    page.set_annotation_order(list(reversed(page.annotation_order())))
    page.flatten_annotations([a.id for a in page.annotations() if a.id != moved.id])
    page.delete_annotation(moved.id, moved.name)


def _ocr(doc: Document, page: Page, engine: Engine) -> None:
    if "eng" not in installed_languages():
        pytest.skip("English OCR data not installed (scripts/fetch_tessdata.py)")
    layer = page.ocr_text_layer("eng", 150, tessdata_for(["eng"]))
    page.add_text_layer(layer)


def _page_ops(doc: Document, page: Page, engine: Engine) -> None:
    page.set_rotation(90)
    page = doc.page(0)
    page.set_crop(Rect(0, 0, 700, 500))


MUTATIONS: dict[str, tuple[str, Callable[[Document, Page, Engine], None]]] = {
    "stamp": ("page_marks", _stamp),
    "watermark": ("page_ops", _watermark),
    "edit_text": ("content_edit", _edit_text),
    "delete_image": ("content_edit", _delete_image),
    "add_content": ("content_edit", _add_content),
    "redact": ("redact", _redact),
    "add_annotation": ("annotations_write", _add_annotation),
    "change_annotations": ("annotations_flatten", _change_annotations),
    "ocr": ("ocr", _ocr),
    "page_ops": ("page_ops", _page_ops),
}


# -- checks -------------------------------------------------------------------------------------
def _look(path: Path, index: int) -> tuple[str, bytes]:
    """Text and pixels of a page, as an independent implementation (PDFium) sees them."""
    pd = pdfium.PdfDocument(path)
    try:
        page = pd[index]
        text = page.get_textpage().get_text_range()
        pixels = page.render(scale=1, may_draw_forms=True).to_pil().convert("RGB").tobytes()
        return text, pixels
    finally:
        pd.close()


def _objgens(value: object) -> set[tuple[int, int]]:
    if isinstance(value, pikepdf.Array):
        return {item.objgen for item in value if item.is_indirect}
    return {value.objgen} if isinstance(value, pikepdf.Object) and value.is_indirect else set()


def _assert_unshared(data: bytes) -> None:
    """Nothing an edit writes to is shared between the pages; fonts and images still are."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        a, b = (p.obj for p in pdf.pages)
        assert not _objgens(a.Contents) & _objgens(b.Contents)
        assert not _objgens(a.get("/Annots", Array())) & _objgens(b.get("/Annots", Array()))
        assert not _objgens(a.Resources) & _objgens(b.Resources)
        for category in ("/Font", "/XObject"):
            assert not _objgens(a.Resources.get(category)) & _objgens(b.Resources.get(category))
        for annot in a.get("/Annots", Array()):
            for other in b.get("/Annots", Array()):
                if "/AP" in annot and "/AP" in other:
                    assert annot.AP.N.objgen != other.AP.N.objgen
        for category, name in (("/Font", "/F1"), ("/XObject", "/Im1")):
            mine = a.Resources.get(category, Dictionary()).get(name)
            if mine is not None:  # still used on the edited page: still the same object
                assert mine.objgen == b.Resources[category][name].objgen


@pytest.mark.parametrize("share", SHARES)
@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_editing_one_page_leaves_a_sharing_page_alone(
    engine: Engine, tmp_path: Path, share: str, mutation: str
) -> None:
    capability, mutate = MUTATIONS[mutation]
    if not getattr(engine.capabilities, capability):
        pytest.skip(f"engine lacks {capability}")
    src = _shared_pdf(tmp_path / "shared.pdf", share)
    before_text, before_pixels = _look(src, 1)
    first_text, first_pixels = _look(src, 0)
    with pikepdf.open(src) as pdf:  # the fixture really shares
        annots_before = len(pdf.pages[1].obj.get("/Annots", Array()))

    doc = engine.open(src.read_bytes())
    try:
        mutate(doc, doc.page(0), engine)
        unsaved = doc.to_bytes(SaveOptions(garbage=1))  # a full save merges identical objects
        out = doc.save(tmp_path / "edited.pdf")
    finally:
        doc.close()

    after_text, after_pixels = _look(out, 1)
    assert after_text == before_text
    assert after_pixels == before_pixels
    assert _look(out, 0) != (first_text, first_pixels)  # the edit did happen
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages[1].obj.get("/Annots", Array())) == annots_before
    reopened = engine.open(out)
    try:
        assert reopened.page(1).text_page(with_chars=False).text == (
            engine.open(src).page(1).text_page(with_chars=False).text
        )
    finally:
        reopened.close()
    _assert_unshared(unsaved)


def test_shared_annotation_keeps_its_id_on_the_edited_page(engine: Engine, tmp_path: Path) -> None:
    if not engine.capabilities.annotations_write:
        pytest.skip("engine lacks annotations_write")
    doc = engine.open(_shared_pdf(tmp_path / "shared.pdf", "annotation").read_bytes())
    try:
        shared = doc.page(0).annotations()[0]
        assert doc.page(1).annotations()[0].id == shared.id
        moved = doc.page(0).update_annotation(
            AnnotationModel(shared.type, 0, Rect(20, 20, 80, 80), id=shared.id, name=shared.name)
        )
        assert moved.id == shared.id and moved.name == shared.name
        other = doc.page(1).annotations()[0]
        assert other.id != shared.id and other.name != shared.name
        assert other.rect.x0 == pytest.approx(300) and moved.rect.x0 == pytest.approx(20)
    finally:
        doc.close()


def test_page_object_listed_twice_is_split(engine: Engine, tmp_path: Path) -> None:
    """A page tree naming the same page object twice (as MuPDF's select used to make)."""
    if not engine.capabilities.page_ops:
        pytest.skip("engine lacks page_ops")
    path = tmp_path / "twice.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 300))
    page = pdf.pages[0].obj
    page.Contents = _content(pdf, "Twice")
    page.Resources = Dictionary(
        Font=Dictionary(
            F1=pdf.make_indirect(
                Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
            )
        )
    )
    pdf.Root.Pages.Kids = Array([page, page])
    pdf.Root.Pages.Count = 2
    pdf.save(path)

    doc = engine.open(path.read_bytes())
    try:
        assert doc.page_count == 2
        doc.page(1).stamp_text(TextStamp("ONLY HERE", Point(20, 40), 12))
        assert "ONLY HERE" in doc.page(1).text_page(with_chars=False).text
        assert "ONLY HERE" not in doc.page(0).text_page(with_chars=False).text
    finally:
        doc.close()


def test_optimize_then_edit_one_duplicate(engine: Engine, fixture_pdf, tmp_path: Path) -> None:
    """The scenario of issue #49: duplicate, optimize (identical objects get merged), edit one."""
    if not (engine.capabilities.page_ops and engine.capabilities.optimize):
        pytest.skip("engine lacks page_ops/optimize")
    doc = engine.open(fixture_pdf("text_multipage").read_bytes())
    doc.select_pages([0, 0])
    optimized = reduce_size(doc, preset("lossless").options).data
    doc.close()
    src = tmp_path / "optimized.pdf"
    src.write_bytes(optimized)
    with pikepdf.open(src) as pdf:  # the merge really happened
        a, b = (p.obj for p in pdf.pages)
        contents = (_objgens(a.Contents) or {a.Contents.objgen}) & (
            _objgens(b.Contents) or {b.Contents.objgen}
        )
        assert contents or a.Resources.objgen == b.Resources.objgen
    before = _look(src, 1)

    doc = engine.open(src)
    try:
        page = doc.page(0)
        page.stamp_text(TextStamp("FOOTER", Point(72, 760), 10))
        if engine.capabilities.content_edit:
            key = next(o.key for o in page.content_objects() if o.type is ObjectType.TEXT)
            page.replace_text(key, "Changed heading")
        if engine.capabilities.annotations_write:
            page.add_annotation(
                AnnotationModel(AnnotationType.SQUARE, 0, Rect(100, 100, 200, 200), color=RED)
            )
        out = doc.save(tmp_path / "edited.pdf")
    finally:
        doc.close()
    assert _look(out, 1) == before
    assert "FOOTER" in _look(out, 0)[0] and "FOOTER" not in _look(out, 1)[0]


def test_deleted_pages_leave_no_references(engine: Engine, tmp_path: Path) -> None:
    """Open action, named destinations (both kinds), bookmarks, links and form fields that
    pointed at a deleted page are dropped; everything else in the catalog survives."""
    if not engine.capabilities.page_ops:
        pytest.skip("engine lacks page_ops")
    path = tmp_path / "dests.pdf"
    pdf = pikepdf.new()
    for _ in range(3):
        pdf.add_blank_page(page_size=(300, 300))
    p0, p1, gone = (p.obj for p in pdf.pages)

    def widget(name: str, page: pikepdf.Dictionary) -> pikepdf.Dictionary:
        return pdf.make_indirect(
            Dictionary(
                Type=Name.Annot,
                Subtype=Name.Widget,
                FT=Name.Tx,
                T=String(name),
                Rect=[10, 10, 100, 30],
                P=page,
            )
        )

    kept_field, gone_field = widget("kept", p0), widget("gone", gone)
    gone_kid = widget("kid", gone)
    del gone_kid["/T"]
    parent = pdf.make_indirect(Dictionary(FT=Name.Tx, T=String("parent"), Kids=Array([gone_kid])))
    gone_kid.Parent = parent
    p0.Annots = Array(
        [
            kept_field,
            pdf.make_indirect(
                Dictionary(
                    Type=Name.Annot,
                    Subtype=Name.Link,
                    Rect=[0, 0, 50, 50],
                    Dest=Array([gone, Name.Fit]),
                )
            ),
        ]
    )
    gone.Annots = Array([gone_field, gone_kid])
    pdf.Root.AcroForm = Dictionary(Fields=Array([kept_field, gone_field, parent]))
    pdf.Root.OpenAction = Dictionary(S=Name.GoTo, D=Array([gone, Name.Fit]))
    pdf.Root.Dests = pdf.make_indirect(
        Dictionary(old=Array([gone, Name.Fit]), keep=Array([p1, Name.Fit]))
    )
    pdf.Root.Names = Dictionary(
        Dests=Dictionary(
            Names=Array(
                [
                    String("a-first"),
                    Array([p0, Name.Fit]),
                    String("b-gone"),
                    pdf.make_indirect(Dictionary(D=Array([gone, Name.XYZ, 0, 0, 0]))),
                ]
            )
        ),
        EmbeddedFiles=Dictionary(
            Names=Array(
                [
                    String("note.txt"),
                    Dictionary(
                        Type=Name.Filespec,
                        F=String("note.txt"),
                        EF=Dictionary(F=pdf.make_stream(b"hello", Type=Name.EmbeddedFile)),
                    ),
                ]
            )
        ),
    )
    outline = pdf.make_indirect(Dictionary(Type=Name.Outlines))
    item = pdf.make_indirect(
        Dictionary(Title=String("Gone"), Parent=outline, Dest=Array([gone, Name.Fit]))
    )
    outline.First = outline.Last = item
    outline.Count = 1
    pdf.Root.Outlines = outline
    pdf.save(path)

    doc = engine.open(path)
    try:
        doc.select_pages([0, 1])
        out = doc.save(tmp_path / "deleted.pdf")
    finally:
        doc.close()

    with pikepdf.open(out) as result:
        root = result.Root
        in_tree = {p.objgen for p in result.pages}
        stray = [
            obj.objgen
            for obj in result.objects
            if isinstance(obj, Dictionary) and obj.get("/Type") == Name.Page
            if obj.objgen not in in_tree
        ]
        assert stray == []  # nothing refers to the deleted page any more
        assert "/OpenAction" not in root
        assert set(root.Dests.keys()) == {"/keep"}
        assert [str(n) for n in root.Names.Dests.Names[::2]] == ["a-first"]
        assert [str(n) for n in root.Names.EmbeddedFiles.Names[::2]] == ["note.txt"]
        assert [str(f.T) for f in root.AcroForm.Fields] == ["kept"]
        assert "/First" not in root.get("/Outlines", Dictionary())
        assert [a.Subtype for a in result.pages[0].obj.Annots] == [Name.Widget]
    pd = pdfium.PdfDocument(out)
    try:
        assert len(pd) == 2
    finally:
        pd.close()
