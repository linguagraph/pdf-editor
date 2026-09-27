from __future__ import annotations

import pytest

from pdfeditor.model.annotations import (
    CALLOUT_KNEE,
    AnnotationModel,
    AnnotationType,
    callout_line,
)
from pdfeditor.model.geometry import Point, Rect

BOX = Rect(200, 100, 300, 140)


@pytest.mark.parametrize(
    ("tip", "expected"),
    [
        # left of the box: lands on the left side's middle, with a knee
        (Point(50, 300), (Point(50, 300), Point(200 - CALLOUT_KNEE, 120), Point(200, 120))),
        # right of the box
        (Point(500, 20), (Point(500, 20), Point(300 + CALLOUT_KNEE, 120), Point(300, 120))),
        # just left, too close for a knee
        (Point(195, 120), (Point(195, 120), Point(200, 120))),
        # straight above / below: top or bottom middle
        (Point(250, 10), (Point(250, 10), Point(250, 100))),
        (Point(220, 400), (Point(220, 400), Point(250, 140))),
    ],
)
def test_callout_line(tip: Point, expected: tuple[Point, ...]) -> None:
    assert callout_line(tip, BOX) == expected


def test_is_callout() -> None:
    plain = AnnotationModel(AnnotationType.FREE_TEXT, 0, BOX)
    assert not plain.is_callout
    plain.vertices = callout_line(Point(0, 0), BOX)
    assert plain.is_callout
    line = AnnotationModel(AnnotationType.LINE, 0, BOX, vertices=(Point(0, 0), Point(1, 1)))
    assert not line.is_callout
