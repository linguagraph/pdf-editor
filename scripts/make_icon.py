"""Draw the app icon (a page with a folded corner and a red band) into src/pdfeditor/data.

Run with:  uv run python scripts/make_icon.py
The build script converts the PNG to a multi-size .ico for the executable.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "pdfeditor" / "data" / "icon.png"
SIZE = 256


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    left, top, right, bottom, fold = 40, 16, 216, 240, 52
    page = [(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)]
    d.polygon([(x + 6, y + 6) for x, y in page], fill=(0, 0, 0, 60))  # shadow
    d.polygon(page, fill=(255, 255, 255, 255), outline=(90, 90, 90, 255), width=4)
    d.polygon(
        [(right - fold, top), (right - fold, top + fold), (right, top + fold)],
        fill=(215, 215, 215, 255),
        outline=(90, 90, 90, 255),
        width=4,
    )
    d.rounded_rectangle((20, 132, 236, 196), radius=12, fill=(200, 32, 38, 255))
    for i, y in enumerate((70, 92, 114)):
        d.rectangle(
            (left + 24, y, right - (48 if i == 0 else 24), y + 8), fill=(160, 160, 160, 255)
        )
    # stylized "PDF" bars in the band (no font dependency)
    for x in (52, 108, 164):
        d.rectangle((x, 146, x + 40, 182), outline=(255, 255, 255, 255), width=8)
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    draw().save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
