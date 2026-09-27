"""Page operations on an open document, each one undoable step (snapshot-based)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage

from pdfeditor.core.commands import SnapshotCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import Document, EngineError, PasswordCallback
from pdfeditor.model.geometry import Rect
from pdfeditor.services.assembly import IMAGE_SUFFIXES
from pdfeditor.services.stamping import trimmed_rect


class PageOpError(Exception):
    """A page operation was refused (e.g. deleting every page)."""


def run(session: DocumentSession, label: str, operation: Callable[[Document], None]) -> None:
    session.execute(SnapshotCommand(label, operation, session.snapshots))


def _plural(n: int, word: str = "Page") -> str:
    return f"{n} {word}s" if n != 1 else f"{word}"


def delete_pages(session: DocumentSession, pages: Sequence[int]) -> None:
    doomed = set(pages)
    keep = [i for i in range(session.page_count) if i not in doomed]
    if not keep:
        raise PageOpError("A document must keep at least one page.")
    run(session, f"Delete {_plural(len(doomed))}", lambda doc: doc.select_pages(keep))


def move_pages(session: DocumentSession, pages: Sequence[int], before: int) -> list[int]:
    """Move ``pages`` (in document order) so they sit before index ``before`` (in the original
    numbering). Returns their new indices."""
    moving = sorted(set(pages))
    rest = [i for i in range(session.page_count) if i not in moving]
    insert_at = sum(1 for i in rest if i < before)
    order = rest[:insert_at] + moving + rest[insert_at:]
    if order == list(range(session.page_count)):
        return moving
    run(session, f"Move {_plural(len(moving))}", lambda doc: doc.select_pages(order))
    return list(range(insert_at, insert_at + len(moving)))


def duplicate_pages(session: DocumentSession, pages: Sequence[int]) -> None:
    chosen = sorted(set(pages))
    last = chosen[-1]
    order = [*range(last + 1), *chosen, *range(last + 1, session.page_count)]
    run(session, f"Duplicate {_plural(len(chosen))}", lambda doc: doc.select_pages(order))


def rotate_pages(session: DocumentSession, pages: Sequence[int], delta: int) -> None:
    chosen = sorted(set(pages))

    def op(doc: Document) -> None:
        for i in chosen:
            page = doc.page(i)
            page.set_rotation((page.rotation + delta) % 360)

    direction = "Right" if delta % 360 == 90 else "Left" if delta % 360 == 270 else ""
    run(session, f"Rotate {_plural(len(chosen))} {direction}".strip(), op)


def insert_blank(session: DocumentSession, at: int, like_page: int | None = None) -> None:
    ref = session.document.page(like_page if like_page is not None else max(0, at - 1)).rect
    run(session, "Insert Blank Page", lambda doc: doc.insert_blank_page(at, ref.width, ref.height))


def insert_file(
    session: DocumentSession,
    path: Path,
    at: int,
    pages: Sequence[int] | None = None,
    password: str | PasswordCallback | None = None,
) -> int:
    """Insert a PDF's pages (all or ``pages``) or an image as a page. Returns pages inserted."""
    if path.suffix.lower() in IMAGE_SUFFIXES:
        data = path.read_bytes()
        run(session, "Insert Image", lambda doc: doc.insert_image_page(at, data))
        return 1
    source = session.engine.open(path, password)
    try:
        chosen = list(range(source.page_count)) if pages is None else list(pages)
        run(
            session,
            f"Insert {_plural(len(chosen))}",
            lambda doc: doc.insert_pages(source, chosen, at),
        )
        return len(chosen)
    finally:
        source.close()


def clipboard_image_png() -> bytes | None:
    image: QImage = QGuiApplication.clipboard().image()
    if image.isNull():
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")  # type: ignore[call-overload]  # stub lacks the QIODevice form
    return bytes(buffer.data().data())


def insert_image_bytes(
    session: DocumentSession, at: int, data: bytes, label: str = "Insert Image"
) -> None:
    run(session, label, lambda doc: doc.insert_image_page(at, data))


def replace_pages(
    session: DocumentSession,
    pages: Sequence[int],
    path: Path,
    source_pages: Sequence[int],
    password: str | PasswordCallback | None = None,
) -> None:
    """Replace ``pages`` (a contiguous run is typical) with ``source_pages`` from ``path``."""
    chosen = sorted(set(pages))
    source = session.engine.open(path, password)
    try:

        def op(doc: Document) -> None:
            doc.insert_pages(source, list(source_pages), chosen[0])
            shift = len(source_pages)
            keep = [
                i
                for i in range(doc.page_count)
                if not (chosen[0] + shift <= i and (i - shift) in chosen)
            ]
            doc.select_pages(keep)

        run(session, f"Replace {_plural(len(chosen))}", op)
    finally:
        source.close()


def crop_pages(
    session: DocumentSession, pages: Sequence[int], margins: tuple[float, float, float, float]
) -> None:
    """Trim ``(left, top, right, bottom)`` points from each page's visible edges."""
    left, top, right, bottom = margins

    def op(doc: Document) -> None:
        for i in pages:
            r = doc.page(i).rect
            doc.page(i).set_crop(Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom))

    run(session, f"Crop {_plural(len(pages))}", op)


def crop_to_rect(session: DocumentSession, pages: Sequence[int], rect: Rect) -> None:
    """Crop every page in ``pages`` to the same visible-space rectangle."""

    def op(doc: Document) -> None:
        for i in pages:
            doc.page(i).set_crop(rect.intersection(doc.page(i).rect))

    run(session, f"Crop {_plural(len(pages))}", op)


def trim_white_margins(
    session: DocumentSession, pages: Sequence[int], padding: float = 12.0
) -> int:
    """Crop away white margins; returns how many pages changed."""
    targets: dict[int, Rect] = {}
    with session.lock:
        for i in pages:
            rect = trimmed_rect(session.document, i, padding)
            if rect is not None:
                targets[i] = rect
    if not targets:
        return 0

    def op(doc: Document) -> None:
        for i, rect in targets.items():
            try:
                doc.page(i).set_crop(rect)
            except EngineError:
                continue

    run(session, "Remove White Margins", op)
    return len(targets)
