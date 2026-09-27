"""Header/footer, Bates numbering, watermark and background (drawn into page content)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from pdfeditor.engine.base import ColorMode, Document, Engine, RenderRequest
from pdfeditor.model.color import BLACK, Color
from pdfeditor.model.geometry import Matrix, Point, Rect
from pdfeditor.model.pages import ImageStamp, TextStamp

TOKEN = re.compile(r"<<(page|pages|label|date|file|bates)>>")
TOKENS_HELP = "<<page>>, <<pages>>, <<label>>, <<date>>, <<file>>, <<bates>>"


class Slot(Enum):
    HEADER_LEFT = "header_left"
    HEADER_CENTER = "header_center"
    HEADER_RIGHT = "header_right"
    FOOTER_LEFT = "footer_left"
    FOOTER_CENTER = "footer_center"
    FOOTER_RIGHT = "footer_right"


@dataclass
class HeaderFooter:
    texts: dict[Slot, str] = field(default_factory=dict)
    font: str = "helv"
    font_size: float = 9.0
    color: Color = BLACK
    margin_x: float = 36.0  # from the left/right page edge to the text
    margin_top: float = 24.0  # from the page edge to the header baseline area
    margin_bottom: float = 24.0
    bates_prefix: str = ""
    bates_start: int = 1
    bates_digits: int = 6
    bates_suffix: str = ""
    date_format: str = "%Y-%m-%d"


def bates_number(spec: HeaderFooter, index: int) -> str:
    number = f"{spec.bates_start + index:0{spec.bates_digits}d}"
    return f"{spec.bates_prefix}{number}{spec.bates_suffix}"


def expand(
    text: str,
    spec: HeaderFooter,
    page: int,
    count: int,
    label: str,
    file_name: str,
    bates_index: int,
) -> str:
    values = {
        "page": str(page + 1),
        "pages": str(count),
        "label": label,
        "date": date.today().strftime(spec.date_format),
        "file": file_name,
        "bates": bates_number(spec, bates_index),
    }
    return TOKEN.sub(lambda m: values[m.group(1)], text)


def apply_header_footer(
    engine: Engine, doc: Document, spec: HeaderFooter, pages: Sequence[int], file_name: str = ""
) -> int:
    """Stamp header/footer text on ``pages``; returns the number of text items drawn."""
    drawn = 0
    count = doc.page_count
    for n, index in enumerate(pages):
        page = doc.page(index)
        rect = page.rect
        label = doc.page_label(index)
        for slot, template in spec.texts.items():
            if not template:
                continue
            text = expand(template, spec, index, count, label, file_name, n)
            width = engine.text_width(text, spec.font, spec.font_size)
            if slot.value.endswith("left"):
                x = rect.x0 + spec.margin_x
            elif slot.value.endswith("right"):
                x = rect.x1 - spec.margin_x - width
            else:
                x = rect.x0 + (rect.width - width) / 2
            if slot.value.startswith("header"):
                y = rect.y0 + spec.margin_top + spec.font_size
            else:
                y = rect.y1 - spec.margin_bottom
            page.stamp_text(TextStamp(text, Point(x, y), spec.font_size, spec.font, spec.color))
            drawn += 1
    return drawn


@dataclass
class Watermark:
    text: str = ""
    image: bytes | None = None
    font: str = "hebo"
    font_size: float = 60.0
    color: Color = field(default_factory=lambda: Color(0.75, 0.1, 0.1))
    opacity: float = 0.3
    angle: float = 45.0  # text only
    scale: float = 0.5  # image width as a fraction of the page width
    on_top: bool = True


def apply_watermark(engine: Engine, doc: Document, spec: Watermark, pages: Sequence[int]) -> None:
    for index in pages:
        page = doc.page(index)
        rect = page.rect
        center = rect.center
        if spec.image is not None:
            w = rect.width * spec.scale
            box = Rect(center.x - w / 2, center.y - w / 2, center.x + w / 2, center.y + w / 2)
            page.stamp_image(ImageStamp(spec.image, box, spec.opacity, spec.on_top))
            continue
        width = engine.text_width(spec.text, spec.font, spec.font_size)
        # Center the text's midpoint on the page: start half the width back along the angle.
        m = Matrix.rotate(-spec.angle)  # visible ccw angle in y-down space
        half = Point(-width / 2, spec.font_size * 0.35).transform(m)
        origin = Point(center.x + half.x, center.y + half.y)
        page.stamp_text(
            TextStamp(
                spec.text,
                origin,
                spec.font_size,
                spec.font,
                spec.color,
                spec.opacity,
                spec.angle,
                spec.on_top,
            )
        )


def apply_background(
    doc: Document, color: Color, pages: Sequence[int], opacity: float = 1.0
) -> None:
    for index in pages:
        doc.page(index).fill_background(color, opacity)


def content_bounds(
    doc: Document, index: int, dpi: float = 50.0, threshold: int = 245
) -> Rect | None:
    """Bounding box of non-white pixels (visible page space), or None for a blank page."""
    from PIL import Image  # bundled; numpy isn't in the executable

    scale = dpi / 72
    result = doc.page(index).render(RenderRequest(matrix=Matrix.scale(scale), color=ColorMode.GRAY))
    img = Image.frombuffer(
        "L", (result.width, result.height), result.samples, "raw", "L", result.stride, 1
    )
    box = img.point(lambda v: 255 if v < threshold else 0).getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
    return Rect(x0 / scale, y0 / scale, x1 / scale, y1 / scale)


def trimmed_rect(doc: Document, index: int, padding: float = 12.0) -> Rect | None:
    """Crop rectangle removing white margins (with ``padding`` points kept around content)."""
    bounds = content_bounds(doc, index)
    if bounds is None:
        return None
    page_rect = doc.page(index).rect
    return bounds.inflated(padding).intersection(page_rect)
