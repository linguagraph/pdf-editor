"""Generate the deterministic test-PDF corpus into tests/fixtures/generated/.

Run with:  uv run python scripts/make_fixtures.py [--skip-large]

This script is a dev tool, not part of the package, so it may use PyMuPDF directly.
"""

from __future__ import annotations

import argparse
import io
import random
import re
import shutil
import struct
import sys
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageChops, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "generated"

LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat."
)
A4 = pymupdf.paper_rect("a4")


def _save(doc: pymupdf.Document, name: str, **kwargs: object) -> None:
    doc.set_metadata(
        {
            "title": name.removesuffix(".pdf"),
            "author": "pdfeditor fixtures",
            "creator": "make_fixtures",
        }
    )
    doc.save(OUT / name, garbage=3, deflate=True, **kwargs)
    doc.close()


def _gradient_png(w: int = 320, h: int = 200) -> bytes:
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for x in range(w):
        draw.line([(x, 0), (x, h)], fill=(x * 255 // w, 120, 255 - x * 255 // w))
    draw.ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], outline=(255, 255, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def text_multipage() -> None:
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Page {i + 1} heading", fontsize=20, fontname="hebo")
        rect = pymupdf.Rect(72, 100, A4.width - 72, A4.height - 72)
        page.insert_textbox(rect, (LOREM + "\n\n") * 4, fontsize=11, fontname="helv")
        page.insert_text((72, A4.height - 40), f"needle-{i + 1}", fontsize=9, fontname="cour")
    _save(doc, "text_multipage.pdf")


def images() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    png = _gradient_png()
    page.insert_text((72, 60), "Page with images", fontsize=16)
    page.insert_image(pymupdf.Rect(72, 80, 392, 280), stream=png)
    page.insert_image(pymupdf.Rect(100, 320, 260, 420), stream=png, rotate=90)
    doc.embfile_add("notes", b"attached notes\n", filename="notes.txt", desc="Attached notes")
    _save(doc, "images.pdf")


def several_images() -> None:
    """Three different pictures on two pages: Extract Images takes each once, as a ZIP."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_image(pymupdf.Rect(72, 80, 392, 280), stream=_gradient_png(320, 200))
    page.insert_image(pymupdf.Rect(72, 320, 272, 420), stream=_gradient_png(200, 100))
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_image(pymupdf.Rect(72, 80, 300, 300), stream=_gradient_png(228, 220))
    _save(doc, "several_images.pdf")


def vector_art() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(72, 72, 272, 172))
    shape.finish(color=(0, 0, 1), fill=(0.8, 0.9, 1), width=2)
    shape.draw_circle((400, 150), 60)
    shape.finish(color=(1, 0, 0), fill=(1, 0.8, 0.8), width=1.5)
    shape.draw_bezier((72, 300), (150, 200), (300, 400), (500, 300))
    shape.finish(color=(0, 0.5, 0), width=3)
    shape.draw_polyline([(72, 500), (150, 450), (230, 520), (310, 460)])
    shape.finish(color=(0.3, 0.3, 0.3), width=1, dashes="[4 2] 0")
    shape.commit()
    _save(doc, "vector_art.pdf")


def annotations() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 100), "Highlight this sentence please.", fontsize=12)
    quads = page.search_for("Highlight this sentence", quads=True)
    page.add_highlight_annot(quads)
    note = page.add_text_annot((400, 90), "A sticky note")
    note.set_info(title="Reviewer", content="A sticky note")
    note.update()
    ft = page.add_freetext_annot(pymupdf.Rect(72, 150, 300, 200), "Free text box", fontsize=11)
    ft.update()
    sq = page.add_rect_annot(pymupdf.Rect(72, 250, 200, 330))
    sq.set_colors(stroke=(1, 0, 0))
    sq.update()
    ink = page.add_ink_annot([[(320, 250), (360, 300), (400, 260), (440, 320)]])
    ink.update()
    page.add_line_annot((72, 400), (300, 450)).update()
    _save(doc, "annotations.pdf")


def outline() -> None:
    doc = pymupdf.open()
    for i in range(6):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Chapter {i // 2 + 1}, section {i % 2 + 1}", fontsize=16)
    first = doc[0]
    first.insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(72, 100, 200, 120),
            "page": 3,
            "to": pymupdf.Point(0, 50),
        }
    )
    first.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(72, 130, 200, 150),
            "uri": "https://example.org/",
        }
    )
    toc = []
    for i in range(6):
        if i % 2 == 0:
            toc.append([1, f"Chapter {i // 2 + 1}", i + 1])
        toc.append([2, f"Section {i // 2 + 1}.{i % 2 + 1}", i + 1])
    doc.set_toc(toc)
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1}])
    _save(doc, "outline.pdf")


def outline_irregular() -> None:
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=16)
    # Three levels deep, a step back up to level 2, then a bookmark with no destination (-1).
    doc.set_toc(
        [
            [1, "Deep A", 1],
            [2, "Deep B", 2],
            [3, "Deep C", 3],
            [2, "Back to B level", 4],
            [1, "No destination", -1],
        ]
    )
    _save(doc, "outline_irregular.pdf")


def outline_unsorted() -> None:
    # Top-level bookmarks whose titles are not in alphabetical order, and one title twice: a split
    # by bookmarks must keep document order and give the two "Appendix" parts their own names.
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=16)
    doc.set_toc([[1, "Intro", 1], [1, "Zebra", 2], [1, "Appendix", 3], [1, "Appendix", 4]])
    _save(doc, "outline_unsorted.pdf")


def page_labels() -> None:
    # Eight pages with four rules: roman, decimal with a prefix, prefix only, and alphabetic
    # past Z (MuPDF's own numbering after Z is what the web shows too).
    doc = pymupdf.open()
    for i in range(8):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=16)
    doc.set_page_labels(
        [
            {"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
            {"startpage": 2, "prefix": "A-", "style": "D", "firstpagenum": 1},
            {"startpage": 5, "prefix": "Cover", "style": "", "firstpagenum": 1},
            {"startpage": 6, "prefix": "", "style": "A", "firstpagenum": 26},
        ]
    )
    _save(doc, "page_labels.pdf")


def encrypted() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 72), "Secret content", fontsize=14)
    perms = pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY
    _save(
        doc,
        "encrypted.pdf",
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="user",
        owner_pw="owner",
        permissions=perms,
    )


def encrypted_owner_only() -> None:
    """Encrypted, but with an empty user password: it opens with no password, and only the owner
    password (which nobody types to read it) is set."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 72), "Open content", fontsize=14)
    _save(
        doc,
        "encrypted_owner_only.pdf",
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="",
        owner_pw="owner",
        permissions=pymupdf.PDF_PERM_PRINT,
    )


def hybrid_xref() -> None:
    """A hybrid-reference file (ISO 32000 7.5.8.4), as some office suites write: a classic xref
    table with /XRefStm, and the page tree node only reachable through that stream."""
    objs = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Count 1/Kids[3 0 R]>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 6 0 R/Resources<<>>>>",
        6: b"<</Length 0>>\nstream\n\nendstream",
    }
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for n in (1, 3, 6):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    offsets[4] = len(out)
    packed = zlib.compress(b"2 0 " + objs[2])
    out += (
        b"4 0 obj\n<</Type/ObjStm/N 1/First 4/Filter/FlateDecode/Length %d>>\nstream\n"
        % len(packed)
        + packed
        + b"\nendstream\nendobj\n"
    )
    offsets[5] = len(out)
    entries = zlib.compress(struct.pack(">BHB", 2, 4, 0))
    out += (
        b"5 0 obj\n<</Type/XRef/W[1 2 1]/Index[2 1]/Size 7/Filter/FlateDecode/Length %d>>\nstream\n"
        % len(entries)
        + entries
        + b"\nendstream\nendobj\n"
    )
    table = len(out)
    out += b"xref\n0 2\n0000000000 65535 f \n" + b"%010d 00000 n \n" % offsets[1]
    out += b"3 4\n"
    for n in (3, 4, 5, 6):
        out += b"%010d 00000 n \n" % offsets[n]
    out += b"trailer\n<</Size 7/Root 1 0 R/XRefStm %d>>\nstartxref\n%d\n%%%%EOF\n" % (
        offsets[5],
        table,
    )
    (OUT / "hybrid_xref.pdf").write_bytes(out)


def rotated_pages() -> None:
    doc = pymupdf.open()
    for rot in (0, 90, 180, 270):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Rotation {rot}", fontsize=18)
        page.set_rotation(rot)
    page = doc.new_page(width=A4.height, height=A4.width)  # landscape
    page.insert_text((72, 72), "Landscape page", fontsize=18)
    page.set_cropbox(pymupdf.Rect(36, 36, A4.height - 36, A4.width - 36))
    _save(doc, "rotated_pages.pdf")


def _anchor(page: Any, text: str, y: float) -> Any:
    """Writes the anchor text and returns its rect, taken while the page is still unrotated."""
    page.insert_text((72, y), text, fontsize=14)
    return page.search_for(text)[0]


def links_rotated() -> None:
    """Links on a rotated page and on a plain one (W5.3 S1, the web link list).

    Page 0 is rotated 90 and holds a GOTO link (to page 1), a URI link, and a link with an empty
    destination (MuPDF reports it as a named action, so the web sees it as unsupported). Page 1
    holds a javascript: URI and a Launch action. Every link rect is taken from its anchor text
    before the rotation, so the rect and the text layer describe the same visible-space box,
    which the test checks.
    """
    doc = pymupdf.open()
    doc.new_page(width=A4.width, height=A4.height)
    doc.new_page(width=A4.width, height=A4.height)
    first, second = doc[0], doc[1]
    first.insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": _anchor(first, "Go to page 2", 100),
            "page": 1,
            "to": pymupdf.Point(0, 50),
        }
    )
    first.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": _anchor(first, "Visit example.org", 160),
            "uri": "https://example.org/",
        }
    )
    first.insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": _anchor(first, "Unnamed destination", 220),
            "page": -1,  # no page and no name: MuPDF reports it as a named action
            "to": pymupdf.Point(0, 0),
        }
    )
    first.set_rotation(90)
    second.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": _anchor(second, "Script link", 100),
            "uri": "javascript:alert(1)",
        }
    )
    second.insert_link(
        {
            "kind": pymupdf.LINK_LAUNCH,
            "from": _anchor(second, "Launch link", 160),
            "file": "tool.exe",
        }
    )
    _save(doc, "links_rotated.pdf")


def cjk_text() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 72), "CJK sample:", fontsize=14)
    page.insert_text((72, 110), "简体中文 繁體中文 日本語 한국어", fontsize=16, fontname="china-s")
    _save(doc, "cjk_text.pdf")


def subset_fonts() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    font = pymupdf.Font("cjk")  # MuPDF's built-in fallback font; always available
    page.insert_font(fontname="F1", fontbuffer=font.buffer)
    page.insert_text((72, 72), "Subset embedded font: Hello World 123", fontsize=14, fontname="F1")
    doc.subset_fonts()
    _save(doc, "subset_fonts.pdf")


def licensed_fonts() -> None:
    """Four embedded TrueType faces, each with a different fsType (the OS/2 embedding flags):
    the licence rules of services/export/images.py ``extract_fonts`` decide which are saved.
    MuPDF embeds them as given, so no program is patched after the fact."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    chars = "".join(dict.fromkeys("Hello World 123"))  # each glyph once
    faces = [
        ("Free Sans", 0x0000),  # installable: saved
        ("Print Sans", 0x0004),  # preview and print: saved
        ("Restricted Sans", 0x0002),  # restricted licence: refused
        ("Bitmap Sans", 0x0200),  # bitmap embedding only: refused
        ("Mixed Sans", 0x0202),  # both refusal bits: refused as restricted
    ]
    for i, (family, fs_type) in enumerate(faces):
        face = _build_face(family, "Regular", 400, False, chars, fs_type=fs_type)
        buffer = io.BytesIO()
        face.save(buffer)
        page.insert_font(fontname=f"F{i}", fontbuffer=buffer.getvalue())
        page.insert_text((72, 72 + 24 * i), "Hello World 123", fontsize=14, fontname=f"F{i}")
    _save(doc, "licensed_fonts.pdf")


def large_doc(pages: int = 1000) -> None:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Large document page {i + 1}", fontsize=14)
        page.insert_textbox(pymupdf.Rect(72, 90, A4.width - 72, 400), LOREM, fontsize=10)
    _save(doc, "large_1000.pdf")


PERF_SEED_EVERY = 10  # a new photo every 10 pages, so the pages don't share one image object
PERF_PHOTO_SIZE = (1200, 800)
PERF_FONT_CHARS = "".join(chr(c) for c in range(0x20, 0x7F))  # printable ASCII, for LOREM


def _perf_photo(seed: int) -> bytes:
    """A photo-like JPEG (1200x800 RGB, quality 80): a seeded gradient plus mild noise.

    Deterministic for a given Pillow version: the same seed gives the same bytes (the gradient is
    fixed, and the noise comes from ``random.Random(seed)``), so the sizes in the W0.2 report
    are reproducible from this lock.
    """
    rng = random.Random(seed)
    w, h = PERF_PHOTO_SIZE
    ramp = Image.linear_gradient("L").resize((w, h), Image.Resampling.BILINEAR)
    turn = ramp.transpose(Image.Transpose.ROTATE_90).resize((w, h))
    channels = (
        ramp.point(lambda v: (v + seed * 17) % 256),
        turn.point(lambda v: (255 - v + seed * 29) % 256),
        ramp.point(lambda v: (v // 2 + seed * 5) % 256),
    )
    gradient = Image.merge("RGB", channels)
    noise = Image.frombytes("L", (w, h), rng.randbytes(w * h)).point(lambda v: v // 8)
    photo = ImageChops.add(gradient, Image.merge("RGB", (noise, noise, noise)))
    buf = io.BytesIO()
    photo.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def perf_mixed(pages: int) -> None:
    """Mixed A4 pages for the W0.2 load and render measurements (web/spike/perf).

    Per page: a base-14 heading and a LOREM paragraph in an embedded TrueType face (built here,
    with glyphs for printable ASCII, since the face above covers only ABCabc), one photo-like
    image, and three vector shapes (a rectangle, a Bezier curve, a filled polygon).
    """
    face = _build_face("Perf Sans", "Regular", 400, False, PERF_FONT_CHARS)
    # fontTools stamps the build time into the font; pin it, and skip the random /ID on save, so
    # two builds give the same bytes.
    face["head"].created = 0
    face["head"].modified = 0
    face_bytes = io.BytesIO()
    face.save(face_bytes)
    font = face_bytes.getvalue()
    photos: dict[int, bytes] = {}
    doc = pymupdf.open()
    for i in range(pages):
        seed = i // PERF_SEED_EVERY
        if seed not in photos:
            photos[seed] = _perf_photo(seed)
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 90), f"Mixed page {i + 1}", fontsize=20, fontname="hebo")
        page.insert_font(fontname="PerfSans", fontbuffer=font)
        page.insert_textbox(
            pymupdf.Rect(72, 110, A4.width - 72, 330), LOREM, fontsize=11, fontname="PerfSans"
        )
        page.insert_image(pymupdf.Rect(72, 350, 523, 650), stream=photos[seed])
        page.draw_rect(pymupdf.Rect(72, 680, 250, 760), color=(0, 0, 0), fill=(0.8, 0.2, 0.2))
        page.draw_bezier(
            pymupdf.Point(300, 760),
            pymupdf.Point(360, 660),
            pymupdf.Point(440, 860),
            pymupdf.Point(520, 760),
            color=(0, 0, 1),
            width=2,
        )
        shape = page.new_shape()
        shape.draw_polyline(
            [
                pymupdf.Point(72, 800),
                pymupdf.Point(200, 780),
                pymupdf.Point(260, 830),
                pymupdf.Point(120, 830),
                pymupdf.Point(72, 800),
            ]
        )
        shape.finish(color=(0, 0.4, 0), fill=(0.6, 0.9, 0.6))
        shape.commit()
    _save(doc, f"perf_mixed_{pages}.pdf", no_new_id=True)


SKEW_DEGREES = 4.0  # skewed_scan: counterclockwise as displayed, about the page center


def _scan_png() -> bytes:
    src = pymupdf.open()
    page = src.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 100), "Scanned document text", fontsize=24)
    page.insert_textbox(pymupdf.Rect(72, 140, A4.width - 72, 400), LOREM, fontsize=14)
    png: bytes = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).tobytes("png")
    src.close()
    return png


def scanned() -> None:
    """Image-only page (no text layer), like a scanner produces; used by OCR tests."""
    doc = pymupdf.open()
    doc.new_page(width=A4.width, height=A4.height).insert_image(A4, stream=_scan_png())
    _save(doc, "scanned.pdf")


def skewed_scan() -> None:
    """The ``scanned`` page fed into the scanner crooked (SKEW_DEGREES); deskew tests."""
    img = Image.open(io.BytesIO(_scan_png()))
    img = img.rotate(SKEW_DEGREES, resample=Image.Resampling.BICUBIC, fillcolor=255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    doc.new_page(width=A4.width, height=A4.height).insert_image(A4, stream=buf.getvalue())
    _save(doc, "skewed_scan.pdf")


def broken_xref() -> None:
    """Valid content with a corrupted xref table; readers must repair on open."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 72), "Recovered after xref repair", fontsize=14)
    data = bytearray(doc.tobytes(garbage=3))  # no deflate/object streams: plain xref table
    doc.close()
    start = data.rfind(b"startxref")
    # Point startxref at a bogus offset.
    data[start:] = b"startxref\n999999\n%%EOF\n"
    (OUT / "broken_xref.pdf").write_bytes(bytes(data))


def layers() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    shown = doc.add_ocg("Shown layer", on=True)
    hidden = doc.add_ocg("Hidden layer", on=False)
    page.insert_text((72, 72), "Always visible", fontsize=14)
    page.insert_text((72, 100), "On the shown layer", fontsize=14, oc=shown)
    page.draw_rect(pymupdf.Rect(72, 120, 300, 300), color=(1, 0, 0), fill=(1, 0, 0), oc=hidden)
    _save(doc, "layers.pdf")


def hidden_layers() -> None:
    """Content on layers that are off by default: page text and art, a hidden form XObject, an
    OCMD, a form XObject with its own hidden section and a hidden annotation (sanitizing)."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    shown = doc.add_ocg("Shown layer", on=True)
    hidden = doc.add_ocg("Secret layer", on=False)
    page.insert_text((72, 72), "Always visible", fontsize=14)
    page.insert_text((72, 100), "On the shown layer", fontsize=14, oc=shown)
    page.insert_text((72, 130), "Hidden layer secret", fontsize=14, oc=hidden)
    page.draw_rect(pymupdf.Rect(300, 120, 400, 200), color=(1, 0, 0), fill=(1, 0, 0), oc=hidden)
    ocmd = doc.set_ocmd(ocgs=[hidden], policy="AllOn")
    page.insert_text((72, 160), "Membership secret", fontsize=14, oc=ocmd)
    stamp = pymupdf.open()
    sp = stamp.new_page(width=200, height=40)
    sp.insert_text((10, 25), "Hidden form secret", fontsize=14)
    page.show_pdf_page(pymupdf.Rect(72, 180, 272, 220), stamp, 0, oc=hidden)
    # a form XObject that is visible itself but has a hidden section inside
    font = doc.get_new_xref()
    doc.update_object(font, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    form = doc.get_new_xref()
    doc.update_object(
        form,
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 400 60] "
        f"/Resources << /Font << /F1 {font} 0 R >> /Properties << /P0 {hidden} 0 R >> >> >>",
    )
    doc.update_stream(
        form,
        b"BT /F1 12 Tf 0 40 Td (Visible form text) Tj ET "
        b"/OC /P0 BDC BT /F1 12 Tf 0 10 Td (Nested form secret) Tj ET EMC",
    )
    resources = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
    xobjects = doc.xref_get_key(resources, "XObject")[1]
    doc.xref_set_key(resources, "XObject", xobjects[:-2] + f"/Fx9 {form} 0 R>>")
    extra = doc.get_new_xref()
    doc.update_object(extra, "<<>>")
    doc.update_stream(extra, b"q 1 0 0 1 72 560 cm /Fx9 Do Q")
    contents = " ".join(f"{x} 0 R" for x in page.get_contents())
    doc.xref_set_key(page.xref, "Contents", f"[{contents} {extra} 0 R]")
    page.insert_text((72, 700), "Visible footer", fontsize=12)
    note = page.add_freetext_annot(pymupdf.Rect(400, 700, 550, 730), "Hidden note secret")
    note.set_oc(hidden)
    note.update()
    _save(doc, "hidden_layers.pdf")


def off_page_text() -> None:
    """Text outside the crop box (inside and beyond the media box), a word straddling the crop
    edge, and a rotated page (sanitizing)."""
    doc = pymupdf.open()
    for rotation in (0, 90):
        page = doc.new_page(width=600, height=800)
        page.insert_text((100, 100), "Visible text stays", fontsize=12)
        page.insert_text((100, 785), "Cropped secret", fontsize=12)  # below the crop box
        page.insert_text((100, 1000), "Below media secret", fontsize=12)
        page.insert_text((-400, 300), "Far left secret", fontsize=12)
        page.insert_text((520, 300), "Straddle", fontsize=12)  # crosses the right crop edge
        page.set_cropbox(pymupdf.Rect(50, 50, 550, 750))
        page.set_rotation(rotation)
    _save(doc, "off_page_text.pdf")


def mixed_content() -> None:
    """Paragraphs, an image, a vector shape and a form XObject: content-editing tests."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 60), "Mixed content heading", fontsize=18, fontname="hebo")
    page.insert_textbox(pymupdf.Rect(72, 80, 400, 180), LOREM, fontsize=11, fontname="tiro")
    page.insert_textbox(
        pymupdf.Rect(72, 200, 400, 260), "A second paragraph that stays put.", fontsize=11
    )
    page.insert_image(pymupdf.Rect(420, 80, 540, 180), stream=_gradient_png(120, 100))
    page.draw_rect(pymupdf.Rect(72, 300, 272, 380), color=(0, 0, 1), fill=(0.8, 0.9, 1), width=2)
    logo = pymupdf.open()
    lp = logo.new_page(width=100, height=60)
    lp.draw_circle((50, 30), 25, color=(1, 0, 0), fill=(1, 0.8, 0))
    page.show_pdf_page(pymupdf.Rect(320, 300, 420, 360), logo, 0)  # becomes a form XObject
    page.add_redact_annot(pymupdf.Rect(72, 420, 200, 440))  # a pending user redaction mark
    _save(doc, "mixed_content.pdf")


def malformed_content() -> None:
    """A page whose content stream is broken (an operator inside an array): the content listing
    must fail on it, and the web's content.objects call must answer with no objects."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 72), "Broken page", fontsize=12)
    broken = b"BT /F1 12 Tf 72 72 Td (unclosed ) Tj << /Bad [ ET q Q Q 1 2 3 @@ ("
    doc.update_stream(page.get_contents()[0], broken)
    _save(doc, "malformed_content.pdf")


def sensitive() -> None:
    """Personal data, an image under text, hidden text and other sanitization targets."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_image(pymupdf.Rect(60, 90, 400, 200), stream=_gradient_png(340, 110))
    lines = [
        "Customer: Jane Example",
        "Email: jane.example@example.com",
        "Phone: +1 (555) 010-7788",
        "Card: 4111 1111 1111 1111",
        "IBAN: DE89 3704 0044 0532 0130 00",
        "SSN: 123-45-6789",
    ]
    for i, text in enumerate(lines):
        page.insert_text((72, 110 + 18 * i), text, fontsize=12)
    page.insert_text((72, 300), "Public paragraph that must survive.", fontsize=12)
    page.insert_text((72, 330), "invisible OCR layer text", fontsize=12, render_mode=3)
    page.draw_line((72, 360), (400, 360), color=(0, 0, 0), width=1)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(72, 290, 300, 305),
            "uri": "https://example.org",
        }
    )
    note = page.add_text_annot((450, 110), "Internal reviewer note")
    note.update()
    doc.embfile_add("secret", b"secret attachment", filename="secret.txt")
    doc.set_metadata({"title": "Confidential report", "author": "Jane Example"})
    doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"></x:xmpmeta>')
    catalog = doc.pdf_catalog()
    js = doc.get_new_xref()
    doc.update_object(js, "<< /S /JavaScript /JS (app.alert('hi');) >>")
    doc.xref_set_key(catalog, "OpenAction", f"{js} 0 R")
    doc.save(OUT / "sensitive.pdf", garbage=3, deflate=True)
    doc.close()


def redact_marks_preexisting() -> None:
    """Two redaction marks already in the file, made by another tool: one with an /NM name and
    one without, over the email and the SSN of ``sensitive``. The web's list must show both."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 128), "Email: jane.example@example.com", fontsize=12)
    page.insert_text((72, 200), "SSN: 123-45-6789", fontsize=12)
    page.insert_text((72, 300), "Public paragraph that must survive.", fontsize=12)
    named = page.add_redact_annot(pymupdf.Rect(70, 116, 330, 132))
    doc.xref_set_key(named.xref, "NM", pymupdf.get_pdf_str("preexisting-email"))
    page.add_redact_annot(pymupdf.Rect(70, 188, 260, 204))  # no /NM: the engine names it
    _save(doc, "redact_marks_preexisting.pdf")


def report() -> None:
    """Structured content for export: headings, paragraphs, a ruled table and an image."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 80), "Quarterly Report", fontsize=24, fontname="hebo")
    page.insert_text((72, 120), "Summary", fontsize=16, fontname="hebo")
    page.insert_textbox(
        pymupdf.Rect(72, 130, A4.width - 72, 200),
        "Sales grew in every region. " + LOREM[:160],
        fontsize=11,
        fontname="helv",
    )
    page.insert_text((72, 230), "Figures", fontsize=16, fontname="hebo")
    rows = [
        ["Region", "Units", "Revenue"],
        ["North", "120", "1,440.00"],
        ["South", "95", "1,140.50"],
        ["West", "210", "2,520.75"],
    ]
    x0, y0, cw, rh = 72, 245, 140, 22
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            font = "hebo" if i == 0 else "helv"
            page.insert_text((x0 + j * cw + 5, y0 + i * rh + 15), cell, fontsize=11, fontname=font)
    for i in range(len(rows) + 1):
        page.draw_line((x0, y0 + i * rh), (x0 + 3 * cw, y0 + i * rh))
    for j in range(4):
        page.draw_line((x0 + j * cw, y0), (x0 + j * cw, y0 + len(rows) * rh))
    page.insert_text((72, 370), "Chart", fontsize=16, fontname="hebo")
    page.insert_image(pymupdf.Rect(72, 385, 312, 535), stream=_gradient_png())
    page.insert_textbox(
        pymupdf.Rect(72, 550, A4.width - 72, 620), "End of report.", fontsize=11, fontname="helv"
    )
    page2 = doc.new_page(width=A4.width, height=A4.height)
    page2.insert_text((72, 80), "Appendix", fontsize=16, fontname="hebo")
    page2.insert_textbox(
        pymupdf.Rect(72, 95, A4.width - 72, 300), LOREM, fontsize=11, fontname="helv"
    )
    _save(doc, "report.pdf")


def heavy() -> None:
    """Wasteful on purpose: a 600-dpi photo-like image, a full embedded font, a thumbnail."""
    import random

    w, h = 2400, 1600
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=(y * 255 // h, 90, 255 - y * 255 // h))
    rnd = random.Random(1)
    for _ in range(300):
        x, y, r = rnd.randrange(w), rnd.randrange(h), rnd.randrange(10, 120)
        fill = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)
    img = Image.blend(img, Image.effect_noise((w, h), 30).convert("RGB"), 0.15)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_image(pymupdf.Rect(72, 72, 360, 264), stream=buf.getvalue())  # 4 x 2.67 in
    page.insert_font(fontname="F0", fontbuffer=pymupdf.Font("cjk").buffer)  # not subset
    page.insert_text((72, 320), "Heavy document text", fontname="F0", fontsize=14)
    page.insert_text((72, 350), "Second line in Helvetica", fontsize=11)
    thumb = io.BytesIO()
    img.resize((120, 80)).save(thumb, format="JPEG")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<< /Type /XObject /Subtype /Image /Width 120 /Height 80 >>")
    doc.update_stream(xref, thumb.getvalue(), new=True)
    doc.xref_set_key(page.xref, "Thumb", f"{xref} 0 R")
    _save(doc, "heavy.pdf")


def two_columns() -> None:
    """A two-column label list like appliance manuals: each row is ONE text line whose gutter
    is a run of spaces, in an embedded (non-base-14) font; plus a wrapped paragraph."""
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=595)
    page.insert_font(fontname="F1", fontbuffer=pymupdf.Font("helv").buffer)  # embedded
    rows = [
        ("Предпране", "Забавено стартиране"),
        ("Бързо", "Заключване на вратата"),
        ("Допълнително вода", "Включено/Изключено"),
        ("Против намачкване", "Старт/Пауза"),
    ]
    y = 60.0
    for left, right in rows:
        gap = " " * max(4, 60 - 2 * len(left))
        page.insert_text((40, y), left + gap + right, fontname="F1", fontsize=10,
                         color=(0.14, 0.12, 0.13))  # fmt: skip
        y += 17.5
    paragraph = (
        "Продължителността на програмата се изписва на екрана на машината докато се "
        "избира програма и автоматично се регулира по време на пране."
    )
    page.insert_textbox(pymupdf.Rect(40, 200, 380, 300), paragraph, fontname="F1", fontsize=10)
    _save(doc, "two_columns.pdf")


def tagged() -> None:
    """A tagged PDF with accessibility problems: no /Lang, no DisplayDocTitle, a Figure without
    alt text, a skipped heading level (H1 -> H3), page-2 content ordered before page-1 content,
    and pale (low-contrast) text."""
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=A4.width, height=A4.height)
    p1, p2 = doc[0], doc[1]  # re-fetched: adding a page orphans earlier Page objects
    for page in (p1, p2):
        page.insert_text((72, 72), "x", fontname="helv", fontsize=1)  # creates the font resource
    p1.insert_image(pymupdf.Rect(72, 150, 272, 250), stream=_gradient_png(200, 100))
    font = p1.get_fonts(full=True)[0][4]
    image = p1.get_images(full=True)[0][7]
    h = A4.height

    def text(tag: str, mcid: int, y: float, size: float, words: str, rgb: str = "0 0 0") -> str:
        return (
            f"/{tag} <</MCID {mcid}>> BDC BT {rgb} rg /{font} {size} Tf 72 {h - y} Td "
            f"({words}) Tj ET EMC\n"
        )

    c1 = (
        text("H1", 0, 72, 20, "Annual Report")
        + text("P", 1, 110, 11, "An introductory paragraph.")
        + f"/Figure <</MCID 2>> BDC q 200 0 0 100 72 {h - 250} cm /{image} Do Q EMC\n"
        + text("H3", 3, 290, 14, "A skipped heading level")
        + text("P", 4, 320, 11, "Pale text that is hard to read.", "0.8 0.8 0.8")
    )
    c2 = text("H2", 0, 72, 16, "Second page heading") + text("P", 1, 110, 11, "Page two text.")
    for page, content in ((p1, c1), (p2, c2)):
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, content.encode("latin-1"))
        doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")

    root = doc.get_new_xref()
    document = doc.get_new_xref()

    def elem(tag: str, page: pymupdf.Page, mcid: int, extra: str = "") -> int:
        x = doc.get_new_xref()
        doc.update_object(x, f"<< /Type /StructElem /S /{tag} /P {document} 0 R "
                             f"/Pg {page.xref} 0 R /K {mcid} {extra}>>")  # fmt: skip
        return x

    h1 = elem("H1", p1, 0)
    para = elem("P", p1, 1)
    fig = elem("Figure", p1, 2)
    h3 = elem("H3", p1, 3)
    pale = elem("P", p1, 4)
    h2 = elem("H2", p2, 0)
    para2 = elem("P", p2, 1)
    # reading order: page 2's paragraph comes before the rest of page 1
    kids = [h1, para, para2, fig, h3, pale, h2]
    doc.update_object(document, f"<< /Type /StructElem /S /Document /P {root} 0 R "
                                f"/K [{' '.join(f'{k} 0 R' for k in kids)}] >>")  # fmt: skip
    parents = doc.get_new_xref()
    doc.update_object(parents, f"<< /Nums [0 [{h1} 0 R {para} 0 R {fig} 0 R {h3} 0 R "
                               f"{pale} 0 R] 1 [{h2} 0 R {para2} 0 R]] >>")  # fmt: skip
    doc.update_object(root, f"<< /Type /StructTreeRoot /K {document} 0 R "
                            f"/ParentTree {parents} 0 R /ParentTreeNextKey 2 >>")  # fmt: skip
    doc.xref_set_key(p1.xref, "StructParents", "0")
    doc.xref_set_key(p2.xref, "StructParents", "1")
    catalog = doc.pdf_catalog()
    doc.xref_set_key(catalog, "StructTreeRoot", f"{root} 0 R")
    doc.xref_set_key(catalog, "MarkInfo", "<< /Marked true >>")
    _save(doc, "tagged.pdf")


def letter_spacing() -> None:
    """Text set with character spacing (Tc) and word spacing (Tw), written directly into the
    content stream: a tracked heading (wide enough that extraction invents spaces between its
    letters), a spaced single line, a spaced paragraph and a centered spaced line."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 72), "x", fontname="helv", fontsize=1)  # creates the font resource
    font = page.get_fonts(full=True)[0][4]
    h = A4.height
    center = A4.width / 2
    centered_width = pymupdf.get_text_length("Centered spaced line", "helv", 12) + 0.5 * 19 + 6
    content = (
        f"BT /{font} 18 Tf 2 Tc 72 {h - 80} Td (Tracked heading) Tj ET\n"
        f"BT /{font} 12 Tf 0.6 Tc 4 Tw 72 {h - 140} Td (Spaced words in one line) Tj ET\n"
        f"BT /{font} 11 Tf 0.4 Tc 2 Tw 14 TL 72 {h - 200} Td (A spaced paragraph with) Tj "
        "T* (several lines of text that) Tj T* (all share the spacing.) Tj ET\n"
        f"BT /{font} 12 Tf 0.5 Tc 3 Tw {center - centered_width / 2:.3f} {h - 320} Td "
        "(Centered spaced line) Tj ET\n"
    )
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, content.encode("latin-1"))
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    _save(doc, "letter_spacing.pdf")


def print_to_pdf() -> None:
    """Like Microsoft's "Print to PDF": the embedded font is called ``CIDFont+F1`` instead of
    its real name, the page number comes first in content order, columns are lined up with
    runs of spaces and a bullet is set larger than its text. Bulgarian text, a picture between
    two paragraphs."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_font(fontname="F1", fontbuffer=pymupdf.Font("cjk").buffer)
    page.insert_text((A4.width / 2, A4.height - 30), "1", fontsize=9, fontname="F1")
    page.insert_text((72, 80), "Символи за управление", fontsize=16, fontname="F1")
    page.insert_text((72, 120), "Предпране" + " " * 12 + "Забавено стартиране", fontname="F1")
    page.insert_text((72, 150), "Бутон за температурата на пералнята.", fontsize=10, fontname="F1")
    page.insert_image(pymupdf.Rect(72, 180, 232, 280), stream=_gradient_png())
    page.insert_text((72, 310), "•", fontsize=14, fontname="F1")
    page.insert_text(
        (84, 310), "Натиснете бутона, за да изберете цикъл на пране.", fontsize=10, fontname="F1"
    )
    for xref, *_ in page.get_fonts(full=True):
        doc.xref_set_key(xref, "BaseFont", "/CIDFont+F1")
        for key in ("DescendantFonts", "FontDescriptor"):
            kind, value = doc.xref_get_key(xref, key)
            if kind == "array":  # [n 0 R]
                sub = int(value.strip("[]").split()[0])
                doc.xref_set_key(sub, "BaseFont", "/CIDFont+F1")
                kind, value = doc.xref_get_key(sub, "FontDescriptor")
            if kind == "xref":
                doc.xref_set_key(int(value.split()[0]), "FontName", "/CIDFont+F1")
    _save(doc, "print_to_pdf.pdf")


def annotations_unnamed() -> None:
    """Comments without /NM, as other applications often write them (issue #168).

    Page 1 has two squares and a sticky note, page 2 a redaction mark, all with the /NM removed
    so the engine has to give each one a synthetic name.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    squares = [
        page.add_rect_annot(pymupdf.Rect(72, 120, 200, 180)),
        page.add_rect_annot(pymupdf.Rect(72, 220, 200, 280)),
    ]
    for sq, colour, text in zip(
        squares, [(1, 0, 0), (0, 0, 1)], ["Box one", "Box two"], strict=True
    ):
        sq.set_colors(stroke=colour)
        sq.set_info(content=text)
        sq.update()
    note = page.add_text_annot((400, 90), "Note three")
    note.set_info(title="Reviewer", content="Note three")
    note.update()
    page2 = doc.new_page(width=A4.width, height=A4.height)
    page2.insert_text((72, 100), "Sensitive words here.", fontsize=12)
    page2.add_redact_annot(pymupdf.Rect(72, 85, 200, 110))
    xrefs = [a.xref for pno in range(len(doc)) for a in doc.load_page(pno).annots()]
    for xref in xrefs:
        doc.xref_set_key(xref, "NM", "null")
    _save(doc, "annotations_unnamed.pdf")


# The pikepdf builders below are copies of the test helpers they replace (tests/engine/
# test_shared_objects.py), so the bytes match what those tests built. pikepdf runs only on the
# host, so each import stays inside its function; the tests load the output with fixture_pdf().


def _pikepdf_content(pdf: Any, text: str) -> Any:
    ops = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET q 100 0 0 100 72 400 cm /Im1 Do Q"
    return pdf.make_stream(ops.encode())


def _shared_objects(share: str) -> None:
    """Two pages that share ``share``: their content (stream and array), one resource
    dictionary (with indirect category dictionaries) or one annotation (and the /Annots array
    listing it). Font and image are always shared."""
    import pikepdf
    from pikepdf import Array, Dictionary, Name, String

    pdf = pikepdf.new()
    font = pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.Type1,
            BaseFont=Name.Helvetica,
            Encoding=Name.WinAnsiEncoding,
        )
    )
    image = pdf.make_stream(
        bytes(4 * (x ^ y) for y in range(8) for x in range(8)),
        Type=Name.XObject,
        Subtype=Name.Image,
        Width=8,
        Height=8,
        ColorSpace=Name.DeviceGray,
        BitsPerComponent=8,
    )

    def resources() -> Dictionary:
        return Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im1=image))

    shared_resources = pdf.make_indirect(
        Dictionary(
            Font=pdf.make_indirect(Dictionary(F1=font)),
            XObject=pdf.make_indirect(Dictionary(Im1=image)),
        )
    )
    # an indirect array holding one stream: stamps append to the array, edits rewrite the stream
    shared_contents = pdf.make_indirect(Array([_pikepdf_content(pdf, "Shared words")]))
    annot = pdf.make_indirect(
        Dictionary(
            Type=Name.Annot,
            Subtype=Name.Square,
            Rect=[300, 300, 400, 400],
            C=[1, 0, 0],
            NM=String("shared-square"),
            AP=Dictionary(
                N=pdf.make_stream(
                    b"1 0 0 RG 4 w 2 2 96 96 re S",
                    Type=Name.XObject,
                    Subtype=Name.Form,
                    BBox=[0, 0, 100, 100],
                )
            ),
        )
    )
    shared_annots = pdf.make_indirect(Array([annot]))  # adding to it would add to both pages
    for n in range(2):
        page = Dictionary(
            Type=Name.Page,
            MediaBox=[0, 0, 612, 792],
            Contents=shared_contents
            if share == "contents"
            else _pikepdf_content(pdf, f"Page {n + 1} words"),
            Resources=shared_resources if share == "resources" else resources(),
        )
        if share == "annotation":
            page.Annots = shared_annots
        pdf.pages.append(pikepdf.Page(pdf.make_indirect(page)))
    if share == "annotation":
        annot.P = pdf.pages[0].obj
    pdf.save(OUT / f"shared_objects_{share}.pdf")


def shared_objects_listed_twice() -> None:
    """A page tree naming the same page object twice (as MuPDF's select used to make)."""
    import pikepdf
    from pikepdf import Array, Dictionary, Name

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 300))
    page = pdf.pages[0].obj
    page.Contents = _pikepdf_content(pdf, "Twice")
    page.Resources = Dictionary(
        Font=Dictionary(
            F1=pdf.make_indirect(
                Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
            )
        )
    )
    pdf.Root.Pages.Kids = Array([page, page])
    pdf.Root.Pages.Count = 2
    pdf.save(OUT / "shared_objects_listed_twice.pdf")


def page_marks_acrobat_like() -> None:
    """A page with an Acrobat-style watermark (an artifact drawing a form XObject whose piece
    info names it), a word processor's running header (a plain pagination artifact) and
    body text (copy of tests/engine/test_page_marks.py _acrobat_like)."""
    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 300))
    page = pdf.pages[0]
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    form = pikepdf.Stream(pdf, b"BT /F1 20 Tf 0 0 Td (ACRO-WM) Tj ET")
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = [0, 0, 200, 40]
    form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    form.PieceInfo = pikepdf.Dictionary(
        ADBE_CompoundType=pikepdf.Dictionary(
            LastModified=pikepdf.String("D:20240101000000"), Private=pikepdf.Name.Watermark
        )
    )
    page.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font),
        XObject=pikepdf.Dictionary(Fm0=pdf.make_indirect(form)),
        Properties=pikepdf.Dictionary(
            MC0=pikepdf.Dictionary(Type=pikepdf.Name.Pagination, Subtype=pikepdf.Name.Header)
        ),
    )
    page.Contents = pdf.make_stream(
        b"/Artifact /MC0 BDC BT /F1 10 Tf 20 280 Td (Running header) Tj ET EMC\n"
        b"BT /F1 12 Tf 20 150 Td (Body text) Tj ET\n"
        b"/Artifact <</Subtype /Watermark /Type /Pagination >>BDC "
        b"q 1 0 0 1 50 60 cm /Fm0 Do Q EMC\n"
    )
    pdf.save(OUT / "page_marks_acrobat_like.pdf")


def incremental_sigflags_empty_embedded() -> None:
    """A copy of text_multipage with /SigFlags 3 and an empty /Names /EmbeddedFiles (copy of
    tests/engine/test_mupdf_incremental.py)."""
    import pikepdf

    with pikepdf.open(OUT / "text_multipage.pdf") as pdf:
        pdf.Root.AcroForm = pikepdf.Dictionary(SigFlags=3, Fields=pikepdf.Array())
        pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array()))
        pdf.save(OUT / "incremental_sigflags_empty_embedded.pdf")


def structure_indirect_values() -> None:
    """A tagged file whose /Lang, /DisplayDocTitle and /Alt are indirect objects (copy of
    tests/engine/test_structure.py test_indirect_values_are_read)."""
    import pikepdf

    with pikepdf.open(OUT / "tagged.pdf") as pdf:
        pdf.Root.Lang = pdf.make_indirect(pikepdf.String("de-DE"))
        pdf.Root.ViewerPreferences = pikepdf.Dictionary(
            DisplayDocTitle=pdf.make_indirect(pikepdf.Object.parse(b"true"))
        )
        pdf.Root.StructTreeRoot.K.K[3].Alt = pdf.make_indirect(pikepdf.String("A chart"))
        pdf.save(OUT / "structure_indirect_values.pdf")


def comments_literal_hash_nm() -> None:
    """A copy of annotations_unnamed whose square has the literal /NM "#123" (copy of
    tests/actions/test_comments.py test_literal_hash_name_from_a_file_stays_addressable)."""
    import pikepdf

    path = OUT / "comments_literal_hash_nm.pdf"
    shutil.copy2(OUT / "annotations_unnamed.pdf", path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        square = next(a for a in pdf.pages[0].Annots if a.get("/Subtype") == "/Square")
        square.NM = pikepdf.String("#123")
        pdf.save(path)


def undo_signed_catalog() -> None:
    """The images sample with a signature field whose catalog says it is signed, /SigFlags 3
    (copy of tests/unit/test_undo_session.py; needs images.pdf, generated first)."""
    import pikepdf

    src = pymupdf.open(OUT / "images.pdf")
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Sig1"
    widget.rect = pymupdf.Rect(50, 50, 200, 100)
    src[0].add_widget(widget)
    path = OUT / "undo_signed_catalog.pdf"
    src.save(path)
    src.close()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm.SigFlags = 3
        pdf.save(path)


def annotations_form() -> None:
    """The annotations sample plus a text field on page 1 (a form widget must survive a flatten;
    needs annotations.pdf, generated first)."""
    src = pymupdf.open(OUT / "annotations.pdf")
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "Name1"
    widget.field_value = "Ada"
    widget.rect = pymupdf.Rect(72, 500, 300, 520)
    src[0].add_widget(widget)
    src.save(OUT / "annotations_form.pdf")
    src.close()


def annotations_signed() -> None:
    """The annotations sample with a signature field and /SigFlags 3, so the catalog says it is
    signed while the comments stay (a flatten of a signed document keeps the signature field and
    the save incremental; needs annotations.pdf, generated first)."""
    import pikepdf

    src = pymupdf.open(OUT / "annotations.pdf")
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Sig1"
    widget.rect = pymupdf.Rect(50, 500, 200, 540)
    src[0].add_widget(widget)
    path = OUT / "annotations_signed.pdf"
    src.save(path)
    src.close()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm.SigFlags = 3
        pdf.save(path)


def comments_flattenable_mixed() -> None:
    """The annotations sample plus a link and a popup on page 1, a hidden comment, and a second
    page that holds only a link (copy of tests/services/test_comments_flattenable.py; needs
    annotations.pdf, generated first)."""
    import pikepdf

    path = OUT / "comments_flattenable_mixed.pdf"
    shutil.copy2(OUT / "annotations.pdf", path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        note = next(a for a in page.obj.Annots if a.get("/Subtype") == "/Text")
        link = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Link,
                Rect=[10, 10, 100, 30],
                Border=[0, 0, 0],
            )
        )
        popup = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Popup,
                Rect=[400, 400, 520, 470],
                Parent=note,
                Open=False,
            )
        )
        page.obj.Annots.append(link)
        page.obj.Annots.append(popup)
        # a hidden comment: flatten skips it, so it isn't counted
        square = next(a for a in page.obj.Annots if a.get("/Subtype") == "/Square")
        square.F = 2
        second = pdf.add_blank_page()
        second_link = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Link,
                Rect=[10, 10, 100, 30],
                Border=[0, 0, 0],
            )
        )
        second.obj.Annots = pikepdf.Array([second_link])
        pdf.save(path)


def comments_threaded() -> None:
    """Threaded comments for comments.list (W7.1b S1). Page 1: note B above note A, and A has a
    text reply and a status-only reply (/State Accepted), dated so the text reply is older. Page
    2: note C. Replies carry /IRT to their parent, as Acrobat writes them (/RT /R)."""
    import pikepdf

    path = OUT / "comments_threaded.pdf"
    doc = pymupdf.open()
    doc.new_page(width=A4.width, height=A4.height).insert_text((72, 300), "Page one.", fontsize=12)
    doc.new_page(width=A4.width, height=A4.height).insert_text((72, 300), "Page two.", fontsize=12)
    _save(doc, "comments_threaded.pdf")

    def stamp(value: str) -> pikepdf.String:
        return pikepdf.String(f"D:{value}Z")

    def note(
        pdf: pikepdf.Pdf,
        rect: list[int],
        name: str,
        author: str,
        text: str,
        date: str,
        **extra: object,
    ) -> pikepdf.Object:
        return pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Text,
                Rect=rect,
                F=4,
                NM=pikepdf.String(name),
                T=pikepdf.String(author),
                Contents=pikepdf.String(text),
                CreationDate=stamp(date),
                M=stamp(date),
                **extra,
            )
        )

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        a = note(
            pdf, [72, 672, 92, 692], "note-a", "Ada", "Please check the total", "20260101100000"
        )
        b = note(pdf, [72, 742, 92, 762], "note-b", "Cy", "Check the heading", "20260101110000")
        text_reply = note(
            pdf,
            [120, 672, 140, 692],
            "reply-a1",
            "Bob",
            "Fixed it",
            "20260102090000",
            IRT=a,
            RT=pikepdf.Name.R,
        )
        status = note(
            pdf,
            [168, 672, 188, 692],
            "reply-a2",
            "Ada",
            "",
            "20260103090000",
            IRT=a,
            RT=pikepdf.Name.R,
            State=pikepdf.String("Accepted"),
            StateModel=pikepdf.String("Review"),
        )
        c = note(pdf, [72, 672, 92, 692], "note-c", "Dee", "On page two", "20260101120000")
        pdf.pages[0].obj.Annots = pikepdf.Array([a, b, text_reply, status])
        pdf.pages[1].obj.Annots = pikepdf.Array([c])
        pdf.save(path)


def damaged_pubsec_filter() -> None:
    """A copy of report whose trailer names an /Encrypt dictionary with the Adobe.PubSec
    handler: the rewrite is pikepdf's, the trailer edit is bytes (copy of
    tests/engine/test_damaged_files.py _with_encrypt_filter)."""
    import pikepdf

    handler = "Adobe.PubSec"
    out = OUT / "damaged_pubsec_filter.pdf"
    with pikepdf.open(OUT / "report.pdf") as pdf:
        pdf.save(out, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    raw = out.read_bytes()
    n = max(int(m) for m in re.findall(rb"(\d+) 0 obj", raw)) + 1
    obj = f"{n} 0 obj\n<< /Filter /{handler} /V 4 /R 4 >>\nendobj\n".encode()
    at = raw.rindex(b"\nxref") + 1  # before the classic xref section (not "startxref")
    raw = raw[:at] + obj + raw[at:]
    raw = re.sub(rb"trailer\s*<<", b"trailer << /Encrypt %d 0 R" % n, raw, count=1)
    out.write_bytes(raw)


GENERATORS: dict[str, Callable[[], None]] = {
    "print_to_pdf": print_to_pdf,
    "letter_spacing": letter_spacing,
    "tagged": tagged,
    "two_columns": two_columns,
    "heavy": heavy,
    "report": report,
    "sensitive": sensitive,
    "redact_marks_preexisting": redact_marks_preexisting,
    "mixed_content": mixed_content,
    "malformed_content": malformed_content,
    "layers": layers,
    "hidden_layers": hidden_layers,
    "off_page_text": off_page_text,
    "scanned": scanned,
    "skewed_scan": skewed_scan,
    "broken_xref": broken_xref,
    "text_multipage": text_multipage,
    "images": images,
    "several_images": several_images,
    "vector_art": vector_art,
    "annotations": annotations,
    "annotations_unnamed": annotations_unnamed,
    "outline": outline,
    "outline_irregular": outline_irregular,
    "outline_unsorted": outline_unsorted,
    "page_labels": page_labels,
    "encrypted": encrypted,
    "encrypted_owner_only": encrypted_owner_only,
    "hybrid_xref": hybrid_xref,
    "rotated_pages": rotated_pages,
    "links_rotated": links_rotated,
    "cjk_text": cjk_text,
    "subset_fonts": subset_fonts,
    "licensed_fonts": licensed_fonts,
    "large_1000": large_doc,
    "perf_mixed_10": lambda: perf_mixed(10),
    "perf_mixed_200": lambda: perf_mixed(200),
    "perf_mixed_1000": lambda: perf_mixed(1000),
    "shared_objects_annotation": lambda: _shared_objects("annotation"),
    "shared_objects_listed_twice": shared_objects_listed_twice,
    "page_marks_acrobat_like": page_marks_acrobat_like,
    "incremental_sigflags_empty_embedded": incremental_sigflags_empty_embedded,
    "structure_indirect_values": structure_indirect_values,
    "comments_literal_hash_nm": comments_literal_hash_nm,
    "undo_signed_catalog": undo_signed_catalog,
    "comments_flattenable_mixed": comments_flattenable_mixed,
    "comments_threaded": comments_threaded,
    "annotations_form": annotations_form,
    "annotations_signed": annotations_signed,
    "damaged_pubsec_filter": damaged_pubsec_filter,
}


FONTS_OUT = OUT / "fonts"
LATIN = "ABCabc"
CYRILLIC = "АБВабв"


def _square_glyph() -> object:
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 500))
    pen.lineTo((500, 500))
    pen.lineTo((500, 0))
    pen.closePath()
    return pen.glyph()


def _build_face(family: str, style: str, weight: int, italic: bool, chars: str, fs_type: int = 0):
    """A minimal (but real, loadable) TrueType face with square glyphs for ``chars``."""
    from fontTools.fontBuilder import FontBuilder

    glyph_order = [".notdef"] + [f"g{ord(c):04x}" for c in chars]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({ord(c): f"g{ord(c):04x}" for c in chars})
    glyph = _square_glyph()
    fb.setupGlyf({name: glyph for name in glyph_order})
    fb.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    selection = 0x01 if italic else 0x40
    fb.setupOS2(usWeightClass=weight, fsSelection=selection, fsType=fs_type)
    fb.setupNameTable({"familyName": family, "styleName": style})
    fb.setupPost()
    return fb.font


def _build_cff(family: str, style: str, weight: int, italic: bool, chars: str):
    """A minimal (but real, loadable) OpenType face with CFF outlines: square glyphs."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen

    glyph_order = [".notdef"] + [f"g{ord(c):04x}" for c in chars]
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({ord(c): f"g{ord(c):04x}" for c in chars})
    charstrings = {}
    for name in glyph_order:
        pen = T2CharStringPen(600, None)
        pen.moveTo((0, 0))
        pen.lineTo((500, 0))
        pen.lineTo((500, 500))
        pen.lineTo((0, 500))
        pen.closePath()
        charstrings[name] = pen.getCharString()
    fb.setupCFF(family.replace(" ", ""), {}, charstrings, {})
    fb.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupOS2(usWeightClass=weight, fsSelection=0x01 if italic else 0x40, fsType=0)
    fb.setupNameTable({"familyName": family, "styleName": style})
    fb.setupPost()
    return fb.font


def _save_collection(path: Path, faces: list[tuple[str, int]]) -> None:
    """A TTC of synthetic Latin faces, one per ``(family, fsType)``."""
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.ttCollection import TTCollection

    parts = []
    for number, (family, fs_type) in enumerate(faces):
        part = FONTS_OUT / f"_ttc_part{number}.ttf"
        _build_face(family, "Regular", 400, False, LATIN, fs_type=fs_type).save(part)
        parts.append(part)
    collection = TTCollection()
    collection.fonts = [TTFont(part) for part in parts]
    collection.save(path)
    for part in parts:
        part.unlink()


def generate_fonts() -> None:
    """Synthetic font files for ``tests/services/test_fonts.py``: no real font is committed,
    and none of this depends on what's installed on the machine running the tests."""
    FONTS_OUT.mkdir(parents=True, exist_ok=True)

    _build_face("Test Sans", "Regular", 400, False, LATIN).save(FONTS_OUT / "TestSans-Regular.ttf")
    _build_face("Test Sans", "Bold", 700, False, LATIN).save(FONTS_OUT / "TestSans-Bold.ttf")
    _build_face("Test Sans", "Italic", 400, True, LATIN).save(FONTS_OUT / "TestSans-Italic.ttf")
    _build_face("Test Sans", "Bold Italic", 700, True, LATIN).save(
        FONTS_OUT / "TestSans-BoldItalic.ttf"
    )

    from fontTools.ttLib import TTFont
    from fontTools.ttLib.ttCollection import TTCollection

    path_a, path_b = FONTS_OUT / "_tcc_a.ttf", FONTS_OUT / "_tcc_b.ttf"
    _build_face("Test Collection One", "Regular", 400, False, LATIN).save(path_a)
    _build_face("Test Collection Two", "Regular", 400, False, LATIN).save(path_b)
    collection = TTCollection()
    collection.fonts = [TTFont(path_a), TTFont(path_b)]
    collection.save(FONTS_OUT / "TestCollection.ttc")
    path_a.unlink()
    path_b.unlink()

    _build_face("Test Restricted", "Regular", 400, False, LATIN, fs_type=0x0002).save(
        FONTS_OUT / "TestRestricted-Regular.ttf"
    )

    _build_face("Test Cyrillic", "Regular", 400, False, LATIN + CYRILLIC).save(
        FONTS_OUT / "TestCyrillic-Regular.ttf"
    )

    (FONTS_OUT / "Garbage.ttf").write_bytes(b"not a font, just garbage bytes" * 10)

    # ~2,000 glyphs, for the subsetting test (engine/mupdf/test_fonts.py): a full copy of this
    # embedded in a PDF must be far bigger than the subset a one-word edit actually needs.
    large_chars = "".join(chr(c) for c in range(0x0100, 0x0100 + 2000))
    _build_face("Test Large", "Regular", 400, False, large_chars).save(
        FONTS_OUT / "TestLarge-Regular.ttf"
    )
    # W6.9 slice 1 (fonts.validate): one fact differs in each file, so each test names one rule.
    _build_face("Test Bitmap Only", "Regular", 400, False, LATIN, fs_type=0x0200).save(
        FONTS_OUT / "TestBitmapOnly-Regular.ttf"
    )
    _build_face("Test Mixed Bits", "Regular", 400, False, LATIN, fs_type=0x0202).save(
        FONTS_OUT / "TestMixedBits-Regular.ttf"
    )
    _build_face("Test Preview Restricted", "Regular", 400, False, LATIN, fs_type=0x0006).save(
        FONTS_OUT / "TestPreviewRestricted-Regular.ttf"
    )
    no_cmap = _build_face("Test No Cmap", "Regular", 400, False, LATIN)
    del no_cmap["cmap"]
    no_cmap.save(FONTS_OUT / "TestNoCmap-Regular.ttf")
    _build_cff("Test Cff", "Regular", 400, False, LATIN).save(FONTS_OUT / "TestCff-Regular.otf")
    _save_collection(
        FONTS_OUT / "TestMixedCollection.ttc",
        [("Test Mixed One", 0), ("Test Mixed Two", 0x0002)],
    )
    _save_collection(
        FONTS_OUT / "TestRestrictedCollection.ttc",
        [("Test Restricted One", 0x0002), ("Test Restricted Two", 0x0002)],
    )
    print("generated fonts/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-large", action="store_true", help="skip the 1000-page fixture")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, gen in GENERATORS.items():
        if args.skip_large and name in {"large_1000", "perf_mixed_1000"}:
            continue
        gen()
        print(f"generated {name}.pdf")
    generate_fonts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
