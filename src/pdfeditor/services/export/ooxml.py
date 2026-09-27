"""Minimal Office Open XML writers: Word (.docx) and Excel (.xlsx), no extra dependencies.

Only what the exports need: styled paragraphs, headings, tables, inline pictures and page
breaks for Word; one sheet per table with numbers recognized for Excel.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from pdfeditor.services.export.structure import (
    Element,
    PageBreak,
    Paragraph,
    Picture,
    Run,
    Table,
)

_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_EMU_PER_PT = 12700
_BODY_WIDTH_PT = 451.0  # A4 minus 1" margins

# PDF base-14 and common PostScript names -> fonts Word users have
_FONT_MAP = {
    "Helvetica": "Arial",
    "Helv": "Arial",
    "ArialMT": "Arial",
    "Times": "Times New Roman",
    "TimesNewRomanPSMT": "Times New Roman",
    "TimesNewRoman": "Times New Roman",
    "Courier": "Courier New",
    "CourierNewPSMT": "Courier New",
    "Symbol": "Symbol",
}
# control characters are not allowed in XML 1.0
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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


def word_font(name: str) -> str:
    return _FONT_MAP.get(name, name)


def _docx_run(run: Run, heading: bool) -> str:
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
    if not heading:
        props.append(f'<w:sz w:val="{round(run.size * 2)}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_text(run.text)}</w:t></w:r>'


def _docx_picture(rel_id: str, n: int, pic: Picture) -> str:
    scale = min(1.0, _BODY_WIDTH_PT / pic.width) if pic.width else 1.0
    cx, cy = round(pic.width * scale * _EMU_PER_PT), round(pic.height * scale * _EMU_PER_PT)
    return (
        "<w:p><w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{n}" name="Picture {n}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{n}" name="image{n}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}" xmlns:r="{_R}"/><a:stretch><a:fillRect/>'
        "</a:stretch></pic:blipFill>"
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def _docx_table(table: Table) -> str:
    width = max((len(r) for r in table.rows), default=0)
    if not width:
        return ""
    col = int(_BODY_WIDTH_PT * 20 / width)  # twips
    grid = "".join(f'<w:gridCol w:w="{col}"/>' for _ in range(width))
    rows = []
    for i, row in enumerate(table.rows):
        cells = []
        for value in row + [""] * (width - len(row)):
            rpr = "<w:rPr><w:b/></w:rPr>" if i == 0 else ""
            run = f'<w:r>{rpr}<w:t xml:space="preserve">{_text(value)}</w:t></w:r>' if value else ""
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


def write_docx(elements: Sequence[Element], path: Path, title: str = "") -> Path:
    body: list[str] = []
    media: list[bytes] = []
    for el in elements:
        if isinstance(el, Paragraph):
            ppr = f'<w:pPr><w:pStyle w:val="Heading{el.level}"/></w:pPr>' if el.level else ""
            runs = "".join(_docx_run(r, bool(el.level)) for r in el.runs)
            body.append(f"<w:p>{ppr}{runs}</w:p>")
        elif isinstance(el, Table):
            body.append(_docx_table(el))
        elif isinstance(el, Picture):
            media.append(el.png)
            body.append(_docx_picture(f"rIdImg{len(media)}", len(media), el))
        elif isinstance(el, PageBreak):
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    sect = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
    )
    document = (
        _XML
        + f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>'
        + "".join(body)
        + sect
        + "</w:body></w:document>"
    )
    doc_rels = (
        _XML
        + f'<Relationships xmlns="{_PKG_REL}">'
        + f'<Relationship Id="rIdStyles" Type="{_DOC_REL}/styles" Target="styles.xml"/>'
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
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
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
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": rels,
        "docProps/core.xml": _core_props(title),
        "word/document.xml": document,
        "word/styles.xml": _DOCX_STYLES,
        "word/_rels/document.xml.rels": doc_rels,
    }
    return _write_zip(path, parts, {f"word/media/image{i}.png": d for i, d in enumerate(media, 1)})


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
