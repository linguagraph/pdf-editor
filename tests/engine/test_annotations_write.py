"""Contract tests for annotation writing (capability ``annotations_write``)."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfeditor.engine.base import ColorMode, Engine, EngineError, RenderRequest
from pdfeditor.model.annotations import AnnotationModel, AnnotationType, ReviewState
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Quad, Rect

RED = Color(1, 0, 0)
BLUE = Color(0, 0, 1)


@pytest.fixture
def writable(engine: Engine) -> Engine:
    if not engine.capabilities.annotations_write:
        pytest.skip("engine can't write annotations")
    return engine


def _ink_bbox(page, request: RenderRequest | None = None) -> Rect | None:
    """Bounding box (page space at scale 1) of clearly colored pixels."""
    img = page.render(request or RenderRequest(color=ColorMode.RGB))
    arr = np.frombuffer(img.samples, dtype=np.uint8).reshape(img.height, img.stride)
    rgb = arr[:, : img.width * 3].reshape(img.height, img.width, 3).astype(int)
    colored = (np.abs(rgb[:, :, 0] - rgb[:, :, 2]) > 80) | (
        np.abs(rgb[:, :, 1] - rgb[:, :, 2]) > 80
    )
    ys, xs = np.nonzero(colored)
    if len(xs) == 0:
        return None
    return Rect(float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


def _models(page_rect: Rect) -> list[AnnotationModel]:
    r = Rect(100, 150, 220, 230)
    pts = (Point(100, 300), Point(180, 260), Point(240, 330))
    return [
        AnnotationModel(
            AnnotationType.TEXT, 0, Rect(300, 100, 320, 120), contents="note", color=Color(1, 1, 0)
        ),
        AnnotationModel(
            AnnotationType.FREE_TEXT,
            0,
            Rect(300, 150, 500, 190),
            contents="Free",
            color=RED,
            font_size=14,
            text_color=RED,
        ),
        AnnotationModel(AnnotationType.SQUARE, 0, r, color=RED, fill=Color(1, 0.8, 0.8)),
        AnnotationModel(AnnotationType.CIRCLE, 0, r.translated(250, 200), color=BLUE),
        AnnotationModel(
            AnnotationType.LINE,
            0,
            Rect(0, 0, 0, 0),
            color=RED,
            vertices=(Point(100, 400), Point(300, 420)),
            line_endings=("None", "ClosedArrow"),
        ),
        AnnotationModel(AnnotationType.POLYGON, 0, Rect(0, 0, 0, 0), color=BLUE, vertices=pts),
        AnnotationModel(
            AnnotationType.POLYLINE,
            0,
            Rect(0, 0, 0, 0),
            color=RED,
            vertices=tuple(p + Point(0, 150) for p in pts),
        ),
        AnnotationModel(
            AnnotationType.INK,
            0,
            Rect(0, 0, 0, 0),
            color=BLUE,
            border_width=2,
            ink=((Point(350, 300), Point(380, 340), Point(420, 310)),),
        ),
        AnnotationModel(
            AnnotationType.HIGHLIGHT,
            0,
            Rect(0, 0, 0, 0),
            color=Color(1, 1, 0),
            quads=(Quad.from_rect(Rect(100, 600, 250, 615)),),
        ),
        AnnotationModel(
            AnnotationType.STRIKEOUT,
            0,
            Rect(0, 0, 0, 0),
            color=RED,
            quads=(Quad.from_rect(Rect(100, 630, 250, 645)),),
        ),
        AnnotationModel(AnnotationType.STAMP, 0, Rect(350, 600, 500, 650), icon="Approved"),
    ]


def _open(engine: Engine, fixture_pdf, tmp_path: Path, name: str = "text_multipage"):
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    return engine.open(path), path


@pytest.mark.parametrize("page_index", [0, 1], ids=["upright", "rotated90"])
def test_create_every_type_roundtrip(
    writable: Engine, fixture_pdf, tmp_path: Path, page_index: int
) -> None:
    doc, path = _open(writable, fixture_pdf, tmp_path, "rotated_pages")
    page = doc.page(page_index)
    created = []
    for model in _models(page.rect):
        model.page_index = page_index
        model.author = "Tester"
        stored = page.add_annotation(model)
        assert stored.id is not None and stored.name
        assert stored.type is model.type and stored.author == "Tester"
        created.append(stored)
    # geometry comes back in visible space
    by_type = {a.type: a for a in page.annotations()}
    assert by_type[AnnotationType.SQUARE].rect.intersects(Rect(100, 150, 220, 230))
    line = by_type[AnnotationType.LINE]
    assert line.vertices[0].x == pytest.approx(100, abs=1) and line.vertices[1].y == pytest.approx(
        420, abs=1
    )
    assert line.line_endings == ("None", "ClosedArrow")
    hl = by_type[AnnotationType.HIGHLIGHT]
    assert hl.quads[0].rect.x0 == pytest.approx(100, abs=1)
    assert by_type[AnnotationType.INK].ink[0][0].x == pytest.approx(350, abs=1)
    assert by_type[AnnotationType.STAMP].icon.endswith("Approved")
    # the square is drawn where the model says, in visible space
    bbox = _ink_bbox(page, RenderRequest(color=ColorMode.RGB, clip=Rect(90, 140, 230, 240)))
    assert bbox is not None
    doc.save()
    doc.close()

    with pikepdf.open(path) as pdf:
        assert len(pdf.pages[page_index].Annots) >= len(created)
    pdoc = pdfium.PdfDocument(path)
    assert len(pdoc) == 5
    pdoc.close()
    again = writable.open(path)
    names = {a.name for a in again.page(page_index).annotations()}
    assert {c.name for c in created} <= names
    again.close()


def test_update_moves_and_restyles(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(0)
    sq = page.add_annotation(
        AnnotationModel(AnnotationType.SQUARE, 0, Rect(100, 300, 200, 400), color=RED)
    )
    sq.rect = Rect(300, 500, 380, 560)
    sq.color = BLUE
    sq.contents = "moved"
    sq.opacity = 0.5
    updated = page.update_annotation(sq)
    assert updated.rect.intersects(Rect(300, 500, 380, 560)) and not updated.rect.intersects(
        Rect(100, 300, 200, 400)
    )
    assert (
        updated.color == BLUE
        and updated.contents == "moved"
        and updated.opacity == pytest.approx(0.5)
    )

    ink = page.add_annotation(
        AnnotationModel(
            AnnotationType.INK,
            0,
            Rect(0, 0, 0, 0),
            color=RED,
            ink=((Point(100, 100), Point(150, 150)),),
        )
    )
    ink.ink = ((Point(200, 200), Point(260, 250)),)
    moved = page.update_annotation(ink)
    assert moved.ink[0][0].x == pytest.approx(200, abs=1)
    assert moved.rect.x0 >= 190  # rect recomputed from the new points

    hl = page.add_annotation(
        AnnotationModel(
            AnnotationType.HIGHLIGHT,
            0,
            Rect(0, 0, 0, 0),
            color=Color(1, 1, 0),
            quads=(Quad.from_rect(Rect(100, 700, 200, 712)),),
        )
    )
    hl.quads = (Quad.from_rect(Rect(300, 700, 400, 712)),)
    assert page.update_annotation(hl).quads[0].rect.x0 == pytest.approx(300, abs=1)
    doc.close()


def test_reply_state_and_lock(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(0)
    parent = page.add_annotation(
        AnnotationModel(AnnotationType.TEXT, 0, Rect(50, 50, 70, 70), contents="Question?")
    )
    reply = page.add_annotation(
        AnnotationModel(
            AnnotationType.TEXT,
            0,
            parent.rect,
            contents="Answer",
            in_reply_to=parent.id,
            state=ReviewState.ACCEPTED,
        )
    )
    assert reply.in_reply_to == parent.id and reply.state is ReviewState.ACCEPTED
    parent.locked = True
    assert page.update_annotation(parent).locked
    doc.save()
    doc.close()
    again = writable.open(path)
    replies = [a for a in again.page(0).annotations() if a.in_reply_to is not None]
    assert replies and replies[0].contents == "Answer"
    again.close()


def test_delete_and_order(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(0)
    a = page.add_annotation(
        AnnotationModel(AnnotationType.SQUARE, 0, Rect(100, 100, 200, 200), color=RED, fill=RED)
    )
    b = page.add_annotation(
        AnnotationModel(AnnotationType.SQUARE, 0, Rect(150, 150, 250, 250), color=BLUE, fill=BLUE)
    )
    assert page.annotation_order() == [a.id, b.id]
    probe = RenderRequest(color=ColorMode.RGB, clip=Rect(170, 170, 175, 175))
    assert page.render(probe).samples[2] > 200  # blue on top
    page.set_annotation_order([b.id, a.id])
    assert page.annotation_order() == [b.id, a.id]
    assert page.render(probe).samples[0] > 200  # red on top now
    with pytest.raises(EngineError):
        page.set_annotation_order([a.id])
    page.delete_annotation(a.id)
    assert [x.id for x in page.annotations()] == [b.id]
    page.delete_annotation(None, name=b.name)
    assert page.annotations() == []
    with pytest.raises(EngineError):
        page.delete_annotation(12345)
    doc.close()


@pytest.mark.parametrize("fixture", ["annotations", "rotated_pages"])
def test_flatten_preserves_appearance(
    writable: Engine, fixture_pdf, tmp_path: Path, fixture: str
) -> None:
    if not writable.capabilities.annotations_flatten:
        pytest.skip("engine can't flatten")
    doc, path = _open(writable, fixture_pdf, tmp_path, fixture)
    page = doc.page(0 if fixture == "annotations" else 1)
    if fixture == "rotated_pages":
        page.add_annotation(
            AnnotationModel(
                AnnotationType.SQUARE,
                1,
                Rect(100, 100, 300, 200),
                color=RED,
                fill=Color(1, 0.8, 0.8),
            )
        )
        page.add_annotation(
            AnnotationModel(
                AnnotationType.HIGHLIGHT,
                1,
                Rect(0, 0, 0, 0),
                color=Color(1, 1, 0),
                quads=(Quad.from_rect(Rect(50, 300, 400, 320)),),
            )
        )
    before = page.render(RenderRequest(color=ColorMode.RGB)).samples
    visible = [
        a for a in page.annotations() if a.type not in (AnnotationType.POPUP,) and not a.flags & 2
    ]
    count = page.flatten_annotations()
    assert count == len(visible)
    assert [a for a in page.annotations() if a.type is not AnnotationType.POPUP] == []
    after = page.render(RenderRequest(color=ColorMode.RGB, annotations=False)).samples
    diff = np.abs(
        np.frombuffer(before, np.uint8).astype(int) - np.frombuffer(after, np.uint8).astype(int)
    )
    assert diff.mean() < 1.0 and (diff > 80).mean() < 0.002
    doc.save()
    doc.close()
    again = writable.open(path)
    index = 0 if fixture == "annotations" else 1
    left = [a for a in again.page(index).annotations() if a.type is not AnnotationType.POPUP]
    assert left == []
    after_reopen = again.page(index).render(RenderRequest(color=ColorMode.RGB)).samples
    assert (
        np.abs(
            np.frombuffer(after_reopen, np.uint8).astype(int)
            - np.frombuffer(before, np.uint8).astype(int)
        ).mean()
        < 1.0
    )
    again.close()


def test_flatten_selected_only(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, _ = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(0)
    keep = page.add_annotation(
        AnnotationModel(AnnotationType.SQUARE, 0, Rect(100, 100, 150, 150), color=RED)
    )
    burn = page.add_annotation(
        AnnotationModel(AnnotationType.SQUARE, 0, Rect(300, 300, 350, 350), color=BLUE)
    )
    assert page.flatten_annotations([burn.id]) == 1
    assert [a.id for a in page.annotations()] == [keep.id]
    blue = _ink_bbox(
        page, RenderRequest(color=ColorMode.RGB, annotations=False, clip=Rect(290, 290, 360, 360))
    )
    assert blue is not None  # now part of the page content
    doc.close()


def test_file_attachment_annotation(writable: Engine, fixture_pdf, tmp_path: Path) -> None:
    doc, path = _open(writable, fixture_pdf, tmp_path)
    page = doc.page(0)
    att = page.add_annotation(
        AnnotationModel(
            AnnotationType.FILE_ATTACHMENT,
            0,
            Rect(500, 50, 520, 70),
            contents="data file",
            file_name="data.csv",
            file_data=b"a,b\n1,2\n",
        )
    )
    assert att.type is AnnotationType.FILE_ATTACHMENT
    doc.save()
    doc.close()
    with pikepdf.open(path) as pdf:
        annot = next(a for a in pdf.pages[0].Annots if a.Subtype == "/FileAttachment")
        assert annot.FS.EF.F.read_bytes() == b"a,b\n1,2\n"


def test_pdf_matrix_maps_to_pdf_space(engine: Engine, fixture_pdf) -> None:
    doc = engine.open(fixture_pdf("rotated_pages"))
    upright, rotated = doc.page(0), doc.page(1)
    top_left = Point(0, 0).transform(upright.pdf_matrix)
    assert top_left.y == pytest.approx(upright.boxes.crop.height)  # PDF origin is bottom-left
    # rotated 90: visible top-left is the PDF bottom-left corner... of the rotated page
    p = Point(0, 0).transform(rotated.pdf_matrix)
    q = Point(rotated.rect.width, rotated.rect.height).transform(rotated.pdf_matrix)
    assert {round(p.x), round(q.x)} == {0, 595} and {round(p.y), round(q.y)} == {0, 842}
    doc.close()
