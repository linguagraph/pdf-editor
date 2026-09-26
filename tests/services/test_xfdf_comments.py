from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pdfeditor.core.commands import AddAnnotationCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.model.annotations import AnnotationModel, AnnotationType, ReviewState
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Quad, Rect
from pdfeditor.services.comments import comment_threads, summary_csv, summary_html
from pdfeditor.services.xfdf import export_xfdf, import_command, parse_xfdf


@pytest.fixture
def session(fixture_pdf, tmp_path: Path):
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("rotated_pages"), path)
    s = DocumentSession.open(path)
    yield s
    s.close()


def _populate(session: DocumentSession) -> None:
    for page in (0, 1):
        session.execute(
            AddAnnotationCommand(
                AnnotationModel(
                    AnnotationType.SQUARE,
                    page,
                    Rect(100, 150, 220, 230),
                    color=Color(1, 0, 0),
                    author="Ann",
                    contents=f"box on {page}",
                )
            )
        )
        session.execute(
            AddAnnotationCommand(
                AnnotationModel(
                    AnnotationType.HIGHLIGHT,
                    page,
                    Rect(0, 0, 0, 0),
                    color=Color(1, 1, 0),
                    quads=(Quad.from_rect(Rect(80, 300, 300, 315)),),
                    author="Bob",
                )
            )
        )
        session.execute(
            AddAnnotationCommand(
                AnnotationModel(
                    AnnotationType.INK,
                    page,
                    Rect(0, 0, 0, 0),
                    color=Color(0, 0, 1),
                    ink=((Point(300, 400), Point(340, 450), Point(380, 410)),),
                )
            )
        )
        session.execute(
            AddAnnotationCommand(
                AnnotationModel(
                    AnnotationType.LINE,
                    page,
                    Rect(0, 0, 0, 0),
                    color=Color(0, 0, 1),
                    vertices=(Point(100, 500), Point(250, 520)),
                    line_endings=("None", "OpenArrow"),
                )
            )
        )
    parent = session.document.page(0).annotations()[0]
    reply = AnnotationModel(
        AnnotationType.TEXT,
        0,
        parent.rect,
        contents="Agreed",
        author="Cy",
        state=ReviewState.ACCEPTED,
    )
    reply.extra = {"irt_name": parent.name}
    session.execute(AddAnnotationCommand(reply))


def test_xfdf_roundtrip_including_rotated_page(
    session: DocumentSession, fixture_pdf, tmp_path: Path
) -> None:
    _populate(session)
    xml = export_xfdf(session.document, "doc.pdf")
    assert 'xmlns="http://ns.adobe.com/xfdf/"' in xml and "<square" in xml and "inreplyto=" in xml

    # import into a fresh copy of the original document
    fresh_path = tmp_path / "fresh.pdf"
    shutil.copy2(fixture_pdf("rotated_pages"), fresh_path)
    fresh = DocumentSession.open(fresh_path)
    fresh.execute(import_command(xml, fresh.document))
    assert len(fresh.undo_stack) == 1  # one undo step for the whole import
    for page in (0, 1):
        src = {
            a.type: a for a in session.document.page(page).annotations() if a.in_reply_to is None
        }
        dst = {a.type: a for a in fresh.document.page(page).annotations() if a.in_reply_to is None}
        assert dst[AnnotationType.SQUARE].rect.intersects(src[AnnotationType.SQUARE].rect)
        assert (
            dst[AnnotationType.SQUARE].author == "Ann"
            and dst[AnnotationType.SQUARE].contents == f"box on {page}"
        )
        assert dst[AnnotationType.HIGHLIGHT].quads[0].rect.x0 == pytest.approx(80, abs=1)
        assert dst[AnnotationType.INK].ink[0][1].x == pytest.approx(340, abs=1)
        line = dst[AnnotationType.LINE]
        assert (
            line.vertices[1].y == pytest.approx(520, abs=1) and line.line_endings[1] == "OpenArrow"
        )
    replies = [a for a in fresh.document.page(0).annotations() if a.in_reply_to is not None]
    assert replies and replies[0].contents == "Agreed" and replies[0].state is ReviewState.ACCEPTED
    parent = next(a for a in fresh.document.page(0).annotations() if a.id == replies[0].in_reply_to)
    assert parent.type is AnnotationType.SQUARE
    fresh.undo()
    assert fresh.document.page(0).annotations() == []
    fresh.close()


def test_import_twice_avoids_duplicate_names(session: DocumentSession) -> None:
    _populate(session)
    xml = export_xfdf(session.document)
    session.execute(import_command(xml, session.document))
    names = [a.name for p in (0, 1) for a in session.document.page(p).annotations()]
    assert len(names) == len(set(names))


def test_parse_ignores_unknown_and_out_of_range(session: DocumentSession) -> None:
    xml = (
        '<xfdf xmlns="http://ns.adobe.com/xfdf/"><annots>'
        '<sound page="0" rect="0,0,1,1"/>'
        '<square page="99" rect="0,0,10,10"/>'
        '<square page="0" rect="10,10,50,50" color="#00FF00"><contents>ok</contents></square>'
        "</annots></xfdf>"
    )
    (model,) = parse_xfdf(xml, session.document)
    assert model.contents == "ok" and model.color == Color(0, 1, 0)


def test_threads_and_summaries(session: DocumentSession) -> None:
    _populate(session)
    threads = comment_threads(session.document)
    assert len(threads) == 8
    boxed = next(t for t in threads if t.comment.contents == "box on 0")
    assert [r.contents for r in boxed.replies] == [
        "Agreed"
    ] and boxed.status is ReviewState.ACCEPTED
    csv_text = summary_csv(threads)
    assert csv_text.splitlines()[0].startswith("Page,Type,Author")
    assert "Agreed" in csv_text and "Rectangle" in csv_text
    html_text = summary_html(threads, "doc.pdf")
    assert "Comments summary: doc.pdf" in html_text and "Page 2" in html_text
