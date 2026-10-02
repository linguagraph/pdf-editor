"""Fonts for Word export: the PDF's embedded TrueType programs, embedded in the .docx.

A reader who doesn't have the PDF's fonts installed would otherwise see Word's substitutes,
whose different widths move every line. Word embeds TrueType fonts (obfuscated, ECMA-376
Part 1, 17.8.1) and uses them where the font isn't installed. Only complete programs whose
license allows embedding are used; others fall back to the font's name.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Sequence

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from pdfeditor.engine.base import Document
from pdfeditor.services.fonts import BITMAP_ONLY as _BITMAP_ONLY
from pdfeditor.services.fonts import RESTRICTED as _RESTRICTED

# Free fonts made to the metrics of Helvetica, Times and Courier: where the PDF can't give Word
# the font itself, the metric twin every Word installation has keeps the lines the same length
_TWINS = {
    "sans": (
        "helvetica", "helv", "arial", "arialmt", "nimbussans", "nimbussanl", "liberationsans",
        "arimo", "texgyreheros", "freesans",
    ),
    "serif": (
        "times", "timesroman", "timesnewroman", "timesnewromanpsmt", "nimbusroman",
        "nimbusromno9l", "liberationserif", "tinos", "texgyretermes", "freeserif",
    ),
    "mono": (
        "courier", "couriernew", "couriernewpsmt", "nimbusmono", "nimbusmonops", "nimbusmonl",
        "liberationmono", "cousine", "texgyrecursor", "freemono",
    ),
}  # fmt: skip
_TWIN_OF = {name: kind for kind, names in _TWINS.items() for name in names}
WORD_TWIN = {"sans": "Arial", "serif": "Times New Roman", "mono": "Courier New"}
# base-14 fonts with the same metrics, by style (regular, bold, italic, bold italic)
STANDARD_TWIN = {
    "sans": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
    "serif": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
    "mono": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"),
}


def metric_twin(family: str) -> str | None:
    """ "sans", "serif" or "mono" for a Helvetica/Times/Courier-compatible family, else None."""
    return _TWIN_OF.get(family.replace(" ", "").replace("-", "").lower())


# tables Windows needs before it will load a font
_REQUIRED = {"cmap", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "name", "OS/2", "post"}
STYLES = ("Regular", "Bold", "Italic", "BoldItalic")


def font_style(program: bytes) -> tuple[str, str] | None:
    """(family, style) of an embeddable TrueType program, else None."""
    try:
        font = TTFont(io.BytesIO(program), lazy=True)
        if not set(font.keys()) >= _REQUIRED or not font.getBestCmap():
            return None
        fs_type = int(font["OS/2"].fsType)
        if fs_type & (_RESTRICTED | _BITMAP_ONLY):
            return None
        family = font["name"].getDebugName(1)
        subfamily = (font["name"].getDebugName(2) or "").lower()
    except Exception:
        return None
    if not family:
        return None
    bold, italic = "bold" in subfamily, "italic" in subfamily or "oblique" in subfamily
    return family, STYLES[bold + 2 * italic]


def for_windows(program: bytes) -> bytes:
    """The program with the Windows-platform names and Unicode cmap Windows needs to load it.
    PDF subsets often carry only Macintosh/Unicode-platform names, or none but the family."""
    font = TTFont(io.BytesIO(program))
    names = font["name"]
    windows = {(r.nameID) for r in names.names if r.platformID == 3}
    cmaps = {(t.platformID, t.platEncID) for t in font["cmap"].tables}
    if {1, 2, 3, 4, 6} <= windows and ((3, 1) in cmaps or (3, 10) in cmaps):
        return program
    family = names.getDebugName(1) or "Embedded"
    style = names.getDebugName(2) or "Regular"
    full = names.getDebugName(4) or (family if style == "Regular" else f"{family} {style}")
    postscript = names.getDebugName(6) or full.replace(" ", "-")
    for name_id, value in (
        (1, family), (2, style), (3, f"{family} {style}"), (4, full), (6, postscript)
    ):  # fmt: skip
        if name_id not in windows:
            names.setName(value, name_id, 3, 1, 0x409)
    if (3, 1) not in cmaps and (3, 10) not in cmaps:
        mapping = font.getBestCmap() or {}
        bmp = {c: g for c, g in mapping.items() if c <= 0xFFFF}
        table = CmapSubtable.newSubtableClass(4)
        table.platformID, table.platEncID, table.language, table.cmap = 3, 1, 0, bmp
        font["cmap"].tables.append(table)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def embeddable_fonts(doc: Document, pages: Sequence[int]) -> dict[str, dict[str, bytes]]:
    """Family -> style -> program for the fonts the pages' text uses. Where subsets differ
    between pages, the one covering the most characters wins."""
    found: dict[str, dict[str, bytes]] = {}
    for index in pages:
        page = doc.page(index)
        names = {
            s.font for b in page.text_page(with_chars=False).blocks for ln in b.lines
            for s in ln.spans
        }  # fmt: skip
        for name in names:
            program = page.font_program(name)
            key = font_style(program) if program else None
            if program is None or key is None:
                continue
            family, style = key
            styles = found.setdefault(family, {})
            if style not in styles or _coverage(program) > _coverage(styles[style]):
                styles[style] = program
    return {
        family: {style: for_windows(program) for style, program in styles.items()}
        for family, styles in found.items()
    }


def _coverage(program: bytes) -> int:
    return len(TTFont(io.BytesIO(program), lazy=True).getBestCmap() or {})


def font_key(program: bytes) -> str:
    """A stable GUID for a font (the same font always gets the same key)."""
    digest = hashlib.sha256(program).hexdigest()
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_OID, digest)).upper() + "}"


def obfuscate(program: bytes, key: str) -> bytes:
    """Word's font obfuscation: the first 32 bytes XORed with the GUID's bytes, reversed."""
    digits = key.strip("{}").replace("-", "")
    guid = bytes.fromhex(digits)[::-1]
    head = bytes(b ^ guid[i % 16] for i, b in enumerate(program[:32]))
    return head + program[32:]
