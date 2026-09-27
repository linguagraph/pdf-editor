"""Marked-content helpers used by auto-tagging (pure, no engine)."""

from __future__ import annotations

import pytest

from pdfeditor.engine.contentstream.marking import (
    Mark,
    has_marked_content,
    insert_marks,
    strip_mcids,
    text_runs,
    text_show_points,
)
from pdfeditor.engine.contentstream.parser import parse, write

STREAM = (
    b"q 2 0 0 2 10 20 cm BT /F1 10 Tf 12 TL 5 700 Td (Title) Tj T* (Line two) Tj"
    b" 0 -30 Td [(a) 5 (b)] TJ ET Q"
    b" BT 1 0 0 1 300 100 Tm 4 Ts (x) Tj (y) ' /OC /L1 BDC (z) Tj EMC ET"
)


def test_text_show_points() -> None:
    ops = parse(STREAM)
    points = text_show_points(ops)
    assert [ops[i].operator for i in points] == ["Tj", "Tj", "TJ", "Tj", "'", "Tj"]
    p = list(points.values())
    # CTM scales by 2 and moves by (10, 20); the probe is 0.3 font sizes above the baseline
    assert (p[0].x, p[0].y) == pytest.approx((20, 20 + 2 * 703))
    assert (p[1].x, p[1].y) == pytest.approx((20, 20 + 2 * (703 - 12)))  # T* uses TL
    assert (p[2].x, p[2].y) == pytest.approx((20, 20 + 2 * (703 - 42)))
    assert (p[3].x, p[3].y) == pytest.approx((300, 107))  # rise 4 + probe 3, CTM restored
    assert (p[4].x, p[4].y) == pytest.approx((300, 107 - 12))  # ' moves to the next line


def test_runs_stop_at_text_objects_and_marked_content() -> None:
    ops = parse(STREAM)
    shows = list(text_show_points(ops))
    owner = dict(zip(shows, ["h", "p", "p", "p", "p", "p"], strict=True))
    runs = text_runs(ops, owner)
    assert [(ops[s].operator, ops[e].operator, k) for s, e, k in runs] == [
        ("Tj", "Tj", "h"),
        ("Tj", "TJ", "p"),  # same owner across positioning operators
        ("Tj", "'", "p"),  # a new BT starts a new run
        ("Tj", "Tj", "p"),  # and so does the existing /OC sequence
    ]
    for s, e, _k in runs:
        assert not has_marked_content(ops, s, e)


def test_insert_marks_round_trips_and_nests() -> None:
    ops = parse(STREAM)
    shows = list(text_show_points(ops))
    runs = text_runs(ops, dict.fromkeys(shows, "p"))
    marks = [(s, e, Mark("P", n)) for n, (s, e, _k) in enumerate(runs)]
    out = parse(write(insert_marks(ops, marks)))
    depth = 0
    for op in out:
        if op.operator in ("BDC", "BMC"):
            depth += 1
        elif op.operator == "EMC":
            depth -= 1
            assert depth >= 0
    assert depth == 0
    mcids = [op.operands[1] for op in out if op.operator == "BDC" and op.operands[0] == "P"]
    assert mcids == [{"MCID": n} for n in range(len(runs))]
    stripped = [op for op in out if op.operator not in ("BDC", "BMC", "EMC")]
    assert stripped == [op for op in ops if op.operator not in ("BDC", "EMC")]
    artifact = insert_marks(ops, [(0, 0, Mark("Artifact"))])
    assert write(artifact[:3]).split() == [b"/Artifact", b"BMC", b"q", b"EMC"]
    with pytest.raises(ValueError):
        insert_marks(ops, [(1, 2, Mark("P", 0)), (1, 3, Mark("P", 1))])


def test_strip_mcids() -> None:
    ops = parse(
        b"/P <</MCID 3>> BDC (a) Tj EMC /Span <</MCID 4 /Lang (en)>> BDC EMC /OC /x BDC EMC"
    )
    out = write(strip_mcids(ops))
    assert b"MCID" not in out
    assert out.split(b"\n")[0].strip() == b"/P BMC"
    assert b"/Lang (en)" in out and b"/OC /x BDC" in out
