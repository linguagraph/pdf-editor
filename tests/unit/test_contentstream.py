from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pdfeditor.engine.contentstream.objects import (
    ObjectKind,
    delete_ranges,
    find_objects,
    page_space_edit,
    wrap_ranges,
)
from pdfeditor.engine.contentstream.parser import (
    ContentSyntaxError,
    HexString,
    Name,
    Operation,
    fmt_number,
    parse,
    write,
)
from pdfeditor.model.geometry import Matrix, Point, Rect


def test_parse_basic_stream() -> None:
    data = (
        b"q 1 0 0 1 50 60 cm /Im1 Do Q\n"
        b"BT /F1 12 Tf 72 700 Td (Hello \\(world\\)\\n) Tj [(A) -120 <0041>] TJ ET\n"
        b"% a comment\n0.5 g 10 10 100 50 re f\n"
        b"/P <</MCID 3>> BDC EMC [/Na#20me true false null 1.5 -.5 +3] 0 d"
    )
    ops = parse(data)
    names = [o.operator for o in ops]
    assert names == [
        "q",
        "cm",
        "Do",
        "Q",
        "BT",
        "Tf",
        "Td",
        "Tj",
        "TJ",
        "ET",
        "g",
        "re",
        "f",
        "BDC",
        "EMC",
        "d",
    ]
    assert ops[2].operands == [Name("Im1")]
    assert ops[7].operands == [b"Hello (world)\n"]
    tj = ops[8].operands[0]
    assert tj[0] == b"A" and tj[1] == -120 and isinstance(tj[2], HexString) and tj[2] == b"\x00A"
    assert ops[13].operands == [Name("P"), {Name("MCID"): 3}]
    assert ops[15].operands[0] == [Name("Na me"), True, False, None, 1.5, -0.5, 3]


def test_inline_image_and_octal() -> None:
    data = b"q BI /W 2 /H 1 /BPC 8 /CS /G ID \x00\xffEIxx\nEI Q (\\101\\1\\\n) Tj"
    ops = parse(data)
    assert [o.operator for o in ops] == ["q", "BI", "Q", "Tj"]
    assert ops[1].inline_data == b"\x00\xffEIxx"  # 'EI' inside the data isn't the end
    assert ops[1].operands[0][Name("W")] == 2
    assert ops[3].operands == [b"A\x01"]


@pytest.mark.parametrize(
    "bad", [b"(unterminated", b"<< /A 1", b"1 2", b"[1 m]", b")", b"BI /W 1 ID xx"]
)
def test_syntax_errors(bad: bytes) -> None:
    with pytest.raises(ContentSyntaxError):
        parse(bad)


def test_fmt_number() -> None:
    assert (
        fmt_number(2.48081e-06) == "0.000002"
        and fmt_number(1e-9) == "0"
        and fmt_number(1.25) == "1.25"
        and fmt_number(-3) == "-3"
    )
    assert "e" not in fmt_number(1e21)


names = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=255), min_size=1, max_size=8
).map(Name)
numbers = st.one_of(
    st.integers(-(10**6), 10**6), st.floats(-1e5, 1e5, allow_nan=False).map(lambda f: round(f, 4))
)
scalars = st.one_of(
    numbers,
    st.booleans(),
    st.none(),
    names,
    st.binary(max_size=12),
    st.binary(max_size=8).map(HexString),
)
operands = st.recursive(
    scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(names, inner, max_size=3),
    ),
    max_leaves=10,
)
operators = st.sampled_from(
    ["q", "Q", "cm", "Tj", "TJ", "re", "f", "Do", "BDC", "gs", "d", "Tf", "'", '"']
)
operations = st.builds(Operation, operators, st.lists(operands, max_size=5))


@settings(max_examples=300, deadline=None)
@given(st.lists(operations, max_size=20))
def test_write_parse_roundtrip(ops: list[Operation]) -> None:
    assert parse(write(ops)) == ops


def test_find_objects_and_bboxes() -> None:
    ops = parse(
        b"q 100 0 0 50 10 20 cm /Im1 Do Q\n"
        b"q 2 0 0 2 0 0 cm 5 5 m 15 5 l 15 25 l S Q\n"
        b"0 0 10 10 re W n\n"
        b"BT 0 0 10 10 re ET\n"
        b"q 1 0 0 1 300 300 cm /Fm1 Do Q\n"
        b"q 20 0 0 20 400 400 cm BI /W 1 /H 1 /BPC 8 /CS /G ID \x80\nEI Q"
    )

    def resolve(name: str):
        return {"Im1": ("Image", None), "Fm1": ("Form", Rect(0, 0, 50, 40))}.get(name)

    objs = find_objects(ops, resolve)
    kinds = [o.kind for o in objs]
    assert kinds == [ObjectKind.IMAGE, ObjectKind.PATH, ObjectKind.FORM, ObjectKind.INLINE_IMAGE]
    image, path, form, inline = objs
    assert image.bbox == Rect(10, 20, 110, 70) and image.name == "Im1"
    assert path.bbox == Rect(10, 10, 30, 50) and path.stroke and not path.fill
    assert (path.start, path.end) == (6, 9)
    assert form.bbox == Rect(300, 300, 350, 340)
    assert inline.bbox == Rect(400, 400, 420, 420)


def test_rewrites_move_objects_in_page_space() -> None:
    ops = parse(b"q 100 0 0 50 10 20 cm /Im1 Do Q 1 0 0 1 5 5 cm 0 0 m 10 10 l S")
    objs = find_objects(ops, lambda n: ("Image", None))
    image, path = objs
    move = Matrix.translate(30, -10)
    x = page_space_edit(image, move)
    assert x is not None
    moved = wrap_ranges(ops, [(image.start, image.end, x)])
    new_image = find_objects(parse(write(moved)), lambda n: ("Image", None))[0]
    assert new_image.bbox == Rect(40, 10, 140, 60)
    # a scaled path moves by the same page-space delta
    xp = page_space_edit(path, move)
    moved_path = find_objects(
        wrap_ranges(ops, [(path.start, path.end, xp)]), lambda n: ("Image", None)
    )[1]
    assert moved_path.bbox.x0 == pytest.approx(35) and moved_path.bbox.y0 == pytest.approx(-5)
    removed = delete_ranges(ops, [(image.start, image.end)])
    assert [o.operator for o in removed].count("Do") == 0
    _ = Point
