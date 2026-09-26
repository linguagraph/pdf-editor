"""Engine-neutral geometry in PDF user space (points; y grows downward, as MuPDF reports it)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)

    def transform(self, m: Matrix) -> Point:
        return Point(m.a * self.x + m.c * self.y + m.e, m.b * self.x + m.d * self.y + m.f)


@dataclass(frozen=True, slots=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_points(cls, points: Iterable[Point]) -> Rect:
        pts = list(points)
        if not pts:
            raise ValueError("Rect.from_points needs at least one point")
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        return cls(min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def is_empty(self) -> bool:
        return self.x1 <= self.x0 or self.y1 <= self.y0

    @property
    def top_left(self) -> Point:
        return Point(self.x0, self.y0)

    @property
    def bottom_right(self) -> Point:
        return Point(self.x1, self.y1)

    @property
    def center(self) -> Point:
        return Point((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def normalized(self) -> Rect:
        return Rect(
            min(self.x0, self.x1),
            min(self.y0, self.y1),
            max(self.x0, self.x1),
            max(self.y0, self.y1),
        )

    def contains(self, p: Point) -> bool:
        return self.x0 <= p.x <= self.x1 and self.y0 <= p.y <= self.y1

    def intersects(self, other: Rect) -> bool:
        return not (
            other.x0 >= self.x1 or other.x1 <= self.x0 or other.y0 >= self.y1 or other.y1 <= self.y0
        )

    def intersection(self, other: Rect) -> Rect:
        return Rect(
            max(self.x0, other.x0),
            max(self.y0, other.y0),
            min(self.x1, other.x1),
            min(self.y1, other.y1),
        )

    def union(self, other: Rect) -> Rect:
        return Rect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def inflated(self, dx: float, dy: float | None = None) -> Rect:
        dy = dx if dy is None else dy
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def translated(self, dx: float, dy: float) -> Rect:
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    def transform(self, m: Matrix) -> Rect:
        """Bounding box of the transformed corners."""
        corners = (
            Point(self.x0, self.y0),
            Point(self.x1, self.y0),
            Point(self.x0, self.y1),
            Point(self.x1, self.y1),
        )
        return Rect.from_points(c.transform(m) for c in corners)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True, slots=True)
class Quad:
    """Four corners of a possibly rotated rectangle (e.g. a glyph run or a search hit)."""

    ul: Point
    ur: Point
    ll: Point
    lr: Point

    @classmethod
    def from_rect(cls, r: Rect) -> Quad:
        return cls(Point(r.x0, r.y0), Point(r.x1, r.y0), Point(r.x0, r.y1), Point(r.x1, r.y1))

    @property
    def rect(self) -> Rect:
        return Rect.from_points((self.ul, self.ur, self.ll, self.lr))

    def transform(self, m: Matrix) -> Quad:
        return Quad(
            self.ul.transform(m), self.ur.transform(m), self.ll.transform(m), self.lr.transform(m)
        )


@dataclass(frozen=True, slots=True)
class Matrix:
    """Affine matrix [a b c d e f] with PDF semantics: x' = a*x + c*y + e, y' = b*x + d*y + f."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    @classmethod
    def identity(cls) -> Matrix:
        return cls()

    @classmethod
    def scale(cls, sx: float, sy: float | None = None) -> Matrix:
        return cls(sx, 0.0, 0.0, sx if sy is None else sy, 0.0, 0.0)

    @classmethod
    def translate(cls, tx: float, ty: float) -> Matrix:
        return cls(1.0, 0.0, 0.0, 1.0, tx, ty)

    @classmethod
    def rotate(cls, degrees: float) -> Matrix:
        rad = math.radians(degrees)
        cos, sin = math.cos(rad), math.sin(rad)
        # Snap exact multiples of 90° to avoid 6e-17 noise in page-rotation math.
        cos, sin = round(cos, 12), round(sin, 12)
        return cls(cos, sin, -sin, cos, 0.0, 0.0)

    def concat(self, other: Matrix) -> Matrix:
        """Return self followed by other (PDF order: point * self * other)."""
        return Matrix(
            self.a * other.a + self.b * other.c,
            self.a * other.b + self.b * other.d,
            self.c * other.a + self.d * other.c,
            self.c * other.b + self.d * other.d,
            self.e * other.a + self.f * other.c + other.e,
            self.e * other.b + self.f * other.d + other.f,
        )

    def __matmul__(self, other: Matrix) -> Matrix:
        return self.concat(other)

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def inverted(self) -> Matrix:
        det = self.determinant
        if det == 0:
            raise ValueError("matrix is not invertible")
        a, b, c, d = self.d / det, -self.b / det, -self.c / det, self.a / det
        return Matrix(a, b, c, d, -(self.e * a + self.f * c), -(self.e * b + self.f * d))

    def as_tuple(self) -> tuple[float, float, float, float, float, float]:
        return (self.a, self.b, self.c, self.d, self.e, self.f)


def simplify(points: list[Point], epsilon: float) -> list[Point]:
    """Ramer-Douglas-Peucker polyline simplification (keeps endpoints)."""
    if len(points) < 3:
        return list(points)
    a, b = points[0], points[-1]
    dx, dy = b.x - a.x, b.y - a.y
    length = math.hypot(dx, dy)
    best, index = -1.0, 0
    for i in range(1, len(points) - 1):
        p = points[i]
        if length == 0:
            d = math.hypot(p.x - a.x, p.y - a.y)
        else:
            d = abs(dy * p.x - dx * p.y + b.x * a.y - b.y * a.x) / length
        if d > best:
            best, index = d, i
    if best <= epsilon:
        return [a, b]
    left = simplify(points[: index + 1], epsilon)
    return left[:-1] + simplify(points[index:], epsilon)
