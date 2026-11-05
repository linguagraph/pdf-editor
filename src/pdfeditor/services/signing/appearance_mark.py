"""The faint logo behind a visible signature.

The mark is the blue "E" cut out of the bundled app icon. It is built once per process from the
icon, because the icon itself is part of the branding pack and must never be edited.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import cache

from PIL import Image, ImageChops, ImageDraw

from pdfeditor.bundle import data_path

# Crop box in the 512 px icon: the document shape without its teal rounded square. The E sits
# inside it; the bottom edge of the document is cut off by the crop.
_CROP = (126, 150, 392, 436)
# Top-right corner of the crop: the page curl, whose edge is pale blue and would show as a line.
_FOLD = (24, 186)
# The E is strongly saturated; the pale document (saturation about 0.2) and the page curl are not.
_SATURATION_LOW, _SATURATION_SPAN = 0.40, 0.25
_FEATHER = 40  # pixels of the crop: the E's tail runs past the crop, so it fades out, not stops
_FADE = 0.22  # the mark's strongest alpha: black text over it stays clearly readable
_MAX_HEIGHT = 220  # pixels; keeps signed files small


@dataclass(frozen=True, slots=True)
class Mark:
    png: bytes  # RGBA PNG
    width: int
    height: int

    @property
    def aspect(self) -> float:
        return self.width / self.height


def _feathered(alpha: Image.Image) -> Image.Image:
    """``alpha`` falling to nothing at its edges, so the crop leaves no hard cut or stray pixels."""
    mask = Image.new("L", alpha.size, 255)
    draw = ImageDraw.Draw(mask)
    for inset in range(_FEATHER):
        right, bottom = alpha.width - 1 - inset, alpha.height - 1 - inset
        draw.rectangle((inset, inset, right, bottom), outline=round(255 * inset / _FEATHER))
    return ImageChops.multiply(alpha, mask)


@cache
def mark() -> Mark:
    """The faded E as a PNG with alpha, downscaled to at most ``_MAX_HEIGHT`` pixels high."""
    icon = Image.open(data_path("branding", "elanero", "icons", "png", "elanero-512.png"))
    crop = icon.convert("RGBA").crop(_CROP)
    red, green, blue, _ = crop.split()
    _, saturation, _ = crop.convert("RGB").convert("HSV").split()
    ramp = [
        round(min(1.0, max(0.0, (v / 255 - _SATURATION_LOW) / _SATURATION_SPAN)) * _FADE * 255)
        for v in range(256)
    ]
    alpha = saturation.point(ramp)
    top, left = _FOLD
    alpha.paste(0, (left, 0, alpha.width, top))
    alpha = _feathered(alpha)
    image = Image.merge("RGBA", (red, green, blue, alpha))
    if image.height > _MAX_HEIGHT:
        width = round(image.width * _MAX_HEIGHT / image.height)
        image = image.resize((width, _MAX_HEIGHT), Image.Resampling.BOX)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return Mark(out.getvalue(), image.width, image.height)
