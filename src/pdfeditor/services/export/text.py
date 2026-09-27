"""Plain text, Markdown and HTML exports."""

from __future__ import annotations

import base64
import html
from collections.abc import Sequence
from pathlib import Path

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.engine.base import Document
from pdfeditor.services.export.structure import (
    Element,
    PageBreak,
    Paragraph,
    Picture,
    Run,
    Table,
)


def to_text(
    doc: Document,
    pages: Sequence[int],
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> str:
    """Text in reading order; pages are separated by a form feed (like ``pdftotext``)."""
    parts: list[str] = []
    for n, index in enumerate(pages):
        if token is not None:
            token.check()
        blocks = doc.page(index).text_page(with_chars=False).blocks
        paras = [
            "\n".join("".join(s.text for s in ln.spans).rstrip() for ln in b.lines)
            for b in blocks
            if not b.is_image
        ]
        parts.append("\n\n".join(p for p in paras if p.strip()) + "\n")
        progress(n + 1, len(pages))
    return "\f".join(parts)


# -- Markdown ----------------------------------------------------------------------------------
def _md_escape(text: str) -> str:
    for ch in ("\\", "*", "_", "`", "[", "]", "<", ">", "|"):
        text = text.replace(ch, "\\" + ch)
    return text


def _md_run(run: Run) -> str:
    text = _md_escape(run.text)
    core = text.strip()
    if not core:
        return text
    lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()) :]
    if run.mono:
        core = f"`{run.text.strip()}`"
    if run.bold and run.italic:
        core = f"***{core}***"
    elif run.bold:
        core = f"**{core}**"
    elif run.italic:
        core = f"*{core}*"
    return lead + core + trail


def _md_table(table: Table) -> str:
    rows = [[_md_escape(c) for c in row] for row in table.rows if row]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def to_markdown(elements: Sequence[Element], image_dir: Path | None = None) -> str:
    """Markdown; pictures are written to ``image_dir`` (and linked relative to its parent),
    or embedded as data URIs when no folder is given."""
    out: list[str] = []
    pictures = 0
    for el in elements:
        if isinstance(el, Paragraph):
            if el.level:
                out.append("#" * el.level + " " + _md_escape(el.text.strip()))
            else:
                out.append("".join(_md_run(r) for r in el.runs))
        elif isinstance(el, Table):
            out.append(_md_table(el))
        elif isinstance(el, Picture):
            pictures += 1
            if image_dir is not None:
                image_dir.mkdir(parents=True, exist_ok=True)
                target = image_dir / f"image-{pictures}.png"
                target.write_bytes(el.png)
                src = f"{image_dir.name}/{target.name}"
            else:
                src = "data:image/png;base64," + base64.b64encode(el.png).decode("ascii")
            out.append(f"![Image {pictures}]({src})")
        elif isinstance(el, PageBreak):
            out.append("---")
    return "\n\n".join(s for s in out if s) + "\n"


# -- HTML --------------------------------------------------------------------------------------
_CSS = """body{font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:50em;margin:2em auto;
padding:0 1em;line-height:1.45}table{border-collapse:collapse;margin:1em 0}
td,th{border:1px solid #999;padding:.25em .6em;text-align:left}img{max-width:100%}
hr.page{border:0;border-top:1px dashed #bbb;margin:2em 0}code{font-size:.95em}"""


def _html_run(run: Run, body: float) -> str:
    text = html.escape(run.text)
    style = []
    hex_color = run.color.to_hex()
    if hex_color.lower() != "#000000":
        style.append(f"color:{hex_color}")
    if abs(run.size - body) >= 1:
        style.append(f"font-size:{run.size / body:.2f}em")
    if run.mono:
        text = f"<code>{text}</code>"
    if run.bold:
        text = f"<b>{text}</b>"
    if run.italic:
        text = f"<i>{text}</i>"
    return f'<span style="{";".join(style)}">{text}</span>' if style else text


def to_html(elements: Sequence[Element], title: str = "") -> str:
    """A standalone HTML page (pictures embedded as data URIs)."""
    sizes = [
        r.size for el in elements if isinstance(el, Paragraph) and not el.level for r in el.runs
    ]
    body = max(set(sizes), key=sizes.count) if sizes else 11.0
    parts: list[str] = []
    for el in elements:
        if isinstance(el, Paragraph):
            if el.level:
                parts.append(f"<h{el.level}>{html.escape(el.text.strip())}</h{el.level}>")
            else:
                parts.append("<p>" + "".join(_html_run(r, body) for r in el.runs) + "</p>")
        elif isinstance(el, Table):
            rows = []
            for i, row in enumerate(el.rows):
                tag = "th" if i == 0 else "td"
                cells = "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in row)
                rows.append(f"<tr>{cells}</tr>")
            parts.append("<table>\n" + "\n".join(rows) + "\n</table>")
        elif isinstance(el, Picture):
            data = base64.b64encode(el.png).decode("ascii")
            parts.append(
                f'<p><img src="data:image/png;base64,{data}" alt="" '
                f'width="{round(el.width * 4 / 3)}"></p>'
            )
        elif isinstance(el, PageBreak):
            parts.append('<hr class="page">')
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        + "\n".join(parts)
        + "\n</body>\n</html>\n"
    )
