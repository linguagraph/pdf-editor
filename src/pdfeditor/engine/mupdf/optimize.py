"""Size audit and in-place optimization for the MuPDF backend."""

from __future__ import annotations

import re

import pymupdf

from pdfeditor.engine.base import OptimizeOptions
from pdfeditor.model.metadata import SpaceUsage

_REF = re.compile(r"(\d+) 0 R")


def _key(fz: pymupdf.Document, xref: int, key: str) -> str:
    kind, value = fz.xref_get_key(xref, key)
    return value if kind != "null" else ""


def _refs(value: str) -> list[int]:
    return [int(m) for m in _REF.findall(value)]


def space_usage(fz: pymupdf.Document) -> SpaceUsage:
    """Classify every object by what it's for and add up its stored size."""
    fonts: set[int] = set()
    content: set[int] = set()
    meta: set[int] = set()
    images: set[int] = set()
    for xref in range(1, fz.xref_length()):
        try:
            kind = _key(fz, xref, "Type")
        except Exception:
            continue
        if kind == "/FontDescriptor":
            for key in ("FontFile", "FontFile2", "FontFile3", "CIDSet"):
                fonts.update(_refs(_key(fz, xref, key)))
        elif kind == "/Font":
            for key in ("ToUnicode", "Widths", "CIDToGIDMap", "FontDescriptor", "W"):
                fonts.update(_refs(_key(fz, xref, key)))
        elif _key(fz, xref, "Subtype") == "/Image":
            # colour spaces (ICC profiles, palettes) and masks belong to the image
            for key in ("ColorSpace", "SMask", "Mask", "Decode"):
                for ref in _refs(_key(fz, xref, key)):
                    images.add(ref)
                    images.update(_refs(fz.xref_object(ref, compressed=True)))
    for page in fz:
        content.update(page.get_contents())
        meta.update(_refs(_key(fz, page.xref, "Thumb")))
    meta.update(_refs(fz.xref_get_key(-1, "Info")[1]))
    meta.update(_refs(_key(fz, fz.pdf_catalog(), "Metadata")))

    sizes = dict.fromkeys(
        (
            "Images",
            "Fonts",
            "Page content",
            "Comments and forms",
            "Structure (tags)",
            "Bookmarks and links",
            "Embedded files",
            "Metadata and thumbnails",
            "Other objects",
        ),
        0,
    )
    for xref in range(1, fz.xref_length()):
        try:
            obj = fz.xref_object(xref, compressed=True)
        except Exception:
            continue
        size = len(obj)
        if fz.xref_is_stream(xref):
            size += len(fz.xref_stream_raw(xref) or b"")
        kind, subtype = _key(fz, xref, "Type"), _key(fz, xref, "Subtype")
        if xref in meta or kind == "/Metadata":
            cat = "Metadata and thumbnails"
        elif subtype == "/Image" or xref in images:
            cat = "Images"
        elif xref in fonts or kind in ("/Font", "/FontDescriptor", "/Encoding"):
            cat = "Fonts"
        elif xref in content or subtype in ("/Form", "/PS") or kind == "/Pattern":
            cat = "Page content"
        elif kind == "/Annot" or "/FT" in obj or kind == "/AcroForm":
            cat = "Comments and forms"
        elif kind in ("/StructElem", "/StructTreeRoot", "/MCR", "/OBJR"):
            cat = "Structure (tags)"
        elif kind in ("/Outlines", "/Action") or ("/Title" in obj and "/Parent" in obj):
            cat = "Bookmarks and links"
        elif kind in ("/EmbeddedFile", "/Filespec"):
            cat = "Embedded files"
        else:
            cat = "Other objects"
        sizes[cat] += size
    total = len(fz.tobytes())
    sizes["File overhead"] = max(0, total - sum(sizes.values()))
    return SpaceUsage(total=max(total, sum(sizes.values())), categories=sizes)


def optimize(fz: pymupdf.Document, options: OptimizeOptions) -> None:
    if options.image_dpi is not None or options.grayscale or options.jpeg_quality < 100:
        threshold = (
            max(options.image_dpi + 1, round(options.image_dpi * options.downsample_above))
            if options.image_dpi is not None
            else None
        )
        fz.rewrite_images(
            dpi_threshold=threshold,
            # MuPDF halves resolution while it stays *above* the target; -1 lets an image land
            # exactly on it (a 600-dpi image reaches 150 dpi instead of stopping at 300)
            dpi_target=max(1, options.image_dpi - 1) if options.image_dpi else 0,
            quality=options.jpeg_quality,
            set_to_gray=options.grayscale,
        )
    if options.subset_fonts:
        fz.subset_fonts()
    if options.remove_thumbnails or options.remove_metadata:
        fz.scrub(
            attached_files=False,
            clean_pages=False,
            embedded_files=False,
            hidden_text=False,
            javascript=False,
            metadata=options.remove_metadata,
            redactions=False,
            remove_links=False,
            reset_fields=False,
            reset_responses=False,
            thumbnails=options.remove_thumbnails,
            xml_metadata=options.remove_metadata,
        )
