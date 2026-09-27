"""Optional-content visibility and hidden-section stripping (engine-neutral)."""

from __future__ import annotations

import pytest

from pdfeditor.engine.contentstream.optional import ocmd_visible, strip_hidden_content
from pdfeditor.engine.contentstream.parser import parse, write

OFF = {2}


def is_on(ocg: int) -> bool:
    return ocg not in OFF


def strip(src: bytes, props=("H",), xobjects=()) -> tuple[str, int]:
    ops, removed = strip_hidden_content(parse(src), set(props), set(xobjects))
    return write(ops).decode(), removed


@pytest.mark.parametrize(
    ("ocgs", "policy", "visible"),
    [
        ([1, 2], "AnyOn", True),
        ([1, 2], "AllOn", False),
        ([1, 2], "AnyOff", True),
        ([1, 2], "AllOff", False),
        ([2], "AllOff", True),
        ([2], "", False),  # AnyOn is the default
        ([], "AllOn", True),  # names no groups: visible
    ],
)
def test_ocmd_policies(ocgs: list[int], policy: str, visible: bool) -> None:
    assert ocmd_visible(ocgs, policy, is_on) is visible


def test_ocmd_visibility_expression_wins() -> None:
    assert ocmd_visible([1], "AnyOn", is_on, ["Not", 1]) is False
    assert ocmd_visible([], "AnyOn", is_on, ["And", 1, ["Not", 2]]) is True
    assert ocmd_visible([], "AnyOn", is_on, ["Or", 2, ["And", 1, 2]]) is False
    assert ocmd_visible([2], "AnyOn", is_on, ["Bogus", 1]) is False  # falls back to /OCGs


def test_hidden_section_is_removed_and_visible_kept() -> None:
    out, removed = strip(
        b"/OC /V BDC BT /F1 12 Tf (keep) Tj ET EMC "
        b"/OC /H BDC q BT /F1 12 Tf (secret) Tj ET Q EMC "
        b"/Span <</MCID 0>> BDC BT (after) Tj ET EMC"
    )
    assert removed == 1
    assert "secret" not in out and "(keep)" in out and "(after)" in out
    assert "/H" not in out


def test_state_survives_badly_nested_sections() -> None:
    # MuPDF writes "/OC /x BDC q ... EMC Q": the q/Q pair straddles the section boundary
    out, _ = strip(b"/OC /H BDC q 1 0 0 rg 0 0 10 10 re f EMC Q 0 g (x) Tj")
    ops = [op.operator for op in parse(out.encode())]
    assert ops.count("q") == ops.count("Q")
    assert "re" not in ops and "f" not in ops


def test_state_set_inside_section_is_kept() -> None:
    out, _ = strip(
        b"BT /OC /H BDC /F2 9 Tf 1 0 0 1 5 5 Tm (secret) Tj (more) ' 1 2 (x) \" EMC (vis) Tj ET"
    )
    names = [op.operator for op in parse(out.encode())]
    assert names.count("Tj") == 1  # only the visible string
    assert {"Tf", "Tm", "T*", "Tw", "Tc"} <= set(names)
    assert "secret" not in out and "more" not in out


def test_clipping_path_in_hidden_section_is_kept_unpainted() -> None:
    out, _ = strip(b"/OC /H BDC 0 0 50 50 re W n 0 0 100 100 re f EMC")
    ops = [op.operator for op in parse(out.encode())]
    assert ops == ["re", "W", "n"]


def test_nested_marked_content_and_hidden_xobjects() -> None:
    out, removed = strip(
        b"/OC /H BDC /Span BMC (a) Tj EMC /P <<>> BDC (b) Tj EMC EMC "
        b"q /Im1 Do Q q /Fm1 Do Q /OC /V BDC /Fm2 Do EMC",
        xobjects={"Im1"},
    )
    assert removed == 2
    ops = [op.operator for op in parse(out.encode())]
    assert "Tj" not in ops
    assert "/Im1" not in out and "/Fm1 Do" in out and "/Fm2 Do" in out
    assert ops.count("BDC") + ops.count("BMC") == ops.count("EMC")


def test_nothing_hidden_leaves_stream_alone() -> None:
    src = b"q 1 0 0 1 0 0 cm /OC /V BDC BT (x) Tj ET EMC Q"
    ops, removed = strip_hidden_content(parse(src), {"H"})
    assert removed == 0 and ops == parse(src)
