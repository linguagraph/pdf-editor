"""PDF/A-2b: preflight checks and conversion, fully in-process (pikepdf + our engine's fonts).

``preflight`` reports the problems that block PDF/A-2b for the common rules (a built-in check,
not a full validator; veraPDF is used when installed). ``convert_to_pdfa`` fixes what can be
fixed automatically and returns the new file plus whatever still blocks conformance:

* identification: XMP ``pdfaid:part=2``/``conformance=B``, Info and XMP in sync
* colour: an sRGB OutputIntent (bundled ICC profile)
* fonts: standard fonts the file only names are embedded (Type1C programs from the engine,
  with widths computed from the program)
* forbidden features: JavaScript and other actions, embedded files, XFA, transfer functions,
  interpolation/alternate images, disallowed annotation types; annotations made printable
* no encryption; PDF version at most 1.7
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
from fontTools.agl import UV2AGL
from fontTools.cffLib import CFFFontSet
from fontTools.encodings.StandardEncoding import StandardEncoding
from fontTools.misc.psCharStrings import T2WidthExtractor

from pdfeditor.bundle import data_path

FontSource = Callable[[str], bytes | None]

_DISALLOWED_ANNOTS = {"/Sound", "/Movie", "/Screen", "/3D", "/RichMedia", "/FileAttachment"}
_NO_AP_NEEDED = {"/Popup", "/Link"}
_JS_ACTIONS = {"/JavaScript", "/Launch", "/Sound", "/Movie", "/ResetForm", "/ImportData"}
F_INVISIBLE, F_HIDDEN, F_PRINT, F_NOVIEW = 1, 2, 4, 32


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    fixable: bool
    count: int = 1

    def __str__(self) -> str:
        n = f" (x{self.count})" if self.count > 1 else ""
        return f"{self.message}{n}"


@dataclass
class ConversionResult:
    data: bytes
    fixed: list[str] = field(default_factory=list)
    remaining: list[Issue] = field(default_factory=list)

    @property
    def conforming(self) -> bool:
        return not self.remaining


def _values(d: Any) -> list[Any]:
    """A pikepdf Dictionary's values (typed loosely: pikepdf's stubs don't cover this)."""
    return [d[k] for k in d.keys()]  # noqa: SIM118 - pikepdf Dictionary, not a dict


# -- walking the document -----------------------------------------------------------------------
def _resources(pdf: pikepdf.Pdf) -> Iterator[Any]:
    """Every /Resources dict reachable from pages, form XObjects, patterns and annotation
    appearances (each once)."""
    seen: set[tuple[int, int]] = set()
    stack: list[Any] = []
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        if res is not None:
            stack.append(res)
        for annot in page.obj.get("/Annots", []):
            ap = annot.get("/AP") if isinstance(annot, pikepdf.Dictionary) else None
            if isinstance(ap, pikepdf.Dictionary):
                for key in ("/N", "/R", "/D"):
                    app = ap.get(key)
                    if isinstance(app, pikepdf.Stream):
                        stack.append(app)
                    elif isinstance(app, pikepdf.Dictionary):
                        stack.extend(v for v in _values(app) if isinstance(v, pikepdf.Stream))
    while stack:
        obj = stack.pop()
        if obj.is_indirect:
            if obj.objgen in seen:
                continue
            seen.add(obj.objgen)
        if isinstance(obj, pikepdf.Stream):
            res = obj.get("/Resources")
            if res is not None:
                stack.append(res)
            continue
        if not isinstance(obj, pikepdf.Dictionary):
            continue
        yield obj
        for category in ("/XObject", "/Pattern"):
            group = obj.get(category)
            if isinstance(group, pikepdf.Dictionary):
                stack.extend(v for v in _values(group) if isinstance(v, pikepdf.Stream))
        fonts = obj.get("/Font")
        if isinstance(fonts, pikepdf.Dictionary):  # Type3 glyph procs have resources too
            for f in _values(fonts):
                if isinstance(f, pikepdf.Dictionary) and f.get("/Subtype") == "/Type3":
                    res = f.get("/Resources")
                    if res is not None:
                        stack.append(res)


def _unique(objs: Iterator[Any]) -> list[Any]:
    seen: set[tuple[int, int]] = set()
    out = []
    for o in objs:
        key = o.objgen if o.is_indirect else (id(o), -1)
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def _fonts(pdf: pikepdf.Pdf) -> list[Any]:
    return _unique(
        f
        for res in _resources(pdf)
        if isinstance(res.get("/Font"), pikepdf.Dictionary)
        for f in _values(res["/Font"])
        if isinstance(f, pikepdf.Dictionary)
    )


def _xobjects(pdf: pikepdf.Pdf) -> list[Any]:
    return _unique(
        x
        for res in _resources(pdf)
        if isinstance(res.get("/XObject"), pikepdf.Dictionary)
        for x in _values(res["/XObject"])
        if isinstance(x, pikepdf.Stream)
    )


def _ext_gstates(pdf: pikepdf.Pdf) -> list[Any]:
    return _unique(
        g
        for res in _resources(pdf)
        if isinstance(res.get("/ExtGState"), pikepdf.Dictionary)
        for g in _values(res["/ExtGState"])
        if isinstance(g, pikepdf.Dictionary)
    )


def _annotations(pdf: pikepdf.Pdf) -> Iterator[tuple[pikepdf.Page, Any]]:
    for page in pdf.pages:
        for a in page.obj.get("/Annots", []):
            if isinstance(a, pikepdf.Dictionary):
                yield page, a


def _is_embedded(font: Any) -> bool:
    subtype = font.get("/Subtype")
    if subtype == "/Type3":
        return True
    target = font
    if subtype == "/Type0":
        kids = font.get("/DescendantFonts")
        if not kids:
            return False
        target = kids[0]
    fd = target.get("/FontDescriptor")
    return isinstance(fd, pikepdf.Dictionary) and any(
        k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")
    )


def _font_name(font: Any) -> str:
    return str(font.get("/BaseFont", "/(unnamed)"))[1:]


def _has_js_action(action: Any) -> bool:
    return isinstance(action, pikepdf.Dictionary) and action.get("/S") in _JS_ACTIONS


def _drop_invalid_xmp(pdf: pikepdf.Pdf) -> bool:
    """Remove a Metadata stream that isn't XMP (pikepdf would log a traceback and replace it
    anyway); True if one was removed."""
    meta = pdf.Root.get("/Metadata")
    if meta is None:
        return False
    try:
        data = meta.read_bytes()
    except Exception:
        data = b""
    if b"rdf:RDF" in data or b"<rdf:" in data:
        return False
    del pdf.Root["/Metadata"]
    return True


def _pdfa_id(pdf: pikepdf.Pdf) -> tuple[str | None, str | None]:
    if "/Metadata" not in pdf.Root:
        return None, None
    try:
        meta = pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False)
        return meta.get("pdfaid:part"), meta.get("pdfaid:conformance")
    except Exception:
        return None, None


# -- preflight ----------------------------------------------------------------------------------
def preflight(
    data: bytes, font_source: FontSource | None = None, encrypted: bool = False
) -> list[Issue]:
    """Problems for PDF/A-2b in ``data`` (unencrypted bytes; pass ``encrypted`` for the source
    file's state). ``font_source`` decides which missing fonts count as fixable."""
    issues: list[Issue] = []

    def add(code: str, message: str, fixable: bool, count: int = 1) -> None:
        if count:
            issues.append(Issue(code, message, fixable, count))

    if encrypted:
        add("encryption", "The document is encrypted (PDF/A forbids encryption)", True)
    with pikepdf.open(io.BytesIO(data)) as pdf:
        root = pdf.Root
        if float(pdf.pdf_version) > 1.7:
            add("version", f"PDF {pdf.pdf_version} (PDF/A-2 is based on PDF 1.7)", True)
        intents = [oi for oi in root.get("/OutputIntents", []) if oi.get("/S") == "/GTS_PDFA1"]
        if not intents:
            add("output-intent", "No PDF/A output intent (colour profile)", True)
        _drop_invalid_xmp(pdf)  # in-memory only: preflight never saves
        part, conformance = _pdfa_id(pdf)
        if part != "2" or (conformance or "").upper() not in ("B", "U", "A"):
            add("identification", "No PDF/A-2 identification in the XMP metadata", True)
        names = root.get("/Names")
        js = 0
        if isinstance(names, pikepdf.Dictionary) and "/JavaScript" in names:
            js += 1
        if _has_js_action(root.get("/OpenAction")) or "/AA" in root:
            js += 1
        js += sum(1 for p in pdf.pages if "/AA" in p.obj)
        js += sum(1 for _p, a in _annotations(pdf) if "/AA" in a or _has_js_action(a.get("/A")))
        add("javascript", "JavaScript or other forbidden actions", True, js)
        files = 1 if isinstance(names, pikepdf.Dictionary) and "/EmbeddedFiles" in names else 0
        add("embedded-files", "Embedded files", True, files)
        acro = root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary) and "/XFA" in acro:
            add("xfa", "XFA form data", True)
        missing = [f for f in _fonts(pdf) if not _is_embedded(f)]
        fixable = [
            f
            for f in missing
            if font_source is not None
            and f.get("/Subtype") in ("/Type1", "/TrueType", "/MMType1")
            and font_source(_font_name(f))
        ]
        add(
            "fonts",
            "Fonts that aren't embedded: " + ", ".join(sorted({_font_name(f) for f in fixable})),
            True,
            len(fixable),
        )
        stuck = [f for f in missing if f not in fixable]
        add(
            "fonts-unfixable",
            "Fonts that aren't embedded and have no stand-in: "
            + ", ".join(sorted({_font_name(f) for f in stuck})),
            False,
            len(stuck),
        )
        bad_types = sum(1 for _p, a in _annotations(pdf) if a.get("/Subtype") in _DISALLOWED_ANNOTS)
        add("annotation-types", "Annotation types PDF/A doesn't allow", True, bad_types)
        flags = 0
        no_ap = 0
        for _p, a in _annotations(pdf):
            if a.get("/Subtype") in _DISALLOWED_ANNOTS:
                continue
            f = int(a.get("/F", 0))
            if a.get("/Subtype") != "/Popup" and (
                not f & F_PRINT or f & (F_INVISIBLE | F_HIDDEN | F_NOVIEW)
            ):
                flags += 1
            if a.get("/Subtype") not in _NO_AP_NEEDED and "/AP" not in a:
                no_ap += 1
        add("annotation-flags", "Annotations that don't print or are hidden", True, flags)
        add("annotation-appearance", "Annotations without an appearance", False, no_ap)
        images = [x for x in _xobjects(pdf) if x.get("/Subtype") == "/Image"]
        add(
            "interpolate",
            "Images with interpolation or alternates",
            True,
            sum(1 for x in images if x.get("/Interpolate") is True or "/Alternates" in x),
        )
        add(
            "postscript",
            "PostScript or reference XObjects",
            True,
            sum(1 for x in _xobjects(pdf) if x.get("/Subtype") == "/PS" or "/Ref" in x),
        )
        add(
            "cmyk",
            "CMYK images with the sRGB output intent (need a CMYK intent)",
            False,
            sum(1 for x in images if x.get("/ColorSpace") == "/DeviceCMYK"),
        )
        add(
            "transfer",
            "Transfer functions or halftones in graphics states",
            True,
            sum(
                1
                for g in _ext_gstates(pdf)
                if ("/TR" in g) or ("/TR2" in g and g["/TR2"] != "/Default") or "/HTP" in g
            ),
        )
    return issues


# -- font embedding -----------------------------------------------------------------------------
def _cff_metrics(program: bytes) -> tuple[dict[str, float], dict[str, object], list[str] | None]:
    """Glyph widths (1000 units/em), top-dict info and the built-in encoding (symbolic fonts)."""
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(program), None)
    top = cff[0]
    private = top.Private
    nominal = getattr(private, "nominalWidthX", 0)
    default = getattr(private, "defaultWidthX", 0)
    subrs = getattr(private, "Subrs", [])
    gsubrs = cff.GlobalSubrs
    scale = top.FontMatrix[0] * 1000 if getattr(top, "FontMatrix", None) else 1.0
    widths: dict[str, float] = {}
    for name in top.charset:
        cs = top.CharStrings[name]
        extractor = T2WidthExtractor(subrs, gsubrs, nominal, default)
        extractor.execute(cs)
        widths[name] = extractor.width * scale
    encoding = top.Encoding if isinstance(getattr(top, "Encoding", None), list) else None
    info = {
        "bbox": [v * scale for v in getattr(top, "FontBBox", [0, -200, 1000, 900])],
        "italic_angle": float(getattr(top, "ItalicAngle", 0)),
        "fixed": bool(getattr(top, "isFixedPitch", 0)),
    }
    return widths, info, encoding


def _code_names(font: Any, builtin: list[str] | None) -> list[str | None]:
    """Glyph name for every code 0-255 under the font's encoding (+ Differences)."""
    enc = font.get("/Encoding")
    base = enc.get("/BaseEncoding") if isinstance(enc, pikepdf.Dictionary) else enc
    names: list[str | None]
    if base == "/WinAnsiEncoding" or base == "/MacRomanEncoding":
        codec = "cp1252" if base == "/WinAnsiEncoding" else "mac_roman"
        names = []
        for code in range(256):
            try:
                uv = ord(bytes([code]).decode(codec))
            except UnicodeDecodeError:
                names.append(None)
                continue
            names.append(UV2AGL.get(uv) if code >= 32 else None)
    elif base is None and builtin is not None:
        names = [n if n and n != ".notdef" else None for n in builtin]
    else:  # StandardEncoding, or none for a text font
        names = [n if n and n != ".notdef" else None for n in StandardEncoding]
    if isinstance(enc, pikepdf.Dictionary) and isinstance(enc.get("/Differences"), pikepdf.Array):
        code = 0
        for item in enc["/Differences"]:
            if isinstance(item, int) or (
                hasattr(item, "__int__") and not isinstance(item, pikepdf.Name)
            ):
                code = int(item)
            else:
                if 0 <= code < 256:
                    names[code] = str(item)[1:]
                code += 1
    return names


def _embed_font(pdf: pikepdf.Pdf, font: Any, program: bytes) -> None:
    widths, info, builtin = _cff_metrics(program)
    names = _code_names(font, builtin)
    first = int(font.get("/FirstChar", 0))
    last = int(font.get("/LastChar", 255))
    first, last = max(0, min(first, 255)), max(0, min(last, 255))
    missing = widths.get(".notdef", 0.0)
    font.FirstChar = first
    font.LastChar = last
    font.Widths = pikepdf.Array(
        [round(widths.get(names[c] or "", missing), 3) for c in range(first, last + 1)]
    )
    name = _font_name(font)
    symbolic = builtin is not None and "/Encoding" not in font
    flags = 4 if symbolic else 32
    if info["fixed"]:
        flags |= 1
    lower = name.lower()
    if "times" in lower or "roman" in lower:
        flags |= 2
    if info["italic_angle"] or "italic" in lower or "oblique" in lower:
        flags |= 64
    bbox = [round(v) for v in info["bbox"]]  # type: ignore[attr-defined]
    stream = pikepdf.Stream(pdf, program)
    stream.Subtype = pikepdf.Name.Type1C
    font.FontDescriptor = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor,
            FontName=pikepdf.Name("/" + name),
            Flags=flags,
            FontBBox=pikepdf.Array(bbox),
            ItalicAngle=info["italic_angle"],
            Ascent=bbox[3],
            Descent=bbox[1],
            CapHeight=round(bbox[3] * 0.72),
            StemV=80,
            FontFile3=stream,
        )
    )
    font.Subtype = pikepdf.Name.Type1


# -- conversion ---------------------------------------------------------------------------------
def srgb_profile() -> bytes:
    return data_path("sRGB.icc").read_bytes()


def convert_to_pdfa(data: bytes, font_source: FontSource) -> ConversionResult:
    """Make PDF/A-2b from unencrypted ``data``; see the module docstring for what is fixed."""
    fixed: list[str] = []
    with pikepdf.open(io.BytesIO(data)) as pdf:
        root = pdf.Root

        # forbidden actions and scripts
        names = root.get("/Names")
        removed_js = 0
        if isinstance(names, pikepdf.Dictionary):
            for key in ("/JavaScript", "/EmbeddedFiles"):
                if key in names:
                    del names[key]
                    fixed.append(
                        "removed embedded files"
                        if key == "/EmbeddedFiles"
                        else "removed document JavaScript"
                    )
        if _has_js_action(root.get("/OpenAction")):
            del root["/OpenAction"]
            removed_js += 1
        for holder in [root, *(p.obj for p in pdf.pages)]:
            if "/AA" in holder:
                del holder["/AA"]
                removed_js += 1
        acro = root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary):
            if "/XFA" in acro:
                del acro["/XFA"]
                fixed.append("removed XFA form data")
            for fld in acro.get("/Fields", []):
                if isinstance(fld, pikepdf.Dictionary) and "/AA" in fld:
                    del fld["/AA"]
                    removed_js += 1
        if "/NeedsRendering" in root:
            del root["/NeedsRendering"]

        # annotations
        dropped = flagged = 0
        for page in pdf.pages:
            annots = page.obj.get("/Annots")
            if not isinstance(annots, pikepdf.Array):
                continue
            keep = pikepdf.Array()
            for a in annots:
                if isinstance(a, pikepdf.Dictionary) and a.get("/Subtype") in _DISALLOWED_ANNOTS:
                    dropped += 1
                    continue
                if isinstance(a, pikepdf.Dictionary):
                    if "/AA" in a:
                        del a["/AA"]
                        removed_js += 1
                    if _has_js_action(a.get("/A")):
                        del a["/A"]
                        removed_js += 1
                    if a.get("/Subtype") != "/Popup":
                        f = int(a.get("/F", 0))
                        new = (f | F_PRINT) & ~(F_INVISIBLE | F_HIDDEN | F_NOVIEW)
                        if new != f:
                            a.F = new
                            flagged += 1
                keep.append(a)
            page.obj.Annots = keep
        if removed_js:
            fixed.append(f"removed {removed_js} JavaScript or other forbidden action(s)")
        if dropped:
            fixed.append(f"removed {dropped} annotation(s) PDF/A doesn't allow")
        if flagged:
            fixed.append(f"made {flagged} annotation(s) printable and visible")

        # images and graphics states
        touched = 0
        for x in _xobjects(pdf):
            for key in ("/Interpolate", "/Alternates", "/OPI"):
                if key in x and (key != "/Interpolate" or x[key] is True):
                    del x[key]
                    touched += 1
            if "/Ref" in x:
                del x["/Ref"]
                touched += 1
        for g in _ext_gstates(pdf):
            for key in ("/TR", "/HTP"):
                if key in g:
                    del g[key]
                    touched += 1
            if "/TR2" in g and g["/TR2"] != "/Default":
                g.TR2 = pikepdf.Name.Default
                touched += 1
        if touched:
            fixed.append(f"removed {touched} forbidden image/graphics-state entr(y/ies)")

        # optional content configurations need names
        oc = root.get("/OCProperties")
        if isinstance(oc, pikepdf.Dictionary):
            for cfg in [oc.get("/D"), *oc.get("/Configs", [])]:
                if isinstance(cfg, pikepdf.Dictionary):
                    if "/Name" not in cfg:
                        cfg.Name = pikepdf.String("Default")
                    if "/AS" in cfg:
                        del cfg["/AS"]

        # fonts
        embedded: set[str] = set()
        for font in _fonts(pdf):
            if _is_embedded(font) or font.get("/Subtype") not in (
                "/Type1",
                "/TrueType",
                "/MMType1",
            ):
                continue
            program = font_source(_font_name(font))
            if program is None:
                continue
            _embed_font(pdf, font, program)
            embedded.add(_font_name(font))
        if embedded:
            fixed.append("embedded fonts: " + ", ".join(sorted(embedded)))

        # colour: sRGB output intent
        if not any(oi.get("/S") == "/GTS_PDFA1" for oi in root.get("/OutputIntents", [])):
            icc = pikepdf.Stream(pdf, srgb_profile())
            icc.N = 3
            intent = pikepdf.Dictionary(
                Type=pikepdf.Name.OutputIntent,
                S=pikepdf.Name.GTS_PDFA1,
                OutputConditionIdentifier=pikepdf.String("sRGB IEC61966-2.1"),
                RegistryName=pikepdf.String("http://www.color.org"),
                Info=pikepdf.String("sRGB IEC61966-2.1"),
                DestOutputProfile=pdf.make_indirect(icc),
            )
            root.OutputIntents = pikepdf.Array([pdf.make_indirect(intent)])
            fixed.append("added an sRGB output intent")

        # identification (pikepdf keeps Info and XMP in step)
        if _drop_invalid_xmp(pdf):
            fixed.append("replaced metadata that wasn't valid XMP")
        if "/Trapped" in pdf.docinfo and pdf.docinfo.Trapped not in (True, False):
            del pdf.docinfo["/Trapped"]
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta.load_from_docinfo(pdf.docinfo, raise_failure=False)
            meta["pdfaid:part"] = "2"
            meta["pdfaid:conformance"] = "B"
        fixed.append("marked as PDF/A-2b")

        out = io.BytesIO()
        pdf.save(out, force_version="1.7", encryption=False, fix_metadata_version=True)
    result = out.getvalue()
    remaining = [i for i in preflight(result, font_source) if i.count]
    return ConversionResult(result, fixed, remaining)


# -- veraPDF (optional) -------------------------------------------------------------------------
def find_verapdf() -> Path | None:
    for name in ("verapdf", "verapdf.bat"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for base in (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
        "/opt",
        "/usr/local",
    ):
        if base:
            for candidate in (
                Path(base) / "veraPDF" / "verapdf.bat",
                Path(base) / "verapdf" / "verapdf",
            ):
                if candidate.exists():
                    return candidate
    return None


def validate_with_verapdf(path: Path, timeout: float = 300) -> tuple[bool, str]:
    """Run veraPDF (if installed) for PDF/A-2b; returns (compliant, report text)."""
    exe = find_verapdf()
    if exe is None:
        raise RuntimeError("veraPDF isn't installed (verapdf.org); the built-in checks still apply")
    proc = subprocess.run(
        [str(exe), "--flavour", "2b", "--format", "text", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    report = (proc.stdout or proc.stderr).strip()
    return report.upper().startswith("PASS") or " PASS " in report.upper(), report
