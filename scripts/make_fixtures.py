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


def scanned() -> None:
    """Image-only page (no text layer), like a scanner produces; used by OCR tests."""
    src = pymupdf.open()
    page = src.new_page(width=A4.width, height=A4.height)
    page.insert_text((72, 100), "Scanned document text", fontsize=24)
    page.insert_textbox(pymupdf.Rect(72, 140, A4.width - 72, 400), LOREM, fontsize=14)
    png = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).tobytes("png")
    src.close()
    doc = pymupdf.open()
    doc.new_page(width=A4.width, height=A4.height).insert_image(A4, stream=png)
    _save(doc, "scanned.pdf")


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


GENERATORS: dict[str, Callable[[], None]] = {
    "sensitive": sensitive,
    "mixed_content": mixed_content,
    "layers": layers,
    "scanned": scanned,
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
