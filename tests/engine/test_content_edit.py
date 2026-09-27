"""Contract tests for content editing (capability ``content_edit``)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, Engine, RenderRequest, UnsupportedFeature
from pdfeditor.model.annotations import AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Rect
from pdfeditor.model.objects import ObjectType, ShapeKind, ShapeSpec, TextStyle
from pdfeditor.model.pages import ImageStamp


@pytest.fixture
def editable(engine: Engine) -> Engine:
    if not engine.capabilities.content_edit:
        pytest.skip("engine lacks content editing")
    return engine


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str = "mixed_content"):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


def _by_type(page, t: ObjectType):
    return [o for o in page.content_objects() if o.type is t]


def _pixel(page, x: float, y: float) -> tuple[int, int, int]:
    s = page.render(
        RenderRequest(color=ColorMode.RGB, annotations=False, clip=Rect(x, y, x + 1, y + 1))
    ).samples
    return s[0], s[1], s[2]


def _texts(page) -> list[str]:
    return [o.text for o in _by_type(page, ObjectType.TEXT)]


def test_lists_objects_with_styles(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    objs = page.content_objects()
    kinds = [o.type for o in objs]
    assert kinds.count(ObjectType.TEXT) == 3
    assert ObjectType.IMAGE in kinds and ObjectType.PATH in kinds and ObjectType.FORM in kinds
    heading, body, _second = _by_type(page, ObjectType.TEXT)
    assert heading.style.font == "Helvetica-Bold" and heading.style.size == pytest.approx(18)
    assert body.style.font == "Times-Roman" and body.style.line_height > 1
    (image,) = _by_type(page, ObjectType.IMAGE)
    assert image.bbox == Rect(420, 80, 540, 180) and image.image_size == (120, 100)
    (form,) = _by_type(page, ObjectType.FORM)
    assert form.bbox.x0 == pytest.approx(320) and form.bbox.y0 == pytest.approx(300)
    doc.close()


def test_delete_and_move_objects(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    (image,) = _by_type(page, ObjectType.IMAGE)
    page.transform_objects([image.key], Matrix.translate(-300, 350))
    page = doc.page(0)
    (moved,) = _by_type(page, ObjectType.IMAGE)
    assert moved.bbox == Rect(120, 430, 240, 530)
    assert _pixel(page, 480, 130) == (255, 255, 255)  # old place is empty
    assert _pixel(page, 180, 480) != (255, 255, 255)
    (path_obj,) = _by_type(page, ObjectType.PATH)
    (form,) = _by_type(page, ObjectType.FORM)
    page.delete_objects([path_obj.key, form.key])
    page = doc.page(0)
    assert not _by_type(page, ObjectType.PATH) and not _by_type(page, ObjectType.FORM)
    assert _pixel(page, 150, 340) == (255, 255, 255)
    assert _texts(page)[0] == "Mixed content heading"  # text untouched
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        assert len(pdf.pages) == 1
    pd = pdfium.PdfDocument(path)
    pd.close()


def test_resize_on_rotated_page(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(1)  # /Rotate 90
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (255, 0, 0)).save(buf, format="PNG")
    page.stamp_image(ImageStamp(buf.getvalue(), Rect(100, 100, 200, 200)))
    page = doc.page(1)
    (image,) = _by_type(page, ObjectType.IMAGE)
    assert image.bbox.x0 == pytest.approx(100, abs=1) and image.bbox.y1 == pytest.approx(200, abs=1)
    # scale 2x about its top-left corner, in visible space
    m = Matrix.translate(-100, -100) @ Matrix.scale(2) @ Matrix.translate(100, 100)
    page.transform_objects([image.key], m)
    (big,) = _by_type(doc.page(1), ObjectType.IMAGE)
    assert big.bbox.x1 == pytest.approx(300, abs=1) and big.bbox.y1 == pytest.approx(300, abs=1)
    assert _pixel(doc.page(1), 280, 280)[0] > 200
    doc.close()


def test_image_extract_and_replace(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    (image,) = _by_type(page, ObjectType.IMAGE)
    data, ext = page.image_data(image.key)
    assert Image.open(io.BytesIO(data)).size == (120, 100) and ext in ("png", "jpeg", "jpx")
    green = io.BytesIO()
    Image.new("RGB", (60, 50), (0, 200, 0)).save(green, format="PNG")
    page.replace_image(image.key, green.getvalue())
    r, g, b = _pixel(doc.page(0), 480, 130)
    assert g > 150 and r < 80 and b < 80
    doc.close()


def test_replace_text_keeps_everything_else(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    body = _by_type(page, ObjectType.TEXT)[1]
    choice = page.replace_text(body.key, "Edited paragraph.\nSecond line with Ünïcode.")
    assert choice is not None and not choice.substituted  # Times-Roman is a standard font
    page = doc.page(0)
    texts = _texts(page)
    assert texts[0] == "Mixed content heading" and "Edited paragraph." in texts[1]
    assert "Ünïcode" in texts[1] and texts[2] == "A second paragraph that stays put."
    new_body = _by_type(page, ObjectType.TEXT)[1]
    assert new_body.bbox.x0 == pytest.approx(72, abs=2) and new_body.bbox.y0 == pytest.approx(
        80, abs=4
    )
    assert new_body.style.size == pytest.approx(11, abs=0.5)
    assert _by_type(page, ObjectType.IMAGE) and _by_type(page, ObjectType.PATH)  # untouched
    marks = [a for a in page.annotations() if a.type is AnnotationType.REDACT]
    assert len(marks) == 1  # the user's own pending redaction mark survived
    doc.save()
    doc.close()
    pd = pdfium.PdfDocument(path)
    assert "Edited paragraph." in pd[0].get_textpage().get_text_range()
    pd.close()


def test_replace_in_subset_font_substitutes(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path, "subset_fonts")
    page = doc.page(0)
    (block,) = _by_type(page, ObjectType.TEXT)
    choice = page.replace_text(block.key, "Qwertyuiop XYZ 987")  # glyphs the subset lacks
    assert choice is not None and choice.substituted
    assert "Qwertyuiop XYZ 987" in _texts(doc.page(0))[0]
    doc.close()


def test_text_on_rotated_page_stays_upright(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    from pdfeditor.model.pages import TextStamp

    doc, _ = _open(editable, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(1)  # /Rotate 90; its original text reads vertically on screen
    original = _by_type(page, ObjectType.TEXT)[0]
    assert not original.editable and "rotated" in original.reason
    page.stamp_text(TextStamp("Upright stamp", Point(300, 300), font_size=14))
    stamp = next(o for o in _by_type(doc.page(1), ObjectType.TEXT) if o.text == "Upright stamp")
    assert stamp.editable
    doc.page(1).replace_text(stamp.key, "Upright replacement")
    line = next(
        ln for b in doc.page(1).text_page().blocks for ln in b.lines if "Upright" in ln.text
    )
    assert line.direction == pytest.approx((1, 0), abs=1e-3)
    assert line.bbox.x0 == pytest.approx(stamp.bbox.x0, abs=3)
    assert line.bbox.y0 == pytest.approx(stamp.bbox.y0, abs=4)
    doc.close()


def test_move_delete_and_add_text(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path)
    page = doc.page(0)
    second = _by_type(page, ObjectType.TEXT)[2]
    page.transform_objects([second.key], Matrix.translate(0, 400))
    moved = next(o for o in _by_type(doc.page(0), ObjectType.TEXT) if o.text.startswith("A second"))
    assert moved.bbox.y0 == pytest.approx(600, abs=4)
    heading = _by_type(doc.page(0), ObjectType.TEXT)[0]
    doc.page(0).delete_objects([heading.key])
    assert not any(t.startswith("Mixed content") for t in _texts(doc.page(0)))
    style = TextStyle(font="Courier", size=14, color=Color(0, 0, 1))
    choice = doc.page(0).add_text(
        Rect(300, 700, 550, 720), "New text box that wraps onto more lines", style
    )
    assert choice.name.startswith("Courier")
    added = next(
        o for o in _by_type(doc.page(0), ObjectType.TEXT) if o.text.startswith("New text box")
    )
    assert added.bbox.height > 20  # grew downward instead of shrinking the font
    doc.close()


def test_tilted_text_is_declined(editable: Engine, fixture_pdf) -> None:
    from pdfeditor.model.pages import TextStamp

    doc = editable.open(fixture_pdf("text_multipage").read_bytes())
    page = doc.page(0)
    page.stamp_text(TextStamp("Tilted", Point(100, 700), angle=30))
    tilted = next(o for o in _by_type(doc.page(0), ObjectType.TEXT) if o.text == "Tilted")
    assert not tilted.editable
    with pytest.raises(UnsupportedFeature):
        doc.page(0).replace_text(tilted.key, "no")
    doc.close()


def test_add_shapes(editable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(editable, fixture_pdf, tmp_path, "text_multipage")
    page = doc.page(0)
    page.add_shape(
        ShapeSpec(
            ShapeKind.RECTANGLE,
            Rect(100, 500, 200, 560),
            stroke=Color(1, 0, 0),
            fill=Color(1, 0.9, 0.9),
        )
    )
    page.add_shape(ShapeSpec(ShapeKind.LINE, start=Point(100, 600), end=Point(300, 600), width=3))
    paths = _by_type(doc.page(0), ObjectType.PATH)
    assert len(paths) == 2
    assert paths[0].bbox.inflated(3).contains(Point(100, 500))
    doc.close()
