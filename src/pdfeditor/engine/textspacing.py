"""Character and word spacing of extracted text (engine-neutral).

PDF text can be set with extra space after every glyph (``Tc``) and after every word space
(``Tw``). Extraction reports each glyph's box at its nominal advance and its origin where it was
actually drawn, so the gap between one glyph's box and the next glyph's origin is exactly that
extra space (plus any kerning, which the median filters out). Knowing it lets an edited block
be re-typeset with the same tracking instead of snapping back to the font's default spacing.
"""

from __future__ import annotations

from itertools import pairwise
from statistics import median

from pdfeditor.model.text import Block, Char, Line

MIN_CHAR_SPACING = 0.05  # points; smaller medians are rounding noise
MIN_WORD_SPACING = 0.1
_MIN_SAMPLES = 3


def _horizontal(line: Line) -> bool:
    return abs(line.direction[0] - 1) <= 1e-3


def _gaps(block: Block) -> tuple[list[float], list[float]]:
    letter: list[float] = []  # after a glyph: Tc
    word: list[float] = []  # after a word space: Tc + Tw
    for line in block.lines:
        if not _horizontal(line):
            continue
        for span in line.spans:
            chars = [c for c in span.chars if not c.synthetic]
            for a, b in pairwise(chars):
                gap = b.origin.x - a.origin.x - a.bbox.width
                if a.c == " ":
                    if not b.c.isspace():
                        word.append(gap)
                elif not a.c.isspace():
                    letter.append(gap)
    return letter, word


def detect_spacing(block: Block) -> tuple[float, float]:
    """``(char_spacing, word_spacing)`` of a block in points (0 when there's no evidence)."""
    letter, word = _gaps(block)
    char_spacing = median(letter) if len(letter) >= _MIN_SAMPLES else 0.0
    if abs(char_spacing) < MIN_CHAR_SPACING:
        char_spacing = 0.0
    word_spacing = median(word) - char_spacing if word else 0.0
    if abs(word_spacing) < MIN_WORD_SPACING:
        word_spacing = 0.0
    return round(char_spacing, 2), round(word_spacing, 2)


def _is_letter_gap(ch: Char, char_spacing: float) -> bool:
    """A space the extractor made up for a gap that is only the letter spacing."""
    return (
        ch.synthetic
        and ch.c == " "
        and char_spacing > 0
        and abs(ch.bbox.width - char_spacing) <= max(0.3, 0.25 * char_spacing)
    )


def spaced_text(block: Block, char_spacing: float) -> str:
    """The block's text without the spaces extraction invents between letter-spaced glyphs
    ("H e l l o" for tracked "Hello"); real word gaps are kept."""
    if char_spacing <= 0 or not any(
        _is_letter_gap(c, char_spacing) for ln in block.lines for s in ln.spans for c in s.chars
    ):
        return block.text
    lines = []
    for line in block.lines:
        parts = []
        for span in line.spans:
            if not span.chars:
                parts.append(span.text)
                continue
            parts.append("".join(c.c for c in span.chars if not _is_letter_gap(c, char_spacing)))
        lines.append("".join(parts))
    return "\n".join(lines)
