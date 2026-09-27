"""Export and conversion: images, text/Markdown/HTML, Word, Excel, extraction, Office import."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from pathlib import Path

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import Document
from pdfeditor.model.text import TableData
from pdfeditor.services.export.ooxml import write_docx, write_xlsx
from pdfeditor.services.export.structure import StructureOptions, analyze
from pdfeditor.services.export.text import to_html, to_markdown, to_text


class TextFormat(Enum):
    TEXT = "txt"
    MARKDOWN = "md"
    HTML = "html"
    WORD = "docx"

    @property
    def label(self) -> str:
        return {"txt": "Plain Text", "md": "Markdown", "html": "HTML", "docx": "Word"}[self.value]


def export_document(
    doc: Document,
    pages: Sequence[int],
    target: Path,
    fmt: TextFormat,
    options: StructureOptions | None = None,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> Path:
    """Write ``pages`` as ``fmt`` to ``target`` (Markdown pictures go to ``<stem>_images/``)."""
    target = target.with_suffix("." + fmt.value)
    target.parent.mkdir(parents=True, exist_ok=True)
    title = doc.metadata().title or target.stem
    if fmt is TextFormat.TEXT:
        target.write_text(to_text(doc, pages, token, progress), encoding="utf-8")
        return target
    elements = analyze(doc, pages, options, token, progress)
    if fmt is TextFormat.MARKDOWN:
        text = to_markdown(elements, target.with_name(target.stem + "_images"))
        target.write_text(text, encoding="utf-8")
    elif fmt is TextFormat.HTML:
        target.write_text(to_html(elements, title), encoding="utf-8")
    else:
        write_docx(elements, target, title)
    return target


def find_tables(
    doc: Document,
    pages: Sequence[int],
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[TableData]:
    found: list[TableData] = []
    for n, index in enumerate(pages):
        if token is not None:
            token.check()
        found.extend(doc.page(index).find_tables())
        progress(n + 1, len(pages))
    return found


def tables_to_xlsx(tables: Sequence[TableData], target: Path) -> Path:
    """One sheet per table, named after its page ("Page 3", "Page 3 (2)", ...)."""
    sheets = [
        (
            f"Page {t.page_index + 1}",
            [[(c or "").replace("\n", " ").strip() for c in row] for row in t.rows],
        )
        for t in tables
    ]
    return write_xlsx(sheets, target.with_suffix(".xlsx"))
