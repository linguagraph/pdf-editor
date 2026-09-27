"""Walk a parsed content stream with a graphics-state tracker and find editable objects.

Found objects are images (``Do`` of an image XObject, or inline images), form XObjects and
vector paths, each with its operator range and its bounding box in PDF user space. Edits are
expressed as operation-list rewrites: delete a range, or wrap it in ``q <matrix> cm ... Q``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from pdfeditor.engine.contentstream.parser import Name, Operation
from pdfeditor.model.geometry import Matrix, Point, Rect

PATH_CONSTRUCTION = {"m", "l", "c", "v", "y", "h", "re"}
PATH_PAINTING = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
CLIPPING = {"W", "W*"}


class ObjectKind(Enum):
    IMAGE = "image"
    INLINE_IMAGE = "inline_image"
    FORM = "form"
    PATH = "path"


@dataclass(frozen=True, slots=True)
class ContentObject:
    kind: ObjectKind
    start: int  # first operation index (inclusive)
    end: int  # last operation index (inclusive)
    bbox: Rect  # PDF user space
    ctm: Matrix  # CTM in effect for the object
    name: str = ""  # XObject resource name
    stroke: bool = False
    fill: bool = False

    @property
    def key(self) -> str:
        """Identity within one version of the content stream."""
        return f"{self.kind.value}:{self.start}"


XObjectResolver = Callable[[str], tuple[str, Rect | None] | None]
"""name -> (subtype "Image"/"Form", form BBox transformed by the form Matrix) or None."""

_UNIT = Rect(0, 0, 1, 1)


def _mat(values: Sequence[object]) -> Matrix | None:
    try:
        a, b, c, d, e, f = (float(v) for v in values)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return Matrix(a, b, c, d, e, f)


def find_objects(ops: list[Operation], resolve: XObjectResolver) -> list[ContentObject]:
    objects: list[ContentObject] = []
    ctm = Matrix.identity()
    stack: list[Matrix] = []
    path_start: int | None = None
    path_points: list[Point] = []
    in_text = False
    for i, op in enumerate(ops):
        name = op.operator
        if name == "q":
            stack.append(ctm)
        elif name == "Q":
            ctm = stack.pop() if stack else Matrix.identity()
        elif name == "cm":
            m = _mat(op.operands)
            if m is not None:
                ctm = m @ ctm
        elif name == "BT":
            in_text = True
        elif name == "ET":
            in_text = False
        elif name in PATH_CONSTRUCTION and not in_text:
            if path_start is None:
                path_start = i
            path_points.extend(_path_points(op))
        elif name in CLIPPING:
            continue
        elif name in PATH_PAINTING and not in_text:
            if path_start is not None and name != "n" and path_points:
                stroke = name in {"S", "s", "B", "B*", "b", "b*"}
                fill = name in {"f", "F", "f*", "B", "B*", "b", "b*"}
                box = Rect.from_points(p.transform(ctm) for p in path_points)
                objects.append(
                    ContentObject(
                        ObjectKind.PATH, path_start, i, box, ctm, stroke=stroke, fill=fill
                    )
                )
            path_start, path_points = None, []
        elif name == "Do" and op.operands and isinstance(op.operands[0], Name):
            info = resolve(str(op.operands[0]))
            if info is None:
                continue
            subtype, form_box = info
            if subtype == "Image":
                objects.append(
                    ContentObject(
                        ObjectKind.IMAGE, i, i, _UNIT.transform(ctm), ctm, str(op.operands[0])
                    )
                )
            elif subtype == "Form" and form_box is not None:
                objects.append(
                    ContentObject(
                        ObjectKind.FORM, i, i, form_box.transform(ctm), ctm, str(op.operands[0])
                    )
                )
        elif name == "BI":
            objects.append(ContentObject(ObjectKind.INLINE_IMAGE, i, i, _UNIT.transform(ctm), ctm))
    return objects


def _path_points(op: Operation) -> list[Point]:
    nums: list[float] = []
    for v in op.operands:
        if isinstance(v, int | float) and not isinstance(v, bool):
            nums.append(float(v))
    if op.operator == "re" and len(nums) == 4:
        x, y, w, h = nums
        return [Point(x, y), Point(x + w, y), Point(x, y + h), Point(x + w, y + h)]
    return [Point(nums[k], nums[k + 1]) for k in range(0, len(nums) - 1, 2)]


# -- rewrites ---------------------------------------------------------------------------------
def delete_ranges(ops: list[Operation], ranges: list[tuple[int, int]]) -> list[Operation]:
    doomed: set[int] = set()
    for start, end in ranges:
        doomed.update(range(start, end + 1))
    return [op for i, op in enumerate(ops) if i not in doomed]


def wrap_ranges(ops: list[Operation], edits: list[tuple[int, int, Matrix]]) -> list[Operation]:
    """Wrap each ``(start, end)`` range in ``q m cm ... Q`` (ranges must not overlap)."""
    starts = {start: m for start, _end, m in edits}
    ends = {end for _start, end, _m in edits}
    out: list[Operation] = []
    for i, op in enumerate(ops):
        if i in starts:
            m = starts[i]
            out.append(Operation("q"))
            out.append(Operation("cm", [m.a, m.b, m.c, m.d, m.e, m.f]))
        out.append(op)
        if i in ends:
            out.append(Operation("Q"))
    return out


def page_space_edit(obj: ContentObject, page_transform: Matrix) -> Matrix | None:
    """The ``cm`` to insert before ``obj`` so it moves by ``page_transform`` (PDF space).

    Inserting ``X cm`` makes the CTM ``X x CTM``; we want ``CTM x P``, so X = CTM P CTM^-1.
    """
    if abs(obj.ctm.determinant) < 1e-12:
        return None
    return obj.ctm @ page_transform @ obj.ctm.inverted()
