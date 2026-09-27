"""Generate the deterministic test-PDF corpus into tests/fixtures/generated/.

Run with:  uv run python scripts/make_fixtures.py [--skip-large]

This script is a dev tool, not part of the package, so it may use PyMuPDF directly.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

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


def large_doc(pages: int = 1000) -> None:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((72, 72), f"Large document page {i + 1}", fontsize=14)
        page.insert_textbox(pymupdf.Rect(72, 90, A4.width - 72, 400), LOREM, fontsize=10)
    _save(doc, "large_1000.pdf")


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


GENERATORS: dict[str, Callable[[], None]] = {
    "letter_spacing": letter_spacing,
    "tagged": tagged,
    "two_columns": two_columns,
    "heavy": heavy,
    "report": report,
    "sensitive": sensitive,
    "mixed_content": mixed_content,
    "layers": layers,
    "hidden_layers": hidden_layers,
    "off_page_text": off_page_text,
    "scanned": scanned,
    "skewed_scan": skewed_scan,
    "broken_xref": broken_xref,
    "text_multipage": text_multipage,
    "images": images,
    "vector_art": vector_art,
    "annotations": annotations,
    "outline": outline,
    "encrypted": encrypted,
    "rotated_pages": rotated_pages,
    "cjk_text": cjk_text,
    "subset_fonts": subset_fonts,
    "large_1000": large_doc,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-large", action="store_true", help="skip the 1000-page fixture")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, gen in GENERATORS.items():
        if args.skip_large and name == "large_1000":
            continue
        gen()
        print(f"generated {name}.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
