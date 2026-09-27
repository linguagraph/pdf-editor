"""Contract tests for FreeText callouts and custom image stamps (``annotations_write``)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfeditor.engine.base import ColorMode, Engine, RenderRequest
from pdfeditor.model.annotations import (
    AnnotationModel,
    AnnotationType,
    callout_line,
    transformed,
)
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Matrix, Point, Rect

RED = Color(1, 0, 0)
TIP = Point(90, 120)
BOX = Rect(250, 200, 420, 250)


@pytest.fixture
def writable(engine: Engine) -> Engine:
    if not engine.capabilities.annotations_write:
        pytest.skip("engine can't write annotations")
    return engine


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str = "rotated_pages"):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


def _image_bytes(fmt: str, size: tuple[int, int] = (80, 40)) -> bytes:
    """A red/transparent (PNG) or red/white (JPEG) checkerboard."""
    mode = "RGBA" if fmt == "PNG" else "RGB"
    img = Image.new(mode, size, (255, 255, 255, 0) if mode == "RGBA" else (255, 255, 255))
    for x in range(size[0]):
        for y in range(size[1]):
            if (x // 10 + y // 10) % 2:
                img.putpixel((x, y), (220, 0, 0, 255) if mode == "RGBA" else (220, 0, 0))
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


def _red_pixels(rgb: np.ndarray, clip: Rect) -> int:
    part = rgb[int(clip.y0) : int(clip.y1), int(clip.x0) : int(clip.x1)].astype(int)
    red = (part[:, :, 0] > 150) & (part[:, :, 1] < 110) & (part[:, :, 2] < 110)
    return int(red.sum())


def _engine_rgb(page) -> np.ndarray:
    img = page.render(RenderRequest(color=ColorMode.RGB))
    arr = np.frombuffer(img.samples, dtype=np.uint8).reshape(img.height, img.stride)
    return arr[:, : img.width * 3].reshape(img.height, img.width, 3)


def _pdfium_rgb(path: Path, index: int) -> np.ndarray:
    pdf = pdfium.PdfDocument(path)
    try:
        bitmap = pdf[index].render(scale=1, may_draw_forms=True)
        return np.array(bitmap.to_pil().convert("RGB"))
    finally:
        pdf.close()


def _callout(page_index: int) -> AnnotationModel:
    return AnnotationModel(
        AnnotationType.FREE_TEXT,
        page_index,
        BOX,
        contents="Look here",
        color=RED,
        text_color=RED,
        border_width=1.5,
        font_size=12,
        vertices=callout_line(TIP, BOX),
        line_endings=("OpenArrow", "None"),
    )


def _near(p: Point, q: Point, tol: float = 1.5) -> bool:
    return abs(p.x - q.x) <= tol and abs(p.y - q.y) <= tol


@pytest.mark.parametrize("page_index", [0, 1], ids=["upright", "rotated90"])
def test_callout_roundtrip(writable: Engine, fixture_pdf, tmp_path: Path, page_index: int) -> None:
    doc, path = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(page_index)
    model = _callout(page_index)
    assert len(model.vertices) == 3  # tip, knee, end on the box's left side
    stored = page.add_annotation(model)
    assert stored.is_callout
    assert stored.rect.x0 == pytest.approx(BOX.x0, abs=1.5)
    assert stored.rect.y1 == pytest.approx(BOX.y1, abs=1.5)
    assert _near(stored.vertices[0], TIP) and _near(stored.vertices[-1], model.vertices[-1])
    assert stored.line_endings[0] == "OpenArrow"
    assert stored.contents == "Look here"
    # the leader line is drawn all the way to the tip
    tip_clip = Rect(TIP.x - 6, TIP.y - 6, TIP.x + 14, TIP.y + 14)
    assert _red_pixels(_engine_rgb(page), tip_clip) > 5
    doc.save()
    doc.close()

    with pikepdf.open(path) as pdf:
        annot = next(a for a in pdf.pages[page_index].Annots if a.Subtype == "/FreeText")
        assert annot.IT == "/FreeTextCallout"
        assert len(annot.CL) == 6
        assert annot.LE == "/OpenArrow"
        assert "/N" in annot.AP
    assert _red_pixels(_pdfium_rgb(path, page_index), tip_clip) > 5

    again = writable.open(path)
    back = next(a for a in again.page(page_index).annotations() if a.name == stored.name)
    assert back.is_callout and _near(back.vertices[0], TIP)
    assert back.rect.x0 == pytest.approx(BOX.x0, abs=1.5)
    again.close()


def test_callout_move_and_detach(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(writable, fixture_pdf, tmp_path, "text_multipage")
    page = doc.page(0)
    stored = page.add_annotation(_callout(0))
    moved = page.update_annotation(transformed(stored, Matrix.translate(40, 300)))
    assert _near(moved.vertices[0], TIP + Point(40, 300))
    assert moved.rect.x0 == pytest.approx(BOX.x0 + 40, abs=1.5)
    assert moved.rect.y0 == pytest.approx(BOX.y0 + 300, abs=1.5)
    # a new tip re-routes the line
    moved.vertices = callout_line(Point(500, 700), moved.rect)
    rerouted = page.update_annotation(moved)
    assert _near(rerouted.vertices[0], Point(500, 700))
    assert rerouted.rect.x0 == pytest.approx(BOX.x0 + 40, abs=1.5)
    # without a line it's a plain text box again
    rerouted.vertices = ()
    plain = page.update_annotation(rerouted)
    assert not plain.is_callout
    assert plain.rect.x0 == pytest.approx(BOX.x0 + 40, abs=1.5)
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        annot = next(a for a in pdf.pages[0].Annots if a.Subtype == "/FreeText")
        assert "/CL" not in annot and "/IT" not in annot


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
@pytest.mark.parametrize("page_index", [0, 1], ids=["upright", "rotated90"])
def test_image_stamp_roundtrip(
    writable: Engine, fixture_pdf, tmp_path: Path, page_index: int, fmt: str
) -> None:
    doc, path = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(page_index)
    rect = Rect(200, 300, 360, 380)  # same 2:1 aspect as the image
    stored = page.add_annotation(
        AnnotationModel(
            AnnotationType.STAMP, page_index, rect, image=_image_bytes(fmt), contents="Logo"
        )
    )
    assert stored.type is AnnotationType.STAMP and stored.contents == "Logo"
    assert stored.image is not None
    assert stored.rect.x0 == pytest.approx(200, abs=1.5)
    assert stored.rect.y1 == pytest.approx(380, abs=1.5)
    inside = Rect(205, 305, 355, 375)
    assert _red_pixels(_engine_rgb(page), inside) > 1000
    # opacity and moving keep the image appearance
    stored.rect = rect.translated(0, 150)
    stored.opacity = 0.9
    moved = page.update_annotation(stored)
    assert moved.image is not None
    assert _red_pixels(_engine_rgb(page), inside.translated(0, 150)) > 1000
    doc.save()
    doc.close()

    with pikepdf.open(path) as pdf:
        annot = next(a for a in pdf.pages[page_index].Annots if a.Subtype == "/Stamp")
        xobjects = annot.AP.N.Resources.XObject
        assert any(xobjects[k].Subtype == "/Image" for k in xobjects)
    assert _red_pixels(_pdfium_rgb(path, page_index), inside.translated(0, 150)) > 1000

    again = writable.open(path)
    back = next(a for a in again.page(page_index).annotations() if a.name == stored.name)
    assert back.image is not None
    decoded = Image.open(io.BytesIO(back.image))
    assert decoded.size == (80, 40)
    # the extracted image re-creates an equivalent stamp (undo of a delete, paste)
    again.page(page_index).delete_annotation(back.id, back.name)
    copy = again.page(page_index).add_annotation(back)
    assert copy.image is not None
    assert _red_pixels(_engine_rgb(again.page(page_index)), inside.translated(0, 150)) > 1000
    again.close()


def test_bad_stamp_image_is_rejected(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    from pdfeditor.engine.base import EngineError

    doc, _ = _open(writable, fixture_pdf, tmp_path, "text_multipage")
    before = len(doc.page(0).annotations())
    with pytest.raises(EngineError):
        doc.page(0).add_annotation(
            AnnotationModel(AnnotationType.STAMP, 0, Rect(100, 100, 200, 150), image=b"not img")
        )
    assert len(doc.page(0).annotations()) == before
    doc.close()


@pytest.mark.parametrize("page_index", [0, 1], ids=["upright", "rotated90"])
def test_image_stamp_is_upright_and_keeps_aspect(
    writable: Engine, fixture_pdf, tmp_path: Path, page_index: int
) -> None:
    img = Image.new("RGB", (80, 40), (0, 0, 220))
    img.paste((220, 0, 0), (0, 0, 40, 40))  # left half red, right half blue
    out = io.BytesIO()
    img.save(out, "PNG")
    doc, path = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(page_index)
    square = Rect(200, 300, 300, 400)
    stored = page.add_annotation(
        AnnotationModel(AnnotationType.STAMP, page_index, square, image=out.getvalue())
    )
    # fitted into the square with the image's 2:1 aspect, centered
    assert stored.rect.width == pytest.approx(100, abs=1.5)
    assert stored.rect.height == pytest.approx(50, abs=1.5)
    assert stored.rect.y0 == pytest.approx(325, abs=1.5)
    doc.save()
    doc.close()
    rgb = _pdfium_rgb(path, page_index).astype(int)
    left, right = rgb[350, 220], rgb[350, 280]
    assert left[0] > 150 and left[2] < 100  # red on the left: not rotated or mirrored
    assert right[2] > 150 and right[0] < 100
