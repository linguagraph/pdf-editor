"""Skew estimation for scanned page images (pure Pillow, backend independent).

Projection-profile method: text lines are horizontal bands, so when the image is rotated by the
right angle its row sums alternate sharply between ink and gaps, and their variance peaks. Every
candidate is rotated and row-summed by Pillow in C (a 1-pixel-wide BOX resize gives the row
means), so this stays fast without numpy, which the frozen executable doesn't ship.
"""

from __future__ import annotations

from PIL import Image, ImageOps

MAX_ANGLE = 10.0  # degrees either way; larger skews are rare and ambiguous with layout
WORK_WIDTH = 1000  # the estimate runs on a downscaled copy this wide
MIN_ANGLE = 0.05  # below this, deskewing only resamples the image


def estimate_skew(image: Image.Image, max_angle: float = MAX_ANGLE) -> float:
    """Skew of the content in degrees, counterclockwise as displayed (so
    ``image.rotate(-angle)`` straightens it). 0.0 when no clear text lines are found."""
    ink = _ink_mask(image)
    if ink.getbbox() is None:
        return 0.0
    best = _search(ink, -max_angle, max_angle, 1.0)
    for step in (0.1, 0.02):
        best = _search(ink, best - 10 * step, best + 10 * step, step)
    return 0.0 if abs(best) < MIN_ANGLE else round(best, 2)


def _ink_mask(image: Image.Image) -> Image.Image:
    """Downscaled, binarized image with ink = 255 on a 0 background (rotation fills with 0)."""
    gray = image.convert("L")
    if gray.width > WORK_WIDTH:
        height = max(1, round(gray.height * WORK_WIDTH / gray.width))
        gray = gray.resize((WORK_WIDTH, height), Image.Resampling.BOX)
    gray = ImageOps.autocontrast(gray)
    return gray.point(lambda v: 255 if v < 128 else 0)


def _search(ink: Image.Image, lo: float, hi: float, step: float) -> float:
    best_angle, best_score = 0.0, -1.0
    count = round((hi - lo) / step)
    for i in range(count + 1):
        angle = lo + i * step
        score = _profile_score(ink, angle)
        # Prefer the smaller rotation on ties so a blank-ish page stays at 0.
        if score > best_score or (score == best_score and abs(angle) < abs(best_angle)):
            best_angle, best_score = angle, score
    return best_angle


def _profile_score(ink: Image.Image, angle: float) -> float:
    rotated = ink.rotate(-angle, resample=Image.Resampling.BILINEAR, fillcolor=0)
    rows = rotated.resize((1, rotated.height), Image.Resampling.BOX).tobytes()  # mode L
    mean = sum(rows) / len(rows)
    return sum((r - mean) ** 2 for r in rows)


def rotate_about_center(image: Image.Image, angle: float) -> Image.Image:
    """Rotate counterclockwise (as displayed) by ``angle`` degrees keeping the size; exposed
    corners are filled white, like paper."""
    fill: int | tuple[int, ...] = 255 if image.mode in ("L", "1") else (255,) * len(image.mode)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=fill)
