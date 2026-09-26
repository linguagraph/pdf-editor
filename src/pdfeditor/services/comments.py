"""Comment threads (annotations + replies) and exportable summaries."""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass, field
from datetime import datetime

from pdfeditor.engine.base import Document
from pdfeditor.model.annotations import AnnotationModel, AnnotationType, ReviewState

TYPE_LABELS = {
    AnnotationType.TEXT: "Note",
    AnnotationType.FREE_TEXT: "Text Box",
    AnnotationType.LINE: "Line",
    AnnotationType.SQUARE: "Rectangle",
    AnnotationType.CIRCLE: "Oval",
    AnnotationType.POLYGON: "Polygon",
    AnnotationType.POLYLINE: "Polyline",
    AnnotationType.HIGHLIGHT: "Highlight",
    AnnotationType.UNDERLINE: "Underline",
    AnnotationType.SQUIGGLY: "Squiggly",
    AnnotationType.STRIKEOUT: "Strikethrough",
    AnnotationType.STAMP: "Stamp",
    AnnotationType.INK: "Drawing",
    AnnotationType.FILE_ATTACHMENT: "Attachment",
    AnnotationType.CARET: "Caret",
    AnnotationType.REDACT: "Redaction mark",
}


_EPOCH = datetime(1970, 1, 1)


def type_label(t: AnnotationType) -> str:
    return TYPE_LABELS.get(t, t.value)


@dataclass
class Thread:
    comment: AnnotationModel
    replies: list[AnnotationModel] = field(default_factory=list)

    @property
    def status(self) -> ReviewState:
        """The latest review state set on the comment or any reply."""
        states = [r.state for r in [self.comment, *self.replies] if r.state is not ReviewState.NONE]
        return states[-1] if states else ReviewState.NONE


def _root_of(a: AnnotationModel, by_id: dict[int | None, AnnotationModel]) -> AnnotationModel:
    seen: set[int | None] = set()
    while a.in_reply_to is not None and a.in_reply_to in by_id and a.id not in seen:
        seen.add(a.id)
        a = by_id[a.in_reply_to]
    return a


def comment_threads(doc: Document) -> list[Thread]:
    """All comments in page order, each with its (transitive) replies, oldest first."""
    threads: list[Thread] = []
    for index in range(doc.page_count):
        models = [a for a in doc.page(index).annotations() if a.type.is_comment]
        by_id = {a.id: a for a in models}

        page_threads: dict[int | None, Thread] = {}
        for a in models:
            if a.in_reply_to is None:
                page_threads[a.id] = Thread(a)
        for a in models:
            if a.in_reply_to is not None:
                root = _root_of(a, by_id)
                thread = page_threads.setdefault(root.id, Thread(root))
                if a is not root:
                    thread.replies.append(a)
        for thread in page_threads.values():
            thread.replies.sort(
                key=lambda r: (r.created or r.modified or _EPOCH).replace(tzinfo=None)
            )
        threads.extend(
            sorted(page_threads.values(), key=lambda t: (t.comment.rect.y0, t.comment.rect.x0))
        )
    return threads


def summary_csv(threads: list[Thread], page_labels: list[str] | None = None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Page", "Type", "Author", "Date", "Status", "Comment", "Reply to"])
    for t in threads:
        for a, parent in [(t.comment, ""), *((r, t.comment.name) for r in t.replies)]:
            label = page_labels[a.page_index] if page_labels else str(a.page_index + 1)
            stamp = a.modified or a.created
            when = stamp.strftime("%Y-%m-%d %H:%M") if stamp else ""
            status = a.state.value if a.state is not ReviewState.NONE else ""
            writer.writerow([label, type_label(a.type), a.author, when, status, a.contents, parent])
    return buf.getvalue()


def summary_html(threads: list[Thread], title: str, page_labels: list[str] | None = None) -> str:
    """A printable summary (turned into PDF by the UI through Qt's HTML printing)."""
    parts = [
        "<html><head><meta charset='utf-8'><style>",
        "body{font-family:sans-serif;font-size:10pt} h1{font-size:15pt} h2{font-size:12pt;"
        "border-bottom:1px solid #999;margin-top:14pt} .c{margin:6pt 0} .meta{color:#555}"
        " .reply{margin-left:18pt;border-left:2px solid #ccc;padding-left:6pt}",
        "</style></head><body>",
        f"<h1>Comments summary: {html.escape(title)}</h1>",
        f"<p class='meta'>{sum(1 + len(t.replies) for t in threads)} comments</p>",
    ]
    page = None
    for t in threads:
        if t.comment.page_index != page:
            page = t.comment.page_index
            label = page_labels[page] if page_labels else str(page + 1)
            parts.append(f"<h2>Page {html.escape(label)}</h2>")
        parts.append(_entry_html(t.comment, "c"))
        parts.extend(_entry_html(r, "c reply") for r in t.replies)
    parts.append("</body></html>")
    return "".join(parts)


def _entry_html(a: AnnotationModel, css: str) -> str:
    when = a.modified or a.created
    meta = " · ".join(
        x
        for x in (
            type_label(a.type),
            a.author,
            when.strftime("%Y-%m-%d %H:%M") if when else "",
            a.state.value if a.state is not ReviewState.NONE else "",
        )
        if x
    )
    text = html.escape(a.contents).replace("\n", "<br>") if a.contents else "<i>(no text)</i>"
    return f"<div class='{css}'><div class='meta'>{html.escape(meta)}</div>{text}</div>"
