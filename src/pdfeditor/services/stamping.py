"""Header/footer, Bates numbering, watermark and background (drawn into page content).

Each is added as a *mark* (``MarkKind``) with the settings used stored next to it, where the
engine supports it (``capabilities.page_marks``), so it can later be found, updated (removed
and drawn again from new settings) or removed, instead of piling up a new copy every time.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from pdfeditor.engine.base import ColorMode, Document, Engine, RenderRequest
from pdfeditor.model.color import BLACK, Color
from pdfeditor.model.geometry import Matrix, Point, Rect
from pdfeditor.model.pages import ImageStamp, MarkKind, TextStamp

log = logging.getLogger(__name__)

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
    engine: Engine,
    doc: Document,
    spec: HeaderFooter,
    pages: Sequence[int],
    file_name: str = "",
    kind: MarkKind = MarkKind.HEADER_FOOTER,
) -> int:
    """Stamp header/footer text on ``pages``; returns the number of text items drawn.

    ``kind`` tells Bates numbers (``MarkKind.BATES``) apart from other headers and footers, so
    each can be updated or removed on its own.
    """
    drawn = 0
    count = doc.page_count
    marking = engine.capabilities.page_marks
    settings = header_footer_settings(spec)
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
            page.stamp_text(
                TextStamp(
                    text,
                    Point(x, y),
                    spec.font_size,
                    spec.font,
                    spec.color,
                    mark=kind if marking else None,
                )
            )
            drawn += 1
        if marking:
            page.set_mark_settings(kind, settings)
    return drawn


@dataclass
class Watermark:
    text: str = ""
    image: bytes | None = None
    image_path: str = ""  # where ``image`` came from, remembered for "Update Watermark"
    font: str = "hebo"
    font_size: float = 60.0
    color: Color = field(default_factory=lambda: Color(0.75, 0.1, 0.1))
    opacity: float = 0.3
    angle: float = 45.0  # text only
    scale: float = 0.5  # image width as a fraction of the page width
    on_top: bool = True


def apply_watermark(engine: Engine, doc: Document, spec: Watermark, pages: Sequence[int]) -> None:
    mark = MarkKind.WATERMARK if engine.capabilities.page_marks else None
    settings = watermark_settings(spec)
    for index in pages:
        page = doc.page(index)
        if mark is not None:
            page.set_mark_settings(mark, settings)
        rect = page.rect
        center = rect.center
        if spec.image is not None:
            w = rect.width * spec.scale
            box = Rect(center.x - w / 2, center.y - w / 2, center.x + w / 2, center.y + w / 2)
            page.stamp_image(ImageStamp(spec.image, box, spec.opacity, spec.on_top, mark=mark))
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
                mark,
            )
        )


def apply_background(
    doc: Document,
    color: Color,
    pages: Sequence[int],
    opacity: float = 1.0,
    engine: Engine | None = None,
) -> None:
    """Paint ``pages`` behind their content; marked as a background when ``engine`` (the
    document's) supports page marks."""
    mark = MarkKind.BACKGROUND if engine is not None and engine.capabilities.page_marks else None
    settings = background_settings(color, opacity)
    for index in pages:
        page = doc.page(index)
        page.fill_background(color, opacity, mark)
        if mark is not None:
            page.set_mark_settings(mark, settings)


# -- finding, updating and removing marks -------------------------------------------------------
MARK_NAMES = {
    MarkKind.HEADER_FOOTER: "Header & Footer",
    MarkKind.BATES: "Bates Numbers",
    MarkKind.WATERMARK: "Watermark",
    MarkKind.BACKGROUND: "Background",
}


@dataclass(frozen=True)
class MarkInfo:
    """Where a kind of mark is in a document, and the settings it was added with."""

    kind: MarkKind
    pages: list[int]
    settings: str = ""  # from the first page that has them; "" if none were stored
    foreign: bool = False  # only other tools' (Acrobat's) marks of this kind were found


def find_marks(
    engine: Engine, doc: Document, kinds: Collection[MarkKind] | None = None
) -> dict[MarkKind, MarkInfo]:
    """The marks in ``doc`` (all kinds, or ``kinds``) by kind; empty without the capability."""
    if not engine.capabilities.page_marks:
        return {}
    pages: dict[MarkKind, list[int]] = {}
    settings: dict[MarkKind, str] = {}
    own: set[MarkKind] = set()
    for index in range(doc.page_count):
        for mark in doc.page(index).page_marks():
            if kinds is not None and mark.kind not in kinds:
                continue
            pages.setdefault(mark.kind, []).append(index)
            if not mark.foreign:
                own.add(mark.kind)
            if mark.settings and mark.kind not in settings:
                settings[mark.kind] = mark.settings
    return {
        kind: MarkInfo(kind, found, settings.get(kind, ""), kind not in own)
        for kind, found in pages.items()
    }


def remove_marks(
    engine: Engine,
    doc: Document,
    kinds: Collection[MarkKind],
    pages: Sequence[int] | None = None,
) -> int:
    """Delete the marks of ``kinds`` (from every page, or ``pages``); returns how many marked
    sections were removed. Everything else on the pages stays as it is."""
    if not engine.capabilities.page_marks:
        return 0
    removed = 0
    for index in range(doc.page_count) if pages is None else pages:
        removed += doc.page(index).remove_marks(kinds)
    return removed


# -- stored settings (versioned JSON) -----------------------------------------------------------
_VERSION = 1
_SLOTS = {slot.value for slot in Slot}


def _dump(kind: str, values: dict[str, Any]) -> str:
    return json.dumps({"kind": kind, "version": _VERSION, **values}, sort_keys=True)


def _load(text: str, kind: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("kind") != kind:
        return None
    return data


def _color(value: Any, default: Color) -> Color:
    try:
        r, g, b = (float(c) for c in value)
    except (TypeError, ValueError):
        return default
    return Color(r, g, b)


def header_footer_settings(spec: HeaderFooter) -> str:
    return _dump(
        "header_footer",
        {
            "texts": {slot.value: text for slot, text in spec.texts.items() if text},
            "font": spec.font,
            "font_size": spec.font_size,
            "color": list(spec.color.rgb()),
            "margin_x": spec.margin_x,
            "margin_top": spec.margin_top,
            "margin_bottom": spec.margin_bottom,
            "bates_prefix": spec.bates_prefix,
            "bates_start": spec.bates_start,
            "bates_digits": spec.bates_digits,
            "bates_suffix": spec.bates_suffix,
            "date_format": spec.date_format,
        },
    )


def header_footer_from_settings(text: str) -> HeaderFooter | None:
    data = _load(text, "header_footer")
    if data is None:
        return None
    try:
        texts = data.get("texts", {})
        return HeaderFooter(
            texts={Slot(k): str(v) for k, v in texts.items() if k in _SLOTS and v},
            font=str(data.get("font", "helv")),
            font_size=float(data.get("font_size", 9.0)),
            color=_color(data.get("color"), BLACK),
            margin_x=float(data.get("margin_x", 36.0)),
            margin_top=float(data.get("margin_top", 24.0)),
            margin_bottom=float(data.get("margin_bottom", 24.0)),
            bates_prefix=str(data.get("bates_prefix", "")),
            bates_start=int(data.get("bates_start", 1)),
            bates_digits=int(data.get("bates_digits", 6)),
            bates_suffix=str(data.get("bates_suffix", "")),
            date_format=str(data.get("date_format", "%Y-%m-%d")),
        )
    except (AttributeError, TypeError, ValueError):
        log.warning("ignoring unreadable header/footer settings")
        return None


def watermark_settings(spec: Watermark) -> str:
    return _dump(
        "watermark",
        {
            "text": spec.text,
            "image": spec.image is not None,
            "image_path": spec.image_path,
            "font": spec.font,
            "font_size": spec.font_size,
            "color": list(spec.color.rgb()),
            "opacity": spec.opacity,
            "angle": spec.angle,
            "scale": spec.scale,
            "on_top": spec.on_top,
        },
    )


def watermark_from_settings(text: str) -> Watermark | None:
    """A stored watermark's settings. An image watermark comes back with ``image_path`` set
    but without the picture (the file is read again when it's applied)."""
    data = _load(text, "watermark")
    if data is None:
        return None
    default = Watermark()
    try:
        return Watermark(
            text=str(data.get("text", "")),
            image_path=str(data.get("image_path", "")) if data.get("image") else "",
            font=str(data.get("font", default.font)),
            font_size=float(data.get("font_size", default.font_size)),
            color=_color(data.get("color"), default.color),
            opacity=float(data.get("opacity", default.opacity)),
            angle=float(data.get("angle", default.angle)),
            scale=float(data.get("scale", default.scale)),
            on_top=bool(data.get("on_top", True)),
        )
    except (TypeError, ValueError):
        log.warning("ignoring unreadable watermark settings")
        return None


def background_settings(color: Color, opacity: float) -> str:
    return _dump("background", {"color": list(color.rgb()), "opacity": opacity})


def background_from_settings(text: str) -> tuple[Color, float] | None:
    data = _load(text, "background")
    if data is None:
        return None
    try:
        return _color(data.get("color"), Color(1, 1, 1)), float(data.get("opacity", 1.0))
    except (TypeError, ValueError):
        return None


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
