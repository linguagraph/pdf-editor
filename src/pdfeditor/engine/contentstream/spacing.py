"""Add character and word spacing to text a layout engine typeset without it.

HTML layout engines (MuPDF's included) ignore CSS ``letter-spacing``/``word-spacing`` and draw
each line with one ``TJ`` of two-byte glyph codes (Identity-H), where the PDF ``Tw`` operator
has no effect (it only applies to the single-byte code 32). So the text is laid out left-aligned
with its lines already broken, and the spacing is added afterwards:

* character spacing with a ``Tc`` after every ``BT``;
* word spacing (plus any justification fill) as ``TJ`` adjustments after every space glyph;
* a per-line shift (a leading ``TJ`` adjustment) for centered and right-aligned lines.

Pure: operates on parsed operations and knows nothing about fonts beyond the space code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pdfeditor.engine.contentstream.parser import Name, Operand, Operation


@dataclass(frozen=True, slots=True)
class LineSpacing:
    shift: float = 0.0  # move the line right by this much (points)
    fill: float = 0.0  # extra space after each word space, on top of the word spacing (points)


@dataclass(frozen=True, slots=True)
class Spacing:
    char_spacing: float  # points (Tc at unit text scale)
    word_spacing: float  # points, added after each space glyph
    space_code: bytes | None  # the space glyph's code in the layout font (None: unknown)
    lines: tuple[LineSpacing, ...] = ()  # one per shown line; ignored if the count differs


_CODE_BYTES = 2  # layout engines write CID fonts with Identity-H


def _codes(data: bytes) -> list[bytes]:
    return [data[i : i + _CODE_BYTES] for i in range(0, len(data), _CODE_BYTES)]


def _is_number(v: Operand) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _shown(op: Operation) -> list[Operand] | None:
    """The TJ array of a ``TJ``/``Tj`` operation (None if it isn't one we understand)."""
    if op.operator == "Tj" and len(op.operands) == 1 and isinstance(op.operands[0], bytes):
        return [op.operands[0]]
    if op.operator == "TJ" and len(op.operands) == 1 and isinstance(op.operands[0], list):
        items = op.operands[0]
        if all(isinstance(i, bytes) or _is_number(i) for i in items):
            return items
    return None


def apply_spacing(ops: list[Operation], spacing: Spacing) -> list[Operation]:
    """A copy of ``ops`` with ``spacing`` applied to its text (see the module docstring)."""
    shown = sum(1 for op in ops if _shown(op) is not None)
    lines = spacing.lines if len(spacing.lines) == shown else ()
    out: list[Operation] = []
    size = 0.0
    scale = 1.0  # text matrix scale along the baseline
    main_font: str | None = None
    font: str | None = None
    char_spacing: Operation | None = None
    line_no = 0
    for op in ops:
        if op.operator == "BT":
            out.append(op)
            scale = 1.0
            char_spacing = Operation("Tc", [0.0])
            out.append(char_spacing)
            continue
        items = _shown(op)
        if op.operator == "Tf" and len(op.operands) == 2 and _is_number(op.operands[1]):
            font = str(op.operands[0]) if isinstance(op.operands[0], Name) else None
            main_font = main_font or font
            size = float(op.operands[1])  # type: ignore[arg-type]
        elif op.operator == "Tm" and len(op.operands) == 6 and all(map(_is_number, op.operands)):
            a, b = float(op.operands[0]), float(op.operands[1])  # type: ignore[arg-type]
            scale = math.hypot(a, b) or 1.0
        elif items is not None:
            line = lines[line_no] if lines else LineSpacing()
            line_no += 1
            if size > 0:
                if char_spacing is not None:  # text space units: undo the text matrix scale
                    char_spacing.operands = [round(spacing.char_spacing / scale, 4)]
                space = spacing.space_code if font == main_font else None
                em = size * scale
                out.append(Operation("TJ", [_spaced(items, spacing, line, space, em)]))
                continue
        out.append(op)
    return out


def _spaced(
    items: list[Operand], spacing: Spacing, line: LineSpacing, space: bytes | None, em: float
) -> list[Operand]:
    """One line's ``TJ`` array with the shift and word gaps applied. ``em`` is the font size in
    points: TJ adjustments are thousandths of it, and negative ones move the next glyph right."""
    out: list[Operand] = []
    if abs(line.shift) > 1e-3:
        out.append(round(-line.shift * 1000 / em, 3))
    gap = spacing.word_spacing + line.fill
    for item in items:
        if not isinstance(item, bytes) or space is None or abs(gap) < 1e-4:
            out.append(item)
            continue
        run = b""
        for code in _codes(item):
            run += code
            if code == space:
                out.append(type(item)(run))
                out.append(round(-gap * 1000 / em, 3))
                run = b""
        if run:
            out.append(type(item)(run))
    return _merge_numbers(out)


def _merge_numbers(items: list[Operand]) -> list[Operand]:
    out: list[Operand] = []
    for item in items:
        if _is_number(item) and out and _is_number(out[-1]):
            out[-1] = round(float(out[-1]) + float(item), 3)  # type: ignore[arg-type]
        else:
            out.append(item)
    return out
