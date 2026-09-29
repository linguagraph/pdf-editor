"""Minimal Office Open XML writers: Word (.docx) and Excel (.xlsx), no extra dependencies.

Only what the exports need: styled paragraphs, headings, tables, inline pictures and page
breaks for Word; one sheet per table with numbers recognized for Excel.
"""

from __future__ import annotations

import io
import itertools
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from pdfeditor.model.geometry import Rect
from pdfeditor.services.export.fonts import (
    STYLES,
    WORD_TWIN,
    font_key,
    metric_twin,
    obfuscate,
)
from pdfeditor.services.export.language import Languages
from pdfeditor.services.export.layout import Cell, Frame, LayoutPage, LayoutTable
from pdfeditor.services.export.structure import (
    Element,
    PageBreak,
    Paragraph,
    Picture,
    Run,
    Table,
)

Fonts = dict[str, dict[str, bytes]]  # family -> style -> TrueType program
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_EMU_PER_PT = 12700
_BODY_WIDTH_PT = 451.0  # A4 minus 1" margins

# PostScript names Word wouldn't recognize (metric twins of the standard fonts are handled by
# ``metric_twin``)
_FONT_MAP = {
    "Symbol": "Symbol",
    "DejaVuSans": "DejaVu Sans",
    "DejaVuSerif": "DejaVu Serif",
}
# control characters are not allowed in XML 1.0
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# PostScript names drop the spaces of the family name ("SegoeUI", "CenturyGothic")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
# a run of spaces used to line up columns (Word's "Print to PDF" pads with spaces)
_SPACE_GAP = re.compile(r" {3,}")


def _text(value: str) -> str:
    return escape(_CONTROL.sub("", value))


def _core_props(title: str) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        _XML + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_text(title)}</dc:title><dc:creator>PDF Editor</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        "</cp:coreProperties>"
    )


# -- Word ---------------------------------------------------------------------------------------
_DOCX_STYLES = (
    _XML + f'<w:styles xmlns:w="{_W}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" '
    'w:cs="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
    '<w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    "</w:style>"
    + "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
        f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/>'
        f'<w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr></w:style>'
        for n, size in ((1, 36), (2, 30), (3, 26))
    )
    + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr>'
    "<w:tblBorders>"
    + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + '</w:tblBorders><w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" '
    'w:type="dxa"/></w:tblCellMar></w:tblPr></w:style></w:styles>'
)


# current Word features (no "Compatibility Mode" banner)
_DOCX_SETTINGS = (
    _XML + f'<w:settings xmlns:w="{_W}"><w:defaultTabStop w:val="720"/><w:compat>'
    '<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" '
    'w:val="15"/></w:compat></w:settings>'
)


def _docx_styles(lang: str) -> str:
    if not lang:
        return _DOCX_STYLES
    default = '<w:sz w:val="22"/></w:rPr></w:rPrDefault>'
    return _DOCX_STYLES.replace(
        default, f'<w:sz w:val="22"/><w:lang w:val="{lang}"/></w:rPr></w:rPrDefault>', 1
    )


def word_font(name: str) -> str:
    if (twin := metric_twin(name)) is not None:
        return WORD_TWIN[twin]
    if name in _FONT_MAP:
        return _FONT_MAP[name]
    return name if " " in name else _CAMEL.sub(" ", name)


def _docx_text(text: str) -> str:
    """``<w:t>`` pieces, with column-aligning space runs turned into tabs."""
    return "<w:tab/>".join(
        f'<w:t xml:space="preserve">{_text(part)}</w:t>' if part else ""
        for part in _SPACE_GAP.split(text)
    )


def _lang(langs: Languages, text: str) -> str:
    tag = langs.for_run(text)
    return f'<w:lang w:val="{tag}"/>' if tag else ""


def _docx_run(run: Run, heading: bool, langs: Languages) -> str:
    props = []
    if run.font:
        f = quoteattr(word_font(run.font))
        props.append(f"<w:rFonts w:ascii={f} w:hAnsi={f} w:cs={f}/>")
    if run.bold:
        props.append("<w:b/>")
    if run.italic:
        props.append("<w:i/>")
    hex_color = run.color.to_hex().lstrip("#").upper()
    if hex_color != "000000":
        props.append(f'<w:color w:val="{hex_color}"/>')
    if abs(run.spacing) >= 0.05:
        props.append(f'<w:spacing w:val="{round(run.spacing * 20)}"/>')
    if not heading:
        props.append(f'<w:sz w:val="{round(run.size * 2)}"/>')
    props.append(_lang(langs, run.text))
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if any(props) else ""
    return f"<w:r>{rpr}{_docx_text(run.text)}</w:r>"


_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def _graphic(rel_id: str, n: int, cx: int, cy: int) -> str:
    return (
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{n}" name="image{n}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}" xmlns:r="{_R}"/><a:stretch><a:fillRect/>'
        "</a:stretch></pic:blipFill>"
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic>"
    )


def _docx_picture(rel_id: str, n: int, pic: Picture) -> str:
    scale = min(1.0, _BODY_WIDTH_PT / pic.width) if pic.width else 1.0
    cx, cy = round(pic.width * scale * _EMU_PER_PT), round(pic.height * scale * _EMU_PER_PT)
    return (
        f'<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0" '
        f'xmlns:wp="{_WP}"><wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="{n}" name="Picture {n}"/>{_graphic(rel_id, n, cx, cy)}'
        "</wp:inline></w:drawing></w:r></w:p>"
    )


_Z_BASE = 251658240  # where Word's own drawing order values start


def _anchored_picture(rel_id: str, n: int, x: float, y: float, w: float, h: float) -> str:
    """A picture at a fixed place on the page, behind the text (points)."""
    emu = [round(v * _EMU_PER_PT) for v in (x, y, w, h)]
    return (
        f'<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        f'relativeHeight="{_Z_BASE + n * 1024}" behindDoc="1" locked="0" layoutInCell="1" '
        'allowOverlap="1" '
        f'xmlns:wp="{_WP}"><wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{emu[0]}</wp:posOffset>'
        f'</wp:positionH><wp:positionV relativeFrom="page"><wp:posOffset>{emu[1]}'
        f'</wp:posOffset></wp:positionV><wp:extent cx="{emu[2]}" cy="{emu[3]}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
        f'<wp:docPr id="{n}" name="Picture {n}"/><wp:cNvGraphicFramePr/>'
        f"{_graphic(rel_id, n, emu[2], emu[3])}</wp:anchor></w:drawing></w:r>"
    )


def _docx_table(table: Table, langs: Languages) -> str:
    width = max((len(r) for r in table.rows), default=0)
    if not width:
        return ""
    col = int(_BODY_WIDTH_PT * 20 / width)  # twips
    grid = "".join(f'<w:gridCol w:w="{col}"/>' for _ in range(width))
    rows = []
    for i, row in enumerate(table.rows):
        cells = []
        for value in row + [""] * (width - len(row)):
            props = ("<w:b/>" if i == 0 else "") + _lang(langs, value)
            rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
            run = f"<w:r>{rpr}{_docx_text(value)}</w:r>" if value else ""
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{col}" w:type="dxa"/></w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{run}</w:p></w:tc>'
            )
        header = "<w:trPr><w:tblHeader/></w:trPr>" if i == 0 else ""
        rows.append(f"<w:tr>{header}{''.join(cells)}</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
        f"</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"
        # Word needs a paragraph between adjacent tables
        "<w:p/>"
    )


def _all_text(elements: Sequence[Element]) -> str:
    parts: list[str] = []
    for el in elements:
        if isinstance(el, Paragraph):
            parts.append(el.text)
        elif isinstance(el, Table):
            parts.extend(cell for row in el.rows for cell in row)
    return " ".join(parts)


def write_docx(
    elements: Sequence[Element],
    path: Path,
    title: str = "",
    language: str = "",
    fonts: Fonts | None = None,
) -> Path:
    """``language`` is the document's declared language (BCP 47); without one it's guessed
    from the text's script, so Word doesn't proof the text as the user's own language.
    ``fonts`` (family -> style -> TrueType program) are embedded in the file."""
    langs = Languages(_all_text(elements), language)
    body: list[str] = []
    media: list[bytes] = []
    for el in elements:
        if isinstance(el, Paragraph):
            ppr = f'<w:pPr><w:pStyle w:val="Heading{el.level}"/></w:pPr>' if el.level else ""
            runs = "".join(_docx_run(r, bool(el.level), langs) for r in el.runs)
            body.append(f"<w:p>{ppr}{runs}</w:p>")
        elif isinstance(el, Table):
            body.append(_docx_table(el, langs))
        elif isinstance(el, Picture):
            media.append(el.png)
            body.append(_docx_picture(f"rIdImg{len(media)}", len(media), el))
        elif isinstance(el, PageBreak):
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    sect = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
    )
    return _write_docx_package(path, "".join(body) + sect, media, title, langs.default, fonts or {})


def _font_table(fonts: Fonts) -> tuple[str, str, dict[str, bytes]]:
    """fontTable.xml, its relationships and the obfuscated font parts."""
    entries, rels = [], []
    files: dict[str, bytes] = {}
    for family, styles in sorted(fonts.items()):
        embeds = []
        for style in STYLES:
            program = styles.get(style)
            if program is None:
                continue
            n = len(files) + 1
            key = font_key(program)
            files[f"word/fonts/font{n}.odttf"] = obfuscate(program, key)
            rels.append(
                f'<Relationship Id="rIdFont{n}" Type="{_DOC_REL}/font" '
                f'Target="fonts/font{n}.odttf"/>'
            )
            embeds.append(f'<w:embed{style} r:id="rIdFont{n}" w:fontKey="{key}"/>')
        entries.append(
            f'<w:font w:name={quoteattr(family)}><w:charset w:val="00"/>'
            f'<w:family w:val="auto"/><w:pitch w:val="variable"/>{"".join(embeds)}</w:font>'
        )
    table = _XML + f'<w:fonts xmlns:w="{_W}" xmlns:r="{_R}">{"".join(entries)}</w:fonts>'
    table_rels = _XML + f'<Relationships xmlns="{_PKG_REL}">{"".join(rels)}</Relationships>'
    return table, table_rels, files


def _write_docx_package(
    path: Path,
    body: str,
    media: Sequence[bytes],
    title: str,
    language: str,
    fonts: Fonts,
) -> Path:
    document = (
        _XML
        + f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>'
        + body
        + "</w:body></w:document>"
    )
    doc_rels = (
        _XML
        + f'<Relationships xmlns="{_PKG_REL}">'
        + f'<Relationship Id="rIdStyles" Type="{_DOC_REL}/styles" Target="styles.xml"/>'
        + f'<Relationship Id="rIdSettings" Type="{_DOC_REL}/settings" Target="settings.xml"/>'
        + f'<Relationship Id="rIdFonts" Type="{_DOC_REL}/fontTable" Target="fontTable.xml"/>'
        + "".join(
            f'<Relationship Id="rIdImg{i}" Type="{_DOC_REL}/image" Target="media/image{i}.png"/>'
            for i in range(1, len(media) + 1)
        )
        + "</Relationships>"
    )
    content_types = (
        _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="odttf" '
        'ContentType="application/vnd.openxmlformats-officedocument.obfuscatedFont"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/fontTable.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    rels = (
        _XML + f'<Relationships xmlns="{_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL}/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    font_table, font_rels, font_files = _font_table(fonts)
    settings = _DOCX_SETTINGS
    if font_files:  # keep the fonts when Word saves the file again
        settings = settings.replace(
            "<w:defaultTabStop", "<w:embedTrueTypeFonts/><w:saveSubsetFonts/><w:defaultTabStop"
        )
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": rels,
        "docProps/core.xml": _core_props(title),
        "word/document.xml": document,
        "word/styles.xml": _docx_styles(language),
        "word/settings.xml": settings,
        "word/fontTable.xml": font_table,
        "word/_rels/fontTable.xml.rels": font_rels,
        "word/_rels/document.xml.rels": doc_rels,
    }
    binary = {f"word/media/image{i}.png": d for i, d in enumerate(media, 1)}
    return _write_zip(path, parts, binary | font_files)


# -- Word, page layout kept ----------------------------------------------------------------------
# Word sets a line of exact height with its baseline this share of a font size above the bottom
_DESCENT = 0.22
_FRAME_SLACK = 1.04  # Word's glyph widths differ a little; a wrapped line would push text down


def _twips(points: float) -> int:
    return round(points * 20)


def _paragraphs(
    frame: Frame,
    langs: Languages,
    extra: str = "",
    left: float = 0.0,
    before: float = 0.0,
    pitch: float | None = None,
) -> str:
    """The frame's lines as paragraphs; ``extra`` goes in every ``pPr`` (a ``framePr``), ``left``
    shifts the text right and ``before`` puts space above the first line (points). Lines with
    the same indent stay one paragraph joined by line breaks, so they edit as one; a first
    line set in or out from the rest (an indented paragraph, a bullet) joins them as the
    paragraph's first-line indent, so justified text stays justified on it too."""
    size = max((r.size for ln in frame.lines for r in ln.runs), default=11.0)
    pitch = pitch or max(frame.pitch, size)
    groups: list[list[int]] = []
    for i, line in enumerate(frame.lines):
        if groups and abs(frame.lines[groups[-1][-1]].indent - line.indent) < 2:
            groups[-1].append(i)
        elif groups and len(groups[-1]) == 1 and i == groups[-1][0] + 1:
            groups[-1].append(i)  # the line under a lone first line sets the indent
        else:
            groups.append([i])
    if frame.justified:
        # a full line ending a group (the text beside it wrapped around something) must be
        # stretched too, but Word leaves a paragraph's last line ragged: give it its own
        # paragraph, set "distributed" (a single line spread to the full width)
        split: list[list[int]] = []
        for group in groups:
            last = group[-1]
            if frame.lines[last].full and last != len(frame.lines) - 1 and len(group) > 1:
                split += [group[:-1], [last]]
            else:
                split.append(group)
        groups = split
    paras = []
    for n, group in enumerate(groups):
        first, rest = frame.lines[group[0]].indent, frame.lines[group[-1]].indent
        indent = left + rest
        shift = first - rest
        hang = (
            f' w:firstLine="{_twips(shift)}"'
            if shift >= 0.5
            else f' w:hanging="{_twips(-shift)}"'
            if shift <= -0.5
            else ""
        )
        jc = ""
        if frame.justified and len(group) > 1:
            jc = '<w:jc w:val="both"/>'
        elif frame.justified and frame.lines[group[-1]].full and group[-1] != len(frame.lines) - 1:
            jc = '<w:jc w:val="distribute"/>'
        ind = f'<w:ind w:left="{_twips(indent)}"{hang}/>' if indent >= 0.5 or hang else ""
        space = _twips(before) if n == 0 else 0
        ppr = (
            f'<w:pPr>{extra}<w:spacing w:before="{space}" w:after="0" '
            f'w:line="{_twips(pitch)}" w:lineRule="exact"/>{ind}{jc}</w:pPr>'
        )
        lines = ["".join(_docx_run(r, False, langs) for r in frame.lines[i].runs) for i in group]
        paras.append(f"<w:p>{ppr}{'<w:r><w:br/></w:r>'.join(lines)}</w:p>")
    return "".join(paras)


def _text_top(frame: Frame) -> float:
    """Where Word's first exact-height line must start for its baseline to match the PDF's."""
    size = max((r.size for ln in frame.lines for r in ln.runs), default=11.0)
    return frame.baseline - max(frame.pitch, size) + _DESCENT * size


def _frame_xml(frame: Frame, langs: Languages) -> str:
    """Paragraphs sharing one ``framePr`` at the frame's place (Word keeps them together)."""
    # justified text is stretched to the frame: give it the PDF's width exactly
    width = frame.rect.width + 0.5 if frame.justified else frame.rect.width * _FRAME_SLACK + 2
    frame_pr = (
        f'<w:framePr w:w="{_twips(width)}" w:hSpace="0" w:vSpace="0" w:wrap="around" '
        f'w:vAnchor="page" w:hAnchor="page" w:x="{_twips(frame.rect.x0)}" '
        f'w:y="{_twips(max(0.0, _text_top(frame)))}"/>'
    )
    return _paragraphs(frame, langs, frame_pr)


_TINY = '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
_EMPTY_CELL = f"<w:p><w:pPr>{_TINY}</w:pPr></w:p>"


def _borders(cell: Cell) -> str:
    sides = []
    for side in ("top", "left", "bottom", "right"):
        border = cell.borders.get(side)
        if border is None:
            sides.append(f'<w:{side} w:val="nil"/>')
        else:
            eighths = max(2, min(96, round(border.width * 8)))
            sides.append(
                f'<w:{side} w:val="single" w:sz="{eighths}" w:space="0" w:color="{border.color}"/>'
            )
    return f"<w:tcBorders>{''.join(sides)}</w:tcBorders>"


def _cell_xml(cell: Cell, width: int, langs: Languages, merge: str) -> str:
    """``merge``: "" (a plain cell), "restart" (top of a vertical merge) or "continue"."""
    span = f'<w:gridSpan w:val="{cell.cols}"/>' if cell.cols > 1 else ""
    vmerge = {"": "", "restart": '<w:vMerge w:val="restart"/>', "continue": "<w:vMerge/>"}[merge]
    shading = (
        f'<w:shd w:val="clear" w:color="auto" w:fill="{cell.shading}"/>' if cell.shading else ""
    )
    direction = f'<w:textDirection w:val="{cell.direction}"/>' if cell.direction else ""
    valign = '<w:vAlign w:val="center"/>' if cell.direction else ""
    # text the PDF fits edge to edge would wrap in Word and grow the row
    fit = "<w:tcFitText/>" if cell.content is not None and cell.content.tight else ""
    tc_pr = (
        f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{span}{vmerge}{_borders(cell)}{shading}'
        f"{direction}{fit}{valign}</w:tcPr>"
    )
    content = cell.content
    if merge == "continue" or content is None:
        body = _EMPTY_CELL
    elif cell.direction:
        body = _paragraphs(content, langs).replace("</w:pPr>", '<w:jc w:val="center"/></w:pPr>')
    else:
        # the PDF's line pitch (often the text box height) can exceed a tight row, which
        # would grow the row and push the rest of the table down: squeeze it to fit, but
        # not below the font size (Word clips an exact line smaller than its text)
        size = max((r.size for ln in content.lines for r in ln.runs), default=11.0)
        pitch = max(content.pitch, size)
        before = max(0.0, _text_top(content) - cell.rect.y0)
        spare = cell.rect.height - before - pitch * len(content.lines)
        if spare < 0:
            before = max(0.0, before + spare)
            pitch = max(size, (cell.rect.height - before) / len(content.lines))
        body = _paragraphs(
            content, langs, left=content.rect.x0 - cell.rect.x0, before=before, pitch=pitch
        )
    return f"<w:tc>{tc_pr}{body}</w:tc>"


def _layout_table_xml(table: LayoutTable, langs: Languages) -> str:
    """A table floating at its place on the page: fixed grid from the PDF's cell edges, merged
    cells, and borders/shading as drawn. Cell margins are zero; text keeps its own inset."""
    cols = [_twips(b - a) for a, b in itertools.pairwise(table.xs)]
    starts = {(c.row, c.col): c for c in table.cells}
    below = {(r, c.col): c for c in table.cells for r in range(c.row + 1, c.row + c.rows)}
    rows = []
    for r, (y0, y1) in enumerate(itertools.pairwise(table.ys)):
        cells = []
        col = 0
        while col < len(cols):
            cell = starts.get((r, col)) or below.get((r, col))
            if cell is None:  # a gap in the detected grid
                blank = Cell(r, col, 1, 1, Rect(table.xs[col], y0, table.xs[col + 1], y1))
                cells.append(_cell_xml(blank, cols[col], langs, ""))
                col += 1
                continue
            width = sum(cols[col : col + cell.cols])
            merge = "" if cell.rows == 1 else "restart" if cell.row == r else "continue"
            cells.append(_cell_xml(cell, width, langs, merge))
            col += cell.cols
        height = f'<w:trHeight w:val="{_twips(y1 - y0)}" w:hRule="atLeast"/>'
        rows.append(f"<w:tr><w:trPr><w:cantSplit/>{height}</w:trPr>{''.join(cells)}</w:tr>")
    margins = "".join(
        f'<w:{side} w:w="0" w:type="dxa"/>' for side in ("top", "left", "bottom", "right")
    )
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in cols)
    return (
        "<w:tbl><w:tblPr>"
        '<w:tblpPr w:leftFromText="0" w:rightFromText="0" w:topFromText="0" '
        'w:bottomFromText="0" w:vertAnchor="page" w:horzAnchor="page" '
        f'w:tblpX="{_twips(table.xs[0])}" w:tblpY="{_twips(table.ys[0])}"/>'
        '<w:tblOverlap w:val="overlap"/>'
        f'<w:tblW w:w="{sum(cols)}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
        f"<w:tblCellMar>{margins}</w:tblCellMar></w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"{''.join(rows)}</w:tbl>"
        # a floating table needs a paragraph after it (two in a row would merge)
        f"<w:p><w:pPr>{_TINY}</w:pPr></w:p>"
    )


def _page_sect(page: LayoutPage) -> str:
    w, h = _twips(page.width), _twips(page.height)
    orient = ' w:orient="landscape"' if w > h else ""
    return (
        f'<w:sectPr><w:pgSz w:w="{w}" w:h="{h}"{orient}/><w:pgMar w:top="0" w:right="0" '
        'w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
    )


def _layout_text(pages: Sequence[LayoutPage]) -> str:
    frames = [f for pg in pages for f in pg.frames]
    frames += [c.content for pg in pages for t in pg.tables for c in t.cells if c.content]
    return " ".join(r.text for f in frames for ln in f.lines for r in ln.runs)


def write_docx_layout(
    pages: Sequence[LayoutPage],
    path: Path,
    title: str = "",
    language: str = "",
    fonts: Fonts | None = None,
) -> Path:
    """One section per page at the PDF page size: text in frames at their places, tables
    floating at theirs, pictures and the vector-art background anchored behind the text."""
    langs = Languages(_layout_text(pages), language)
    body: list[str] = []
    media: list[bytes] = []
    for n, page in enumerate(pages):
        body.extend(_frame_xml(frame, langs) for frame in page.frames)
        body.extend(_layout_table_xml(table, langs) for table in page.tables)
        placed = [(Rect(0, 0, page.width, page.height), page.background)] if page.background else []
        placed += [(r, pic) for r, pic in page.pictures]
        drawings = []
        for rect, pic in placed:
            media.append(pic.png)
            k = len(media)
            drawings.append(
                _anchored_picture(f"rIdImg{k}", k, rect.x0, rect.y0, rect.width, rect.height)
            )
        # the page's last flowing paragraph: holds the pictures and ends the page's section
        last = n == len(pages) - 1
        sect = "" if last else _page_sect(page)
        body.append(f"<w:p><w:pPr>{_TINY}{sect}</w:pPr>{''.join(drawings)}</w:p>")
        if last:
            body.append(_page_sect(page))
    return _write_docx_package(path, "".join(body), media, title, langs.default, fonts or {})


# -- Excel --------------------------------------------------------------------------------------
_NUMBER = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def cell_number(value: str) -> float | None:
    """``"1,440.50"`` -> 1440.5; text that isn't a plain number -> None."""
    v = value.strip()
    if not _NUMBER.match(v) or (len(v) > 1 and v.lstrip("+-")[:1] == "0" and "." not in v):
        return None  # keep IDs like "007" as text
    return float(v.replace(",", ""))


def _col(n: int) -> str:
    name = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def sheet_names(names: Sequence[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        base = _BAD_SHEET_CHARS.sub("_", name).strip("'")[:31] or "Sheet"
        candidate, k = base, 2
        while candidate.lower() in (o.lower() for o in out):
            suffix = f" ({k})"
            candidate, k = base[: 31 - len(suffix)] + suffix, k + 1
        out.append(candidate)
    return out


def _sheet(rows: Sequence[Sequence[str]]) -> str:
    xml_rows = []
    for r, row in enumerate(rows):
        cells = []
        for c, value in enumerate(row):
            if not value:
                continue
            ref = f"{_col(c)}{r + 1}"
            style = ' s="1"' if r == 0 else ""
            number = cell_number(value) if r else None
            if number is not None:
                cells.append(f'<c r="{ref}"{style}><v>{number:g}</v></c>')
            else:
                cells.append(
                    f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">'
                    f"{_text(value)}</t></is></c>"
                )
        xml_rows.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    return (
        _XML + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
    )


def write_xlsx(sheets: Sequence[tuple[str, Sequence[Sequence[str]]]], path: Path) -> Path:
    """One worksheet per (name, rows); the first row is bold, numbers become numeric cells."""
    if not sheets:
        raise ValueError("nothing to export: no tables were chosen")
    names = sheet_names([n for n, _ in sheets])
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    workbook = (
        _XML
        + f'<workbook xmlns="{ns}" xmlns:r="{_R}"><sheets>'
        + "".join(
            f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>'
            for i, name in enumerate(names, 1)
        )
        + "</sheets></workbook>"
    )
    wb_rels = (
        _XML
        + f'<Relationships xmlns="{_PKG_REL}">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="{_DOC_REL}/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(names) + 1)
        )
        + f'<Relationship Id="rIdStyles" Type="{_DOC_REL}/styles" Target="styles.xml"/>'
        + "</Relationships>"
    )
    styles = (
        _XML + f'<styleSheet xmlns="{ns}">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        '</cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" '
        'borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" '
        'applyFont="1"/></cellXfs></styleSheet>'
    )
    main = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    content_types = (
        _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{main}.sheet.main+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{main}.styles+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{main}.worksheet+xml"/>'
            for i in range(1, len(names) + 1)
        )
        + '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    rels = (
        _XML + f'<Relationships xmlns="{_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL}/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": rels,
        "docProps/core.xml": _core_props(path.stem),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": wb_rels,
        "xl/styles.xml": styles,
    }
    for i, (_, rows) in enumerate(sheets, 1):
        parts[f"xl/worksheets/sheet{i}.xml"] = _sheet(rows)
    return _write_zip(path, parts, {})


def _write_zip(path: Path, parts: dict[str, str], binary: dict[str, bytes]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text.encode("utf-8"))
        for name, data in binary.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)
    return path
